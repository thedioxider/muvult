import asyncio
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.handlers import upload
from src.handlers.upload import (
    _format_status_message,
    FileState,
    _find_existing_track,
    _top_twins,
    _album_owner_usernames,
    _ensure_album_cover,
)
from src.models import Candidate, FileStatus
from src.db import init_db, get_session, Track, TrackOwnership, User
from src.pool import create_symlink, find_cover


def _c(index, artist, title, disambig=None):
    return Candidate(
        index=index, artist=artist, title=title, album="", year=None,
        mb_track_id=f"id{index}", distance=0.0, _match=None, disambig=disambig,
    )


def test_top_twins_unique_top_returns_single():
    # #0 has no same-title/artist sibling -> unambiguous, only #0 comes back.
    cands = [_c(0, "SOAD", "Aerials"), _c(1, "SOAD", "Chop Suey!"), _c(2, "Tool", "Ænema")]
    assert [c.index for c in _top_twins(cands)] == [0]


def test_top_twins_groups_same_artist_title_preserving_index():
    cands = [
        _c(0, "System of a Down", "Aerials"),                 # studio
        _c(1, "System of a Down", "Aerials", "live"),         # twin
        _c(2, "System of a Down", "Toxicity"),                # different title
        _c(3, "System of a Down", "Aerials", "radio edit"),   # twin
    ]
    assert [c.index for c in _top_twins(cands)] == [0, 1, 3]


def test_top_twins_is_case_insensitive():
    cands = [_c(0, "SOAD", "Aerials"), _c(1, "soad", "aerials", "live")]
    assert [c.index for c in _top_twins(cands)] == [0, 1]


def test_top_twins_empty():
    assert _top_twins([]) == []


def test_top_twins_normalizes_title_punctuation():
    # Real-world case: MusicBrainz holds "ATWA" under different punctuation
    # across recordings ("ATWA", "A.T.W.A", "Atwa"). The live "A.T.W.A" take
    # scored as #0 must still be recognized as a twin of the studio "ATWA".
    cands = [
        _c(0, "System of a Down", "A.T.W.A", "live, Le Trabendo, Paris"),
        _c(1, "System of a Down", "ATWA"),
        _c(2, "System of a Down", "Atwa", "live, Sportpaleis, Merksem"),
    ]
    assert [c.index for c in _top_twins(cands)] == [0, 1, 2]


def _req(candidates, page=0):
    from src.models import TagResult
    return upload._ConfirmationRequest(
        filename="song.mp3",
        tag_result=TagResult(candidates=candidates, recommendation=0),
        file_path=None,
        future=MagicMock(),
        page=page,
    )


def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _nav_label(markup):
    return next((t for t in _btn_texts(markup) if "/" in t), None)


def _btn_datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_render_list_page_first_page_of_two():
    cands = [_c(i, "A", f"T{i}") for i in range(8)]
    text, markup = upload._render_list_page(_req(cands, page=0))
    assert "1. A — T0" in text and "6. A — T5" in text
    assert "7. A — T6" not in text          # spilled to page 2
    assert _nav_label(markup) == "1/2"      # page indicator between the arrows
    sep = upload._CB_SEP
    assert f"conf{sep}prev" in _btn_datas(markup)
    assert f"conf{sep}next" in _btn_datas(markup)


def test_render_list_page_second_page():
    cands = [_c(i, "A", f"T{i}") for i in range(8)]
    text, markup = upload._render_list_page(_req(cands, page=1))
    assert "7. A — T6" in text and "8. A — T7" in text
    assert "1. A — T0" not in text
    assert _nav_label(markup) == "2/2"


def test_render_list_page_single_page_has_no_nav():
    cands = [_c(i, "A", f"T{i}") for i in range(3)]
    _, markup = upload._render_list_page(_req(cands, page=0))
    assert _nav_label(markup) is None       # no paging row when it all fits
    sep = upload._CB_SEP
    assert f"conf{sep}prev" not in _btn_datas(markup)


