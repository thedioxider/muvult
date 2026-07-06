import asyncio
import logging
import os
import re
from asyncio import get_running_loop
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from beets import config as beets_config, plugins
from beets.autotag.match import tag_item
from beets.library import Library
from beets.library.models import Item

from .models import Candidate, TagResult

log = logging.getLogger(__name__)

_BEETS_DB = "/data/beets_pool.db"
_lib: Library | None = None

# Track disambiguation (e.g. "live") goes into the filename so a distinct recording
# of the same track gets a distinct canonical path. Track number and album version
# are deliberately excluded: the number is positional (MB renumbers/renumerates)
# and the true dedup key is the recording id, not the path; album version splits
# nothing the recording id doesn't already merge.
_PATH_FORMAT = "$albumartist/$album/$title%if{$trackdisambig, ($trackdisambig)}"

# All beets work runs on a single thread: MusicBrainz is rate-limited to 1 req/s
# so parallel tagging only contends, and the shared beets Library (one sqlite
# file) can hit "database is locked" under concurrent writes. Serializing here
# keeps downloads and confirmation prompts concurrent while these steps queue.
_beets_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="beets")


def setup_beets(music_root: str, search_limit: int = 48) -> None:
    global _lib
    pool_path = str(Path(music_root) / ".pool")
    beets_config.read(user=False, defaults=True)
    beets_config["plugins"].set(["musicbrainz"])
    beets_config["musicbrainz"]["search_limit"].set(search_limit)
    plugins.load_plugins()
    from .beets_patches import patch_mb_search
    patch_mb_search()
    beets_config["asciify_paths"].set(True)
    beets_config["paths"]["default"].set(_PATH_FORMAT)
    beets_config["paths"]["singleton"].set(_PATH_FORMAT)
    _lib = Library(_BEETS_DB, directory=pool_path)


async def get_candidates(file_path: Path) -> TagResult:
    loop = get_running_loop()
    return await loop.run_in_executor(_beets_pool, _get_candidates_sync, file_path)


def normalize_title(title: str, loose: bool = False) -> str:
    """Fold a title for identity comparisons.

    Strict (default) just lowercases -- used for deduplication, where a
    false-positive match would silently make a legitimate distinct candidate
    unreachable. Loose also strips all punctuation, so titles MusicBrainz
    submissions disagree on (``ATWA`` / ``A.T.W.A`` / ``Atwa``) compare
    equal -- used where a false-positive match is safe, e.g. offering a human
    a confirmation choice.
    """
    if not loose:
        return title.lower()
    return re.sub(r"[^\w]+", "", title, flags=re.UNICODE).lower()


def _dedup_matches(matches: list) -> list:
    """Filter out recordings that are indistinguishable to the user.

    MusicBrainz often holds several recording entities for one performance (e.g.
    one per release of the same album), differing only in trivia -- an ISRC,
    which release they hang off, a small length delta -- but sharing artist,
    title, and disambiguation. We keep one representative per
    (artist, title, disambig) group. (Length is *not* part of the key: candidate
    lengths are already corrected to the nearest per-release track length before
    scoring, so two takes of the same track no longer straddle a second boundary.)

    Within a group the survivor is chosen by, in order:

    - carrying an ISRC (the canonically-registered, typically worldwide
      recording) -- the authoritative identity of the same track wins outright,
    - then best beets match (lowest corrected distance, i.e. closest length),
    - then lowest MBID, so the choice is deterministic across uploads.

    Output order follows the input list.
    """
    def rank(m) -> tuple:
        return (
            0 if getattr(m.info, "isrc", None) else 1,
            m.distance.distance,
            m.info.track_id or "",
        )

    groups: dict[tuple, list] = {}
    for m in matches:
        key = (
            (m.info.artist or "").lower(),
            normalize_title(m.info.title or ""),
            getattr(m.info, "trackdisambig", None) or "",
        )
        groups.setdefault(key, []).append(m)

    keep = {id(min(ms, key=rank)) for ms in groups.values()}
    return [m for m in matches if id(m) in keep]


