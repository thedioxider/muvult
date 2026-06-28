import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlmodel import select

from ..beets_svc import get_candidates, apply_and_move, move_as_is
from ..db import Track, TrackOwnership, User, get_session
from ..models import Candidate, ConfirmationMode, FileStatus, TagResult
from ..pool import create_symlink, remove_symlink, remove_pool_file, update_symlinks
from ..quality import is_better

upload_router = Router()

_confirmation_queues: dict[int, asyncio.Queue] = {}
_confirmation_active: dict[int, bool] = {}

_STATUS_ICONS = {
    FileStatus.DOWNLOADING: "📥",
    FileStatus.TAGGING: "🔍",
    FileStatus.PENDING: "⚠️",
    FileStatus.IMPORTED: "✅",
    FileStatus.SKIPPED: "⏭️",
    FileStatus.DUPLICATE: "🔁",
    FileStatus.FAILED: "❌",
}

_STATUS_LABELS = {
    FileStatus.DOWNLOADING: "Downloading",
    FileStatus.TAGGING: "Fetching metadata",
    FileStatus.PENDING: "Pending",
    FileStatus.IMPORTED: "Imported",
    FileStatus.SKIPPED: "Skipped",
    FileStatus.DUPLICATE: "Duplicate (skipped)",
    FileStatus.FAILED: "Failed",
}

_TERMINAL = {FileStatus.IMPORTED, FileStatus.SKIPPED, FileStatus.DUPLICATE, FileStatus.FAILED}


@dataclass
class FileState:
    original_name: str
    status: FileStatus
    note: str = ""


def _format_status_message(states: dict[str, FileState]) -> str:
    groups: dict[FileStatus, list[FileState]] = {}
    for fs in states.values():
        groups.setdefault(fs.status, []).append(fs)

    lines = []
    for status in FileStatus:
        files = groups.get(status, [])
        if not files:
            continue
        icon = _STATUS_ICONS[status]
        label = _STATUS_LABELS[status]
        lines.append(f"{icon} {label} ({len(files)}):")
        for f in files:
            note = f" — {f.note}" if f.note else ""
            lines.append(f"  • {f.original_name}{note}")
    return "\n".join(lines) or "Processing..."


@dataclass
class _ConfirmationRequest:
    filename: str
    tag_result: TagResult
    file_path: Path
    future: asyncio.Future