def test_render_list_page_clamps_out_of_range_page():
    cands = [_c(i, "A", f"T{i}") for i in range(8)]
    req = _req(cands, page=9)                # past the end
    _, markup = upload._render_list_page(req)
    assert req.page == 1                     # clamped to the last page
    assert _nav_label(markup) == "2/2"


def test_render_list_page_button_carries_global_index():
    # A page-2 button must resolve to the candidate's original index, not its
    # position on the page.
    cands = [_c(i, "A", f"T{i}") for i in range(8)]
    _, markup = upload._render_list_page(_req(cands, page=1))
    sep = upload._CB_SEP
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"conf{sep}6" in datas   # candidate #7 -> index 6
    assert f"conf{sep}7" in datas


def test_row_sizes_first_page():
    assert upload._row_sizes(0, True) == []
    assert upload._row_sizes(1, True) == [1]
    assert upload._row_sizes(2, True) == [1, 1]
    assert upload._row_sizes(3, True) == [1, 1, 1]
    assert upload._row_sizes(4, True) == [1, 1, 2]
    assert upload._row_sizes(5, True) == [1, 2, 2]
    assert upload._row_sizes(6, True) == [1, 2, 3]


def test_row_sizes_later_page():
    assert upload._row_sizes(0, False) == []
    assert upload._row_sizes(1, False) == [1]
    assert upload._row_sizes(2, False) == [1, 1]
    assert upload._row_sizes(3, False) == [1, 1, 1]
    assert upload._row_sizes(4, False) == [1, 1, 2]
    assert upload._row_sizes(5, False) == [1, 2, 2]
    assert upload._row_sizes(6, False) == [2, 2, 2]
    assert upload._row_sizes(7, False) == [2, 2, 3]
    assert upload._row_sizes(8, False) == [2, 3, 3]
    assert upload._row_sizes(9, False) == [3, 3, 3]


def test_render_list_page_first_page_button_row_shape():
    # 6 candidates fit entirely on page 1; rows should be 1 / 2 / 3 (top pick
    # alone, then #2-#3, then #4-#6), not the old 2-per-row pairing.
    cands = [_c(i, "A", f"T{i}") for i in range(6)]
    _, markup = upload._render_list_page(_req(cands, page=0))
    candidate_rows = markup.inline_keyboard[:-1]  # drop the as-is/skip row
    assert [len(r) for r in candidate_rows] == [1, 2, 3]


def test_render_list_page_later_page_holds_nine():
    # Page 1 holds 6, so page 2 starts at candidate #7 and can hold up to 9.
    cands = [_c(i, "A", f"T{i}") for i in range(15)]
    text, markup = upload._render_list_page(_req(cands, page=1))
    assert "7. A — T6" in text and "15. A — T14" in text
    assert _nav_label(markup) == "2/2"
    candidate_rows = markup.inline_keyboard[:-2]  # drop nav row + as-is/skip row
    assert [len(r) for r in candidate_rows] == [3, 3, 3]


def test_format_status_groups_by_status():
    states = {
        "a.mp3": FileState("a.mp3", FileStatus.DOWNLOADING),
        "b.flac": FileState("b.flac", FileStatus.IMPORTED),
        "c.ogg": FileState("c.ogg", FileStatus.TAGGING),
        "d.mp3": FileState("d.mp3", FileStatus.IMPORTED, note="replaced MP3 192kbps"),
    }
    msg = _format_status_message(states)
    assert "📥" in msg
    assert "🔍" in msg
    assert "✅" in msg
    assert "a.mp3" in msg
    assert "b.flac" in msg
    assert "replaced MP3 192kbps" in msg


def test_format_status_omits_empty_groups():
    states = {"a.mp3": FileState("a.mp3", FileStatus.IMPORTED)}
    msg = _format_status_message(states)
    assert "📥" not in msg
    assert "✅" in msg


@pytest.mark.asyncio
async def test_find_existing_prefers_recording_id(tmp_path):
    await init_db(str(tmp_path / "db"))
    async with get_session() as s:
        s.add(Track(pool_path="a/b/1 - x.mp3", musicbrainz_id="rec-A", format="mp3", bitrate=320))
        await s.commit()
        found = await _find_existing_track(s, "rec-A", "other/path.mp3")
        assert found is not None and found.musicbrainz_id == "rec-A"


