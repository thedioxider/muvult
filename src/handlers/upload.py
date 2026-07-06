import asyncio
from asyncio import get_running_loop
import html
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlmodel import select

from ..beets_svc import get_candidates, apply_and_stage, stage_as_is
from ..db import Track, TrackOwnership, User, get_session
from ..models import ConfirmationMode, FileStatus, TagResult
from ..pool import create_symlink, pool_rel, promote_pool_file, remove_pool_file, update_symlinks
from ..quality import is_better
from ..tg_utils import safe_answer

upload_router = Router()

log = logging.getLogger(__name__)

_CB_SEP = "\x1f"

_confirmation_queues: dict[int, asyncio.Queue] = {}
_confirmation_active: dict[int, bool] = {}
_active_confirmations: dict[int, "_ConfirmationRequest"] = {}

_group_pending: dict[str, list[tuple[str, str]]] = {}
_group_meta: dict[str, tuple] = {}
_group_tasks: dict[str, asyncio.Task] = {}

# Guards the pool-write + DB-commit critical section in _process_file so
# concurrent uploads of the same track can't collide on the unique pool_path.
_pool_commit_lock = asyncio.Lock()

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


async def _find_existing_track(session, mb_id: str | None, pool_path: str):
    """Locate the Track a staged upload maps onto.

    Prefer the recording id; fall back to the canonical pool path. A re-upload that
    now resolves to a *different* recording id (e.g. after a better MB match) still
    lands on the row already occupying its path -- which is unique -- so we replace
    that row in place instead of inserting a duplicate that would collide. Because
    the path carries the track disambiguation, a genuinely different recording of
    the same track (a live take) resolves to a different path and never adopts the
    wrong row.
    """
    if mb_id:
        result = await session.exec(select(Track).where(Track.musicbrainz_id == mb_id))
        if (existing := result.first()) is not None:
            return existing
    result = await session.exec(select(Track).where(Track.pool_path == pool_path))
    return result.first()


def _top_twins(candidates: list) -> list:
    """Candidate #0 together with any same-(artist, title) 'twins'.

    After dedup, two candidates sharing artist and title differ only by
    disambiguation -- distinct MB recordings of the "same" track (a live take, a
    radio edit, ...). This returns #0 and every such twin, in list order, so a
    single-element result means the top match is unambiguous. Each Candidate keeps
    its original ``.index`` (its position in the full list), which the confirmation
    callbacks resolve against.
    """
    if not candidates:
        return []
    key = (candidates[0].artist.lower(), candidates[0].title.lower())
    return [c for c in candidates if (c.artist.lower(), c.title.lower()) == key]


@dataclass
class FileState:
    original_name: str
    status: FileStatus
    note: str = ""


def _format_status_message(states: dict[str, FileState]) -> str:
    groups: dict[FileStatus, list[FileState]] = {}
    for fs in states.values():
        groups.setdefault(fs.status, []).append(fs)

    all_done = all(fs.status in _TERMINAL for fs in states.values())

    lines = [] if all_done else ["<b><i>⏳ Processing...</i></b>", ""]
    for status in FileStatus:
        files = groups.get(status, [])
        if not files:
            continue
        icon = _STATUS_ICONS[status]
        label = _STATUS_LABELS[status]
        lines.append(f"<b>{icon} {label}</b> ({len(files)}):")
        for f in files:
            note = f" ~ {f.note}" if f.note else ""
            lines.append(f"— <i>{f.original_name}</i>{note}")
    return "\n".join(lines) or "⏳ Processing..."


def _candidate_detail(c) -> str:
    """Recording-appropriate suffix: MB disambiguation and duration.

    Singleton matches carry no album/year (a recording spans many releases),
    so we show what a recording actually has to tell near-identical titles apart.
    """
    bits = []
    if c.disambig:
        bits.append(html.escape(c.disambig))
    if c.length:
        m, s = divmod(int(c.length), 60)
        bits.append(f"{m}:{s:02d}")
    return f" [{', '.join(bits)}]" if bits else ""


