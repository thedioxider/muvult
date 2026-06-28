from pathlib import Path


def create_symlink(pool_file: Path, user_dir: Path) -> Path:
    pool_root = pool_file.parent
    while pool_root.name != ".pool":
        pool_root = pool_root.parent

    relative = pool_file.relative_to(pool_root)
    link_path = user_dir / relative
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(pool_file.resolve())
    return link_path


def remove_symlink(symlink_path: Path) -> None:
    if symlink_path.is_symlink():
        symlink_path.unlink()


def remove_pool_file(pool_file: Path) -> None:
    if pool_file.exists():
        pool_file.unlink()


def update_symlinks(old_pool: Path, new_pool: Path, symlink_paths: list[Path]) -> list[Path]:
    new_links = []
    for link in symlink_paths:
        user_dir = _find_user_dir(link, old_pool)
        remove_symlink(link)
        new_link = create_symlink(new_pool, user_dir)
        new_links.append(new_link)
    return new_links


def _find_user_dir(link: Path, pool_file: Path) -> Path:
    pool_root = pool_file.parent
    while pool_root.name != ".pool":
        pool_root = pool_root.parent

    relative = pool_file.relative_to(pool_root)
    parts_count = len(relative.parts)
    user_dir = link
    for _ in range(parts_count):
        user_dir = user_dir.parent
    return user_dir
