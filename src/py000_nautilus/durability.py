"""Small cross-platform primitives for durable local state replacement."""

from __future__ import annotations

import os
from pathlib import Path

_DIRECTORY_FSYNC_SUPPORTED = os.name == "posix"


class ParentDirectorySyncError(OSError):
    """The destination was replaced, but its directory entry may not be durable."""


def replace_and_sync_parent(source: str | Path, destination: str | Path) -> None:
    """Atomically replace a file and sync its parent directory where supported.

    Python exposes a portable file ``fsync`` on Windows, but not a portable way
    to open and sync a directory. On non-POSIX platforms this therefore retains
    the caller's file-fsync plus ``os.replace`` guarantee without overstating
    parent-directory durability.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    directory_fd: int | None = None
    close_error: OSError | None = None
    if _DIRECTORY_FSYNC_SUPPORTED:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(destination_path.parent, flags)
    try:
        os.replace(source_path, destination_path)
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                raise ParentDirectorySyncError(
                    f"replacement completed but parent directory sync failed: "
                    f"{destination_path.parent}"
                ) from exc
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError as exc:
                close_error = exc
    if close_error is not None:
        raise ParentDirectorySyncError(
            f"replacement completed but parent directory close failed: "
            f"{destination_path.parent}"
        ) from close_error
