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

from beets.autotag.match import Recommendation

from ..beets_svc import get_candidates, apply_and_stage, stage_as_is, normalize_title, fetch_cover_art_full
from ..db import Track, TrackOwnership, User, get_session
from ..models import Candidate, ConfirmationMode, FileStatus, TagResult
from ..pool import (
    create_symlink,
    ensure_cover_symlink,
    find_cover,
    pool_rel,
    promote_pool_file,
    remove_pool_file,
    update_symlinks,
)
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

# Live status edits are throttled. Editing on *every* file transition spends a
# Telegram per-chat rate-limit token per edit -- edits share the same
# burst-tolerant ~1 msg/s per-chat bucket as sends/deletes -- and a multi-track
# upload's transitions burst well past that, tripping flood control (429), which
# silently froze the status message mid-upload while the imports themselves kept
# going. Instead each transition marks its partition dirty and one per-user
# worker coalesces them into at most one edit per partition per
# _STATUS_THROTTLE_SECONDS, always rendering the *latest* state (so the terminal
# state still lands) and skipping the edit when the text hasn't changed. Flush
# resends and confirmation prompts are deliberately left alone -- resends are
# rare (once per debounce settle) and prompts are paced by the user's clicks.
_STATUS_THROTTLE_SECONDS = 2.0
_status_dirty: dict[int, set[int]] = {}  # tg_id -> dirty partition indices
_status_workers: dict[int, asyncio.Task] = {}
_status_last_text: dict[tuple[int, int], str] = {}  # (tg_id, idx) -> last rendered text

# Confirmation prompts share the same per-chat bucket: each answered prompt fires
# a delete (of the answered one) plus a send (of the next), and a fast clicker
# can burst these past the limit. Consecutive prompt *sends* are paced
# dynamically -- the wait is measured against how long the user spent on the
# previous prompt, so a long deliberation adds only _PROMPT_MIN_HOLD_SECONDS (just
# enough to separate the delete from the next send), while a quick answer waits
# out the rest of _PROMPT_THROTTLE_SECONDS. Prompts stay one-message-per-file
# (sent fresh, deleted on answer) so it's always clear the previous was answered.
_PROMPT_THROTTLE_SECONDS = 1.0  # target minimum spacing between consecutive prompt sends
_PROMPT_MIN_HOLD_SECONDS = 0.5  # floor pause after an answer before the next prompt

# Combined-confidence bar a fingerprint match must clear to be trusted without a
# human look. In AUTO a lone match at/above it auto-imports (below it prompts); in
# OFF a match at/above it is picked outright, and below it we fall back to beets'
# text search (the fingerprint is deemed too weak to be authoritative).
_FP_CONFIDENCE_THRESHOLD = 0.80


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
    worker = _status_workers.get(tg_id)
    worker_pending = worker is not None and not worker.done()
    # Don't close while a throttled status edit is still queued -- the batch must
    # survive until its final (terminal) state has actually been rendered.
    if all_done and not timer_pending and not worker_pending and _user_batches.get(tg_id) is batch:
        _user_batches.pop(tg_id, None)
        _status_dirty.pop(tg_id, None)
        for key in [k for k in _status_last_text if k[0] == tg_id]:
            _status_last_text.pop(key, None)


async def _report_batch_status(bot: Bot, tg_id: int, file_id: str) -> None:
    """Mark a file's partition dirty and ensure a throttled edit worker is running.

    Editing here directly, on every transition, floods the per-chat rate limit
    (see the _STATUS_THROTTLE_SECONDS note above); instead the actual edit is
    coalesced and rate-limited by _drain_status_updates.
    """
    async with _get_batch_lock(tg_id):
        batch = _user_batches.get(tg_id)
        if batch is None or file_id not in batch.position:
            return
        idx = batch.position[file_id]
    _status_dirty.setdefault(tg_id, set()).add(idx)
    if tg_id not in _status_workers or _status_workers[tg_id].done():
        _status_workers[tg_id] = asyncio.create_task(_drain_status_updates(bot, tg_id))