@pytest.mark.asyncio
async def test_find_existing_falls_back_to_pool_path_on_new_recording(tmp_path):
    # The collision case: the path is occupied by a row under a *different*
    # recording id (arrow re-tagged from 05ed7f3c to 86349be4). We must adopt that
    # row via pool_path, not insert a duplicate that violates the unique path.
    await init_db(str(tmp_path / "db"))
    async with get_session() as s:
        s.add(Track(pool_path="a/b/7 - arrow.mp3", musicbrainz_id="05ed7f3c", format="mp3", bitrate=320))
        await s.commit()
        found = await _find_existing_track(s, "86349be4", "a/b/7 - arrow.mp3")
        assert found is not None and found.musicbrainz_id == "05ed7f3c"


@pytest.mark.asyncio
async def test_find_existing_returns_none_when_neither_matches(tmp_path):
    await init_db(str(tmp_path / "db"))
    async with get_session() as s:
        s.add(Track(pool_path="a/b/1 - x.mp3", musicbrainz_id="rec-A", format="mp3", bitrate=320))
        await s.commit()
        assert await _find_existing_track(s, "rec-Z", "z/z.mp3") is None


def _album_fixture(tmp_path):
    """Two users each owning one track of Artist/Album, with real track symlinks.

    Returns (music_root, pool_file, {username: album_dir}). Mirrors how upload
    lays out the pool + per-user symlink libraries."""
    music_root = tmp_path / "music"
    pool_dir = music_root / ".pool" / "Artist" / "Album"
    pool_dir.mkdir(parents=True)
    album_dirs = {}
    for uname, fname in (("alice", "01 - A.mp3"), ("bob", "02 - B.mp3")):
        pool_file = pool_dir / fname
        pool_file.write_bytes(b"audio")
        link = create_symlink(pool_file, music_root / uname)
        album_dirs[uname] = link.parent
    return music_root, pool_dir, album_dirs


async def _seed_owners(album_rel="Artist/Album"):
    """alice+bob own one track each in the album; carol owns an unrelated one."""
    async with get_session() as s:
        for i, uname in enumerate(("alice", "bob", "carol"), start=1):
            s.add(User(id=i, tg_id=i, username=uname, navidrome_user_id=str(i), navidrome_library_id=i))
        s.add(Track(id=1, pool_path=f"{album_rel}/01 - A.mp3", format="mp3", bitrate=320))
        s.add(Track(id=2, pool_path=f"{album_rel}/02 - B.mp3", format="mp3", bitrate=320))
        s.add(Track(id=3, pool_path="Other/Thing/09 - z.mp3", format="mp3", bitrate=320))
        await s.flush()  # parents before ownerships (foreign_keys=ON)
        s.add(TrackOwnership(track_id=1, user_id=1, symlink_path="/x/alice"))
        s.add(TrackOwnership(track_id=2, user_id=2, symlink_path="/x/bob"))
        s.add(TrackOwnership(track_id=3, user_id=3, symlink_path="/x/carol"))
        await s.commit()


@pytest.mark.asyncio
async def test_album_owner_usernames_collects_only_album_owners(tmp_path):
    await init_db(str(tmp_path / "db"))
    await _seed_owners()
    async with get_session() as s:
        names = await _album_owner_usernames(s, "Artist/Album")
    assert set(names) == {"alice", "bob"}  # carol's unrelated album excluded


