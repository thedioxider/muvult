# Workarounds for upstream beets bugs.
# TODO: file an issue on beetbox/beets:
#   beets 2.6.2 (PR #6354) changed MusicBrainz Lucene field queries from
#   phrase search (artist:"half·alive") to group syntax (artist:(half·alive)).
#   Group syntax tokenizes on unicode word separators like the middle dot in
#   "half·alive", producing artist:half OR artist:alive → wrong results.
#   The AND-joined phrase format used before 2.6.2 was correct.


def patch_mb_phrase_search() -> None:
    """Revert MusicBrainzAPI.format_search_term to phrase search.

    Phrase search preserves unicode artists as exact tokens; group syntax
    tokenizes them and produces unrelated candidates.
    """
    from beetsplug._utils.musicbrainz import MusicBrainzAPI

    @staticmethod  # type: ignore[misc]
    def _phrase_fmt(field: str, term: str) -> str:
        if not (term := term.lower().strip()):
            return ""
        term = term.replace("\\", "\\\\").replace('"', '\\"')
        return f'{field}:"{term}"' if field else f'"{term}"'

    MusicBrainzAPI.format_search_term = _phrase_fmt