async def _drain_status_updates(bot: Bot, tg_id: int) -> None:
    """Coalesce pending status edits into at most one edit per throttle window.

    Handles *one* dirty partition per pass (lowest index first), rendering its
    *current* state -- so the latest transition always wins, the terminal state
    lands even if it arrived mid-sleep, and a multi-message batch's partitions
    take turns rather than bursting several edits into one window. A partition
    whose text is unchanged is dropped without an edit or a wait (costs no
    rate-limit token); only an edit that actually goes out is followed by the
    throttle sleep. Exits when nothing is dirty, re-arming if a transition
    slipped in during the final pass, and closes the batch once fully drained.
    """
    try:
        while _status_dirty.get(tg_id):
            async with _get_batch_lock(tg_id):
                dirty = _status_dirty.get(tg_id)
                if not dirty:
                    break
                idx = min(dirty)  # round-robin by index; finite transitions drain them all
                dirty.discard(idx)
                if not dirty:
                    _status_dirty.pop(tg_id, None)
                batch = _user_batches.get(tg_id)
                if batch is None:
                    return
                edited = await _render_partition(bot, tg_id, batch, idx)
            if edited:
                await asyncio.sleep(_STATUS_THROTTLE_SECONDS)
    finally:
        _status_workers.pop(tg_id, None)
        if _status_dirty.get(tg_id):  # a transition arrived during the final pass
            _status_workers[tg_id] = asyncio.create_task(_drain_status_updates(bot, tg_id))
            return
        async with _get_batch_lock(tg_id):
            batch = _user_batches.get(tg_id)
            if batch is not None:
                _maybe_close_batch(tg_id, batch)


async def _render_partition(bot: Bot, tg_id: int, batch: _UserBatch, idx: int) -> bool:
    """Edit one partition's message to its current state; caller holds the lock.

    Returns whether an edit was actually sent -- an unchanged render is skipped
    so it neither spends a rate-limit token nor triggers the throttle wait.
    """
    if idx >= len(batch.message_ids):
        return False
    text = _format_status_message(_partition_states(batch, idx))
    if _status_last_text.get((tg_id, idx)) == text:
        return False
    try:
        await bot.edit_message_text(
            text, chat_id=batch.chat_id, message_id=batch.message_ids[idx], parse_mode="HTML"
        )
        _status_last_text[(tg_id, idx)] = text
        return True
    except Exception:
        return False


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
            _status_last_text[(tg_id, idx)] = text  # keep the throttle's cache in step with what's on screen
        batch.chat_id = chat_id
        _batch_debounce_tasks.pop(tg_id, None)
        _maybe_close_batch(tg_id, batch)

# Guards the pool-write + DB-commit critical section in _process_file so
# concurrent uploads of the same track can't collide on the unique pool_path.
_pool_commit_lock = asyncio.Lock()

# Bounds concurrent downloads: handle_audio spawns one unbounded _process_file
# task per file, so a 100-file drop would fire 100 parallel bot.download calls,
# saturating the (local) Bot API server and aiogram's aiohttp pool until get_file
# times out. Tagging is serialized on _beets_pool anyway, so a small cap loses
# nothing.
_DOWNLOAD_CONCURRENCY = 4
_download_semaphore = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)

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

    Match on the canonical pool path first -- it's the unique key and where the
    file actually lives, so a row already occupying it *is* this track (adopt it in
    place rather than inserting a duplicate that would collide on the unique path).
    The path carries the track disambiguation, so a genuinely different recording
    of the same track (a live take) resolves to a different path and never adopts
    the wrong row. Only if no row holds the path do we fall back to the recording
    id, which catches the same recording previously filed under a different path.
    """
    result = await session.exec(select(Track).where(Track.pool_path == pool_path))
    if (existing := result.first()) is not None:
        return existing
    if mb_id:
        result = await session.exec(select(Track).where(Track.musicbrainz_id == mb_id))
        return result.first()
    return None


async def _album_owner_usernames(session, rel_album: str) -> list[str]:
    """Usernames of every user owning any track in the given album folder.

    ``rel_album`` is the album dir relative to the pool root (``<albumartist>/
    <album>``); tracks are matched by that path prefix. Drives the cover fan-out
    so a newly-created album cover is linked into all current owners' libraries."""
    prefix = rel_album + "/"
    tracks = (
        await session.exec(select(Track).where(Track.pool_path.startswith(prefix, autoescape=True)))
    ).all()
    track_ids = [t.id for t in tracks]
    if not track_ids:
        return []
    owns = (
        await session.exec(select(TrackOwnership).where(TrackOwnership.track_id.in_(track_ids)))
    ).all()
    user_ids = {o.user_id for o in owns}
    if not user_ids:
        return []
    users = (await session.exec(select(User).where(User.id.in_(user_ids)))).all()
    return [u.username for u in users]