_PAGE_SIZE = 6


@dataclass
class _ConfirmationRequest:
    filename: str
    tag_result: TagResult
    file_path: Path
    future: asyncio.Future
    page: int = 0


def _render_list_page(req: "_ConfirmationRequest") -> tuple[str, InlineKeyboardMarkup]:
    """Render one page of the candidate list, with paging controls when needed.

    Candidates are shown ``_PAGE_SIZE`` at a time. Each candidate keeps its global
    number (``.index + 1``), and its button carries the original ``.index`` so the
    selection resolves against the full list regardless of the current page. When
    there is more than one page a nav row (⬅️ / ``page/total`` / ➡️) is added;
    ``req.page`` is clamped here so the caller can bump it freely. Mutates
    ``req.page`` to the clamped value.
    """
    cands = req.tag_result.candidates
    pages = max(1, (len(cands) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    req.page = max(0, min(req.page, pages - 1))
    start = req.page * _PAGE_SIZE
    fname = html.escape(req.filename)

    text = f"❓ Matches for:\n<i>{fname}</i>\n\n"
    rows: list[list[InlineKeyboardButton]] = []
    for c in cands[start:start + _PAGE_SIZE]:
        n = c.index + 1
        artist = html.escape(c.artist)
        title = html.escape(c.title)
        text += f"{n}. {artist} — {title}{_candidate_detail(c)}\n"
        btn = InlineKeyboardButton(
            text=f"#{n} ({(1 - c.distance) * 100:.0f}%)",
            callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}{c.index}",
        )
        if rows and len(rows[-1]) == 1:
            rows[-1].append(btn)
        else:
            rows.append([btn])

    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="<<", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}prev"),
            InlineKeyboardButton(text=f"{req.page + 1}/{pages}", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}noop"),
            InlineKeyboardButton(text=">>", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}next"),
        ])
    rows.append([
        InlineKeyboardButton(text="Import as-is", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}asis"),
        InlineKeyboardButton(text="Skip", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}skip"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_status(bot: Bot, chat_id: int, msg_id: int, states: dict[str, FileState]) -> None:
    try:
        await bot.edit_message_text(_format_status_message(states), chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    except Exception:
        pass


async def _ask_confirmation(bot: Bot, tg_id: int, req: "_ConfirmationRequest") -> None:
    tag = req.tag_result
    fname = html.escape(req.filename)
    if not tag.candidates:
        buttons = [[
            InlineKeyboardButton(text="Import as-is", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}asis"),
            InlineKeyboardButton(text="Skip", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}skip"),
        ]]
        text = f"❌ No matches found for:\n<i>{fname}</i>"
    elif tag.recommendation >= 3:
        c = tag.candidates[0]
        artist = html.escape(c.artist)
        title = html.escape(c.title)
        text = (
            f"❓ Import <i>{fname}</i>?\n\n"
            f"Match: <i>{artist} — {title}</i>{_candidate_detail(c)}\n"
            f"Confidence: <b>{(1 - c.distance) * 100:.0f}%</b>"
        )
        buttons = [[
            InlineKeyboardButton(text="Import", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}0"),
            InlineKeyboardButton(text="See others", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}list"),
            InlineKeyboardButton(text="Skip", callback_data=f"conf{_CB_SEP}{req.filename}{_CB_SEP}skip"),
        ]]
    else:
        text, markup = _render_list_page(req)
        await bot.send_message(tg_id, text, reply_markup=markup, parse_mode="HTML")
        return

    await bot.send_message(tg_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


async def _process_file(
    bot: Bot,
    tg_id: int,
    user_id: int,
    filename: str,
    file_id: str,
    states: dict[str, FileState],
    status_chat_id: int,
    status_msg_id: int,
) -> None:
    from ..config import settings

    staging_dir = Path(settings.staging_root) / str(tg_id)
    # Each file gets its own subdir keyed by the unique file_id so two files that
    # share a name (common within one album) neither clobber each other on disk
    # nor collide in the status dict. The filename -- and its extension, which
    # as-is naming reuses -- is preserved inside. states is likewise keyed by
    # file_id, with the filename kept only for display.
    file_dir = staging_dir / file_id
    file_dir.mkdir(parents=True, exist_ok=True)
    file_path = file_dir / filename

    try:
        states[file_id] = FileState(filename, FileStatus.DOWNLOADING)
        await _edit_status(bot, status_chat_id, status_msg_id, states)
        await bot.download(file_id, destination=file_path)

        states[file_id].status = FileStatus.TAGGING
        await _edit_status(bot, status_chat_id, status_msg_id, states)
        tag_result = await get_candidates(file_path)

        async with get_session() as session:
            result = await session.exec(select(User).where(User.tg_id == tg_id))
            db_user = result.first()
        user_settings = json.loads(db_user.settings) if db_user else {}
        mode = ConfirmationMode(user_settings.get("confirmation", "auto"))
        enrich = user_settings.get("enrich", True)

        is_high = tag_result.recommendation >= 3
        chosen_index: int | str | None = None

        if mode == ConfirmationMode.OFF:
            chosen_index = 0 if (is_high and tag_result.candidates) else "asis"
        elif mode == ConfirmationMode.AUTO and is_high and tag_result.candidates:
            twins = _top_twins(tag_result.candidates)
            if len(twins) == 1:
                chosen_index = 0  # unique high-confidence match: import without asking
            else:
                # High confidence, but candidate #0 has same-artist+title twins that
                # dedup kept apart by disambiguation (a live take, a radio edit, ...).
                # We can't tell which the user meant, so prompt -- but only among the
                # twins, not the whole list. The buttons carry each candidate's
                # original .index (its position in the full list), so the int result
                # still resolves against tag_result.candidates below.
                chosen_index = await _queue_confirmation(
                    bot, tg_id, file_id, filename,
                    TagResult(candidates=twins, recommendation=0),
                    file_path, states, status_chat_id, status_msg_id,
                )
        else:
            while True:
                chosen_index = await _queue_confirmation(bot, tg_id, file_id, filename, tag_result, file_path, states, status_chat_id, status_msg_id)
                if chosen_index != "list":
                    break
                tag_result.recommendation = 0

        if chosen_index is None:
            states[file_id] = FileState(filename, FileStatus.SKIPPED)
            await _edit_status(bot, status_chat_id, status_msg_id, states)
            return

        is_asis = chosen_index == "asis"
        if is_asis:
            staged, dest = await stage_as_is(file_path, db_user.username)
            mb_id = None
        else:
            candidate = tag_result.candidates[int(chosen_index)]
            staged, dest = await apply_and_stage(file_path, candidate, enrich)
            mb_id = candidate.mb_track_id

        from beets import mediafile as mf_lib
        try:
            mf = mf_lib.MediaFile(str(staged))
            new_bitrate = mf.bitrate // 1000 if mf.bitrate else 0
            new_format = staged.suffix.lstrip(".")
        except Exception:
            new_bitrate, new_format = 0, staged.suffix.lstrip(".")

        note = ""
        pool_root = Path(settings.music_root) / ".pool"
        # Serialize the dedup->promote->insert->ownership->commit critical
        # section: files are processed concurrently, so two uploads resolving to
        # the same canonical path could both find no existing Track, both write
        # onto dest, and collide on the unique pool_path. beets tagging is already
        # serialized; this short section is the only remaining shared-state race.
        async with _pool_commit_lock, get_session() as session:
            existing = await _find_existing_track(session, mb_id, pool_rel(dest))

            if existing:
                old_pool = pool_root / existing.pool_path
                is_upgrade = is_better(new_bitrate, new_format, existing.bitrate, existing.format)
                # A same-or-better upload replaces the recorded copy (refreshing
                # tags on a re-upload of equal quality); a strictly worse one
                # loses -- unless the recorded copy is missing (a dangling row),
                # in which case even a worse upload heals it instead of being
                # discarded.
                replaces = is_better(
                    new_bitrate, new_format, existing.bitrate, existing.format, or_equal=True
                )
                if replaces or not old_pool.exists():
                    pool_file = promote_pool_file(staged, dest)
                    if pool_file != old_pool:
                        own_result = await session.exec(
                            select(TrackOwnership).where(TrackOwnership.track_id == existing.id)
                        )
                        ownerships = own_result.all()
                        old_links = [Path(o.symlink_path) for o in ownerships]
                        new_links = update_symlinks(old_pool, pool_file, old_links)
                        remove_pool_file(old_pool)
                        for ownership, new_l in zip(ownerships, new_links):
                            ownership.symlink_path = str(new_l)
                    if is_upgrade:
                        note = f"{existing.format.upper()} {existing.bitrate}kbps → {new_format.upper()} {new_bitrate}kbps"
                    existing.pool_path = pool_rel(pool_file)
                    existing.musicbrainz_id = mb_id  # may have changed (better MB match)
                    existing.bitrate = new_bitrate
                    existing.format = new_format
                    track_id = existing.id
                else:
                    staged.unlink(missing_ok=True)  # drop the staged copy; canonical stays
                    pool_file = old_pool
                    track_id = existing.id
            else:
                pool_file = promote_pool_file(staged, dest)
                track = Track(
                    pool_path=pool_rel(pool_file), musicbrainz_id=mb_id,
                    format=new_format, bitrate=new_bitrate, is_asis=is_asis,
                )
                session.add(track)
                await session.flush()
                track_id = track.id

            own_check = await session.exec(
                select(TrackOwnership)
                .where(TrackOwnership.track_id == track_id, TrackOwnership.user_id == user_id)
            )
            already_owned = own_check.first()
            if not already_owned:
                u_result = await session.exec(select(User).where(User.id == user_id))
                user_dir = Path(settings.music_root) / u_result.first().username
                symlink = create_symlink(pool_file, user_dir, flat=is_asis)
                session.add(TrackOwnership(track_id=track_id, user_id=user_id, symlink_path=str(symlink)))
            await session.commit()

        if already_owned:
            states[file_id] = FileState(filename, FileStatus.DUPLICATE)
        else:
            states[file_id] = FileState(filename, FileStatus.IMPORTED, note)
        await _edit_status(bot, status_chat_id, status_msg_id, states)

    except Exception as e:
        log.exception("processing %s failed", filename)
        states[file_id] = FileState(filename, FileStatus.FAILED, str(e)[:80])
        await _edit_status(bot, status_chat_id, status_msg_id, states)
    finally:
        # Drop the per-file staging dir (and any leftover download) on every
        # exit -- imported files were already moved into the pool, the rest are
        # scratch. The sidecar reaper is a backstop for crashes, not this path.
        shutil.rmtree(file_dir, ignore_errors=True)


async def _queue_confirmation(
    bot: Bot,
    tg_id: int,
    file_id: str,
    filename: str,
    tag_result: TagResult,
    file_path: Path,
    states: dict[str, FileState],
    status_chat_id: int,
    status_msg_id: int,
) -> int | str | None:
    future: asyncio.Future = get_running_loop().create_future()

    req = _ConfirmationRequest(filename=filename, tag_result=tag_result, file_path=file_path, future=future)

    if tg_id not in _confirmation_queues:
        _confirmation_queues[tg_id] = asyncio.Queue()

    await _confirmation_queues[tg_id].put(req)
    states[file_id].status = FileStatus.PENDING
    await _edit_status(bot, status_chat_id, status_msg_id, states)

    if not _confirmation_active.get(tg_id):
        _confirmation_active[tg_id] = True
        asyncio.create_task(_drain_confirmation_queue(bot, tg_id))

    return await future


async def _drain_confirmation_queue(bot: Bot, tg_id: int) -> None:
    q = _confirmation_queues.get(tg_id)
    try:
        while q and not q.empty():
            req: _ConfirmationRequest = await q.get()
            if not req.future.done():
                _active_confirmations[tg_id] = req
                try:
                    await _ask_confirmation(bot, tg_id, req)
                    await req.future
                except Exception as e:
                    if not req.future.done():
                        req.future.set_exception(e)
                finally:
                    _active_confirmations.pop(tg_id, None)
    finally:
        _confirmation_active[tg_id] = False


@upload_router.callback_query(lambda c: c.data and c.data.startswith(f"conf{_CB_SEP}"))
async def cb_confirmation(callback: CallbackQuery, bot: Bot) -> None:
    _, filename, choice = callback.data.split(_CB_SEP, 2)
    tg_id = callback.from_user.id

    req = _active_confirmations.get(tg_id)
    if not req or req.filename != filename or req.future.done():
        await safe_answer(callback, "No pending confirmation")
        return

    if choice in ("prev", "next", "noop"):
        # Paging: re-render the list in place; the pending choice is untouched.
        if choice != "noop":
            req.page += 1 if choice == "next" else -1
            text, markup = _render_list_page(req)  # clamps req.page
            try:
                await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass  # e.g. "message is not modified" at a page boundary
        await safe_answer(callback)
        return

    if choice == "skip":
        req.future.set_result(None)
    elif choice == "asis":
        req.future.set_result("asis")
    elif choice == "list":
        req.future.set_result("list")
    else:
        try:
            req.future.set_result(int(choice))
        except ValueError:
            await safe_answer(callback, "Invalid choice")
            return

    await callback.message.delete()
    await safe_answer(callback)


async def _flush_group(group_id: str) -> None:
    await asyncio.sleep(0.5)
    files = _group_pending.pop(group_id, [])
    meta = _group_meta.pop(group_id, None)
    _group_tasks.pop(group_id, None)
    if not files or meta is None:
        return
    bot, tg_id, user_id, chat_id = meta
    # keyed by file_id (f[1]); filename (f[0]) is display only
    states: dict[str, FileState] = {f[1]: FileState(f[0], FileStatus.DOWNLOADING) for f in files}
    status_msg = await bot.send_message(chat_id, _format_status_message(states), parse_mode="HTML")
    for filename, file_id in files:
        asyncio.create_task(_process_file(
            bot=bot, tg_id=tg_id, user_id=user_id,
            filename=filename, file_id=file_id,
            states=states, status_chat_id=chat_id, status_msg_id=status_msg.message_id,
        ))


@upload_router.message(lambda m: m.audio or m.document)
async def handle_audio(message: Message, bot: Bot) -> None:
    tg_id = message.from_user.id

    audio = message.audio or message.document
    filename = Path(getattr(audio, "file_name", None) or f"{audio.file_id}.audio").name

    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        row = result.first()
    if not row:
        await message.answer("You don't have a user account yet. Use /adduser to add yourself.")
        return
    user_id = row.id

    group_id = message.media_group_id
    if group_id:
        _group_pending.setdefault(group_id, []).append((filename, audio.file_id))
        _group_meta[group_id] = (bot, tg_id, user_id, message.chat.id)
        if group_id not in _group_tasks:
            _group_tasks[group_id] = asyncio.create_task(_flush_group(group_id))
    else:
        states: dict[str, FileState] = {audio.file_id: FileState(filename, FileStatus.DOWNLOADING)}
        status_msg = await message.answer(_format_status_message(states), parse_mode="HTML")
        asyncio.create_task(_process_file(
            bot=bot, tg_id=tg_id, user_id=user_id,
            filename=filename, file_id=audio.file_id,
            states=states, status_chat_id=message.chat.id, status_msg_id=status_msg.message_id,
        ))