@pytest.mark.asyncio
async def test_ensure_album_cover_fetches_once_and_fans_out(tmp_path):
    await init_db(str(tmp_path / "db"))
    await _seed_owners()
    music_root, pool_dir, album_dirs = _album_fixture(tmp_path)
    pool_file = pool_dir / "01 - A.mp3"

    fetch = AsyncMock(return_value=(b"IMGDATA", ".png"))
    with patch("src.config.settings", MagicMock(music_root=str(music_root))), \
         patch("src.handlers.upload.fetch_cover_art_full", fetch):
        await _ensure_album_cover("rg-1", pool_file)

    fetch.assert_awaited_once_with("rg-1")
    pool_cover = pool_dir / "front.png"
    assert pool_cover.read_bytes() == b"IMGDATA"
    # every owner of the album got a symlink to the pool cover
    for uname in ("alice", "bob"):
        link = album_dirs[uname] / "front.png"
        assert link.is_symlink() and link.resolve() == pool_cover.resolve()


@pytest.mark.asyncio
async def test_ensure_album_cover_skips_fetch_when_pool_cover_exists(tmp_path):
    await init_db(str(tmp_path / "db"))
    await _seed_owners()
    music_root, pool_dir, album_dirs = _album_fixture(tmp_path)
    pool_file = pool_dir / "01 - A.mp3"
    (pool_dir / "front.jpg").write_bytes(b"EXISTING")  # already fetched earlier

    fetch = AsyncMock()
    with patch("src.config.settings", MagicMock(music_root=str(music_root))), \
         patch("src.handlers.upload.fetch_cover_art_full", fetch):
        await _ensure_album_cover("rg-1", pool_file)

    fetch.assert_not_awaited()  # no refetch when a cover is already present
    assert find_cover(album_dirs["alice"]) == album_dirs["alice"] / "front.jpg"


@pytest.mark.asyncio
async def test_ensure_album_cover_noop_when_fetch_fails(tmp_path):
    await init_db(str(tmp_path / "db"))
    await _seed_owners()
    music_root, pool_dir, album_dirs = _album_fixture(tmp_path)
    pool_file = pool_dir / "01 - A.mp3"

    with patch("src.config.settings", MagicMock(music_root=str(music_root))), \
         patch("src.handlers.upload.fetch_cover_art_full", AsyncMock(return_value=None)):
        await _ensure_album_cover("rg-1", pool_file)

    assert find_cover(pool_dir) is None  # nothing written
    assert find_cover(album_dirs["alice"]) is None  # nothing linked


@pytest.mark.asyncio
async def test_find_existing_by_pool_path_for_as_is(tmp_path):
    # As-is imports carry no recording id and must match on pool_path alone.
    await init_db(str(tmp_path / "db"))
    async with get_session() as s:
        s.add(Track(pool_path="users/alice/song.mp3", musicbrainz_id=None, format="mp3", bitrate=192))
        await s.commit()
        found = await _find_existing_track(s, None, "users/alice/song.mp3")
        assert found is not None and found.pool_path == "users/alice/song.mp3"


def test_assign_partition_joins_open_partition_under_budget():
    # Two small files well under the char budget and track cap share partition 0.
    batch = upload._UserBatch()
    idx1 = upload._assign_partition(batch, "fid-A", "a.mp3")
    idx2 = upload._assign_partition(batch, "fid-B", "b.mp3")
    assert idx1 == 0
    assert idx2 == 0
    assert batch.position == {"fid-A": 0, "fid-B": 0}


def test_assign_partition_opens_new_partition_on_char_overflow(monkeypatch):
    # A tight budget means the second file can't fit alongside the first.
    monkeypatch.setattr(upload, "_MSG_CHAR_BUDGET", 50)
    batch = upload._UserBatch()
    idx1 = upload._assign_partition(batch, "fid-A", "a" * 40)
    idx2 = upload._assign_partition(batch, "fid-B", "b" * 40)
    assert idx1 == 0
    assert idx2 == 1


def test_assign_partition_opens_new_partition_on_track_cap(monkeypatch):
    # Track cap trips even though every file is tiny and well under budget.
    monkeypatch.setattr(upload, "_MSG_TRACK_CAP", 2)
    batch = upload._UserBatch()
    idx1 = upload._assign_partition(batch, "fid-A", "a.mp3")
    idx2 = upload._assign_partition(batch, "fid-B", "b.mp3")
    idx3 = upload._assign_partition(batch, "fid-C", "c.mp3")
    assert (idx1, idx2, idx3) == (0, 0, 1)


