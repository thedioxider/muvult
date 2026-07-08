import asyncio
import logging
import os
import re
from asyncio import get_running_loop
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen

from beets import config as beets_config, plugins
from beets.autotag.match import Recommendation, tag_item
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


def setup_beets(
    music_root: str, search_limit: int = 48, acoustid_api_key: str | None = None
) -> None:
    global _lib
    pool_path = str(Path(music_root) / ".pool")
    beets_config.read(user=False, defaults=True)
    # chroma (AcoustID fingerprinting) sits alongside musicbrainz as a second
    # candidate source: it resolves the *actual* recording from the audio,
    # independent of how well the file's existing tags text-search. `auto` off
    # because we drive the fingerprint lookup ourselves (see _fingerprint_item) --
    # the import-pipeline listener it would otherwise register never fires under
    # our direct tag_item() calls.
    beets_config["plugins"].set(["musicbrainz", "chroma"])
    beets_config["musicbrainz"]["search_limit"].set(search_limit)
    beets_config["chroma"]["auto"].set(False)
    plugins.load_plugins()
    from .beets_patches import patch_acoustid_lookup, patch_mb_search
    patch_mb_search()
    patch_acoustid_lookup()
    # chroma resolves recordings with a hardcoded, globally-shared AcoustID app
    # key that gets rate-limited (returning a "status: error" chroma treats as
    # "no match" -- a silent fingerprint miss). Our own key gives a private
    # 3 req/s budget. The key is only used for lookups via the module constant;
    # beets' `acoustid.apikey` config is submission-only, which we don't do.
    if acoustid_api_key:
        import beetsplug.chroma as chroma
        chroma.API_KEY = acoustid_api_key
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


def _fingerprint_item(item: Item) -> set[str]:
    """AcoustID recording ids for this file's audio, or an empty set.

    Runs the chroma fingerprint lookup ourselves (muvult never enters beets'
    import pipeline, where chroma would otherwise trigger it) so that chroma's
    ``item_candidates`` fires inside the following ``tag_item`` -- and so we know
    which candidates are fingerprint-backed. Fully guarded: a missing ``fpcalc``
    binary, a network failure, or no match all degrade to search-only tagging.
    """
    try:
        from beetsplug import chroma

        chroma.acoustid_match(log, item.path)
        match = chroma._matches.get(item.path)
        return set(match[0]) if match else set()
    except Exception:
        log.exception("acoustid fingerprint lookup failed")
        return set()


def _get_candidates_sync(file_path: Path) -> TagResult:
    item = Item.from_path(str(file_path))
    fp_ids = _fingerprint_item(item)
    proposal = tag_item(item)
    # A fingerprint match is the authoritative audio identity: when present, the
    # candidate set is *only* the fingerprinted recordings (deduped as usual),
    # and text-search hits are discarded. Text search is the fallback used solely
    # when the fingerprint found nothing (unknown/obscure track).
    fp_matches = [m for m in proposal.candidates if m.info.track_id in fp_ids]
    if fp_matches:
        matches = _dedup_matches(fp_matches)
        fingerprinted = True
        # A fingerprint hit is the authoritative audio identity, so it enters the
        # confirmation gate as strong regardless of how the stale tags text-scored.
        recommendation = Recommendation.strong
    else:
        matches = _dedup_matches(proposal.candidates)
        fingerprinted = False
        recommendation = proposal.recommendation
    candidates = []
    for i, match in enumerate(matches):
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
    return TagResult(
        candidates=candidates,
        recommendation=recommendation,
        fingerprinted=fingerprinted,
    )


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
            # "work-rels" is needed alongside RECORDING_INCLUDES's
            # "work-level-rels" -- without it composer/lyricist come back empty.
            includes=RECORDING_INCLUDES
            + ["work-rels", "releases", "release-groups", "media", "artist-credits"],
        )
    except Exception:
        log.exception("recording lookup failed for %s", rec_id)
        return None


# MusicBrainz' special-purpose artist for various-artists compilations.
_VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"


