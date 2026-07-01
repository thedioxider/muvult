# Reshape beets' MusicBrainz recording search for singleton tagging.
#
# beets builds the recording query as all-optional Lucene clauses over the
# fields {artist, recording, alias} (alias duplicates the title). That is both
# too loose and too strict: with every clause optional the right recording is
# buried under unrelated artists, yet a common title token like "self" drags in
# noise; and since 2.6.2 (PR #6354) group syntax field:(a b) tokenizes on
# unicode separators (the middle dot in "half·alive" -> half OR alive).
#
# Empirically validated shape for the recording search:
#
#     +artist:(<title tokens>)
#     +(recording:(<title tokens>) alias:(<title tokens>))
#     release:(<album tokens>)
#
# - artist and title are REQUIRED across fields (AND between clauses) so both
#   must match -- this kills the "self" noise.
# - each field is OR *within* itself, so extra query tokens like
#   "(Album Version)" or "(with ...)" are tolerated instead of breaking the
#   match; no title-stripping heuristics needed.
# - recording and alias form one required cross-field group: a recording that
#   only matches via its alias is still found, which a required-recording
#   clause would exclude.
# - album is an OPTIONAL boost: it clusters same-album tracks near the top
#   without ever excluding the target when the album tag is missing or wrong
#   (singles, compilations, deluxe editions).


def patch_mb_search() -> None:
    """Apply the recording-search reshaping patches (idempotent)."""
    _patch_search_query()
    _patch_recording_criteria()


def _patch_search_query() -> None:
    """Compose the MB query from the whole filter dict, not field by field.

    format_search_term only ever sees one field, so it cannot build the
    required cross-field recording/alias group. search() receives every filter
    at once, so we override it instead.
    """
    from beetsplug._utils import musicbrainz as mb

    def _search(self, entity, filters, **kwargs):
        # Reuse beets' own term formatter
        fmt = self.format_search_term
        remaining = dict(filters)
        clauses: list[str] = []

        if artist := fmt("artist", remaining.pop("artist", "")):
            clauses.append(f"+{artist}")

        title = [
            g for f in ("recording", "alias") if (g := fmt(f, remaining.pop(f, "")))
        ]
        if len(title) == 1:
            clauses.append(f"+{title[0]}")
        elif title:
            clauses.append(f"+({' '.join(title)})")

        if release := fmt("release", remaining.pop("release", "")):
            clauses.append(release)

        # Any other field (e.g. album-search criteria) stays required.
        for field, term in remaining.items():
            if g := fmt(field, term):
                clauses.append(f"+{g}")

        query = " ".join(clauses)
        mb.log.debug("Searching for MusicBrainz {}s with: {!r}", entity, query)
        kwargs["query"] = query
        normalised_entity = entity.replace("-", "_")
        return self._get_resource(entity, **kwargs)[f"{normalised_entity}s"]

    mb.MusicBrainzAPI.search = _search


def _patch_recording_criteria() -> None:
    """Add the album tag as a `release` filter for recording search.

    beets' recording criteria is {artist, recording, alias} with no album
    field; we inject `release` so the query can use it as an optional boost.
    """
    from beetsplug.musicbrainz import MusicBrainzPlugin

    original = MusicBrainzPlugin.get_search_query_with_filters

    def _with_release(self, query_type, items, artist, name, va_likely):
        query, criteria = original(self, query_type, items, artist, name, va_likely)
        if query_type != "album" and items:
            if album := (items[0].album or "").strip():
                criteria["release"] = album
        return query, criteria

    MusicBrainzPlugin.get_search_query_with_filters = _with_release
