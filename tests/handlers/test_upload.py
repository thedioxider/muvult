import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.handlers import upload
from src.handlers.upload import _format_status_message, FileState, _find_existing_track, _top_twins
from src.models import Candidate, FileStatus
from src.db import init_db, get_session, Track


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


@pytest.mark.asyncio
async def test_find_existing_by_pool_path_for_as_is(tmp_path):
    # As-is imports carry no recording id and must match on pool_path alone.
    await init_db(str(tmp_path / "db"))
    async with get_session() as s:
        s.add(Track(pool_path="users/alice/song.mp3", musicbrainz_id=None, format="mp3", bitrate=192))
        await s.commit()
        found = await _find_existing_track(s, None, "users/alice/song.mp3")
        assert found is not None and found.pool_path == "users/alice/song.mp3"


@pytest.mark.asyncio
async def test_flush_group_keys_states_by_file_id(monkeypatch):
    # Two files sharing a name (common in an album) must not collide: states is
    # keyed by the unique file_id, with the filename kept only for display.
    gid = "grp"
    upload._group_pending[gid] = [("song.mp3", "fid-A"), ("song.mp3", "fid-B")]
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=10))
    upload._group_meta[gid] = (bot, 1, 1, 99)

    captured: dict = {}

    async def fake_process(**kwargs):
        captured["states"] = kwargs["states"]

    monkeypatch.setattr(upload, "_process_file", fake_process)
    await upload._flush_group(gid)
    await asyncio.sleep(0.05)  # let the spawned per-file tasks run

    assert set(captured["states"].keys()) == {"fid-A", "fid-B"}
    assert [fs.original_name for fs in captured["states"].values()] == ["song.mp3", "song.mp3"]
