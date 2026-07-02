import asyncio
import os
import shutil
from asyncio import get_running_loop
from pathlib import Path

from beets import config as beets_config, plugins
from beets.autotag.match import tag_item
from beets.library import Library
from beets.library.models import Item

from .models import Candidate, TagResult

_BEETS_DB = "/data/beets_pool.db"
_lib: Library | None = None


def setup_beets(music_root: str) -> None:
    global _lib
    pool_path = str(Path(music_root) / ".pool")
    beets_config.read(user=False, defaults=True)
    beets_config["plugins"].set(["musicbrainz"])
    beets_config["musicbrainz"]["search_limit"].set(10)
    plugins.load_plugins()
    from .beets_patches import patch_mb_search
    patch_mb_search()
    beets_config["asciify_paths"].set(True)
    beets_config["paths"]["default"].set("$albumartist/$album/$track - $title")
    beets_config["paths"]["singleton"].set("$albumartist/$album/$track - $title")
    _lib = Library(_BEETS_DB, directory=pool_path)


async def get_candidates(file_path: Path) -> TagResult:
    loop = get_running_loop()
    return await loop.run_in_executor(None, _get_candidates_sync, file_path)


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


async def apply_and_move(file_path: Path, candidate: Candidate) -> Path:
    loop = get_running_loop()
    return await loop.run_in_executor(None, _apply_and_move_sync, file_path, candidate)


def _apply_and_move_sync(file_path: Path, candidate: Candidate) -> Path:
    match = candidate._match
    match.apply_metadata()
    item = match.item
    item.write(path=str(file_path))
    item.add(_lib)
    dest = Path(os.fsdecode(item.destination()))
    if dest.exists():
        dest.unlink()
    item.move(store=True)
    return Path(os.fsdecode(item.path))


async def move_as_is(file_path: Path, username: str) -> Path:
    loop = get_running_loop()
    return await loop.run_in_executor(None, _move_as_is_sync, file_path, username)


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
