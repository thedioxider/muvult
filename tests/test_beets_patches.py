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
