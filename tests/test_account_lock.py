"""Real local locks, ordinary entry boundaries and legacy canary interoperability."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.live.node import TradingNode
from test_live_both_entry import _profile as both_profile
from test_live_maker_entry import _CREDENTIALS, _maker_profile
from test_live_taker_entry import _paper_profile

from py000_nautilus import account_lock, live_taker_entry
from py000_nautilus.bitfinex_v1_paper_canary import PaperCanaryError, _lock_canary
from py000_nautilus.live_both_entry import run_live_both_entry
from py000_nautilus.live_taker_canary import _lock_paper_account
from py000_nautilus.live_taker_entry import run_live_maker_entry, run_live_taker_entry


@pytest.fixture(autouse=True)
def private_lock_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(account_lock, "_LOCK_ROOT", tmp_path)


def test_legacy_canaries_share_the_ordinary_account_lock() -> None:
    with account_lock.lock_bitfinex_account(123):
        with pytest.raises(BlockingIOError):
            _lock_paper_account(123)
        with pytest.raises(PaperCanaryError):
            _lock_canary(123)
        with account_lock.lock_bitfinex_account(124):
            pass
    with _lock_canary(123), pytest.raises(BlockingIOError):
        account_lock.lock_bitfinex_account(123)


@pytest.mark.parametrize("rehearse", [False, True])
@pytest.mark.parametrize("venue", ["bitfinex", "mt5"])
@pytest.mark.parametrize("mode", ["taker", "maker", "both"])
def test_conflict_precedes_builder_and_state_writes(
    tmp_path: Path, mode: str, venue: str, rehearse: bool,
) -> None:
    entries: dict[str, tuple[Callable[..., Any], Callable[..., Any]]] = {
        "taker": (_paper_profile, run_live_taker_entry),
        "maker": (_maker_profile, run_live_maker_entry),
        "both": (both_profile, run_live_both_entry),
    }
    factory, run = entries[mode]
    profile: Any = factory(tmp_path / "different-state-namespace")
    source_id = profile.bitfinex_exec_config.user_id
    hedge_id = profile.mt5_exec_config.expected_account_id
    lock = (account_lock.lock_bitfinex_account(source_id) if venue == "bitfinex"
            else account_lock.lock_mt5_account(hedge_id))

    def forbidden(**_kwargs: Any) -> Any:
        pytest.fail("conflicting writer reached builder")

    with lock, pytest.raises(BlockingIOError):
        run(profile, rehearse=rehearse, run_paper=not rehearse,
            environment=_CREDENTIALS, node_builder=forbidden)
    assert not (tmp_path / "different-state-namespace").exists()
    # Also proves partial acquisition releases BFX when the second (MT5) lock fails.
    with account_lock.lock_bitfinex_account(source_id), account_lock.lock_mt5_account(hedge_id):
        pass


@pytest.mark.parametrize("fail_dispose", [False, True])
def test_locks_cover_run_and_dispose_then_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_dispose: bool,
) -> None:
    profile = _paper_profile(tmp_path)
    observed: list[str] = []
    real_dispose = live_taker_entry._dispose_node

    def verify(phase: str) -> None:
        with pytest.raises(BlockingIOError):
            account_lock.lock_bitfinex_account(profile.bitfinex_exec_config.user_id)
        with pytest.raises(BlockingIOError):
            account_lock.lock_mt5_account(profile.mt5_exec_config.expected_account_id)
        observed.append(phase)

    def run(_node: TradingNode, **_kwargs: Any) -> None:
        verify("run")

    def dispose(node: TradingNode) -> None:
        verify("dispose")
        real_dispose(node)
        if fail_dispose:
            raise RuntimeError("disposal test")

    monkeypatch.setattr(TradingNode, "run", run)
    monkeypatch.setattr(live_taker_entry, "_dispose_node", dispose)
    if fail_dispose:
        with pytest.raises(RuntimeError, match="disposal test"):
            run_live_taker_entry(profile, run_paper=True, environment=_CREDENTIALS)
    else:
        run_live_taker_entry(profile, run_paper=True, environment=_CREDENTIALS)
    assert observed == ["run", "dispose"]
    with (
        account_lock.lock_bitfinex_account(profile.bitfinex_exec_config.user_id),
        account_lock.lock_mt5_account(profile.mt5_exec_config.expected_account_id),
    ):
        pass


def test_process_exit_releases_lock_without_replacing_inode(tmp_path: Path) -> None:
    # Parent holds first; a fresh interpreter must fail, not load stale state.
    code = (
        f"from pathlib import Path; from py000_nautilus import account_lock; "
        f"account_lock._LOCK_ROOT = Path({str(tmp_path)!r}); "
        "from py000_nautilus.account_lock import lock_bitfinex_account; "
        "lock = lock_bitfinex_account(456)"
    )
    env = {**os.environ, "TMPDIR": str(tmp_path),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    path = tmp_path / "py000-bitfinex-paper-456.lock"
    with account_lock.lock_bitfinex_account(456):
        inode = path.stat().st_ino
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, timeout=15,
        )
        assert result.returncode != 0 and b"BlockingIOError" in result.stderr
    # Child exits without explicitly closing the acquired stream.
    result = subprocess.run(
        [sys.executable, "-c", code + "; import os; os._exit(0)"],
        env=env, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    with account_lock.lock_bitfinex_account(456):
        assert path.stat().st_ino == inode


@pytest.mark.parametrize("venue", ["bitfinex", "mt5"])
def test_default_root_excludes_same_account_across_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, venue: str,
) -> None:
    monkeypatch.setattr(account_lock, "_LOCK_ROOT", Path("/tmp"))
    unique_id = 990_000_000 + os.getpid()
    call = (f"lock_bitfinex_account({unique_id})" if venue == "bitfinex"
            else f"lock_mt5_account('test-lock-{unique_id}')")
    lock = (account_lock.lock_bitfinex_account(unique_id) if venue == "bitfinex"
            else account_lock.lock_mt5_account(f"test-lock-{unique_id}"))
    code = "from py000_nautilus.account_lock import *; lock = " + call
    environment = {**os.environ, "TMPDIR": str(tmp_path),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    with lock:
        result = subprocess.run(
            [sys.executable, "-c", code], env=environment, capture_output=True, timeout=15,
        )
        assert result.returncode != 0 and b"BlockingIOError" in result.stderr
    result = subprocess.run(
        [sys.executable, "-c", code], env=environment, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
