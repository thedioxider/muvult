import asyncio
import logging
import os
import shutil
from asyncio import get_running_loop
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from beets import config as beets_config, plugins
from beets.autotag.match import tag_item
from beets.library import Library
from beets.library.models import Item

from .models import Candidate, TagResult

log = logging.getLogger(__name__)

_BEETS_DB = "/data/beets_pool.db"
_lib: Library | None = None

# All beets work runs on a single thread: MusicBrainz is rate-limited to 1 req/s
# so parallel tagging only contends, and the shared beets Library (one sqlite
# file) can hit "database is locked" under concurrent writes. Serializing here
# keeps downloads and confirmation prompts concurrent while these steps queue.
_beets_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="beets")


def setup_beets(music_root: str, search_limit: int = 8) -> None:
    global _lib
    pool_path = str(Path(music_root) / ".pool")
    beets_config.read(user=False, defaults=True)
    beets_config["plugins"].set(["musicbrainz"])
    beets_config["musicbrainz"]["search_limit"].set(search_limit)
    plugins.load_plugins()
    from .beets_patches import patch_mb_search
    patch_mb_search()
    beets_config["asciify_paths"].set(True)
    beets_config["paths"]["default"].set("$albumartist/$album/$track - $title")
    beets_config["paths"]["singleton"].set("$albumartist/$album/$track - $title")
    _lib = Library(_BEETS_DB, directory=pool_path)


async def get_candidates(file_path: Path) -> TagResult:
    loop = get_running_loop()
    return await loop.run_in_executor(_beets_pool, _get_candidates_sync, file_path)


def _dedup_matches(matches: list) -> list:
    """Filter out recordings that are indistinguishable to the user.

    MusicBrainz often holds several recording entities for one performance (e.g.
    one per release of the same album), differing only in trivia -- a sub-second
    length delta, an ISRC, which release they hang off -- but sharing artist,
    title, and displayed duration. We keep one representative per
    (artist, title, seconds) group.

    Within a group the survivor is chosen by, in order:

    - best beets match (lowest distance),
    - carrying an ISRC (the canonically-registered, typically worldwide
      recording),
    - lowest MBID, so the choice is deterministic across uploads.

    Output order follows the input list.
    """
    keys = [
        (
            (m.info.artist or "").lower(),
            (m.info.title or "").lower(),
            int(m.info.length) if getattr(m.info, "length", None) else None,
        )
        for m in matches
    ]
    chosen: dict[tuple, tuple] = {}  # key -> (rank, match)
    for key, m in zip(keys, matches):
        rank = (
            m.distance.distance,
            0 if getattr(m.info, "isrc", None) else 1,
            m.info.track_id or "",
        )
        if key not in chosen or rank < chosen[key][0]:
            chosen[key] = (rank, m)
    return [m for key, m in zip(keys, matches) if chosen[key][1] is m]


def _get_candidates_sync(file_path: Path) -> TagResult:
    item = Item.from_path(str(file_path))
    proposal = tag_item(item)
    candidates = []
    for i, match in enumerate(_dedup_matches(proposal.candidates)[:6]):
        info = match.info
        candidates.append(
            Candidate(
                index=i,
                artist=info.artist or "",
                title=info.title or "",
                album=info.album or "",
                year=getattr(info, "year", None),
                mb_track_id=info.track_id,
                distance=match.distance.distance,
                _match=match,
                length=getattr(info, "length", None),
                disambig=getattr(info, "trackdisambig", None),
            )
        )
    return TagResult(candidates=candidates, recommendation=int(proposal.recommendation))


async def apply_and_move(file_path: Path, candidate: Candidate, enrich: bool = True) -> Path:
    loop = get_running_loop()
    return await loop.run_in_executor(_beets_pool, _apply_and_move_sync, file_path, candidate, enrich)


def _status_rank(status: str | None) -> int:
    return {"Official": 2, "Pseudo-Release": 1}.get(status or "", 0)


def _type_rank(release_group: dict | None) -> int:
    """Clean releases (no secondary types) beat all secondary-typed ones;
    within each tier, Album > EP > Single > Broadcast/Other."""
    rg = release_group or {}
    primary = {"Album": 3, "EP": 2, "Single": 1}.get(rg.get("primary_type") or "", 0)
    clean = 0 if rg.get("secondary_types") else 4
    return clean + primary


def _country_rank(country: str | None) -> int:
    # Worldwide first, then any defined country, then null/unknown.
    if country == "XW":
        return 2
    return 1 if country else 0


