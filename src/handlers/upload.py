import asyncio
from asyncio import get_running_loop
import html
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlmodel import select

from ..beets_svc import get_candidates, apply_and_stage, stage_as_is, normalize_title
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

# Per-user status-message batching. Every incoming audio/document message --
# whether or not Telegram tagged it as part of a media group -- joins the
# sender's batch; album files coalesce into it purely because they arrive
# within the debounce window of each other, no group_id special-casing needed.
_MSG_CHAR_BUDGET = 4096 * 2 // 3  # Telegram's hard message-text cap is 4096 chars
_MSG_TRACK_CAP = 32  # backstop for many short filenames whose overhead adds up
_LINE_OVERHEAD = 24  # approx markup + note-text allowance per file line
_BATCH_DEBOUNCE_SECONDS = 1.0


@dataclass
class _UserBatch:
    states: dict[str, "FileState"] = field(default_factory=dict)
    position: dict[str, int] = field(default_factory=dict)  # file_id -> partition index (stable once set)
    open_chars: int = 0  # running estimated length of the still-open (last) partition
    open_count: int = 0  # file count of the still-open (last) partition
    partition_count: int = 0
    chat_id: int | None = None
    message_ids: list[int] = field(default_factory=list)  # one per partition, replaced wholesale on each flush


_user_batches: dict[int, _UserBatch] = {}
_batch_locks: dict[int, asyncio.Lock] = {}
_batch_debounce_tasks: dict[int, asyncio.Task] = {}


def _get_batch_lock(tg_id: int) -> asyncio.Lock:
    return _batch_locks.setdefault(tg_id, asyncio.Lock())


def _estimate_line_chars(filename: str) -> int:
    return len(html.escape(filename)) + _LINE_OVERHEAD


def _assign_partition(batch: _UserBatch, file_id: str, filename: str) -> int:
    """Pick the partition (== Telegram message) a new file lands in.

    A file joins the current open partition unless doing so would overflow its
    estimated char budget or its track cap, in which case a new partition opens.
    Assignment is permanent -- a file never moves to a different partition once
    set, so each message's formatting stays self-contained (own status-group
    headers), never split mid-listing across two messages.
    """
    est = _estimate_line_chars(filename)
    overflow = (
        batch.partition_count == 0
        or batch.open_count >= _MSG_TRACK_CAP
        or batch.open_chars + est > _MSG_CHAR_BUDGET
    )
    if overflow:
        batch.partition_count += 1
        batch.open_chars = 0
        batch.open_count = 0
    idx = batch.partition_count - 1
    batch.open_chars += est
    batch.open_count += 1
    batch.position[file_id] = idx
    return idx


def _partition_states(batch: _UserBatch, idx: int) -> dict[str, "FileState"]:
    return {fid: st for fid, st in batch.states.items() if batch.position[fid] == idx}


def _maybe_close_batch(tg_id: int, batch: _UserBatch) -> None:
    all_done = all(fs.status in _TERMINAL for fs in batch.states.values())
    task = _batch_debounce_tasks.get(tg_id)
    timer_pending = task is not None and not task.done()
    if all_done and not timer_pending and _user_batches.get(tg_id) is batch:
        _user_batches.pop(tg_id, None)


async def _report_batch_status(bot: Bot, tg_id: int, file_id: str) -> None:
    async with _get_batch_lock(tg_id):
        batch = _user_batches.get(tg_id)
        if batch is None or file_id not in batch.position:
            return
        idx = batch.position[file_id]
        if idx < len(batch.message_ids):
            text = _format_status_message(_partition_states(batch, idx))
            try:
                await bot.edit_message_text(
                    text, chat_id=batch.chat_id, message_id=batch.message_ids[idx], parse_mode="HTML"
                )
            except Exception:
                pass
        _maybe_close_batch(tg_id, batch)