def _format_artist_credit(credit: list | None) -> tuple[str, list[str]]:
    """Flatten an MB artist-credit into (display name, [artist ids]).

    Joins each credited part with its ``joinphrase`` so features/collabs read as
    MB wrote them ("Alice feat. Bob"); ids keep source order.
    """
    parts = credit or []
    name = "".join(
        (p.get("name") or (p.get("artist") or {}).get("name") or "")
        + (p.get("joinphrase") or "")
        for p in parts
    ).strip()
    ids = [aid for p in parts if (aid := (p.get("artist") or {}).get("id"))]
    return name, ids


def _year_of(date: str | None) -> int | None:
    if not date:
        return None
    try:
        return int(str(date)[:4])
    except ValueError:
        return None


def _track_numbering(release: dict) -> dict:
    """Track/disc position of the recording on this release.

    The recording lookup embeds only the matching recording's own track(s) under
    each release's media, so the first track of the first non-empty medium *is*
    this recording's entry -- no rec-id match needed. A non-numeric track number
    (vinyl "A1") is skipped rather than coerced. Disc total is not derivable (only
    the medium carrying the track is present), so it is omitted.
    """
    for medium in release.get("media") or []:
        tracks = medium.get("tracks") or medium.get("track") or []
        if not tracks:
            continue
        out: dict = {}
        num = tracks[0].get("number") or tracks[0].get("position")
        try:
            out["track"] = int(str(num))
        except (TypeError, ValueError):
            pass
        if medium.get("position") is not None:
            out["disc"] = medium["position"]
        if medium.get("track_count") is not None:
            out["tracktotal"] = medium["track_count"]
        return out
    return {}


def _album_fields(chosen: dict) -> dict | None:
    """Album-level tags for a singleton: identity from the release *group*,
    numbering from the chosen release.

    Album identity -- name, artist, year, type, group id -- is read from the
    release group, never the specific release. Every pressing and remaster of an
    album shares one group, so this is invariant to which physical release
    ``select_release`` happened to pick; the pressing roulette that used to decide
    the album *title* (a Japanese pressing, a 2016 remaster, a "printed in
    England" edition) can no longer touch it. Only the per-track numbering is
    release-specific, and it can only come from a release the recording is on.

    Deliberately omits every pressing-specific field -- ``mb_albumid``,
    ``albumdisambig``, ``catalognum``, ``barcode``, ``country``, ``label``,
    ``script``, ... A per-track-varying ``mb_albumid`` in particular fragments the
    album in players that key on it (Navidrome's default album PID is
    ``musicbrainz_albumid|...``); dropping it makes them fall back to the
    group-level ``(albumartistid, album, releasedate)`` we set identically on
    every track. Returns ``None`` when there's no usable release group.
    """
    rg = chosen.get("release_group") or {}
    if not rg.get("id"):
        return None
    data: dict = {"mb_releasegroupid": rg["id"]}
    if title := rg.get("title"):
        data["album"] = title
    name, ids = _format_artist_credit(rg.get("artist_credit") or chosen.get("artist_credit"))
    if name:
        data["albumartist"] = name
    if ids:
        data["mb_albumartistid"] = ids[0]
        data["comp"] = 1 if _VARIOUS_ARTISTS_ID in ids else 0
    if (year := _year_of(rg.get("first_release_date"))) is not None:
        data["year"] = year
    if primary := rg.get("primary_type"):
        data["albumtype"] = primary.lower()
        data["albumtypes"] = [t.lower() for t in [primary, *(rg.get("secondary_types") or [])]]
    data.update(_track_numbering(chosen))
    return data


# Cover Art Archive requires a descriptive User-Agent; it 404s (not errors) when a
# release group has no front image, which the guarded fetch treats as "no art".
_CAA_USER_AGENT = "muvult/1.1 (https://github.com/thedioxider)"


