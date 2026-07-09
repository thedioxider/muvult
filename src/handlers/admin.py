import secrets
import shlex
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
from ..pool import (
    _is_cover,
    pool_rel,
    remove_pool_file,
    remove_symlink,
    user_library_root,
)
from ..tg_utils import safe_answer


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


async def _local_remove(tg_id: int) -> str | None:
    from ..config import settings
    pool_root = Path(settings.music_root) / ".pool"
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
                remove_pool_file(pool_root / track.pool_path)
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
    old_dir = Path(settings.music_root, old)
    new_dir = Path(settings.music_root, new)
    old_dir.rename(new_dir)
    async with get_session() as session:
        result = await session.exec(select(User).where(User.username == old))
        user = result.first()
        if not user:
            return
        result = await session.exec(
            select(TrackOwnership).where(TrackOwnership.user_id == user.id)
        )
        for ownership in result.all():
            # Rebase the link onto the renamed dir by path math, not string
            # substitution -- the latter could rewrite a matching segment
            # elsewhere in the path (e.g. an album named like the user).
            rel = Path(ownership.symlink_path).relative_to(old_dir)
            ownership.symlink_path = str(new_dir / rel)
        user.username = new
        await session.commit()


@admin_router.message(Command("adduser"))
async def cmd_adduser(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Usage: /adduser <navidrome_username> <tg_id>")
        return

    username = parts[1]
    try:
        tg_id = int(parts[2])
    except ValueError:
        await message.answer("tg_id must be an integer")
        return

    from ..config import settings
    nd = _nd()

    Path(settings.music_root, username).mkdir(parents=True, exist_ok=True)
    try:
        library_id = await nd.create_library(username)
        nd_user = await nd.get_user(username)
        navidrome_user_id = nd_user["id"]
        if not nd_user.get("isAdmin"):
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
        await message.answer("Usage: /removeuser <username>")
        return

    username = parts[1]

    async with get_session() as session:
        result = await session.exec(select(User).where(User.username == username))
        user = result.first()
    if not user:
        await message.answer(f"No user: {username}")
        return

    # The Navidrome library is left in place (never deleted); only the local
    # pool/symlinks/DB are cleaned up.
    await _local_remove(user.tg_id)
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
        user = (await session.exec(select(User).where(User.username == old))).first()
        taken = (await session.exec(select(User).where(User.username == new))).first()
    if not user:
        await message.answer(f"No user: {old}")
        return
    if taken:
        await message.answer(f"Username already taken: {new}")
        return

    from ..config import settings
    if Path(settings.music_root, new).exists():
        await message.answer(f"A directory already exists for {new}; not renaming.")
        return

    try:
        await _local_rename(old, new)
    except OSError as e:
        await message.answer(f"Rename failed: {e}")
        return

    # Navidrome is updated last, only after the local rename succeeded.
    try:
        await _nd().update_library(user.navidrome_library_id, new)
    except httpx.HTTPError as e:
        await message.answer(
            f"Renamed {old} → {new}, but Navidrome was unreachable ({e}); "
            f"its library path is now stale — fix it in Navidrome once it's back."
        )
        return

    await message.answer(f"Renamed {old} → {new}")


@admin_router.message(Command("recreatelinks"))
async def cmd_recreatelinks(message: Message) -> None:
    parts = (message.text or "").split()
    username = parts[1] if len(parts) > 1 else None

    from ..library import recreate_links
    result = await recreate_links(username)
    if result is None:
        await message.answer(f"No user: {username}")
        return
    count, missing = result

    target = username or "all users"
    msg = f"Recreated {count} symlink(s) for {target}"
    if missing:
        msg += "\n\nMissing pool files:\n" + "\n".join(f"— <i>{p}</i>" for p in missing)
    await message.answer(msg, parse_mode="HTML")


_pending_pool_deletes: dict[str, list[int]] = {}


def _parse_wildcard_prefix(path: str) -> str | None:
    parts = path.split("/")
    if parts[-1] != "*" or len(parts) < 2:
        return None
    prefix_parts = parts[:-1]
    if any(p in ("", "..") or "*" in p for p in prefix_parts):
        return None
    return "/".join(prefix_parts)


async def _remove_track(session: Any, pool_root: Path, track: Track) -> None:
    from ..config import settings
    music_root = Path(settings.music_root)
    own_result = await session.exec(select(TrackOwnership).where(TrackOwnership.track_id == track.id))
    for o in own_result.all():
        link = Path(o.symlink_path)
        remove_symlink(link, user_library_root(link, music_root))
    remove_pool_file(pool_root / track.pool_path)
    await session.delete(track)


async def _drop_ownership(session: Any, track: Track, user_id: int) -> bool | None:
    """Remove user's ownership. Returns True if orphaned, False if others remain, None if not owned."""
    own = (await session.exec(
        select(TrackOwnership).where(
            TrackOwnership.track_id == track.id, TrackOwnership.user_id == user_id
        )
    )).first()
    if not own:
        return None
    from ..config import settings
    link = Path(own.symlink_path)
    remove_symlink(link, user_library_root(link, Path(settings.music_root)))
    await session.delete(own)
    await session.flush()
    return not (await session.exec(
        select(TrackOwnership).where(TrackOwnership.track_id == track.id)
    )).first()


def _orphan_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes, delete", callback_data=f"pool_del:{key}:yes"),
        InlineKeyboardButton(text="No, keep", callback_data=f"pool_del:{key}:no"),
    ]])


