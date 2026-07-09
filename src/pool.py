import os
import shutil
from pathlib import Path

# Folder cover art (``front.<ext>``): one real image per album in the pool,
# symlinked into each owner's album folder so Navidrome prefers it over the
# embedded 500px art. Restricted to image suffixes so a track literally titled
# "front" can never be mistaken for a cover during pruning.
_COVER_STEM = "front"
_COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Sidecar lyrics files: zero or more per track, sharing the track file's exact
# stem in the same folder (``Title.flac`` -> ``Title.lrc``). Each owner's library
# mirrors the pool's sidecars for a track -- they are linked alongside the track,
# reconciled on ``recreatelinks``, and removed with it (a replacement/move drops
# them all). ``.txt``/``.yaml`` are broad, but the exact-stem match keeps them
# tied to one track.
_LYRICS_EXTS = {".elrc", ".lrc", ".txt"}


def _is_cover(path: Path) -> bool:
    return path.stem.lower() == _COVER_STEM and path.suffix.lower() in _COVER_EXTS


def _is_sidecar(path: Path, stem: str) -> bool:
    return path.stem == stem and path.suffix.lower() in _LYRICS_EXTS


def find_sidecars(pool_file: Path) -> list[Path]:
    """Real lyrics files in the pool sharing ``pool_file``'s exact stem."""
    try:
        return sorted(
            e for e in pool_file.parent.iterdir()
            if not e.is_symlink() and e.is_file() and _is_sidecar(e, pool_file.stem)
        )
    except OSError:
        return []


def _library_lyrics(link_parent: Path, stem: str) -> list[Path]:
    """Lyrics entries in a user's album dir for ``stem`` -- our symlinks *and*
    real files a lyrics plugin may have written (dangling links included)."""
    try:
        return [e for e in link_parent.iterdir() if not e.is_dir() and _is_sidecar(e, stem)]
    except OSError:
        return []


def remove_sidecars(pool_file: Path) -> None:
    """Delete the real pool lyrics files sharing ``pool_file``'s stem."""
    for sc in find_sidecars(pool_file):
        sc.unlink(missing_ok=True)


def remove_link_sidecars(track_link: Path) -> None:
    """Remove every lyrics entry (link or real plugin file) beside a track link."""
    for e in _library_lyrics(track_link.parent, track_link.stem):
        e.unlink(missing_ok=True)


def _points_into_pool(link: Path, pool_root: Path) -> bool:
    """Whether a symlink's target path lands inside the pool (target need not exist)."""
    try:
        target = Path(os.path.normpath(link.parent / os.readlink(link)))
        target.relative_to(pool_root)
        return True
    except (OSError, ValueError):
        return False


def absorb_user_lyrics(pool_file: Path, link_parents: list[Path]) -> None:
    """Move a real, not-yet-pooled lyrics file from any owner's library into the
    pool so it can be shared. Named by the track stem it already carries; the file
    is left in the pool for a following ``reconcile_sidecars`` to link back. First
    occurrence of a given filename wins; symlinks (already ours) are skipped."""
    pool_dir = pool_file.parent
    for parent in link_parents:
        for e in _library_lyrics(parent, pool_file.stem):
            if e.is_symlink():
                continue
            dest = pool_dir / e.name
            if not dest.exists():
                shutil.move(str(e), str(dest))


def reconcile_sidecars(pool_file: Path, link_parent: Path) -> None:
    """Reconcile ``link_parent``'s lyrics for this track against the pool.

    Links a pool sidecar the library lacks, and converts a same-named real file
    (one a lyrics plugin wrote) or a symlink pointing elsewhere into a symlink to
    the pool file -- so a lyrics present in both places is always the pool's. Also
    drops a **stale** lyrics symlink into the pool whose target is gone. It never
    removes a library lyrics *real file* with no pool counterpart -- that is the
    user's own and only goes when the track itself does (`remove_link_sidecars`).
    Idempotent -- an already-correct link is left untouched."""
    pool_root = _pool_root(pool_file)
    desired = {sc.name: sc for sc in find_sidecars(pool_file)}
    for name, sc in desired.items():
        target = link_parent / name
        if target.is_symlink():
            try:
                if target.resolve() == sc.resolve():
                    continue
            except OSError:
                pass  # dangling/broken -> repoint below
            target.unlink()
        elif target.exists():
            target.unlink()  # real same-name file -> replace with a link to the pool
        target.symlink_to(os.path.relpath(sc, link_parent))
    for e in _library_lyrics(link_parent, pool_file.stem):
        # stale link into the pool (its sidecar was removed) -> prune; a real file
        # or a link pointing outside the pool is the user's own and stays.
        if e.name not in desired and e.is_symlink() and _points_into_pool(e, pool_root):
            e.unlink(missing_ok=True)


