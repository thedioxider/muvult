"""Tests for the MusicBrainz recording-search reshaping patch.

These exercise the real patched code paths (they import beetsplug), so they
require the beets runtime to be importable.
"""

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