@admin_router.callback_query(lambda c: c.data and c.data.startswith("pool_del:"))
async def cb_pool_delete(callback: CallbackQuery) -> None:
    _, key, choice = callback.data.split(":", 2)
    track_ids = _pending_pool_deletes.pop(key, None)
    if track_ids is None:
        await callback.message.edit_text(callback.message.text + "\n\n(Expired)")
        await safe_answer(callback)
        return
    suffix = "\n\nKept in pool."
    if choice == "yes":
        from ..config import settings
        pool_root = Path(settings.music_root) / ".pool"
        async with get_session() as session:
            for tid in track_ids:
                t = (await session.exec(select(Track).where(Track.id == tid))).first()
                if t:
                    remove_pool_file(pool_root / t.pool_path)
                    await session.delete(t)
            await session.commit()
        suffix = f"\n\nDeleted {len(track_ids)} track(s) from pool."
    await callback.message.edit_text(callback.message.text + suffix)
    await safe_answer(callback)


@admin_router.message(Command("removetrack"))
async def cmd_removetrack(message: Message) -> None:
    try:
        args = shlex.split((message.text or "").split(" ", 1)[1] if " " in (message.text or "") else "")
    except ValueError:
        await message.answer("Invalid syntax (unclosed quote?)")
        return
    if not args:
        await message.answer("Usage: /removetrack <path> [username]")
        return

    rel_path = args[0]
    username: str | None = None
    target_user_id: int | None = None

    if len(args) >= 2:
        async with get_session() as session:
            u = (await session.exec(select(User).where(User.username == args[1]))).first()
        if not u:
            await message.answer(f"No user: {args[1]}")
            return
        username, target_user_id = args[1], u.id
    from ..config import settings
    pool_root = Path(settings.music_root) / ".pool"
    prefix = _parse_wildcard_prefix(rel_path)

    async with get_session() as session:
        if prefix is not None:
            result = await session.exec(select(Track))
            tracks = [t for t in result.all() if t.pool_path.startswith(prefix + "/")]
        else:
            t = (await session.exec(select(Track).where(Track.pool_path == rel_path))).first()
            tracks = [t] if t else []

        if not tracks:
            await message.answer(f"No tracks found: {rel_path}")
            return

        if target_user_id is None:
            for track in tracks:
                await _remove_track(session, pool_root, track)
            await session.commit()
            await message.answer(f"Removed {len(tracks)} track(s).")
            return

        orphaned: list[int] = []
        removed = 0
        for track in tracks:
            r = await _drop_ownership(session, track, target_user_id)
            if r is not None:
                removed += 1
                if r:
                    orphaned.append(track.id)
        await session.commit()

    if not removed:
        await message.answer(f"No tracks owned by {username}: {rel_path}")
        return

    base = f"Removed {removed} track(s) from {username}."
    if not orphaned:
        await message.answer(base)
        return

    key = secrets.token_hex(6)
    _pending_pool_deletes[key] = orphaned
    await message.answer(
        base + f"\n{len(orphaned)} track(s) now have no owners. Delete from pool and DB?",
        reply_markup=_orphan_keyboard(key),
    )


_pending_retags: dict[str, dict[str, Any]] = {}


def _album_rgid(album_dir: Path) -> str | None:
    """Release-group id read from any audio file in an album dir."""
    try:
        from beets import mediafile as mf_lib
        for e in album_dir.iterdir():
            if e.is_file() and not _is_cover(e) and (rgid := mf_lib.MediaFile(str(e)).mb_releasegroupid):
                return rgid
    except Exception:
        pass
    return None


async def _resolve_retag_scope(raw: str | None):
    """``(track_ids, cover_dirs, track_albums)`` for a /retag arg, or ``None`` if the
    wildcard is invalid. Re-taggable rows carry a ``musicbrainz_id``; ``cover_dirs``
    (empty => no refetch) are albums whose folder cover to refresh; ``track_albums``
    drives the confirm gate. No arg -> whole library; ``prefix/*`` -> that subtree;
    a bare ``front.<ext>`` -> that album's cover only; else an exact track path."""
    async with get_session() as session:
        rows = [t for t in (await session.exec(select(Track))).all() if t.musicbrainz_id]
    if raw is None:
        tracks, refetch = rows, True
    elif raw.endswith("/*"):
        prefix = _parse_wildcard_prefix(raw)
        if prefix is None:
            return None
        tracks, refetch = [t for t in rows if t.pool_path.startswith(prefix + "/")], True
    elif _is_cover(Path(raw)):
        return [], {str(Path(raw).parent)}, set()
    else:
        tracks, refetch = [t for t in rows if t.pool_path == raw], False
    albums = {str(Path(t.pool_path).parent) for t in tracks}
    return [t.id for t in tracks], (albums if refetch else set()), albums


