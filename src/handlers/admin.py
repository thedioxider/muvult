import shutil
from pathlib import Path
from typing import Any

import httpx
from aiogram import Router
from aiogram.filters import Command, Filter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlmodel import select

from ..db import Track, TrackOwnership, User, get_session
from ..navidrome import NavidromeClient
from ..pool import remove_pool_file, remove_symlink

_ND_SEP = "\x1f"


class _AdminOnly(Filter):
    async def __call__(self, _ev: Any, is_admin: bool = False, **_kw: Any) -> bool:
        return is_admin


admin_router = Router()
admin_router.message.filter(_AdminOnly())
admin_router.callback_query.filter(_AdminOnly())


_nd_client: NavidromeClient | None = None


def _nd() -> NavidromeClient:
    global _nd_client
    if _nd_client is None:
        from ..config import settings
        _nd_client = NavidromeClient(
            base_url=settings.nd_url,
            user=settings.nd_admin_user,
            password=settings.nd_admin_pass,
            music_path=settings.nd_music_path,
        )
    return _nd_client


def _nd_err_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes, proceed anyway", callback_data=f"nd_skip{_ND_SEP}{action}"),
        InlineKeyboardButton(text="No", callback_data=f"nd_skip{_ND_SEP}cancel"),
    ]])


async def _local_remove(tg_id: int) -> str | None:
    from ..config import settings
    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        user = result.first()
        if not user:
            return None

        result = await session.exec(
            select(TrackOwnership, Track)
            .join(Track, Track.id == TrackOwnership.track_id)
            .where(TrackOwnership.user_id == user.id)
        )
        for ownership, track in result.all():
            remove_symlink(Path(ownership.symlink_path))
            other = await session.exec(
                select(TrackOwnership)
                .where(TrackOwnership.track_id == track.id, TrackOwnership.user_id != user.id)
            )
            if not other.first():
                remove_pool_file(Path(track.pool_path))
                await session.delete(track)

        username = user.username
        await session.delete(user)
        await session.commit()

    user_dir = Path(settings.music_root) / username
    if user_dir.exists():
        shutil.rmtree(str(user_dir))
    return username


async def _local_rename(old: str, new: str) -> None:
    from ..config import settings
    Path(settings.music_root, old).rename(Path(settings.music_root, new))
    async with get_session() as session:
        result = await session.exec(select(User).where(User.username == old))
        user = result.first()
        if not user:
            return
        result = await session.exec(
            select(TrackOwnership).where(TrackOwnership.user_id == user.id)
        )
        for ownership in result.all():
            ownership.symlink_path = ownership.symlink_path.replace(f"/{old}/", f"/{new}/", 1)
        user.username = new
        await session.commit()


@admin_router.message(Command("adduser"))
async def cmd_adduser(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Usage: /adduser <tg_id> <navidrome_username>")
        return

    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("tg_id must be an integer")
        return

    from ..config import settings
    username = parts[2]
    nd = _nd()

    Path(settings.music_root, username).mkdir(parents=True, exist_ok=True)
    try:
        library_id = await nd.create_library(username)
        navidrome_user_id = await nd.get_user_id(username)
        await nd.set_user_library(navidrome_user_id, library_id)
    except httpx.HTTPError as e:
        await message.answer(f"Navidrome unreachable: {e}")
        return

    async with get_session() as session:
        session.add(User(
            tg_id=tg_id, username=username,
            navidrome_user_id=navidrome_user_id, navidrome_library_id=library_id,
        ))
        await session.commit()

    await message.answer(f"Added user {username} (tg_id={tg_id})")


@admin_router.message(Command("removeuser"))
async def cmd_removeuser(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Usage: /removeuser <tg_id>")
        return

    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("tg_id must be an integer")
        return

    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        user = result.first()
    if not user:
        await message.answer(f"No user with tg_id={tg_id}")
        return

    try:
        await _nd().delete_library(user.navidrome_library_id)
    except httpx.HTTPError as e:
        await message.answer(
            f"Navidrome unreachable: {e}\n"
            f"Remove {user.username} from DB anyway? (library will remain in Navidrome)",
            reply_markup=_nd_err_keyboard(f"rm{_ND_SEP}{tg_id}"),
        )
        return

    username = await _local_remove(tg_id)
    await message.answer(f"Removed user {username}")


@admin_router.message(Command("settgid"))
async def cmd_settgid(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Usage: /settgid <username> <new_tg_id>")
        return

    username, new_tg_id_str = parts[1], parts[2]
    try:
        new_tg_id = int(new_tg_id_str)
    except ValueError:
        await message.answer("new_tg_id must be an integer")
        return

    async with get_session() as session:
        result = await session.exec(select(User).where(User.username == username))
        user = result.first()
        if not user:
            await message.answer(f"No user: {username}")
            return
        user.tg_id = new_tg_id
        await session.commit()

    await message.answer(f"Updated tg_id for {username} → {new_tg_id}")


@admin_router.message(Command("setusername"))
async def cmd_setusername(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Usage: /setusername <old_username> <new_username>")
        return

    old, new = parts[1], parts[2]

    async with get_session() as session:
        result = await session.exec(select(User).where(User.username == old))
        user = result.first()
    if not user:
        await message.answer(f"No user: {old}")
        return

    try:
        await _nd().update_library(user.navidrome_library_id, new)
    except httpx.HTTPError as e:
        await message.answer(
            f"Navidrome unreachable: {e}\n"
            f"Rename {old} → {new} in DB anyway? (Navidrome library will keep old name)",
            reply_markup=_nd_err_keyboard(f"mv{_ND_SEP}{old}{_ND_SEP}{new}"),
        )
        return

    await _local_rename(old, new)
    await message.answer(f"Renamed {old} → {new}")


@admin_router.callback_query(lambda c: c.data and c.data.startswith(f"nd_skip{_ND_SEP}"))
async def cb_nd_skip(callback: CallbackQuery) -> None:
    parts = callback.data.split(_ND_SEP)
    action = parts[1]

    if action == "cancel":
        await callback.message.edit_text("Cancelled.")
        await callback.answer()
        return

    if action == "rm":
        tg_id = int(parts[2])
        username = await _local_remove(tg_id)
        text = f"Removed user {username} (Navidrome library not deleted)." if username else "User not found."
        await callback.message.edit_text(text)

    elif action == "mv":
        old, new = parts[2], parts[3]
        await _local_rename(old, new)
        await callback.message.edit_text(f"Renamed {old} → {new} (Navidrome library not updated).")

    await callback.answer()


@admin_router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    async with get_session() as session:
        result = await session.exec(select(User).order_by(User.username))
        rows = result.all()

    if not rows:
        await message.answer("No users.")
        return

    lines = [f"• {u.username} (tg_id={u.tg_id})" for u in rows]
    await message.answer("Users:\n" + "\n".join(lines))