@lru_cache(maxsize=256)
def _fetch_cover_art(rgid: str) -> bytes | None:
    """Release-group front cover bytes from the Cover Art Archive, or None.

    Cached by release-group id so a bulk album upload fetches the shared cover
    once. Best-effort: any failure (no art, network, timeout) returns None.
    """
    url = f"https://coverartarchive.org/release-group/{rgid}/front-500"
    try:
        with urlopen(Request(url, headers={"User-Agent": _CAA_USER_AGENT}), timeout=15) as resp:
            return resp.read()
    except Exception:
        log.debug("cover art fetch failed for release-group %s", rgid)
        return None


def _image_ext(data: bytes) -> str:
    """Filename extension for image bytes by magic number; defaults to ``.jpg``."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".jpg"


@lru_cache(maxsize=256)
def _fetch_cover_art_full(rgid: str) -> tuple[bytes, str] | None:
    """Full-resolution release-group front cover from the CAA, as ``(bytes, ext)``.

    Unlike ``_fetch_cover_art`` (the 500px rendition used for embedding), this
    pulls the original scan (``/front``), whose format varies -- hence the
    detected extension. Cached by release-group id so a bulk album fetches once;
    best-effort (any failure returns None)."""
    url = f"https://coverartarchive.org/release-group/{rgid}/front"
    try:
        with urlopen(Request(url, headers={"User-Agent": _CAA_USER_AGENT}), timeout=30) as resp:
            data = resp.read()
    except Exception:
        log.debug("full cover art fetch failed for release-group %s", rgid)
        return None
    if not data:
        return None
    return data, _image_ext(data)


async def fetch_cover_art_full(rgid: str) -> tuple[bytes, str] | None:
    """Async wrapper for ``_fetch_cover_art_full`` (plain network, not a beets
    call -- runs on the default executor, not ``_beets_pool``)."""
    loop = get_running_loop()
    return await loop.run_in_executor(None, _fetch_cover_art_full, rgid)


def _embed_cover_art(file_path: Path, rgid: str) -> None:
    """Embed the release group's front cover into the file, best-effort."""
    data = _fetch_cover_art(rgid)
    if not data:
        return
    try:
        from mediafile import Image, ImageType, MediaFile

        mf = MediaFile(str(file_path))
        mf.images = [Image(data=data, type=ImageType.front)]
        mf.save()
    except Exception:
        log.debug("cover art embed failed for %s", file_path)


def _tag_acoustid(item: Item) -> None:
    """Stamp the file with its AcoustID id, if we fingerprinted it.

    The id identifies the audio itself, so it's written whenever the fingerprint
    lookup produced one -- independent of which candidate won -- making "was this
    fingerprinted?" answerable from the file's ``ACOUSTID_ID`` tag alone. chroma's
    ``_acoustids`` was populated by ``_fingerprint_item`` for this same path.
    """
    try:
        from beetsplug import chroma

        if aid := chroma._acoustids.get(item.path):
            item.acoustid_id = aid
    except Exception:
        pass


def _enrich_from_release(
    releases: list[dict],
    rec_id: str,
    track_artist: str | None,
    file_length_s: float | None,
) -> dict | None:
    """Pick the release the file most likely came from and derive album-level tags.

    ``select_release`` (length-aware) chooses the numbering release -- among the
    releases the recording actually appears on, so a deluxe-only bonus track is
    numbered from the deluxe edition it lives on. ``_album_fields`` then reads
    album identity from that release's *group* and the track number from the
    release itself. Returns ``None`` on any failure so the caller keeps the plain
    recording-level tags. Runs on ``_beets_pool``.
    """
    try:
        file_length_ms = int(file_length_s * 1000) if file_length_s else None
        chosen = select_release(releases, track_artist, file_length_ms)
        if chosen is None:
            return None
        return _album_fields(chosen)
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
    enriched = None
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
    _tag_acoustid(item)
    item.write(path=str(file_path))
    # Cover art rides on album identity: embed the release group's front image so a
    # symlinked pool (no per-album folder to drop a cover in) still shows art.
    if enriched and (rgid := enriched.get("mb_releasegroupid")):
        _embed_cover_art(file_path, rgid)
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
