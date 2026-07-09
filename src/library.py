"""Pool/ownership orchestration shared by the upload and retag flows.

These helpers sit above ``pool.py`` (pure filesystem) and touch the ORM: moving a
staged file into the pool while keeping every owner's symlink and the ``Track`` row
in sync, and materialising an album's folder cover for all its owners.
"""

import logging
from pathlib import Path

from sqlmodel import select

from .beets_svc import fetch_cover_art_full
from .db import Track, TrackOwnership, User, get_session
from .pool import (
    ensure_cover_symlink,
    find_cover,
    pool_rel,
    promote_pool_file,
    remove_pool_file,
    update_symlinks,
)

log = logging.getLogger(__name__)


async def promote_and_relink(session, pool_root: Path, track: Track, staged: Path, dest: Path) -> Path:
    """Move ``staged`` onto ``dest`` and reconcile the pool with the canonical path.

    When the winning tags map to the *same* path the file is overwritten in place and
    every owner's symlink stays valid. When they map elsewhere (e.g. corrected album
    identity) the old pool file is dropped, all owners' symlinks are repointed, and
    ``track.pool_path`` is updated. Returns the final pool path."""
    old_pool = pool_root / track.pool_path
    pool_file = promote_pool_file(staged, dest)
    if pool_file != old_pool:
        ownerships = (
            await session.exec(select(TrackOwnership).where(TrackOwnership.track_id == track.id))
        ).all()
        old_links = [Path(o.symlink_path) for o in ownerships]
        new_links = update_symlinks(old_pool, pool_file, old_links)
        remove_pool_file(old_pool)
        for ownership, new_l in zip(ownerships, new_links):
            ownership.symlink_path = str(new_l)
    track.pool_path = pool_rel(pool_file)
    return pool_file


async def album_owner_usernames(session, rel_album: str) -> list[str]:
    """Usernames of every user owning any track in the given album folder.

    ``rel_album`` is the album dir relative to the pool root (``<albumartist>/
    <album>``); tracks are matched by that path prefix. Drives the cover fan-out
    so a newly-created album cover is linked into all current owners' libraries."""
    prefix = rel_album + "/"
    tracks = (
        await session.exec(select(Track).where(Track.pool_path.startswith(prefix, autoescape=True)))
    ).all()
    track_ids = [t.id for t in tracks]
    if not track_ids:
        return []
    owns = (
        await session.exec(select(TrackOwnership).where(TrackOwnership.track_id.in_(track_ids)))
    ).all()
    user_ids = {o.user_id for o in owns}
    if not user_ids:
        return []
    users = (await session.exec(select(User).where(User.id.in_(user_ids)))).all()
    return [u.username for u in users]


async def ensure_album_cover(rgid: str, pool_file: Path, *, force: bool = False) -> None:
    """Fetch (once) and link the album's full-res folder cover into every owner.

    The pool holds one real ``front.<ext>`` per album (fetched from the CAA on
    first need); each owner's album folder gets a relative symlink to it, which
    Navidrome prefers over the embedded 500px art. Fanning out to *all* current
    owners -- not just the uploader -- means a user who imported the album while
    art was missing gets covered the moment anyone re-triggers the fetch. Every
    step is idempotent, so duplicates and re-uploads self-heal missing links.

    ``force`` drops the existing pool cover and owners' cover symlinks first, so a
    fresh image is fetched and relinked (used by ``/retag`` to refresh art)."""
    from .config import settings

    music_root = Path(settings.music_root)
    album_dir = pool_file.parent
    rel_album = str(Path(pool_rel(pool_file)).parent)
    if force:
        if (existing := find_cover(album_dir)) is not None:
            existing.unlink(missing_ok=True)
        async with get_session() as session:
            for uname in await album_owner_usernames(session, rel_album):
                if (link := find_cover(music_root / uname / rel_album)) is not None:
                    link.unlink(missing_ok=True)
    cover = find_cover(album_dir)
    if cover is None:
        result = await fetch_cover_art_full(rgid)
        if result is None:
            return
        data, ext = result
        cover = find_cover(album_dir)  # re-check: a concurrent import may have won
        if cover is None:
            cover = album_dir / f"front{ext}"
            cover.write_bytes(data)
    async with get_session() as session:
        usernames = await album_owner_usernames(session, rel_album)
    for uname in usernames:
        ensure_cover_symlink(cover, music_root / uname / rel_album)
