"""POSIX privilege-drop helpers for root orchestrator / agy-agent worker separation."""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path

from antigravity_core.config.static_config import PathsStaticConfig


def agent_exec_identity(paths: PathsStaticConfig) -> tuple[int | None, int | None]:
    """Returns (uid, gid) to drop privileges to agy-agent when running as root, else (None, None)."""
    if os.geteuid() == 0:
        return paths.agent_uid, paths.agent_gid
    return None, None


@contextmanager
def as_agent_identity(paths: PathsStaticConfig) -> Iterator[None]:
    """Temporarily switches effective UID/GID to agy-agent so created files are born agent-owned."""
    if os.geteuid() != 0:
        yield
        return
    prev_uid = os.geteuid()
    prev_gid = os.getegid()
    os.setegid(paths.agent_gid)
    try:
        os.seteuid(paths.agent_uid)
        try:
            yield
        finally:
            os.seteuid(prev_uid)
    finally:
        os.setegid(prev_gid)


def chown_path(path: Path, uid: int, gid: int) -> None:
    """Changes ownership of a single path (without following symlinks) when running as root."""
    if os.geteuid() != 0:
        return
    if path.is_symlink():
        os.lchown(path, uid, gid)
    elif path.exists():
        os.chown(path, uid, gid)