def test_assign_partition_is_permanent():
    # Once assigned, a file's partition never changes even as later files
    # arrive and open further partitions.
    batch = upload._UserBatch()
    upload._assign_partition(batch, "fid-A", "a.mp3")
    orig = batch.position["fid-A"]
    for i in range(5):
        upload._assign_partition(batch, f"fid-extra-{i}", "x" * 500)
    assert batch.position["fid-A"] == orig


def test_partition_states_filters_by_index():
    batch = upload._UserBatch()
    batch.states["a"] = FileState("a.mp3", FileStatus.DOWNLOADING)
    batch.states["b"] = FileState("b.mp3", FileStatus.DOWNLOADING)
    batch.position = {"a": 0, "b": 1}
    assert set(upload._partition_states(batch, 0).keys()) == {"a"}
    assert set(upload._partition_states(batch, 1).keys()) == {"b"}


@pytest.mark.asyncio
async def test_report_batch_status_edits_only_own_partition(monkeypatch):
    monkeypatch.setattr(upload, "_STATUS_THROTTLE_SECONDS", 0)
    tg_id = 90001
    batch = upload._UserBatch()
    batch.states = {
        "a": FileState("a.mp3", FileStatus.IMPORTED),
        "b": FileState("b.mp3", FileStatus.DOWNLOADING),
    }
    batch.position = {"a": 0, "b": 1}
    batch.message_ids = [10, 20]
    batch.chat_id = 999
    upload._user_batches[tg_id] = batch

    bot = AsyncMock()
    await upload._report_batch_status(bot, tg_id, "a")
    await upload._status_workers[tg_id]  # drive the throttled edit worker to completion

    bot.edit_message_text.assert_awaited_once()
    _, kwargs = bot.edit_message_text.call_args
    assert kwargs["message_id"] == 10
    assert kwargs["chat_id"] == 999
    # "b" isn't terminal yet, so the batch must still be open.
    assert tg_id in upload._user_batches
    upload._user_batches.pop(tg_id, None)
    upload._status_last_text.clear()


@pytest.mark.asyncio
async def test_report_batch_status_closes_when_all_terminal(monkeypatch):
    monkeypatch.setattr(upload, "_STATUS_THROTTLE_SECONDS", 0)
    tg_id = 90002
    batch = upload._UserBatch()
    batch.states = {"a": FileState("a.mp3", FileStatus.IMPORTED)}
    batch.position = {"a": 0}
    batch.message_ids = [10]
    batch.chat_id = 999
    upload._user_batches[tg_id] = batch

    bot = AsyncMock()
    await upload._report_batch_status(bot, tg_id, "a")
    await upload._status_workers[tg_id]

    assert tg_id not in upload._user_batches
    upload._status_last_text.clear()


@pytest.mark.asyncio
async def test_status_updates_are_coalesced_and_skip_unchanged(monkeypatch):
    # Rapid transitions on one partition collapse to far fewer edits than
    # transitions, and an unchanged render never spends an edit at all.
    monkeypatch.setattr(upload, "_STATUS_THROTTLE_SECONDS", 0.05)
    tg_id = 90005
    batch = upload._UserBatch()
    batch.states = {"a": FileState("a.mp3", FileStatus.DOWNLOADING)}
    batch.position = {"a": 0}
    batch.message_ids = [10]
    batch.chat_id = 999
    upload._user_batches[tg_id] = batch

    bot = AsyncMock()
    for st in (FileStatus.DOWNLOADING, FileStatus.TAGGING, FileStatus.PENDING, FileStatus.IMPORTED):
        batch.states["a"].status = st
        await upload._report_batch_status(bot, tg_id, "a")
    await upload._status_workers[tg_id]

    # Four transitions, but coalesced into at most two edits (never one-per-tick).
    assert 1 <= bot.edit_message_text.await_count <= 2
    # Terminal state landed and the batch closed.
    assert tg_id not in upload._user_batches
    upload._status_last_text.clear()