async def _edit_status(bot: Bot, chat_id: int, msg_id: int, states: dict[str, FileState]) -> None:
    try:
        await bot.edit_message_text(_format_status_message(states), chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass


async def _ask_confirmation(bot: Bot, tg_id: int, req: "_ConfirmationRequest") -> None:
    tag = req.tag_result
    if not tag.candidates:
        buttons = [[
            InlineKeyboardButton(text="Import as-is", callback_data=f"conf:{req.filename}:asis"),
            InlineKeyboardButton(text="Skip", callback_data=f"conf:{req.filename}:skip"),
        ]]
        text = f"No matches found for *{req.filename}*"
    elif tag.recommendation >= 3:
        c = tag.candidates[0]
        text = (
            f"*{req.filename}*\n"
            f"Match: {c.artist} — {c.title} ({c.album}, {c.year})\n"
            f"Confidence: {(1 - c.distance) * 100:.0f}%"
        )
        buttons = [[
            InlineKeyboardButton(text="Import", callback_data=f"conf:{req.filename}:0"),
            InlineKeyboardButton(text="See others", callback_data=f"conf:{req.filename}:list"),
            InlineKeyboardButton(text="Skip", callback_data=f"conf:{req.filename}:skip"),
        ]]
    else:
        text = f"Low confidence matches for *{req.filename}*:"
        rows = []
        for c in tag.candidates:
            rows.append([InlineKeyboardButton(
                text=f"{c.artist} — {c.title} ({(1 - c.distance) * 100:.0f}%)",
                callback_data=f"conf:{req.filename}:{c.index}",
            )])
        rows.append([
            InlineKeyboardButton(text="Import as-is", callback_data=f"conf:{req.filename}:asis"),
            InlineKeyboardButton(text="Skip", callback_data=f"conf:{req.filename}:skip"),
        ])
        buttons = rows

    await bot.send_message(tg_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")


async def _process_file(
    bot: Bot,
    tg_id: int,
    user_id: int,
    file_msg_id: int | None,
    filename: str,
    file_id: str,
    states: dict[str, FileState],
    status_chat_id: int,
    status_msg_id: int,
) -> None:
    from ..config import settings

    staging_dir = Path(settings.staging_root) / str(tg_id)
    staging_dir.mkdir(parents=True, exist_ok=True)
    file_path = staging_dir / filename

    try:
        states[filename] = FileState(filename, FileStatus.DOWNLOADING)
        await _edit_status(bot, status_chat_id, status_msg_id, states)
        await bot.download(file_id, destination=file_path)

        states[filename].status = FileStatus.TAGGING
        await _edit_status(bot, status_chat_id, status_msg_id, states)
        tag_result = await get_candidates(file_path)

        async with get_session() as session:
            result = await session.exec(select(User).where(User.tg_id == tg_id))
            db_user = result.first()
        mode_str = json.loads(db_user.settings).get("confirmation", "auto") if db_user else "auto"
        mode = ConfirmationMode(mode_str)

        is_high = tag_result.recommendation >= 3
        chosen_index: int | str | None = None

        if mode == ConfirmationMode.OFF:
            chosen_index = 0 if (is_high and tag_result.candidates) else "asis"
        elif mode == ConfirmationMode.AUTO:
            if is_high and tag_result.candidates:
                chosen_index = 0
            else:
                chosen_index = await _queue_confirmation(bot, tg_id, filename, tag_result, file_path, states, status_chat_id, status_msg_id)
        else:
            chosen_index = await _queue_confirmation(bot, tg_id, filename, tag_result, file_path, states, status_chat_id, status_msg_id)

        if chosen_index is None:
            states[filename] = FileState(filename, FileStatus.SKIPPED)
            await _edit_status(bot, status_chat_id, status_msg_id, states)
            return

        if chosen_index == "asis":
            pool_file = await move_as_is(file_path)
            mb_id = None
        else:
            candidate = tag_result.candidates[int(chosen_index)]
            pool_file = await apply_and_move(file_path, candidate)
            mb_id = candidate.mb_track_id

        from beets import mediafile as mf_lib
        try:
            mf = mf_lib.MediaFile(str(pool_file))
            new_bitrate = mf.bitrate // 1000 if mf.bitrate else 0
            new_format = pool_file.suffix.lstrip(".")
        except Exception:
            new_bitrate, new_format = 0, pool_file.suffix.lstrip(".")

        note = ""
        async with get_session() as session:
            if mb_id:
                result = await session.exec(select(Track).where(Track.musicbrainz_id == mb_id))
            else:
                result = await session.exec(select(Track).where(Track.pool_path == str(pool_file)))
            existing = result.first()

            if existing:
                if is_better(new_bitrate, new_format, existing.bitrate, existing.format):
                    own_result = await session.exec(
                        select(TrackOwnership).where(TrackOwnership.track_id == existing.id)
                    )
                    ownerships = own_result.all()
                    old_links = [Path(o.symlink_path) for o in ownerships]
                    new_links = update_symlinks(Path(existing.pool_path), pool_file, old_links)
                    remove_pool_file(Path(existing.pool_path))
                    note = f"replaced {existing.format.upper()} {existing.bitrate}kbps"
                    existing.pool_path = str(pool_file)
                    existing.bitrate = new_bitrate
                    existing.format = new_format
                    for ownership, new_l in zip(ownerships, new_links):
                        ownership.symlink_path = str(new_l)
                    track_id = existing.id
                else:
                    remove_pool_file(pool_file)
                    states[filename] = FileState(filename, FileStatus.DUPLICATE, "better quality exists")
                    await _edit_status(bot, status_chat_id, status_msg_id, states)
                    await session.commit()
                    return
            else:
                track = Track(
                    pool_path=str(pool_file), musicbrainz_id=mb_id,
                    format=new_format, bitrate=new_bitrate,
                )
                session.add(track)
                await session.flush()
                track_id = track.id

            own_check = await session.exec(
                select(TrackOwnership)
                .where(TrackOwnership.track_id == track_id, TrackOwnership.user_id == user_id)
            )
            if not own_check.first():
                u_result = await session.exec(select(User).where(User.id == user_id))
                user_dir = Path(settings.music_root) / u_result.first().username
                symlink = create_symlink(pool_file, user_dir)
                session.add(TrackOwnership(track_id=track_id, user_id=user_id, symlink_path=str(symlink)))
            await session.commit()

        states[filename] = FileState(filename, FileStatus.IMPORTED, note)
        await _edit_status(bot, status_chat_id, status_msg_id, states)

    except Exception as e:
        states[filename] = FileState(filename, FileStatus.FAILED, str(e)[:80])
        await _edit_status(bot, status_chat_id, status_msg_id, states)
        if file_path.exists():
            file_path.unlink()


async def _queue_confirmation(
    bot: Bot,
    tg_id: int,
    filename: str,
    tag_result: TagResult,
    file_path: Path,
    states: dict[str, FileState],
    status_chat_id: int,
    status_msg_id: int,
) -> int | str | None:
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()

    req = _ConfirmationRequest(filename=filename, tag_result=tag_result, file_path=file_path, future=future)

    if tg_id not in _confirmation_queues:
        _confirmation_queues[tg_id] = asyncio.Queue()

    await _confirmation_queues[tg_id].put(req)
    states[filename].status = FileStatus.PENDING
    await _edit_status(bot, status_chat_id, status_msg_id, states)

    if not _confirmation_active.get(tg_id):
        asyncio.create_task(_drain_confirmation_queue(bot, tg_id))

    return await future


async def _drain_confirmation_queue(bot: Bot, tg_id: int) -> None:
    _confirmation_active[tg_id] = True
    q = _confirmation_queues.get(tg_id)
    while q and not q.empty():
        req: _ConfirmationRequest = await q.get()
        if not req.future.done():
            await _ask_confirmation(bot, tg_id, req)
            await req.future
    _confirmation_active[tg_id] = False


@upload_router.callback_query(lambda c: c.data and c.data.startswith("conf:"))
async def cb_confirmation(callback: CallbackQuery, bot: Bot) -> None:
    _, filename, choice = callback.data.split(":", 2)
    tg_id = callback.from_user.id

    q = _confirmation_queues.get(tg_id)
    if not q:
        await callback.answer("No pending confirmation")
        return

    pending: list[_ConfirmationRequest] = []
    while not q.empty():
        pending.append(q.get_nowait())

    resolved = False
    for req in pending:
        if req.filename == filename and not req.future.done():
            if choice == "skip":
                req.future.set_result(None)
            elif choice == "asis":
                req.future.set_result("asis")
            elif choice == "list":
                req.future.set_result("list")
            else:
                req.future.set_result(int(choice))
            resolved = True
        else:
            await q.put(req)

    await callback.message.delete()
    await callback.answer()

    if resolved and choice == "list":
        q2 = _confirmation_queues.get(tg_id)
        for req in pending:
            if req.filename == filename:
                req.tag_result.recommendation = 0
                new_future: asyncio.Future = asyncio.get_event_loop().create_future()
                new_req = _ConfirmationRequest(req.filename, req.tag_result, req.file_path, new_future)
                await q2.put(new_req)
                asyncio.create_task(_drain_confirmation_queue(bot, tg_id))


@upload_router.message(lambda m: m.audio or m.document)
async def handle_audio(message: Message, bot: Bot) -> None:
    from ..config import settings

    tg_id = message.from_user.id

    audio = message.audio or message.document
    filename = getattr(audio, "file_name", None) or f"{audio.file_id}.audio"

    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        row = result.first()
    if not row:
        return
    user_id = row.id

    states: dict[str, FileState] = {filename: FileState(filename, FileStatus.DOWNLOADING)}
    status_msg = await message.answer(_format_status_message(states))

    asyncio.create_task(_process_file(
        bot=bot,
        tg_id=tg_id,
        user_id=user_id,
        file_msg_id=message.message_id,
        filename=filename,
        file_id=audio.file_id,
        states=states,
        status_chat_id=message.chat.id,
        status_msg_id=status_msg.message_id,
    ))
