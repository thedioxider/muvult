import os
import shutil
from pathlib import Path

# Folder cover art (``front.<ext>``): one real image per album in the pool,
# symlinked into each owner's album folder so Navidrome prefers it over the
# embedded 500px art. Restricted to image suffixes so a track literally titled
# "front" can never be mistaken for a cover during pruning.
_COVER_STEM = "front"
_COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _is_cover(path: Path) -> bool:
    return path.stem.lower() == _COVER_STEM and path.suffix.lower() in _COVER_EXTS


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


def create_symlink(pool_file: Path, user_dir: Path, *, flat: bool = False) -> Path:
    relative = Path(pool_file.name) if flat else pool_file.relative_to(_pool_root(pool_file))
    link_path = user_dir / relative
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(os.path.relpath(pool_file, link_path.parent))
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


def remove_symlink(symlink_path: Path, library_root: Path | None = None) -> None:
    if symlink_path.is_symlink():
        symlink_path.unlink()
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