def _retag_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes, re-tag", callback_data=f"retag_go:{key}:yes"),
        InlineKeyboardButton(text="No", callback_data=f"retag_go:{key}:no"),
    ]])


async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:  # unchanged text / transient edit -- progress is best-effort
        pass


async def _run_retag(status: Message, track_ids: list[int], cover_dirs: list[str]) -> str:
    """Re-tag each track by its stored MB id (fresh metadata + enrichment), then
    refresh ``cover_dirs`` album covers. Edits ``status`` with progress."""
    from ..config import settings
    from ..beets_svc import retag_by_id
    from ..library import ensure_album_cover, promote_and_relink

    pool_root = Path(settings.music_root) / ".pool"
    staging = Path(settings.staging_root) / "retag"
    staging.mkdir(parents=True, exist_ok=True)
    total, done, failed, covers = len(track_ids), 0, 0, 0

    for i, tid in enumerate(track_ids, 1):
        async with get_session() as session:
            track = (await session.exec(select(Track).where(Track.id == tid))).first()
            if track is None:
                continue
            tmp = staging / f"{tid}_{Path(track.pool_path).name}"
            try:
                shutil.copy2(pool_root / track.pool_path, tmp)
                staged, dest = await retag_by_id(tmp, track.musicbrainz_id, enrich=True)
                # If the refreshed path already belongs to another row, keep this file
                # where it is (re-tagged in place) rather than moving onto -- and
                # overwriting -- that track. Tags still get refreshed; only the move is
                # skipped.
                new_rel = pool_rel(dest)
                if new_rel != track.pool_path and (await session.exec(
                    select(Track).where(Track.pool_path == new_rel, Track.id != tid)
                )).first() is not None:
                    dest = pool_root / track.pool_path
                await promote_and_relink(session, pool_root, track, staged, dest)
                await session.commit()
                done += 1
            except Exception:
                log.exception("retag failed for %s", track.pool_path)
                failed += 1
                tmp.unlink(missing_ok=True)
        if i % 5 == 0 or i == total:
            await _safe_edit(status, f"Re-tagging… {i}/{total}" + (f" ({failed} failed)" if failed else ""))

    for album in cover_dirs:
        if rgid := _album_rgid(pool_root / album):
            try:
                await ensure_album_cover(rgid, pool_root / album / "_anchor", force=True)
                covers += 1
            except Exception:
                log.debug("cover refetch failed for %s", album)

    parts = [f"Re-tagged {done} track(s)"]
    if failed:
        parts.append(f"{failed} failed")
    if cover_dirs:
        parts.append(f"{covers} cover(s) refreshed")
    return ", ".join(parts) + "."


@admin_router.message(Command("retag"))
async def cmd_retag(message: Message) -> None:
    try:
        args = shlex.split((message.text or "").split(" ", 1)[1] if " " in (message.text or "") else "")
    except ValueError:
        await message.answer("Invalid syntax (unclosed quote?)")
        return
    scope = await _resolve_retag_scope(args[0] if args else None)
    if scope is None:
        await message.answer("Invalid wildcard. Use an exact path, prefix/*, or no argument for the whole library.")
        return
    track_ids, cover_dirs, track_albums = scope
    if not track_ids and not cover_dirs:
        await message.answer("Nothing to re-tag.")
        return

    summary = f"{len(track_ids)} track(s)" + (f" + {len(cover_dirs)} cover(s)" if cover_dirs else "")
    if len(track_albums) > 1:  # confirm only when spanning more than one album
        key = secrets.token_hex(6)
        _pending_retags[key] = {"track_ids": track_ids, "cover_dirs": list(cover_dirs)}
        await message.answer(f"Re-tag {summary} across {len(track_albums)} albums?", reply_markup=_retag_keyboard(key))
        return
    status = await message.answer(f"Re-tagging {summary}…")
    await _safe_edit(status, await _run_retag(status, track_ids, list(cover_dirs)))


@admin_router.callback_query(lambda c: c.data and c.data.startswith("retag_go:"))
async def cb_retag(callback: CallbackQuery) -> None:
    _, key, choice = callback.data.split(":", 2)
    payload = _pending_retags.pop(key, None)
    if payload is None:
        await callback.message.edit_text(callback.message.text + "\n\n(Expired)")
    elif choice != "yes":
        await callback.message.edit_text(callback.message.text + "\n\nCancelled.")
    else:
        await callback.message.edit_text(callback.message.text + "\n\nRe-tagging…")
        result = await _run_retag(callback.message, payload["track_ids"], payload["cover_dirs"])
        await _safe_edit(callback.message, result)
    await safe_answer(callback)


@admin_router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    async with get_session() as session:
        result = await session.exec(select(User).order_by(User.username))
        rows = result.all()

    if not rows:
        await message.answer("No users")
        return

    lines = [f"— {u.username} (tg_id={u.tg_id})" for u in rows]
    await message.answer("Users:\n" + "\n".join(lines))
