"""Cooperative same-host account exclusion for the trusted POSIX runner.

Never unlink lock files: replacing an inode can admit two concurrent writers.
Profiles, state paths, strategy IDs and MT5 magic are deliberately not lock keys.
"""

import fcntl
import hashlib
import os
from pathlib import Path
from typing import TextIO

_LOCK_ROOT = Path("/tmp")  # Stable across shells, launchers and TMPDIR overrides.


def lock_bitfinex_account(user_id: int) -> TextIO:
    """All current canaries share this root; stop older installations before upgrade."""
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Bitfinex lock requires a positive user ID")
    return _lock(f"py000-bitfinex-paper-{user_id}.lock")


def lock_mt5_account(account_id: str) -> TextIO:
    if not isinstance(account_id, str) or not account_id or account_id != account_id.strip():
        raise ValueError("MT5 lock requires the canonical expected account identity")
    key = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    return _lock(f"py000-mt5-account-{key}.lock")


def _lock(name: str) -> TextIO:
    descriptor = os.open(_LOCK_ROOT / name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "a+", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        stream.close()
        raise
    return stream
