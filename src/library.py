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
    absorb_user_lyrics,
    create_symlink,
    ensure_cover_symlink,
    find_cover,
    link_path_for,
    links_to,
    pool_rel,
    promote_pool_file,
    reconcile_sidecars,
    remove_link_sidecars,
    remove_pool_file,
    remove_sidecars,
    remove_symlink,
    update_symlinks,
    user_library_root,
)

log = logging.getLogger(__name__)


async def promote_and_relink(session, pool_root: Path, track: Track, staged: Path, dest: Path) -> Path:
    """Move ``staged`` onto ``dest`` and reconcile the pool with the canonical path.

    When the winning tags map to the *same* path the file is overwritten in place and
    every owner's symlink stays valid. When they map elsewhere (e.g. corrected album
    identity) the old pool file is dropped, all owners' symlinks are repointed, and
    ``track.pool_path`` is updated. Returns the final pool path."""
    old_pool = pool_root / track.pool_path
    ownerships = (
        await session.exec(select(TrackOwnership).where(TrackOwnership.track_id == track.id))
    ).all()
    # A replacement (quality upgrade, re-tag, or move) invalidates any existing
    # lyrics -- the new file carries none, and stale lyrics for a superseded
    # recording are worse than none -- so purge them everywhere before promoting.
    # On a path change the relink below finds no pool sidecars to re-link; on an
    # in-place overwrite (no relink) this is the only thing that clears them.
    remove_sidecars(old_pool)
    for o in ownerships:
        remove_link_sidecars(Path(o.symlink_path))
    pool_file = promote_pool_file(staged, dest)
    if pool_file != old_pool:
        old_links = [Path(o.symlink_path) for o in ownerships]
        new_links = update_symlinks(old_pool, pool_file, old_links)
        remove_pool_file(old_pool)
        for ownership, new_l in zip(ownerships, new_links):
            ownership.symlink_path = str(new_l)
    track.pool_path = pool_rel(pool_file)
    return pool_file


async def recreate_links(username: str | None = None) -> tuple[int, list[str]] | None:
    """Reconcile every owner's library against the pool, idempotently.

    Rebuilds a track symlink only when it is missing, stale, or mispointed
    (keeping the track's lyrics in place -- a repair is not a removal); otherwise
    leaves it. Then, per affected track, reconciles lyrics across **all** its
    owners: a real lyrics file that appeared in some library is absorbed into the
    pool (`absorb_user_lyrics`) and shared to everyone, pool sidecars are linked
    where missing, and stale links into the pool are pruned. Reaches the same end
    state as a full wipe-and-recreate without the SSD churn -- so it is safe to
    run daily. Returns ``(rebuilt_count, missing_pool_paths)``, or ``None`` when a
    named user does not exist."""
    from .config import settings

    music_root = Path(settings.music_root)
    pool_root = music_root / ".pool"
    count = 0
    missing: list[str] = []
    async with get_session() as session:
        if username:
            user = (await session.exec(select(User).where(User.username == username))).first()
            if not user:
                return None
            users = [user]
        else:
            users = (await session.exec(select(User))).all()

        affected_track_ids: set[int] = set()
        for user in users:
            user_dir = music_root / user.username
            rows = (
                await session.exec(
                    select(TrackOwnership, Track)
                    .join(Track, Track.id == TrackOwnership.track_id)
                    .where(TrackOwnership.user_id == user.id)
                )
            ).all()
            for ownership, track in rows:
                pool_file = pool_root / track.pool_path
                if not pool_file.exists():
                    missing.append(track.pool_path)
                    continue
                desired = link_path_for(pool_file, user_dir, flat=track.is_asis)
                current = Path(ownership.symlink_path)
                if current != desired or not links_to(current, pool_file):
                    # A repair, not a removal -- keep the track's lyrics.
                    remove_symlink(current, user_library_root(current, music_root), remove_lyrics=False)
                    new_link = create_symlink(pool_file, user_dir, flat=track.is_asis)
                    ownership.symlink_path = str(new_link)
                    count += 1
                affected_track_ids.add(track.id)

        # Lyrics run across *all* owners of each affected track (not just the
        # filtered users), so a lyrics found in one library is shared to everyone.
        for track_id in affected_track_ids:
            track = (await session.exec(select(Track).where(Track.id == track_id))).first()
            pool_file = pool_root / track.pool_path
            if not pool_file.exists():
                continue
            owns = (
                await session.exec(
                    select(User)
                    .join(TrackOwnership, TrackOwnership.user_id == User.id)
                    .where(TrackOwnership.track_id == track_id)
                )
            ).all()
            owner_dirs = [
                link_path_for(pool_file, music_root / u.username, flat=track.is_asis).parent
                for u in owns
            ]
            absorb_user_lyrics(pool_file, owner_dirs)
            for d in owner_dirs:
                reconcile_sidecars(pool_file, d)
        await session.commit()

    return count, missing


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