async def _flush_user_batch(bot: Bot, tg_id: int, chat_id: int) -> None:
    try:
        await asyncio.sleep(_BATCH_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    async with _get_batch_lock(tg_id):
        batch = _user_batches.get(tg_id)
        if batch is None:
            return
        for msg_id in batch.message_ids:
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        batch.message_ids = []
        for idx in range(batch.partition_count):
            text = _format_status_message(_partition_states(batch, idx))
            msg = await bot.send_message(chat_id, text, parse_mode="HTML")
            batch.message_ids.append(msg.message_id)
        batch.chat_id = chat_id
        _batch_debounce_tasks.pop(tg_id, None)
        _maybe_close_batch(tg_id, batch)

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
    radio edit, ...). Title comparison is loose (punctuation-insensitive, e.g.
    "ATWA" == "A.T.W.A") since MusicBrainz submissions spell the same title
    inconsistently -- a false-positive twin here just adds a harmless extra
    choice to the prompt below, so it's safe to err on catching more. This
    returns #0 and every such twin, in list order, so a single-element result
    means the top match is unambiguous. Each Candidate keeps its original
    ``.index`` (its position in the full list), which the confirmation
    callbacks resolve against.
    """
    if not candidates:
        return []
    key = (candidates[0].artist.lower(), normalize_title(candidates[0].title, loose=True))
    return [
        c for c in candidates
        if (c.artist.lower(), normalize_title(c.title, loose=True)) == key
    ]


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
    # Set once the prompt is actually sent (_ask_confirmation); lets the callback
    # handler check the click came from *this* message, not a stale one left over
    # from a crash/restart -- cheaper than embedding an id in callback_data, and
    # frees callback_data to hold only the choice (filenames alone can already
    # blow Telegram's 64-byte callback_data cap, see BUTTON_DATA_INVALID).
    message_id: int | None = None


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
            callback_data=f"conf{_CB_SEP}{c.index}",
        )
        if rows and len(rows[-1]) == 1:
            rows[-1].append(btn)
        else:
            rows.append([btn])

    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="<<", callback_data=f"conf{_CB_SEP}prev"),
            InlineKeyboardButton(text=f"{req.page + 1}/{pages}", callback_data=f"conf{_CB_SEP}noop"),
            InlineKeyboardButton(text=">>", callback_data=f"conf{_CB_SEP}next"),
        ])
    rows.append([
        InlineKeyboardButton(text="Import as-is", callback_data=f"conf{_CB_SEP}asis"),
        InlineKeyboardButton(text="Skip", callback_data=f"conf{_CB_SEP}skip"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_confirmation(bot: Bot, tg_id: int, req: "_ConfirmationRequest") -> None:
    tag = req.tag_result
    fname = html.escape(req.filename)
    if not tag.candidates:
        buttons = [[
            InlineKeyboardButton(text="Import as-is", callback_data=f"conf{_CB_SEP}asis"),
            InlineKeyboardButton(text="Skip", callback_data=f"conf{_CB_SEP}skip"),
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
            InlineKeyboardButton(text="Import", callback_data=f"conf{_CB_SEP}0"),
            InlineKeyboardButton(text="See others", callback_data=f"conf{_CB_SEP}list"),
            InlineKeyboardButton(text="Skip", callback_data=f"conf{_CB_SEP}skip"),
        ]]
    else:
        text, markup = _render_list_page(req)
        msg = await bot.send_message(tg_id, text, reply_markup=markup, parse_mode="HTML")
        req.message_id = msg.message_id
        return

    msg = await bot.send_message(tg_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    req.message_id = msg.message_id


async def _process_file(
    bot: Bot,
    tg_id: int,
    user_id: int,
    filename: str,
    file_id: str,
    states: dict[str, FileState],
    report: Callable[[], Awaitable[None]],
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
        await report()
        await bot.download(file_id, destination=file_path)

        states[file_id].status = FileStatus.TAGGING
        await report()
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
                    file_path, states, report,
                )
        else:
            while True:
                chosen_index = await _queue_confirmation(bot, tg_id, file_id, filename, tag_result, file_path, states, report)
                if chosen_index != "list":
                    break
                tag_result.recommendation = 0

        if chosen_index is None:
            states[file_id] = FileState(filename, FileStatus.SKIPPED)
            await report()
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
        await report()

    except Exception as e:
        log.exception("processing %s failed", filename)
        states[file_id] = FileState(filename, FileStatus.FAILED, str(e)[:80])
        await report()
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
    report: Callable[[], Awaitable[None]],
) -> int | str | None:
    future: asyncio.Future = get_running_loop().create_future()

    req = _ConfirmationRequest(filename=filename, tag_result=tag_result, file_path=file_path, future=future)

    if tg_id not in _confirmation_queues:
        _confirmation_queues[tg_id] = asyncio.Queue()

    await _confirmation_queues[tg_id].put(req)
    states[file_id].status = FileStatus.PENDING
    await report()

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
    _, choice = callback.data.split(_CB_SEP, 1)
    tg_id = callback.from_user.id

    req = _active_confirmations.get(tg_id)
    if (
        not req
        or req.future.done()
        or not callback.message
        or req.message_id != callback.message.message_id
    ):
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

    file_id = audio.file_id
    async with _get_batch_lock(tg_id):
        batch = _user_batches.setdefault(tg_id, _UserBatch())
        batch.states[file_id] = FileState(filename, FileStatus.DOWNLOADING)
        _assign_partition(batch, file_id, filename)

        old_task = _batch_debounce_tasks.get(tg_id)
        if old_task:
            old_task.cancel()
        _batch_debounce_tasks[tg_id] = asyncio.create_task(
            _flush_user_batch(bot, tg_id, message.chat.id)
        )
        states = batch.states

    asyncio.create_task(_process_file(
        bot=bot, tg_id=tg_id, user_id=user_id,
        filename=filename, file_id=file_id,
        states=states,
        report=lambda: _report_batch_status(bot, tg_id, file_id),
    ))
