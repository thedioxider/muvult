from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
from src.models import Candidate, TagResult
from src.beets_svc import (
    _dedup_matches,
    _get_candidates_sync,
    _apply_and_move_sync,
    _move_as_is_sync,
)


def _mk_match(artist, title, length, isrc, mbid, distance):
    return SimpleNamespace(
        info=SimpleNamespace(artist=artist, title=title, length=length, isrc=isrc, track_id=mbid),
        distance=SimpleNamespace(distance=distance),
    )


def test_dedup_keeps_isrc_on_distance_tie():
    # the real half-alive case: same song, two MB recordings, near-equal length,
    # only one carries the ISRC (the worldwide release)
    matches = [
        _mk_match("half·alive with Kimbra", "ice cold.", 177.0, None, "aaa", 0.02),
        _mk_match("half·alive with Kimbra", "ice cold.", 177.634, "USRC11901785", "bbb", 0.02),
    ]
    result = _dedup_matches(matches)
    assert [m.info.track_id for m in result] == ["bbb"]


def test_dedup_lower_distance_wins():
    matches = [
        _mk_match("A", "T", 100.0, None, "x", 0.05),
        _mk_match("A", "T", 100.0, None, "y", 0.01),
    ]
    assert [m.info.track_id for m in _dedup_matches(matches)] == ["y"]


def test_dedup_lowest_mbid_breaks_full_tie():
    matches = [
        _mk_match("A", "T", 100.0, None, "zzz", 0.03),
        _mk_match("A", "T", 100.0, None, "aaa", 0.03),
    ]
    assert [m.info.track_id for m in _dedup_matches(matches)] == ["aaa"]


def test_dedup_keeps_distinct_tracks_in_order():
    matches = [
        _mk_match("A", "Song One", 100.0, None, "1", 0.01),
        _mk_match("A", "Song Two", 200.0, None, "2", 0.02),
        _mk_match("A", "Song One", 240.0, None, "3", 0.03),  # same title, different length
    ]
    assert [m.info.track_id for m in _dedup_matches(matches)] == ["1", "2", "3"]


def test_dedup_sub_second_length_groups_together():
    # 177.0 and 177.9 both display as 2:57 (truncated), so they must collapse
    matches = [
        _mk_match("A", "T", 177.0, None, "a", 0.01),
        _mk_match("A", "T", 177.9, None, "b", 0.01),
    ]
    assert len(_dedup_matches(matches)) == 1


def _make_proposal(distance=0.05, rec=3):
    info = MagicMock()
    info.artist = "Radiohead"
    info.title = "Creep"
    info.album = "Pablo Honey"
    info.year = 1993
    info.track_id = "mb-track-abc"

    match = MagicMock()
    match.distance.distance = distance
    match.info = info
    match.item = MagicMock()

    proposal = MagicMock()
    proposal.candidates = [match]
    proposal.recommendation = rec
    return proposal, match


def test_get_candidates_sync_maps_fields(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")

    proposal, _ = _make_proposal()

    with (
        patch("src.beets_svc.Item.from_path", return_value=MagicMock()),
        patch("src.beets_svc.tag_item", return_value=proposal),
    ):
        result = _get_candidates_sync(audio)

    assert isinstance(result, TagResult)
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert c.artist == "Radiohead"
    assert c.title == "Creep"
    assert c.mb_track_id == "mb-track-abc"
    assert c.distance == pytest.approx(0.05)
    assert result.recommendation == 3


def test_apply_and_move_sync(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")

    proposal, raw_match = _make_proposal()
    moved_path = tmp_path / ".pool" / "Radiohead" / "Pablo Honey" / "01 - Creep.mp3"
    raw_match.item.path = str(moved_path)

    candidate = Candidate(
        index=0, artist="Radiohead", title="Creep", album="Pablo Honey",
        year=1993, mb_track_id="mb-track-abc", distance=0.05, _match=raw_match,
    )

    with patch("src.beets_svc._lib", MagicMock()):
        dest = _apply_and_move_sync(audio, candidate)

    raw_match.apply_metadata.assert_called_once()
    raw_match.item.write.assert_called_once_with(path=str(audio))
    raw_match.item.add.assert_called_once()
    raw_match.item.move.assert_called_once_with(store=True)
    assert dest == moved_path


def test_move_as_is_sync(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")

    lib_dir = tmp_path / ".pool"
    mock_lib = MagicMock()
    mock_lib.directory = str(lib_dir).encode()

    with patch("src.beets_svc._lib", mock_lib):
        dest = _move_as_is_sync(audio, "alice")

    expected = lib_dir / "users" / "alice" / "song.mp3"
    assert dest == expected
    assert dest.is_file()
    assert dest.read_bytes() == b"audio data"
    assert not audio.exists()  # moved, not copied


def test_move_as_is_sync_rejects_path_traversal(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")

    mock_lib = MagicMock()
    mock_lib.directory = str(tmp_path / ".pool").encode()

    with patch("src.beets_svc._lib", mock_lib):
        with pytest.raises(ValueError, match="path traversal"):
            _move_as_is_sync(audio, "../escape")
