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
    _fetch_cover_art_full,
    _image_ext,
    _nearest_release_length,
    _album_fields,
    _format_artist_credit,
    _track_numbering,
    _year_of,
    select_release,
)


_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16
_GIF = b"GIF89a" + b"\x00" * 32


def test_image_ext_by_magic_number():
    assert _image_ext(_JPEG) == ".jpg"
    assert _image_ext(_PNG) == ".png"
    assert _image_ext(_WEBP) == ".webp"
    assert _image_ext(_GIF) == ".gif"
    assert _image_ext(b"garbage") == ".jpg"  # unknown -> jpg


def test_fetch_cover_art_full_returns_bytes_and_ext():
    with patch("src.beets_svc.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = _PNG
        assert _fetch_cover_art_full("rg-png") == (_PNG, ".png")


def test_fetch_cover_art_full_swallows_errors():
    with patch("src.beets_svc.urlopen", side_effect=Exception("boom")):
        assert _fetch_cover_art_full("rg-fail") is None


def test_fetch_cover_art_full_none_on_empty_body():
    with patch("src.beets_svc.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = b""
        assert _fetch_cover_art_full("rg-empty") is None


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


def test_format_artist_credit_single():
    credit = [{"name": "Metallica", "joinphrase": "", "artist": {"id": "m1", "name": "Metallica"}}]
    assert _format_artist_credit(credit) == ("Metallica", ["m1"])


def test_format_artist_credit_feature_joins_with_joinphrase():
    credit = [
        {"name": "Alice", "joinphrase": " feat. ", "artist": {"id": "a"}},
        {"name": "Bob", "joinphrase": "", "artist": {"id": "b"}},
    ]
    assert _format_artist_credit(credit) == ("Alice feat. Bob", ["a", "b"])


def test_format_artist_credit_empty():
    assert _format_artist_credit(None) == ("", [])


def test_year_of():
    assert _year_of("1983-07-25") == 1983
    assert _year_of("1983") == 1983
    assert _year_of(None) is None
    assert _year_of("") is None


def test_track_numbering_reads_disc_and_totals():
    release = {"media": [{"position": 1, "track_count": 12, "tracks": [{"number": "5"}]}]}
    assert _track_numbering(release) == {"track": 5, "disc": 1, "tracktotal": 12}


def test_track_numbering_skips_nonnumeric_track():
    # A vinyl "A1" position isn't an int track number -- skip it rather than coerce.
    release = {"media": [{"position": 1, "tracks": [{"number": "A1"}]}]}
    assert _track_numbering(release) == {"disc": 1}


def test_track_numbering_empty_without_media():
    assert _track_numbering({"media": []}) == {}


def _rg_release(rid, *, rgid="rg1", title="Kill 'Em All", date="1983-07-25",
                primary="Album", secondary=None, artist="Metallica", artist_id="m1",
                track="4", disc=1, track_count=12, **extra):
    """A recording-lookup-shaped release: inline release_group + this recording's
    own media track. `extra` injects pressing-specific junk that must be dropped."""
    return {
        "id": rid,
        "release_group": {
            "id": rgid, "title": title, "first_release_date": date,
            "primary_type": primary, "secondary_types": secondary or [],
            "artist_credit": [{"name": artist, "artist": {"id": artist_id}}],
        },
        "media": [{"position": disc, "track_count": track_count, "tracks": [{"number": track}]}],
        **extra,
    }


def test_album_fields_identity_from_release_group():
    data = _album_fields(_rg_release("r1"))
    assert data["album"] == "Kill 'Em All"
    assert data["mb_releasegroupid"] == "rg1"
    assert data["albumartist"] == "Metallica"
    assert data["mb_albumartistid"] == "m1"
    assert data["year"] == 1983
    assert data["albumtype"] == "album"
    assert data["albumtypes"] == ["album"]
    assert data["comp"] == 0
    assert (data["track"], data["disc"], data["tracktotal"]) == (4, 1, 12)


def test_album_fields_drops_pressing_specific_junk():
    # The chosen release may be a German/Japanese pressing carrying country,
    # catalognum, barcode, its own edition title, mb_albumid -- none of it should
    # leak into the tags (which would fragment the album across pressings).
    data = _album_fields(_rg_release(
        "r1", title="Kill 'Em All",
        country="DE", barcode="123", disambiguation="made in W Germany",
    ))
    for junk in ("mb_albumid", "albumdisambig", "catalognum", "barcode",
                 "country", "label", "script"):
        assert junk not in data
    # album name is the group's canonical title, not any pressing's title
    assert data["album"] == "Kill 'Em All"


def test_album_fields_marks_various_artists_compilation():
    from src.beets_svc import _VARIOUS_ARTISTS_ID
    data = _album_fields(_rg_release("r1", artist="Various Artists", artist_id=_VARIOUS_ARTISTS_ID))
    assert data["comp"] == 1


def test_album_fields_none_without_release_group():
    assert _album_fields({"id": "r1", "release_group": {}}) is None


def _mk_match(artist, title, length, isrc, mbid, distance, disambig=None):
    return SimpleNamespace(
        info=SimpleNamespace(
            artist=artist, title=title, album=None, length=length, isrc=isrc,
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


def test_dedup_isrc_beats_lower_distance():
    # ISRC presence takes priority over length: the ISRC recording wins even when a
    # non-ISRC take has a better (lower) distance.
    matches = [
        _mk_match("A", "T", 100.0, None, "x", 0.01),
        _mk_match("A", "T", 100.0, "USRC11901785", "y", 0.20),
    ]
    assert [m.info.track_id for m in _dedup_matches(matches)] == ["y"]


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


def test_enrich_from_release_picks_release_then_reads_group():
    from src.beets_svc import _enrich_from_release
    # Two releases in the same group; select_release picks one, but album identity
    # is the group's -- so either pick yields the same album/year/rgid.
    releases = [
        _rg_release("r-us", title="Kill 'Em All", date="1983-07-25"),
        _rg_release("r-jp", title="Kill 'Em All", date="1983-07-25", country="JP"),
    ]
    data = _enrich_from_release(releases, "rec-1", "Metallica", None)
    assert data["album"] == "Kill 'Em All"
    assert data["mb_releasegroupid"] == "rg1"
    assert data["year"] == 1983


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
        patch("src.beets_svc._fingerprint_item", return_value=set()),
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
    assert result.fingerprinted is False


def test_get_candidates_sync_fingerprint_is_exclusive(tmp_path):
    # A fingerprint match makes the candidate set *only* the fingerprinted
    # recordings: the text-search hit ("wrong-id") is dropped even though it scored
    # a lower distance, and the result is flagged strong + fingerprinted.
    from beets.autotag.match import Recommendation
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")

    fp_hit = _mk_match("Metallica", "Whiplash", 260.0, "US1", "fp-id", 0.30)
    text_hit = _mk_match("Metallica", "Whiplash?", 260.0, None, "wrong-id", 0.01)
    proposal = MagicMock()
    proposal.candidates = [text_hit, fp_hit]
    proposal.recommendation = Recommendation.none

    with (
        patch("src.beets_svc.Item.from_path", return_value=MagicMock()),
        patch("src.beets_svc._fingerprint_item", return_value={"fp-id"}),
        patch("src.beets_svc.tag_item", return_value=proposal),
    ):
        result = _get_candidates_sync(audio)

    assert [c.mb_track_id for c in result.candidates] == ["fp-id"]
    assert result.fingerprinted is True
    assert result.recommendation == Recommendation.strong


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
    item.trackdisambig = None  # no marker appended unless a test sets one
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


def test_apply_and_stage_sync_appends_disambig_to_title(tmp_path):
    # The recording disambiguation lives only in the pool path, so Navidrome shows a
    # bare "Aerials" indistinguishable from the studio take. We append the full
    # disambiguation to the title *tag*, while the pool destination is still computed
    # from the untouched title.
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")
    dest_path = tmp_path / ".pool" / "SOAD" / "Toxicity" / "Aerials (live, 2005-06-12).mp3"
    item, plugin, rec = _mk_apply_mocks(dest_path, track_data={"title": "Aerials"})
    item.title = "Aerials"
    disambig = "live, 2005-06-12: Download Festival, Donington, England, UK"
    item.trackdisambig = disambig

    title_at_dest = {}
    def _dest():  # capture the title used to compute the pool path
        title_at_dest["title"] = item.title
        return str(dest_path)
    item.destination.side_effect = _dest

    candidate = Candidate(
        index=0, artist="System of a Down", title="Aerials", album="",
        year=None, mb_track_id="rec-1", distance=0.0, _match=MagicMock(),
    )

    with (
        patch("src.beets_svc.Item.from_path", return_value=item),
        patch("src.beets_svc._get_recording_full", return_value=rec),
        patch("src.beets_svc._mb_plugin", return_value=plugin),
        patch("src.beets_svc._lib", MagicMock()),
    ):
        staged, dest = _apply_and_stage_sync(audio, candidate, enrich=False)

    assert title_at_dest["title"] == "Aerials"          # path uses the untouched title
    assert item.title == f"Aerials ({disambig})"        # tag gets the full disambiguation
    assert dest == dest_path
    item.write.assert_called_once_with(path=str(audio))


def test_apply_and_stage_sync_no_disambig_leaves_title(tmp_path):
    # A studio recording has no disambiguation -> the title tag is untouched.
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")
    dest_path = tmp_path / ".pool" / "SOAD" / "Toxicity" / "Aerials.mp3"
    item, plugin, rec = _mk_apply_mocks(dest_path, track_data={"title": "Aerials"})
    item.title = "Aerials"
    item.trackdisambig = None

    candidate = Candidate(
        index=0, artist="System of a Down", title="Aerials", album="",
        year=None, mb_track_id="rec-1", distance=0.0, _match=MagicMock(),
    )

    with (
        patch("src.beets_svc.Item.from_path", return_value=item),
        patch("src.beets_svc._get_recording_full", return_value=rec),
        patch("src.beets_svc._mb_plugin", return_value=plugin),
        patch("src.beets_svc._lib", MagicMock()),
    ):
        _apply_and_stage_sync(audio, candidate, enrich=False)

    assert item.title == "Aerials"


def test_apply_and_stage_sync_enriches_with_file_length_not_recording_length(tmp_path):
    # track_info carries the *recording* length, and item.update overwrites
    # item.length with it. Enrichment's length tiebreak must still see the file's
    # own duration -- otherwise a recording length that matches a vinyl edition's
    # track pulls the track onto that edition (the ok-ok split).
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio data")
    dest = tmp_path / ".pool" / "x.mp3"
    item, plugin, rec = _mk_apply_mocks(dest, track_data={"title": "ok ok?", "length": 228.0})
    item.length = 228.533  # the file's true duration

    def _overwrite(data):  # mimic Item.update copying length out of the tags
        if "length" in data:
            item.length = data["length"]
    item.update.side_effect = _overwrite

    candidate = Candidate(
        index=0, artist="half·alive", title="ok ok?", album="",
        year=None, mb_track_id="rec-1", distance=0.0, _match=MagicMock(),
    )

    with (
        patch("src.beets_svc.Item.from_path", return_value=item),
        patch("src.beets_svc._get_recording_full", return_value=rec),
        patch("src.beets_svc._mb_plugin", return_value=plugin),
        patch("src.beets_svc._enrich_from_release", return_value={"album": "Now, Not Yet"}) as enrich,
        patch("src.beets_svc._lib", MagicMock()),
    ):
        _apply_and_stage_sync(audio, candidate)

    assert enrich.call_args.args[3] == 228.533  # file duration, not the 228.0 recording length


def test_select_release_country_outranks_length():
    # ok-ok case: the worldwide edition wins over a countryless one even though the
    # latter's track length matches the file exactly -- country ranks above length.
    worldwide = {"id": "digital", "status": "Official", "country": "XW",
                 "release_group": {"primary_type": "Album"},
                 "media": [{"track": [{"length": 228542}]}]}
    vinyl = {"id": "vinyl", "status": "Official", "country": None,
             "release_group": {"primary_type": "Album"},
             "media": [{"track": [{"length": 228000}]}]}
    assert select_release([vinyl, worldwide], "half·alive", 228000)["id"] == "digital"


def test_select_release_length_tiebreak_is_millisecond():
    # Same status/artist/type/country: length decides, at millisecond precision.
    # File 228.400s is nearer b (228.600) than a (228.000) by ms; a whole-second
    # rounding would have flipped it to a (228 vs 229).
    a = _rel("r-a", "Album", "Album", [], "Official", "2019", "XW")
    b = _rel("r-b", "Album", "Album", [], "Official", "2019", "XW")
    a["media"] = [{"track": [{"length": 228000}]}]
    b["media"] = [{"track": [{"length": 228600}]}]
    assert select_release([a, b], "half•alive", 228400)["id"] == "r-b"


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


def _dest_for(lib, **fields):
    from beets.library.models import Item
    it = Item(format="MP3", **fields)
    it.add(lib)
    import os
    return os.fsdecode(it.destination())


def test_path_format_track_disambig_only(tmp_path):
    # The path is albumartist/album/title, with only a track disambiguation
    # appended. Track number and album version are excluded, so they don't affect
    # the canonical location.
    from beets import config as beets_config
    from beets.library import Library
    from src.beets_svc import _PATH_FORMAT

    beets_config.read(user=False, defaults=True)
    beets_config["asciify_paths"].set(True)
    beets_config["paths"]["singleton"].set(_PATH_FORMAT)
    lib = Library(str(tmp_path / "b.db"), directory=str(tmp_path / "pool"))

    plain = _dest_for(lib, albumartist="Artist", album="Album", track=7, title="Song")
    assert plain.endswith("Artist/Album/Song")

    # album version does not change the path; only a track disambig does
    edition = _dest_for(
        lib, albumartist="Artist", album="Album", albumdisambig="deluxe edition",
        track=3, title="Song",
    )
    assert edition.endswith("Artist/Album/Song")

    live = _dest_for(lib, albumartist="Artist", album="Album", title="Song", trackdisambig="live")
    assert live.endswith("Artist/Album/Song (live)")
