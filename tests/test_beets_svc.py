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
    select_release,
    earliest_official_year,
)


def _rel(rid, title, primary, secondary, status, date, country, artist="half•alive"):
    """Build a beets-normalized release dict (hyphenless keys) for selection tests."""
    return {
        "id": rid,
        "title": title,
        "status": status,
        "date": date,
        "country": country,
        "release_group": {"primary_type": primary, "secondary_types": secondary},
        "artist_credit": [{"name": artist}],
    }


# Real release lists gathered from MusicBrainz during design.
STILL_FEEL_RELEASES = [
    _rel("r-sf-single", "still feel.", "Single", [], "Official", "2018-08-03", "XW"),
    _rel("r-star7", "*7", "Album", ["Compilation"], "Official", "2019-05-17", "XW"),
    _rel("r-runaway", "RUNAWAY", "Single", [], "Official", "2019-06-13", "XW"),
    _rel("r-puregold", "Pure Gold", "Single", [], "Official", "2019-07-19", "XW"),
    _rel("r-okok", "ok ok?", "Single", [], "Official", "2019-07-31", "XW"),
    _rel("r-nny-digital", "Now, Not Yet", "Album", [], "Official", "2019-08-09", "XW"),
    _rel("r-nny-vinyl", "Now, Not Yet", "Album", [], "Official", "2019-09-06", None),
    _rel("r-promo", "Promo Only", "Album", ["Compilation"], "Promotion", "2019", "US",
         artist="Various Artists"),
]

BABA_RELEASES = [
    _rel("r-wn-vinyl95", "Who's Next", "Album", [], "Official", "1995", "US", artist="The Who"),
    _rel("r-wn-deluxe03", "Who's Next", "Album", [], "Official", "2003-05-19", "XE", artist="The Who"),
    _rel("r-wn-2025", "Who's Next", "Album", [], "Official", "2025-01-17", None, artist="The Who"),
]

SKIP_RELEASES = [
    _rel("r-skip-xw", "Skiptracing", "Album", [], "Official", "2016-08-26", "XW", artist="Mild High Club"),
    _rel("r-skip-us", "Skiptracing", "Album", [], "Official", "2016-08-26", "US", artist="Mild High Club"),
]


def test_select_release_prefers_studio_album_over_singles_and_comps():
    assert select_release(STILL_FEEL_RELEASES, "half•alive")["id"] == "r-nny-digital"


def test_select_release_earliest_defined_country_breaks_edition_tie():
    assert select_release(BABA_RELEASES, "The Who")["id"] == "r-wn-vinyl95"


def test_select_release_worldwide_beats_defined_country():
    assert select_release(SKIP_RELEASES, "Mild High Club")["id"] == "r-skip-xw"


def test_select_release_official_outranks_better_typed_nonofficial():
    promo_album = _rel("r-promo-alb", "X", "Album", [], "Promotion", "2000", "XW")
    official_single = _rel("r-off-sgl", "Y", "Single", [], "Official", "2001", "XW")
    assert select_release([promo_album, official_single])["id"] == "r-off-sgl"


def test_select_release_matching_artist_outranks_various_artists():
    va_album = _rel("r-va", "Big Hits", "Album", [], "Official", "2000", "XW",
                    artist="Various Artists")
    own_single = _rel("r-own", "song", "Single", [], "Official", "2001", "XW",
                      artist="half•alive")
    assert select_release([va_album, own_single], "half•alive")["id"] == "r-own"


def test_select_release_orders_album_over_single_within_secondary_typed():
    comp_single = _rel("r-cs", "x", "Single", ["Compilation"], "Official", "2000", "XW")
    comp_album = _rel("r-ca", "x", "Album", ["Compilation"], "Official", "2000", "XW")
    assert select_release([comp_single, comp_album])["id"] == "r-ca"


def test_select_release_clean_single_beats_compilation_album():
    comp_album = _rel("r-ca", "x", "Album", ["Compilation"], "Official", "2000", "XW")
    clean_single = _rel("r-cs", "y", "Single", [], "Official", "2001", "XW")
    assert select_release([comp_album, clean_single])["id"] == "r-cs"


def test_select_release_empty_returns_none():
    assert select_release([]) is None


def test_earliest_official_year_uses_single_predating_album():
    assert earliest_official_year(STILL_FEEL_RELEASES) == 2018


def test_earliest_official_year_baba():
    assert earliest_official_year(BABA_RELEASES) == 1995


def test_earliest_official_year_ignores_non_official():
    rels = [
        _rel("a", "x", "Album", [], "Bootleg", "1990", "XW"),
        _rel("b", "x", "Album", [], "Official", "1995", "XW"),
    ]
    assert earliest_official_year(rels) == 1995


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


def test_apply_and_move_sync_writes_enriched_metadata(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")

    proposal, raw_match = _make_proposal()
    moved_path = tmp_path / ".pool" / "half·alive" / "Now, Not Yet" / "05 - still feel..mp3"
    raw_match.item.path = str(moved_path)

    candidate = Candidate(
        index=0, artist="half·alive", title="still feel.", album="",
        year=None, mb_track_id="rec-1", distance=0.05, _match=raw_match,
    )
    enriched = {"album": "Now, Not Yet", "track": 5, "disc": 1, "year": 2018}

    with (
        patch("src.beets_svc._lib", MagicMock()),
        patch("src.beets_svc._enrich_from_release", return_value=enriched),
    ):
        dest = _apply_and_move_sync(audio, candidate)

    raw_match.item.update.assert_called_once_with(enriched)
    raw_match.apply_metadata.assert_not_called()
    raw_match.item.write.assert_called_once_with(path=str(audio))
    raw_match.item.add.assert_called_once()
    raw_match.item.move.assert_called_once_with(store=True)
    assert dest == moved_path


def test_apply_and_move_sync_falls_back_to_recording_level(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")

    proposal, raw_match = _make_proposal()
    moved_path = tmp_path / ".pool" / "unmatched.mp3"
    raw_match.item.path = str(moved_path)

    candidate = Candidate(
        index=0, artist="A", title="B", album="", year=None,
        mb_track_id="rec-1", distance=0.05, _match=raw_match,
    )

    with (
        patch("src.beets_svc._lib", MagicMock()),
        patch("src.beets_svc._enrich_from_release", return_value=None),
    ):
        _apply_and_move_sync(audio, candidate)

    raw_match.apply_metadata.assert_called_once()
    raw_match.item.update.assert_not_called()
    raw_match.item.write.assert_called_once_with(path=str(audio))


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
