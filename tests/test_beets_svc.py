from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
from src.models import Candidate, TagResult
from src.beets_svc import (
    _dedup_matches,
    _get_candidates_sync,
    _apply_and_stage_sync,
    _stage_as_is_sync,
    _nearest_release_length,
    _album_for_id_cached,
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


def _mk_match(artist, title, length, isrc, mbid, distance, disambig=None):
    return SimpleNamespace(
        info=SimpleNamespace(
            artist=artist, title=title, length=length, isrc=isrc,
            track_id=mbid, trackdisambig=disambig,
        ),
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
    # Different titles, or same title with a distinguishing disambiguation, stay
    # separate; the two "Song One" takes are told apart by their disambig.
    matches = [
        _mk_match("A", "Song One", 100.0, None, "1", 0.01, disambig="studio"),
        _mk_match("A", "Song Two", 200.0, None, "2", 0.02),
        _mk_match("A", "Song One", 240.0, None, "3", 0.03, disambig="live"),
    ]
    assert [m.info.track_id for m in _dedup_matches(matches)] == ["1", "2", "3"]


def test_dedup_collapses_same_title_artist_regardless_of_length():
    # No disambiguation: same (artist, title) is one logical track even when the
    # recording lengths differ -- lengths are corrected before scoring, so they no
    # longer partition the group. Lower distance wins.
    matches = [
        _mk_match("A", "T", 100.0, None, "1", 0.01),
        _mk_match("A", "T", 240.0, None, "2", 0.02),
    ]
    assert [m.info.track_id for m in _dedup_matches(matches)] == ["1"]


def _rec_with_release_tracks(rec_len_ms, *release_track_lens_ms):
    """A search-shaped recording dict with per-release track lengths."""
    return {
        "id": "rec",
        "length": rec_len_ms,
        "releases": [
            {"id": f"r{i}", "media": [{"track": [{"length": tl}]}]}
            for i, tl in enumerate(release_track_lens_ms)
        ],
    }


def test_nearest_release_length_picks_track_closest_to_file():
    # The real arrow case: recording length 221.0s, but the album track length is
    # 222.706s; the file is 222.707s. The nearest per-release track length wins.
    rec = _rec_with_release_tracks(221000, 223000, 222706)
    assert _nearest_release_length(rec, 222.707) == pytest.approx(222.706)


def test_nearest_release_length_none_without_track_lengths():
    rec = {"id": "rec", "length": 221000, "releases": [{"id": "r", "media": []}]}
    assert _nearest_release_length(rec, 222.7) is None


def test_nearest_release_length_none_without_file_length():
    rec = _rec_with_release_tracks(221000, 222706)
    assert _nearest_release_length(rec, None) is None


def test_select_release_length_breaks_edition_tie():
    # Two editions tie on status/artist/type; the one whose track length matches
    # the file (~222.7s) wins, ahead of country/date/id.
    a = _rel("r-a", "Album", "Album", [], "Official", "2019", "XW")
    b = _rel("r-b", "Album", "Album", [], "Official", "2019", "XW")
    a["media"] = [{"track": [{"length": 200000}]}]
    b["media"] = [{"track": [{"length": 222706}]}]
    assert select_release([a, b], "half•alive", 222707)["id"] == "r-b"


def test_album_for_id_cached_hits_mb_once():
    _album_for_id_cached.cache_clear()
    album = MagicMock()
    plugin = MagicMock()
    plugin.album_for_id.return_value = album
    with patch("src.beets_svc._mb_plugin", return_value=plugin):
        first = _album_for_id_cached("rel-1")
        second = _album_for_id_cached("rel-1")
    assert first is album and second is album
    plugin.album_for_id.assert_called_once_with("rel-1")


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


def _mk_apply_mocks(dest, *, rec=None, track_data=None, artist="half·alive"):
    """Wire the mocks the new by-id import path needs.

    Returns (item, plugin, rec) with `src.beets_svc.Item.from_path` and the MB
    plugin's `track_info` prepared. `rec` defaults to a recording carrying one
    release so enrichment is attempted.
    """
    item = MagicMock()
    item.destination.return_value = str(dest)
    item.length = 222.7
    rec = rec if rec is not None else {"id": "rec-1", "length": 200000, "releases": [{"id": "r"}]}
    ti = MagicMock()
    ti.item_data = track_data if track_data is not None else {"title": "t", "artist": artist}
    ti.artist = artist
    plugin = MagicMock()
    plugin.track_info.return_value = ti
    return item, plugin, rec


def test_apply_and_stage_sync_writes_recording_then_enriched(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")
    dest_path = tmp_path / ".pool" / "half·alive" / "Now, Not Yet" / "05 - still feel..mp3"
    item, plugin, rec = _mk_apply_mocks(dest_path, track_data={"title": "still feel."})

    candidate = Candidate(
        index=0, artist="half·alive", title="still feel.", album="",
        year=None, mb_track_id="rec-1", distance=0.05, _match=MagicMock(),
    )
    enriched = {"album": "Now, Not Yet", "track": 5, "disc": 1, "year": 2018}

    with (
        patch("src.beets_svc.Item.from_path", return_value=item),
        patch("src.beets_svc._get_recording_full", return_value=rec) as lookup,
        patch("src.beets_svc._mb_plugin", return_value=plugin),
        patch("src.beets_svc._enrich_from_release", return_value=enriched),
        patch("src.beets_svc._lib", MagicMock()),
    ):
        staged, dest = _apply_and_stage_sync(audio, candidate)

    lookup.assert_called_once_with("rec-1")  # exactly one recording lookup on import
    item.update.assert_any_call({"title": "still feel."})  # authoritative recording tags
    item.update.assert_any_call(enriched)  # enrichment layered on top
    item.write.assert_called_once_with(path=str(audio))
    item.add.assert_called_once()
    item.move.assert_not_called()  # placement is deferred to the caller
    assert staged == audio  # tagged in place, still in staging
    assert dest == dest_path


def test_apply_and_stage_sync_never_touches_the_pool(tmp_path):
    # A re-upload whose tags map onto an existing canonical pool file must not
    # touch the pool at all: staging returns where the file *would* go, leaving
    # both the canonical file and the pool untouched until the caller promotes.
    pool = tmp_path / ".pool" / "half·alive" / "Now, Not Yet"
    pool.mkdir(parents=True)
    canonical = pool / "05 - still feel..mp3"
    canonical.write_bytes(b"ORIGINAL POOL FILE")

    audio = tmp_path / "staging" / "song.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"NEW UPLOAD")

    item, plugin, rec = _mk_apply_mocks(canonical)
    candidate = Candidate(
        index=0, artist="half·alive", title="still feel.", album="",
        year=None, mb_track_id="rec-1", distance=0.05, _match=MagicMock(),
    )

    with (
        patch("src.beets_svc.Item.from_path", return_value=item),
        patch("src.beets_svc._get_recording_full", return_value=rec),
        patch("src.beets_svc._mb_plugin", return_value=plugin),
        patch("src.beets_svc._enrich_from_release", return_value={"album": "Now, Not Yet"}),
        patch("src.beets_svc._lib", MagicMock()),
    ):
        staged, dest = _apply_and_stage_sync(audio, candidate)

    assert dest == canonical
    assert staged == audio  # stays in staging, not moved into the pool
    assert canonical.read_bytes() == b"ORIGINAL POOL FILE"  # untouched
    assert list(pool.iterdir()) == [canonical]  # no temp file created in the pool


def test_apply_and_stage_sync_recording_level_when_enrich_returns_none(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")
    dest = tmp_path / ".pool" / "unmatched.mp3"
    item, plugin, rec = _mk_apply_mocks(dest, track_data={"title": "B", "artist": "A"})

    candidate = Candidate(
        index=0, artist="A", title="B", album="", year=None,
        mb_track_id="rec-1", distance=0.05, _match=MagicMock(),
    )

    with (
        patch("src.beets_svc.Item.from_path", return_value=item),
        patch("src.beets_svc._get_recording_full", return_value=rec),
        patch("src.beets_svc._mb_plugin", return_value=plugin),
        patch("src.beets_svc._enrich_from_release", return_value=None),
        patch("src.beets_svc._lib", MagicMock()),
    ):
        _apply_and_stage_sync(audio, candidate)

    item.update.assert_called_once_with({"title": "B", "artist": "A"})  # recording only
    item.write.assert_called_once_with(path=str(audio))


def test_apply_and_stage_sync_falls_back_when_lookup_fails(tmp_path):
    # If the import lookup fails, apply the search-level candidate metadata.
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")
    item = MagicMock()
    item.destination.return_value = str(tmp_path / ".pool" / "x.mp3")
    item.length = 222.7

    match = MagicMock()
    match.info.item_data = {"title": "B", "artist": "A"}
    match.info.artist = "A"
    candidate = Candidate(
        index=0, artist="A", title="B", album="", year=None,
        mb_track_id="rec-1", distance=0.05, _match=match,
    )

    with (
        patch("src.beets_svc.Item.from_path", return_value=item),
        patch("src.beets_svc._get_recording_full", return_value=None),
        patch("src.beets_svc._enrich_from_release") as enrich,
        patch("src.beets_svc._lib", MagicMock()),
    ):
        _apply_and_stage_sync(audio, candidate)

    item.update.assert_called_once_with({"title": "B", "artist": "A"})
    enrich.assert_not_called()  # no releases to enrich from


def test_apply_and_stage_sync_skips_enrichment_when_disabled(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")
    dest = tmp_path / ".pool" / "half·alive" / "05 - still feel..mp3"
    item, plugin, rec = _mk_apply_mocks(dest, track_data={"title": "still feel."})

    candidate = Candidate(
        index=0, artist="half·alive", title="still feel.", album="",
        year=None, mb_track_id="rec-1", distance=0.05, _match=MagicMock(),
    )

    with (
        patch("src.beets_svc.Item.from_path", return_value=item),
        patch("src.beets_svc._get_recording_full", return_value=rec),
        patch("src.beets_svc._mb_plugin", return_value=plugin),
        patch("src.beets_svc._enrich_from_release") as enrich,
        patch("src.beets_svc._lib", MagicMock()),
    ):
        _apply_and_stage_sync(audio, candidate, enrich=False)

    enrich.assert_not_called()
    item.update.assert_called_once_with({"title": "still feel."})


def test_stage_as_is_sync(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")

    lib_dir = tmp_path / ".pool"
    mock_lib = MagicMock()
    mock_lib.directory = str(lib_dir).encode()

    with patch("src.beets_svc._lib", mock_lib):
        staged, dest = _stage_as_is_sync(audio, "alice")

    expected = lib_dir / "users" / "alice" / "song.mp3"
    assert dest == expected
    assert staged == audio  # not moved; still in staging until promoted
    assert staged.read_bytes() == b"audio data"
    assert audio.exists()


def test_stage_as_is_sync_never_touches_the_pool(tmp_path):
    lib_dir = tmp_path / ".pool"
    canonical = lib_dir / "users" / "alice" / "song.mp3"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"ORIGINAL")

    audio = tmp_path / "staging" / "song.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"NEW")

    mock_lib = MagicMock()
    mock_lib.directory = str(lib_dir).encode()

    with patch("src.beets_svc._lib", mock_lib):
        staged, dest = _stage_as_is_sync(audio, "alice")

    assert dest == canonical
    assert staged == audio
    assert canonical.read_bytes() == b"ORIGINAL"  # untouched


def test_stage_as_is_sync_rejects_path_traversal(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")

    mock_lib = MagicMock()
    mock_lib.directory = str(tmp_path / ".pool").encode()

    with patch("src.beets_svc._lib", mock_lib):
        with pytest.raises(ValueError, match="path traversal"):
            _stage_as_is_sync(audio, "../escape")