async def _ensure_album_cover(rgid: str, pool_file: Path) -> None:
    """Fetch (once) and link the album's full-res folder cover into every owner.

    The pool holds one real ``front.<ext>`` per album (fetched from the CAA on
    first need); each owner's album folder gets a relative symlink to it, which
    Navidrome prefers over the embedded 500px art. Fanning out to *all* current
    owners -- not just the uploader -- means a user who imported the album while
    art was missing gets covered the moment anyone re-triggers the fetch. Every
    step is idempotent, so duplicates and re-uploads self-heal missing links."""
    from ..config import settings

    music_root = Path(settings.music_root)
    album_dir = pool_file.parent
    rel_album = str(Path(pool_rel(pool_file)).parent)
    cover = find_cover(album_dir)
    if cover is None:
        result = await fetch_cover_art_full(rgid)
        if result is None:
            return
        data, ext = result
        cover = find_cover(album_dir)  # re-check: a concurrent import may have won
        if cover is None:
            cover = album_dir / f"front{ext}"
            cover.write_bytes(data)
    async with get_session() as session:
        usernames = await _album_owner_usernames(session, rel_album)
    for uname in usernames:
        ensure_cover_symlink(cover, music_root / uname / rel_album)


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


def _reindexed(candidates: list[Candidate]) -> list[Candidate]:
    """Copies of ``candidates`` renumbered 0..n so ``.index`` matches position.

    A prompt built from a sublist (e.g. ``_top_twins``) must have its buttons carry
    positions in *that* list, since the callback resolves a pick against the list in
    view. Copies (not in-place) so the originals' indices are left untouched.
    """
    from dataclasses import replace

    return [replace(c, index=i) for i, c in enumerate(candidates)]


def _off_search_fallback(tag_result: TagResult) -> "Candidate | str":
    """OFF-mode choice when a fingerprint match is below the confidence bar.

    Falls back to beets' text search exactly as if the file had not fingerprinted:
    import the top text candidate when beets recommends it strongly, else import
    as-is. Returns a ``Candidate`` or the ``"asis"`` sentinel.
    """
    sc = tag_result.search_candidates
    rec = tag_result.search_recommendation
    is_high = rec is not None and rec >= Recommendation.strong
    return sc[0] if (is_high and sc) else "asis"


async def _confirm_looping(
    bot: Bot, tg_id: int, file_id: str, filename: str,
    tag_result: TagResult, file_path: Path,
    states: dict[str, "FileState"], report: Callable[[], Awaitable[None]],
) -> "Candidate | str | None":
    """Prompt, re-prompting as a full list when the user taps "See others".

    The strong single-match prompt's "See others" resolves to the ``"list"``
    sentinel; we drop the recommendation to force the list layout and re-ask.
    Returns a chosen ``Candidate``, ``"asis"``, or ``None`` (skip).
    """
    while True:
        result = await _queue_confirmation(
            bot, tg_id, file_id, filename, tag_result, file_path, states, report
        )
        if result != "list":
            return result
        tag_result.recommendation = Recommendation.none


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