def _release_track_length_ms(release: dict) -> int | None:
    """Length (ms) of the recording's own track on this release.

    Search-embedded releases carry only the matching recording's track(s) under
    ``media -> track/tracks``; take the shortest when several are present.
    """
    lens = [
        t["length"]
        for medium in (release.get("media") or [])
        for t in (medium.get("tracks") or medium.get("track") or [])
        if t.get("length")
    ]
    return min(lens) if lens else None


def _nearest_release_length(rec: dict, file_length_s: float | None) -> float | None:
    """Per-release track length (seconds) closest to the file's duration.

    Fixes the singleton blind spot: a recording's own ``length`` can differ from
    the track's length on the specific release the file came from. Returns None
    when nothing usable is known, so the caller keeps the recording length.
    """
    if not file_length_s:
        return None
    target = file_length_s * 1000
    best: int | None = None
    for release in rec.get("releases") or []:
        tl = _release_track_length_ms(release)
        if tl is not None and (best is None or abs(tl - target) < abs(best - target)):
            best = tl
    return best / 1000.0 if best is not None else None


def _get_candidates_sync(file_path: Path) -> TagResult:
    item = Item.from_path(str(file_path))
    proposal = tag_item(item)
    candidates = []
    for i, match in enumerate(_dedup_matches(proposal.candidates)):
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


async def apply_and_stage(
    file_path: Path, candidate: Candidate, enrich: bool = True
) -> tuple[Path, Path]:
    loop = get_running_loop()
    return await loop.run_in_executor(_beets_pool, _apply_and_stage_sync, file_path, candidate, enrich)


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