def links_to(link: Path, pool_file: Path) -> bool:
    """Whether ``link`` is a symlink resolving to ``pool_file`` (idempotence check)."""
    try:
        return link.is_symlink() and link.resolve() == pool_file.resolve()
    except OSError:
        return False


def find_cover(album_dir: Path) -> Path | None:
    """The album folder's ``front.<ext>`` cover (file or symlink), or None."""
    try:
        for entry in album_dir.iterdir():
            if _is_cover(entry):
                return entry
    except OSError:
        pass
    return None


def ensure_cover_symlink(pool_cover: Path, user_album_dir: Path) -> Path | None:
    """Relative ``front.<ext>`` symlink in the user's album folder -> pool cover.

    Idempotent: a no-op if the folder already has any ``front.*``. The album dir
    is created if missing (it normally exists, holding the user's track links)."""
    if find_cover(user_album_dir) is not None:
        return None
    user_album_dir.mkdir(parents=True, exist_ok=True)
    link = user_album_dir / pool_cover.name
    link.symlink_to(os.path.relpath(pool_cover, user_album_dir))
    return link


def _pool_root(pool_file: Path) -> Path:
    root = pool_file.parent
    while root.name != ".pool":
        root = root.parent
    return root


def pool_rel(pool_file: Path) -> str:
    return str(pool_file.relative_to(_pool_root(pool_file)))


def link_path_for(pool_file: Path, user_dir: Path, *, flat: bool = False) -> Path:
    """Where a track's symlink lives in ``user_dir`` -- without creating it."""
    relative = Path(pool_file.name) if flat else pool_file.relative_to(_pool_root(pool_file))
    return user_dir / relative


def create_symlink(pool_file: Path, user_dir: Path, *, flat: bool = False) -> Path:
    link_path = link_path_for(pool_file, user_dir, flat=flat)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(os.path.relpath(pool_file, link_path.parent))
    reconcile_sidecars(pool_file, link_path.parent)
    return link_path


def _remove_lone_cover(directory: Path) -> None:
    """Drop a ``front.<ext>`` cover that is a directory's only remaining entry.

    A cover file/symlink would otherwise keep an album dir non-empty and block
    pruning after its last track leaves, orphaning itself. Removing it only when
    it is *alone* means a dir with tracks still in it keeps its cover untouched."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    if entries and all(_is_cover(e) for e in entries):
        for e in entries:
            e.unlink()


def _cleanup_empty_parents(path: Path, stop_at: Path | None = None) -> None:
    parent = path.parent
    while stop_at is None or parent != stop_at:
        _remove_lone_cover(parent)
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def user_library_root(symlink_path: Path, music_root: Path) -> Path:
    """The user's library dir: the first path component under ``music_root``."""
    return music_root / symlink_path.relative_to(music_root).parts[0]


def remove_symlink(
    symlink_path: Path, library_root: Path | None = None, *, remove_lyrics: bool = True
) -> None:
    if symlink_path.is_symlink():
        symlink_path.unlink()
        # The track's lyrics go with it (both our links and any real plugin
        # file), so a user's lyrics never outlive the track they belong to.
        # ``remove_lyrics=False`` keeps them for a relocation/repair (recreatelinks).
        if remove_lyrics:
            remove_link_sidecars(symlink_path)
        # Prune now-empty album/artist dirs, but keep the user's library dir
        # itself (removing it on the last track would orphan the Navidrome
        # library). ``library_root`` is that boundary; None keeps old behavior.
        _cleanup_empty_parents(symlink_path, library_root)


def promote_pool_file(staged: Path, dest: Path) -> Path:
    """Move a staged import onto its canonical pool path, replacing any file there.

    A no-op when ``staged`` already is ``dest`` (nothing collided at tag time).
    Called only once the caller has decided this copy wins.
    """
    if staged == dest:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(staged), str(dest))
    return dest


def remove_pool_file(pool_file: Path) -> None:
    if pool_file.exists():
        remove_sidecars(pool_file)  # drop the track's real lyrics files with it
        pool_file.unlink()
        _cleanup_empty_parents(pool_file, _pool_root(pool_file))


def update_symlinks(old_pool: Path, new_pool: Path, symlink_paths: list[Path]) -> list[Path]:
    new_links = []
    for link in symlink_paths:
        user_dir = _find_user_dir(link, old_pool)
        remove_symlink(link, user_dir)
        new_link = create_symlink(new_pool, user_dir)
        new_links.append(new_link)
    return new_links


def _find_user_dir(link: Path, pool_file: Path) -> Path:
    relative = pool_file.relative_to(_pool_root(pool_file))
    parts_count = len(relative.parts)
    user_dir = link
    for _ in range(parts_count):
        user_dir = user_dir.parent
    return user_dir