_FIRST_PAGE_SIZE = 6  # page 1 reserves row 1 for the lone top pick, losing 3 slots
_PAGE_SIZE = 9  # every later page is a full 3x3 grid


def _row_sizes(n: int, is_first_page: bool) -> list[int]:
    """Row sizes (top to bottom) for laying out ``n`` candidates in <=3 rows.

    Rows are non-decreasing top-to-bottom with adjacent rows differing by at
    most 1 button, so a page never reads as lopsided (a near-empty row next to
    a full one). On the first page, row 1 is hard-locked to the lone top
    pick -- highlighting it -- and the rest of ``n`` is balanced evenly across
    the remaining two rows. Every other page has no locked row: all of ``n``
    balances evenly across up to 3 rows. Empty rows are omitted.
    """
    if n <= 0:
        return []
    if is_first_page:
        rows = [1]
        remaining = n - 1
        if remaining > 0:
            row2 = remaining // 2
            row3 = remaining - row2
            if row2 > 0:
                rows.append(row2)
            rows.append(row3)
        return rows
    rows_count = min(3, n)
    base, rem = divmod(n, rows_count)
    threshold = rows_count - rem
    return [base + (1 if i >= threshold else 0) for i in range(rows_count)]


def _page_bounds(total: int, page: int) -> tuple[int, int]:
    """Start/end candidate indices (into the full list) for a 0-based page."""
    if page == 0:
        return 0, min(total, _FIRST_PAGE_SIZE)
    start = _FIRST_PAGE_SIZE + (page - 1) * _PAGE_SIZE
    return start, min(total, start + _PAGE_SIZE)


