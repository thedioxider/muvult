import asyncio
import os
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
    beets_config["musicbrainz"]["searchlimit"].set(10)
    plugins.load_plugins()
    from .beets_patches import patch_mb_phrase_search
    patch_mb_phrase_search()
    beets_config["asciify_paths"].set(True)
    beets_config["paths"]["default"].set("$albumartist/$album/$track - $title")
    beets_config["paths"]["singleton"].set("$albumartist/$album/$track - $title")
    _lib = Library(_BEETS_DB, directory=pool_path)


async def get_candidates(file_path: Path) -> TagResult:
    loop = get_running_loop()
    return await loop.run_in_executor(None, _get_candidates_sync, file_path)


def _get_candidates_sync(file_path: Path) -> TagResult:
    item = Item.from_path(str(file_path))
    proposal = tag_item(item)
    candidates = []
    for i, match in enumerate(proposal.candidates[:6]):
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
    file_path.rename(dest)
    return dest
