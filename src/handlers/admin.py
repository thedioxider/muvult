import shutil
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlmodel import select

from ..db import Track, TrackOwnership, User, get_session
from ..navidrome import NavidromeClient
from ..pool import remove_pool_file, remove_symlink

admin_router = Router()


def _nd() -> NavidromeClient:
    from ..config import settings
    return NavidromeClient(
        base_url=settings.nd_url,
        user=settings.nd_admin_user,
        password=settings.nd_admin_pass,
        music_path=settings.nd_music_path,
    )


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
    library_id = await nd.create_library(username)
    navidrome_user_id = await nd.get_user_id(username)
    await nd.set_user_library(navidrome_user_id, library_id)

    async with get_session() as session:
        session.add(User(
            tg_id=tg_id, username=username,
            navidrome_user_id=navidrome_user_id, navidrome_library_id=library_id,
        ))
        await session.commit()

    await message.answer(f"Added user {username} (tg_id={tg_id})")


@admin_router.message(Command("removeuser"))
async def cmd_removeuser(message: Message) -> None:
    from ..config import settings

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

        result = await session.exec(
            select(TrackOwnership, Track)
            .join(Track, Track.id == TrackOwnership.track_id)
            .where(TrackOwnership.user_id == user.id)
        )
        ownerships = result.all()

        for ownership, track in ownerships:
            remove_symlink(Path(ownership.symlink_path))
            other = await session.exec(
                select(TrackOwnership)
                .where(TrackOwnership.track_id == track.id, TrackOwnership.user_id != user.id)
            )
            if not other.first():
                remove_pool_file(Path(track.pool_path))
                await session.delete(track)

        lib_id = user.navidrome_library_id
        username = user.username
        await session.delete(user)
        await session.commit()

    await _nd().delete_library(lib_id)

    user_dir = Path(settings.music_root) / username
    if user_dir.exists():
        shutil.rmtree(str(user_dir))

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
    from ..config import settings

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

        Path(settings.music_root, old).rename(Path(settings.music_root, new))

        result = await session.exec(
            select(TrackOwnership).where(TrackOwnership.user_id == user.id)
        )
        for ownership in result.all():
            ownership.symlink_path = ownership.symlink_path.replace(f"/{old}/", f"/{new}/", 1)

        lib_id = user.navidrome_library_id
        user.username = new
        await session.commit()

    await _nd().update_library(lib_id, new)
    await message.answer(f"Renamed {old} → {new}")


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