def select_release(
    releases: list[dict],
    track_artist: str | None = None,
    file_length_ms: int | None = None,
) -> dict | None:
    """Choose the best release a recording appears on for singleton enrichment.

    Strict priority: Official status, then matching primary artist, then studio
    album, then worldwide country, then (when ``file_length_ms`` is known) the
    release whose track length is closest to the file, then earliest date (missing
    last), then lowest release MBID. Length sits below country -- edition identity
    is a stronger signal than a duration delta -- and is compared in milliseconds
    as a fine final tiebreak.
    """
    releases = [r for r in releases if r]
    if not releases:
        return None

    def length_delta(r: dict) -> float:
        if file_length_ms is None:
            return 0.0  # no signal: leave ordering to the other criteria
        tl = _release_track_length_ms(r)
        return abs(tl - file_length_ms) if tl is not None else float("inf")

    def key(r: dict) -> tuple:
        return (
            -_status_rank(r.get("status")),
            -_artist_rank(r, track_artist),
            -_type_rank(r.get("release_group")),
            -_country_rank(r.get("country")),
            length_delta(r),
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


def _mb_plugin():
    from beets import metadata_plugins

    return metadata_plugins.get_metadata_source("musicbrainz")


def _get_recording_full(rec_id: str) -> dict | None:
    """One authoritative recording lookup by id, with releases folded in.

    Serves both purposes at import: complete recording metadata (relation credits
    and all) *and* the releases enrichment needs -- no second request.
    """
    plugin = _mb_plugin()
    if plugin is None:
        return None
    from beetsplug._utils.musicbrainz import RECORDING_INCLUDES

    try:
        return plugin.mb_api.get_recording(
            rec_id,
            includes=RECORDING_INCLUDES
            + ["releases", "release-groups", "media", "artist-credits"],
        )
    except Exception:
        log.exception("recording lookup failed for %s", rec_id)
        return None


@lru_cache(maxsize=256)
def _album_for_id_cached(release_id: str):
    """Cache release lookups by id: a bulk album upload hits each release once.

    ``merge_with_album`` reads the AlbumInfo and returns a fresh dict without
    mutating it, so sharing the cached object across tracks is safe.
    """
    plugin = _mb_plugin()
    if plugin is None:
        return None
    try:
        return plugin.album_for_id(release_id)
    except Exception:
        log.exception("album_for_id failed for %s", release_id)
        return None


def _enrich_from_release(
    releases: list[dict],
    rec_id: str,
    track_artist: str | None,
    file_length_s: float | None,
) -> dict | None:
    """Resolve a recording's releases to one release and return album-level data.

    Given the releases from the import lookup, pick one (see ``select_release``,
    length-aware), pull full album metadata via beets' own ``album_for_id`` +
    ``merge_with_album`` (cached), and override the year with the recording's
    earliest official release. Returns ``None`` on any failure so the caller keeps
    the plain recording-level tags. Runs on ``_beets_pool``.
    """
    try:
        file_length_ms = int(file_length_s * 1000) if file_length_s else None
        chosen = select_release(releases, track_artist, file_length_ms)
        if chosen is None:
            return None
        album_info = _album_for_id_cached(chosen["id"])
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


def _apply_and_stage_sync(
    file_path: Path, candidate: Candidate, enrich: bool = True
) -> tuple[Path, Path]:
    """Tag the file in place (in staging) and report where it should be filed.

    The imported track's metadata comes from a single authoritative recording
    lookup by id (not from the search-built candidate), with release enrichment
    layered on top. Returns ``(staged, dest)``: ``staged`` is the tagged file,
    still in staging; ``dest`` is the canonical pool path its tags map to. Nothing
    is written into the pool here -- the caller moves ``staged`` onto ``dest``
    (``promote_pool_file``) only once dedup decides this copy wins.
    """
    item = Item.from_path(str(file_path))
    # The file's own duration, captured before item.update overwrites item.length
    # with the *recording* length -- enrichment's length tiebreak must compare the
    # release track lengths against the actual file, not the recording.
    file_length = item.length
    rec = _get_recording_full(candidate.mb_track_id) if candidate.mb_track_id else None
    if rec is not None:
        plugin = _mb_plugin()
        track_info = plugin.track_info(rec)
        item.update(track_info.item_data)
        track_artist = track_info.artist
        releases = rec.get("releases") or []
    else:  # lookup failed: fall back to the search-level candidate metadata
        info = candidate._match.info
        item.update(info.item_data)
        track_artist = info.artist
        releases = []
    if enrich and releases:
        enriched = _enrich_from_release(
            releases, candidate.mb_track_id, track_artist, file_length
        )
        if enriched is not None:
            item.update(enriched)
    item.add(_lib)  # attaches the library so destination() sees dir + path formats
    dest = Path(os.fsdecode(item.destination()))
    # destination() is pure computation; drop the row so the pool beets DB isn't
    # left with an entry pointing at the (transient) staging path. delete=False
    # keeps the file on disk.
    item.remove(delete=False)
    # Surface the recording disambiguation in the title *tag* so tag-reading players
    # (Navidrome) show e.g. "Aerials (live, 2005-06-12: Download Festival, ...)"
    # instead of a bare "Aerials" that looks identical to the studio take. dest was
    # already computed above from the untouched title. Idempotent on re-upload: the
    # title is reset to the clean recording title by item.update before we append.
    if disambig := (item.trackdisambig or "").strip():
        item.title = f"{item.title} ({disambig})"
    item.write(path=str(file_path))
    return file_path, dest


async def stage_as_is(file_path: Path, username: str) -> tuple[Path, Path]:
    loop = get_running_loop()
    return await loop.run_in_executor(_beets_pool, _stage_as_is_sync, file_path, username)


def _stage_as_is_sync(file_path: Path, username: str) -> tuple[Path, Path]:
    """As-is counterpart to ``_apply_and_stage_sync``: validate and report the
    per-user pool path without moving anything (placement is the caller's job)."""
    users_root = (Path(os.fsdecode(_lib.directory)) / "users").resolve()
    dest = (users_root / username / file_path.name).resolve()
    if not dest.is_relative_to(users_root):
        raise ValueError(f"path traversal detected for username {username!r}")
    return file_path, dest