def _release_artist(release: dict) -> str:
    # First credited artist only; albums stay under the primary artist even when a
    # track is a collaboration.
    parts = release.get("artist_credit") or []
    return parts[0].get("name", "") if parts and isinstance(parts[0], dict) else ""


def _artist_rank(release: dict, track_artist: str | None) -> int:
    # Prefer releases whose primary artist is the track's own artist over Various
    # Artists compilations and other-artist releases.
    if not track_artist:
        return 0
    return 1 if _release_artist(release).casefold() == track_artist.casefold() else 0


def select_release(releases: list[dict], track_artist: str | None = None) -> dict | None:
    """Choose the best release a recording appears on for singleton enrichment.

    Strict priority: Official status, then matching primary artist, then studio
    album, then worldwide country, then earliest date (missing last), then lowest
    release MBID.
    """
    releases = [r for r in releases if r]
    if not releases:
        return None

    def key(r: dict) -> tuple:
        return (
            -_status_rank(r.get("status")),
            -_artist_rank(r, track_artist),
            -_type_rank(r.get("release_group")),
            -_country_rank(r.get("country")),
            r.get("date") or "9999",
            r.get("id") or "",
        )

    return min(releases, key=key)


def earliest_official_year(releases: list[dict]) -> int | None:
    """Year of the earliest Official release of the recording (any release if none)."""
    dates = [r["date"] for r in releases if r.get("status") == "Official" and r.get("date")]
    if not dates:
        dates = [r["date"] for r in releases if r.get("date")]
    if not dates:
        return None
    try:
        return int(min(dates)[:4])
    except ValueError:
        return None


def _enrich_from_release(match) -> dict | None:
    """Resolve the matched recording to a release and return album-level item data.

    Singleton (recording) matches carry no album/track/disc/year. We pick a release
    the recording appears on (see ``select_release``), pull full album metadata via
    beets' own ``album_for_id`` + ``merge_with_album``, and override the year with the
    recording's earliest official release. Returns ``None`` on any failure so the
    caller falls back to plain recording-level tagging. Runs on ``_beets_pool``.
    """
    rec_id = getattr(match.info, "track_id", None)
    if not rec_id:
        return None
    try:
        from beets import metadata_plugins
        from beetsplug._utils.musicbrainz import RECORDING_INCLUDES

        plugin = metadata_plugins.get_metadata_source("musicbrainz")
        if plugin is None:
            return None
        rec = plugin.mb_api.get_recording(
            rec_id,
            includes=RECORDING_INCLUDES
            + ["releases", "release-groups", "artist-credits"],
        )
        releases = rec.get("releases") or []
        artists = getattr(match.info, "artists", None)
        track_artist = artists[0] if artists else getattr(match.info, "artist", None)
        chosen = select_release(releases, track_artist)
        if chosen is None:
            return None
        album_info = plugin.album_for_id(chosen["id"])
        if album_info is None:
            return None
        album_track = next(
            (t for t in album_info.tracks if t.track_id == rec_id), None
        )
        if album_track is None:
            return None
        data = album_track.merge_with_album(album_info)
        if (year := earliest_official_year(releases)) is not None:
            data["year"] = year
        return data
    except Exception:
        log.exception("release enrichment failed for recording %s", rec_id)
        return None


def _apply_and_move_sync(file_path: Path, candidate: Candidate, enrich: bool = True) -> Path:
    match = candidate._match
    item = match.item
    enriched = _enrich_from_release(match) if enrich else None
    if enriched is not None:
        item.update(enriched)
    else:
        match.apply_metadata()  # recording-level only; no album/track/disc/year
    item.write(path=str(file_path))
    item.add(_lib)
    dest = Path(os.fsdecode(item.destination()))
    if dest.exists():
        dest.unlink()
    item.move(store=True)
    return Path(os.fsdecode(item.path))


async def move_as_is(file_path: Path, username: str) -> Path:
    loop = get_running_loop()
    return await loop.run_in_executor(_beets_pool, _move_as_is_sync, file_path, username)


def _move_as_is_sync(file_path: Path, username: str) -> Path:
    users_root = (Path(os.fsdecode(_lib.directory)) / "users").resolve()
    dest = (users_root / username / file_path.name).resolve()
    if not dest.is_relative_to(users_root):
        raise ValueError(f"path traversal detected for username {username!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(file_path), str(dest))
    return dest
