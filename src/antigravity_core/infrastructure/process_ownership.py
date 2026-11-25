"""POSIX ownership and privilege-drop helpers for root orchestrator / agy-agent worker separation."""

import os
from pathlib import Path

from antigravity_core.config.static_config import PathsStaticConfig


def agent_exec_identity(paths: PathsStaticConfig) -> tuple[int | None, int | None]:
    """Returns (uid, gid) to drop privileges to agy-agent when running as root, else (None, None)."""
    if os.geteuid() == 0:
        return paths.agent_uid, paths.agent_gid
    return None, None


def chown_path(path: Path, uid: int, gid: int) -> None:
    """Changes ownership of a single path (without following symlinks) when running as root."""
    if os.geteuid() != 0:
        return
    if path.is_symlink():
        os.lchown(path, uid, gid)
    elif path.exists():
        os.chown(path, uid, gid)


def chown_tree(root: Path, uid: int, gid: int) -> None:
    """Recursively changes ownership of a directory tree when running as root."""
    if os.geteuid() != 0 or not root.exists():
        return
    chown_path(root, uid, gid)
    if root.is_dir() and not root.is_symlink():
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dp = Path(dirpath)
            chown_path(dp, uid, gid)
            for name in dirnames:
                chown_path(dp / name, uid, gid)
            for name in filenames:
                chown_path(dp / name, uid, gid)


def chown_parents_up_to(leaf: Path, stop_root: Path, uid: int, gid: int) -> None:
    """Changes ownership of leaf and its parent directories up to and including stop_root."""
    if os.geteuid() != 0:
        return
    stop_resolved = stop_root.resolve()
    curr = leaf
    while True:
        chown_path(curr, uid, gid)
        if curr.resolve() == stop_resolved or curr.parent == curr:
            break
        curr = curr.parent
