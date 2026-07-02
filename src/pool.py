import os
import shutil
from pathlib import Path


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


def _cleanup_empty_parents(path: Path, stop_at: Path | None = None) -> None:
    parent = path.parent
    while stop_at is None or parent != stop_at:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def remove_symlink(symlink_path: Path) -> None:
    if symlink_path.is_symlink():
        symlink_path.unlink()
        _cleanup_empty_parents(symlink_path)


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
        remove_symlink(link)
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