def _total_pages(total: int) -> int:
    if total <= _FIRST_PAGE_SIZE:
        return 1
    return 1 + -(-(total - _FIRST_PAGE_SIZE) // _PAGE_SIZE)


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
    # Flipped by the "Show all results" button on a fingerprint prompt: the active
    # candidate list (rendered and resolved against) switches from the fingerprint
    # set (tag_result.candidates) to the full text-search net
    # (tag_result.search_candidates). One-way for the life of the prompt.
    showing_all: bool = False


def _active_candidates(req: "_ConfirmationRequest") -> list:
    """The candidate list currently in view: the full text net once "Show all
    results" has been clicked, otherwise the primary (fingerprint or text) set."""
    tr = req.tag_result
    return tr.search_candidates if req.showing_all else tr.candidates


def _render_list_page(req: "_ConfirmationRequest") -> tuple[str, InlineKeyboardMarkup]:
    """Render one page of the active candidate list, with paging controls.

    Page 1 holds ``_FIRST_PAGE_SIZE`` candidates, every later page holds
    ``_PAGE_SIZE`` (see ``_page_bounds``/``_total_pages``); row layout within a
    page comes from ``_row_sizes``. Each candidate's button carries its ``.index``
    (its position in the active list, which the callback resolves against) and
    shows its combined confidence percent. When there is more than one page a nav
    row (``<<`` / ``page/total`` / ``>>``) is added; ``req.page`` is clamped here
    so the caller can bump it freely. Mutates ``req.page`` to the clamped value. A
    fingerprint prompt also gets a "Show all results" row that reveals the full
    text-search net (``tag_result.search_candidates``).
    """
    cands = _active_candidates(req)
    pages = _total_pages(len(cands))
    req.page = max(0, min(req.page, pages - 1))
    start, end = _page_bounds(len(cands), req.page)
    fname = html.escape(req.filename)

    text = f"❓ Matches for:\n<i>{fname}</i>\n\n"
    page_cands = cands[start:end]
    sizes = _row_sizes(len(page_cands), req.page == 0)

    rows: list[list[InlineKeyboardButton]] = []
    it = iter(page_cands)
    for size in sizes:
        row: list[InlineKeyboardButton] = []
        for c in (next(it) for _ in range(size)):
            n = c.index + 1
            artist = html.escape(c.artist)
            title = html.escape(c.title)
            text += f"{n}. {artist} — {title}{_candidate_detail(c)}\n"
            row.append(InlineKeyboardButton(
                text=f"#{n} ({c.confidence * 100:.0f}%)",
                callback_data=f"conf{_CB_SEP}{c.index}",
            ))
        rows.append(row)

    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="<<", callback_data=f"conf{_CB_SEP}prev"),
            InlineKeyboardButton(text=f"{req.page + 1}/{pages}", callback_data=f"conf{_CB_SEP}noop"),
            InlineKeyboardButton(text=">>", callback_data=f"conf{_CB_SEP}next"),
        ])
    # On a fingerprint prompt, offer the discarded text-search net as a fallback.
    if not req.showing_all and req.tag_result.fingerprinted and req.tag_result.search_candidates:
        rows.append([
            InlineKeyboardButton(text="Show all results", callback_data=f"conf{_CB_SEP}showall"),
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
    elif tag.recommendation >= Recommendation.strong and not tag.fingerprinted:
        c = tag.candidates[0]
        artist = html.escape(c.artist)
        title = html.escape(c.title)
        text = (
            f"❓ Import <i>{fname}</i>?\n\n"
            f"Match: <i>{artist} — {title}</i>{_candidate_detail(c)}\n"
            f"Confidence: <b>{c.confidence * 100:.0f}%</b>"
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
        async with _download_semaphore:
            await bot.download(file_id, destination=file_path)
        log.info("received %r (tg_id=%s, %d bytes)", filename, tg_id, file_path.stat().st_size)

        states[file_id].status = FileStatus.TAGGING
        await report()
        tag_result = await get_candidates(file_path)
        log.info(
            "tagged %r: %s, %d candidate(s)", filename,
            "fingerprint" if tag_result.fingerprinted
            else "search" if tag_result.candidates else "no match",
            len(tag_result.candidates),
        )

        async with get_session() as session:
            result = await session.exec(select(User).where(User.tg_id == tg_id))
            db_user = result.first()
        user_settings = json.loads(db_user.settings) if db_user else {}
        mode = ConfirmationMode(user_settings.get("confirmation", "auto"))
        enrich = user_settings.get("enrich", True)

        cands = tag_result.candidates
        # chosen is a Candidate (import it), "asis", or None (skip).
        chosen: "Candidate | str | None" = None

        if tag_result.fingerprinted:
            top = cands[0].confidence if cands else 0.0
            passes = top >= _FP_CONFIDENCE_THRESHOLD
            if mode == ConfirmationMode.OFF:
                # Trust a confident fingerprint outright; below the bar fall back to
                # beets' text search rather than picking a weak audio match.
                chosen = cands[0] if passes else _off_search_fallback(tag_result)
            elif mode == ConfirmationMode.AUTO and passes and len(cands) == 1:
                chosen = cands[0]  # lone, confident fingerprint: import silently
            else:
                # AUTO below the bar or with twins, and ON: prompt with the
                # fingerprint set (a "Show all results" button reveals the text net).
                chosen = await _confirm_looping(
                    bot, tg_id, file_id, filename, tag_result, file_path, states, report
                )
        else:
            # Text-search path, unchanged in spirit: OFF imports #0 when beets
            # recommends strongly (else as-is); AUTO auto-imports a unique top pick
            # but prompts among same-title twins; ON always prompts.
            is_high = tag_result.recommendation >= Recommendation.strong
            if mode == ConfirmationMode.OFF:
                chosen = cands[0] if (is_high and cands) else "asis"
            elif mode == ConfirmationMode.AUTO and is_high and cands:
                group = _top_twins(cands)
                if len(group) == 1:
                    chosen = group[0]
                else:
                    chosen = await _confirm_looping(
                        bot, tg_id, file_id, filename,
                        TagResult(candidates=_reindexed(group), recommendation=Recommendation.none),
                        file_path, states, report,
                    )
            else:
                chosen = await _confirm_looping(
                    bot, tg_id, file_id, filename, tag_result, file_path, states, report
                )

        if chosen is None:
            states[file_id] = FileState(filename, FileStatus.SKIPPED)
            log.info("skipped %r (tg_id=%s): no selection", filename, tg_id)
            await report()
            return

        is_asis = chosen == "asis"
        if is_asis:
            staged, dest = await stage_as_is(file_path, db_user.username)
            mb_id = None
        else:
            candidate = chosen
            staged, dest = await apply_and_stage(file_path, candidate, enrich)
            mb_id = candidate.mb_track_id

        from beets import mediafile as mf_lib
        rgid = None
        try:
            mf = mf_lib.MediaFile(str(staged))
            new_bitrate = mf.bitrate // 1000 if mf.bitrate else 0
            new_format = staged.suffix.lstrip(".")
            # Set only when enrichment resolved a release group; drives the folder
            # cover. None for as-is/unenriched imports -> no folder cover.
            rgid = (mf.mb_releasegroupid or None) if not is_asis else None
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
                # Overwrite the pooled copy only on a strict quality upgrade, or to
                # heal a dangling row whose pool file went missing. An equal-quality
                # upload keeps the existing copy and drops the staged duplicate -- a
                # same-quality re-upload no longer overwrites (so it no longer
                # refreshes tags); a strictly worse one likewise loses.
                if is_upgrade or not old_pool.exists():
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

        # Folder cover art, outside the pool lock (network fetch; keeps the
        # critical section tight). Best-effort: never fails an otherwise-good
        # import. Idempotent -- safe on duplicates, which self-heal a missing link.
        if rgid:
            try:
                await _ensure_album_cover(rgid, pool_file)
            except Exception:
                log.debug("cover art linking failed for %s", pool_rel(pool_file))

        if already_owned:
            states[file_id] = FileState(filename, FileStatus.DUPLICATE)
            log.info("duplicate %r (user=%s): already owns %s",
                     filename, db_user.username, pool_rel(pool_file))
        else:
            states[file_id] = FileState(filename, FileStatus.IMPORTED, note)
            action = "as-is" if is_asis else "upgraded" if note else "imported"
            log.info("%s %r (user=%s) -> %s",
                     action, filename, db_user.username, pool_rel(pool_file))
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
) -> "Candidate | str | None":
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
    loop = get_running_loop()
    last_sent = 0.0  # monotonic time the previous prompt was sent (0 => none yet)
    try:
        while q and not q.empty():
            req: _ConfirmationRequest = await q.get()
            if not req.future.done():
                if last_sent:
                    # Pace the next send against how long the user spent on the
                    # last prompt: a quick answer waits out the rest of the
                    # throttle window, a slow one only the floor hold.
                    wait = max(_PROMPT_MIN_HOLD_SECONDS, _PROMPT_THROTTLE_SECONDS - (loop.time() - last_sent))
                    await asyncio.sleep(wait)
                _active_confirmations[tg_id] = req
                try:
                    await _ask_confirmation(bot, tg_id, req)
                    last_sent = loop.time()
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

    if choice in ("prev", "next", "noop", "showall"):
        # In-place re-render; the pending choice is untouched. Paging bumps the
        # page; "Show all results" swaps the active list to the full text net and
        # resets to page 1.
        if choice != "noop":
            if choice == "showall":
                req.showing_all = True
                req.page = 0
            else:
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
        # A candidate pick: the button carries the candidate's position in the
        # currently-active list (fingerprint set, or full text net after "Show
        # all"), so resolve it here and hand back the Candidate itself.
        active = _active_candidates(req)
        try:
            idx = int(choice)
        except ValueError:
            await safe_answer(callback, "Invalid choice")
            return
        if not 0 <= idx < len(active):
            await safe_answer(callback, "Invalid choice")
            return
        req.future.set_result(active[idx])

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