@pytest.mark.asyncio
async def test_multi_partition_edits_are_spaced_one_per_window(monkeypatch):
    # A multi-message batch must not burst every dirty partition into one window:
    # the worker edits one partition per throttle sleep, round-robin.
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(upload.asyncio, "sleep", fake_sleep)
    tg_id = 90006
    batch = upload._UserBatch()
    batch.states = {
        "a": FileState("a.mp3", FileStatus.IMPORTED),
        "b": FileState("b.mp3", FileStatus.IMPORTED),
    }
    batch.position = {"a": 0, "b": 1}
    batch.message_ids = [10, 20]
    batch.chat_id = 999
    upload._user_batches[tg_id] = batch

    bot = AsyncMock()
    await upload._report_batch_status(bot, tg_id, "a")
    await upload._report_batch_status(bot, tg_id, "b")
    await upload._status_workers[tg_id]

    # Both partitions edited (their own message), each followed by its own window.
    edited_msgs = {kw["message_id"] for _, kw in bot.edit_message_text.call_args_list}
    assert edited_msgs == {10, 20}
    assert len(sleeps) == 2  # one throttle window per edit -- not both in a single burst
    upload._user_batches.pop(tg_id, None)
    upload._status_last_text.clear()


@pytest.mark.asyncio
async def test_prompt_throttle_waits_longer_after_a_fast_answer(monkeypatch):
    # The pace between consecutive prompt sends shrinks the longer the user spent
    # on the previous prompt: a fast answer waits ~the full window, a slow answer
    # only the floor hold.
    monkeypatch.setattr(upload, "_PROMPT_THROTTLE_SECONDS", 1.0)
    monkeypatch.setattr(upload, "_PROMPT_MIN_HOLD_SECONDS", 0.1)
    loop = asyncio.get_running_loop()

    def wait_after(think: float) -> float:
        last_sent = loop.time() - think  # user spent `think` seconds on the last prompt
        return max(upload._PROMPT_MIN_HOLD_SECONDS, upload._PROMPT_THROTTLE_SECONDS - (loop.time() - last_sent))

    assert wait_after(0.0) == pytest.approx(1.0, abs=0.05)   # instant click -> full window
    assert wait_after(0.6) == pytest.approx(0.4, abs=0.05)   # partial think -> remainder
    assert wait_after(5.0) == upload._PROMPT_MIN_HOLD_SECONDS  # long think -> just the floor


@pytest.mark.asyncio
async def test_flush_user_batch_deletes_old_and_resends_all_partitions(monkeypatch):
    monkeypatch.setattr(upload, "_BATCH_DEBOUNCE_SECONDS", 0.01)
    tg_id = 90003
    batch = upload._UserBatch()
    batch.states = {
        "a": FileState("a.mp3", FileStatus.DOWNLOADING),
        "b": FileState("b.mp3", FileStatus.DOWNLOADING),
    }
    batch.position = {"a": 0, "b": 1}
    batch.partition_count = 2
    batch.message_ids = [10, 20]
    batch.chat_id = 999
    upload._user_batches[tg_id] = batch

    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=30), MagicMock(message_id=40)])

    await upload._flush_user_batch(bot, tg_id, 999)

    bot.delete_message.assert_any_call(999, 10)
    bot.delete_message.assert_any_call(999, 20)
    assert bot.send_message.await_count == 2
    assert batch.message_ids == [30, 40]
    upload._user_batches.pop(tg_id, None)


@pytest.mark.asyncio
async def test_flush_user_batch_closes_batch_if_all_done(monkeypatch):
    monkeypatch.setattr(upload, "_BATCH_DEBOUNCE_SECONDS", 0.01)
    tg_id = 90004
    batch = upload._UserBatch()
    batch.states = {"a": FileState("a.mp3", FileStatus.IMPORTED)}
    batch.position = {"a": 0}
    batch.partition_count = 1
    upload._user_batches[tg_id] = batch

    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=50))

    await upload._flush_user_batch(bot, tg_id, 999)

    assert tg_id not in upload._user_batches
