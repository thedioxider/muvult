"""Tests for the MusicBrainz recording-search reshaping patch.

These exercise the real patched code paths (they import beetsplug), so they
require the beets runtime to be importable.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.beets_patches import patch_mb_search


@pytest.fixture(scope="module", autouse=True)
def _patched():
    patch_mb_search()


def _query_for(monkeypatch, filters):
    """Run the patched search with a stubbed API call and return the query."""
    from beetsplug._utils.musicbrainz import MusicBrainzAPI

    captured = {}

    def fake_get_resource(self, entity, **kwargs):
        captured["query"] = kwargs["query"]
        return {f"{entity.replace('-', '_')}s": []}

    monkeypatch.setattr(MusicBrainzAPI, "_get_resource", fake_get_resource)
    api = MusicBrainzAPI.__new__(MusicBrainzAPI)
    api.search("recording", filters)
    return captured["query"]


def test_full_query_shape(monkeypatch):
    query = _query_for(
        monkeypatch,
        {
            "artist": "Self",
            "recording": "The End Of It All (Album Version)",
            "alias": "The End Of It All (Album Version)",
            "release": "Breakfast With Girls",
        },
    )
    assert query == (
        r"+artist:(self) "
        r"+(recording:(the end of it all \(album version\)) "
        r"alias:(the end of it all \(album version\))) "
        r"release:(breakfast with girls)"
    )


def test_no_album_omits_release(monkeypatch):
    query = _query_for(
        monkeypatch,
        {"artist": "half·alive", "recording": "ice cold", "alias": "ice cold"},
    )
    # unicode middle dot is not a Lucene special char -> preserved, not tokenized
    assert query == "+artist:(half·alive) +(recording:(ice cold) alias:(ice cold))"


def test_title_group_collapses_to_single_field(monkeypatch):
    # only `recording` present -> required, but no wrapping group parens
    query = _query_for(monkeypatch, {"artist": "x", "recording": "foo bar"})
    assert query == "+artist:(x) +recording:(foo bar)"


def test_title_without_artist(monkeypatch):
    query = _query_for(monkeypatch, {"recording": "foo", "alias": "foo"})
    assert query == "+(recording:(foo) alias:(foo))"


def test_lucene_special_chars_escaped(monkeypatch):
    query = _query_for(monkeypatch, {"artist": "AC/DC", "recording": "T.N.T!"})
    assert query == r"+artist:(ac\/dc) +recording:(t.n.t\!)"


def test_blank_and_unknown_fields(monkeypatch):
    # blank values drop out; an unrecognised field stays required
    query = _query_for(
        monkeypatch, {"artist": "  ", "recording": "song", "arid": "abc-123"}
    )
    assert query == "+recording:(song) +arid:(abc\\-123)"


def test_release_injected_from_album_tag():
    from beetsplug.musicbrainz import MusicBrainzPlugin

    plugin = MusicBrainzPlugin.__new__(MusicBrainzPlugin)
    item = MagicMock()
    item.album = "Breakfast With Girls"

    _, criteria = plugin.get_search_query_with_filters(
        "track", [item], "Self", "The End Of It All", False
    )
    assert criteria["release"] == "Breakfast With Girls"
    assert criteria["recording"] == "the end of it all"


def test_no_release_when_album_tag_missing():
    from beetsplug.musicbrainz import MusicBrainzPlugin

    plugin = MusicBrainzPlugin.__new__(MusicBrainzPlugin)
    item = MagicMock()
    item.album = ""

    _, criteria = plugin.get_search_query_with_filters(
        "track", [item], "Self", "The End Of It All", False
    )
    assert "release" not in criteria


def test_track_info_from_thin_search_hit():
    # A search hit is thinner than a lookup payload: no disambiguation, maybe no
    # length, and its artist_credit elements carry no joinphrase. Candidate building
    # must not depend on those (the arrow regression: reusing track_info raised
    # KeyError on live data and yielded zero candidates).
    from src.beets_patches import _track_info_from_hit

    hit = {  # shaped like a real recording-search result
        "id": "86349be4-552a-4b0f-ac7b-129bfda0fbd3",
        "title": "arrow",
        "isrcs": ["USRC11803910"],
        "artist_credit": [
            {"name": "half•alive", "artist": {"id": "00d0", "name": "half•alive"}}
        ],
    }  # note: no "joinphrase", no "disambiguation", no "length"

    info = _track_info_from_hit(hit)

    assert info.track_id == "86349be4-552a-4b0f-ac7b-129bfda0fbd3"
    assert info.isrc == "USRC11803910"
    assert info.artist == "half•alive"
    assert info.artist_id == "00d0"
    assert info.length is None
    assert info.trackdisambig is None


def test_track_info_from_hit_joins_multiple_credits():
    from src.beets_patches import _track_info_from_hit

    hit = {
        "id": "r1",
        "title": "duet",
        "length": 200000,
        "artist_credit": [
            {"name": "A", "joinphrase": " & ", "artist": {"id": "a"}},
            {"name": "B", "artist": {"id": "b"}},
        ],
    }
    info = _track_info_from_hit(hit)
    assert info.artist == "A & B"
    assert info.length == pytest.approx(200.0)


def test_item_candidates_builds_from_search_without_lookups():
    # The override must turn one search into candidates directly, with no
    # per-candidate get_recording, and correct each length to the nearest
    # per-release track length (the arrow fix).
    from beetsplug.musicbrainz import MusicBrainzPlugin

    plugin = MusicBrainzPlugin.__new__(MusicBrainzPlugin)
    hit = {
        "id": "rec-1",
        "title": "arrow",
        "length": 221000,  # recording length
        "artist_credit": [{"name": "half·alive"}],
        "isrcs": ["USRC11803910"],
        "releases": [{"id": "r", "media": [{"track": [{"length": 222706}]}]}],
    }
    plugin._get_candidates = MagicMock(return_value=[hit])
    plugin.track_info = lambda rec: SimpleNamespace(
        track_id=rec["id"], length=rec["length"] / 1000.0
    )
    plugin.mb_api = MagicMock()  # must not be used for lookups

    item = MagicMock()
    item.length = 222.707  # file duration, matches the album track length

    result = list(plugin.item_candidates(item, "half·alive", "arrow"))

    plugin._get_candidates.assert_called_once()
    plugin.mb_api.get_recording.assert_not_called()
    assert len(result) == 1
    assert result[0].length == pytest.approx(222.706)  # corrected off recording length


# --- AcoustID lookup retry -------------------------------------------------
#
# The library makes a single un-retried POST, so one network blip or a
# rate-limit `status: error` response silently drops a fingerprint match to
# text-search-only. These exercise the retry wrapper directly.


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("src.beets_patches.time.sleep", lambda *_: None)


def test_acoustid_retry_returns_ok_on_first_call():
    from src.beets_patches import _acoustid_lookup_with_retry

    fn = MagicMock(return_value={"status": "ok", "results": []})
    res = _acoustid_lookup_with_retry(fn, "key", "fp", 100)
    assert res["status"] == "ok"
    assert fn.call_count == 1  # a genuine no-match is not retried


def test_acoustid_retry_recovers_from_web_service_error():
    import acoustid

    from src.beets_patches import _acoustid_lookup_with_retry

    fn = MagicMock(side_effect=[acoustid.WebServiceError("boom"), {"status": "ok"}])
    res = _acoustid_lookup_with_retry(fn, "k", "fp", 1)
    assert res == {"status": "ok"}
    assert fn.call_count == 2


def test_acoustid_retry_recovers_from_transient_error_status():
    from src.beets_patches import _acoustid_lookup_with_retry

    # A non-permanent error (e.g. rate limit / server error) is retried.
    fn = MagicMock(
        side_effect=[
            {"status": "error", "error": {"message": "rate limit exceeded"}},
            {"status": "ok", "results": [1]},
        ]
    )
    res = _acoustid_lookup_with_retry(fn, "k", "fp", 1)
    assert res["status"] == "ok"
    assert fn.call_count == 2


def test_acoustid_retry_does_not_retry_permanent_error():
    from src.beets_patches import _acoustid_lookup_with_retry

    # A bad key fails identically every time -- return at once, don't burn retries.
    fn = MagicMock(
        return_value={"status": "error", "error": {"code": 4, "message": "invalid API key"}}
    )
    res = _acoustid_lookup_with_retry(fn, "k", "fp", 1)
    assert res["status"] == "error"
    assert fn.call_count == 1


def test_acoustid_retry_reraises_after_exhaustion():
    import acoustid

    from src.beets_patches import _ACOUSTID_RETRIES, _acoustid_lookup_with_retry

    fn = MagicMock(side_effect=acoustid.WebServiceError("down"))
    with pytest.raises(acoustid.WebServiceError):
        _acoustid_lookup_with_retry(fn, "k", "fp", 1)
    assert fn.call_count == _ACOUSTID_RETRIES


def test_acoustid_retry_returns_error_status_after_exhaustion():
    from src.beets_patches import _ACOUSTID_RETRIES, _acoustid_lookup_with_retry

    fn = MagicMock(return_value={"status": "error", "error": {"message": "rate limit"}})
    res = _acoustid_lookup_with_retry(fn, "k", "fp", 1)
    # returned (not raised) so chroma treats it as no-match, as before
    assert res["status"] == "error"
    assert fn.call_count == _ACOUSTID_RETRIES


def test_patch_acoustid_lookup_is_idempotent():
    import acoustid

    from src.beets_patches import patch_acoustid_lookup

    patch_acoustid_lookup()
    first = acoustid.lookup
    assert getattr(acoustid.lookup, "_muvult_retry", False) is True
    patch_acoustid_lookup()
    assert acoustid.lookup is first  # not double-wrapped


def test_patch_acoustid_lookup_captures_top_cluster_score(monkeypatch):
    import acoustid

    from src import beets_patches
    from src.beets_patches import (
        last_fingerprint_score,
        patch_acoustid_lookup,
        reset_fingerprint_score,
    )

    patch_acoustid_lookup()
    reset_fingerprint_score()

    # The wrapper stashes results[0]["score"] from the raw response.
    monkeypatch.setattr(
        beets_patches, "_acoustid_lookup_with_retry",
        lambda fn, *a, **k: {"status": "ok", "results": [{"score": 0.87}, {"score": 0.1}]},
    )
    acoustid.lookup("key", "fp", 100)
    assert last_fingerprint_score() == pytest.approx(0.87)

    # A no-result / malformed response leaves the score cleared.
    monkeypatch.setattr(
        beets_patches, "_acoustid_lookup_with_retry",
        lambda fn, *a, **k: {"status": "ok", "results": []},
    )
    acoustid.lookup("key", "fp", 100)
    assert last_fingerprint_score() is None
