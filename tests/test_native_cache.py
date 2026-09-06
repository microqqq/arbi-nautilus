"""Opt-in test: PY000_NATIVE_CACHE_PORT must name a fresh, disposable local Redis.

No Docker/Redis installation, startup, namespace deletion or external trading is
performed by this module. Ordinary pytest skips it explicitly when not selected.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest


def _worker(kind: str, mode: str, port: int, state_dir: Path) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, str(root / "tests/native_cache_worker.py"),
         kind, mode, str(port), str(state_dir)],
        cwd=root, env=os.environ | {"PYTHONPATH": f"{root / 'src'}{os.pathsep}{root / 'tests'}"},
        text=True, capture_output=True, timeout=20,
    )


def _result(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert process.returncode == 0, (process.stdout + process.stderr)[-6000:]
    lines = [line.removeprefix("W6A_JSON=") for line in process.stdout.splitlines()
             if line.startswith("W6A_JSON=")]
    assert len(lines) == 1
    return cast(dict[str, Any], json.loads(lines[0]))


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_ordinary_builder_native_redis_survives_a_fresh_process(
    tmp_path: Path, kind: str,
) -> None:
    selected_port = os.environ.get("PY000_NATIVE_CACHE_PORT")
    if selected_port is None:
        pytest.skip("explicit disposable Redis test not selected (PY000_NATIVE_CACHE_PORT unset)")
    port = int(selected_port)
    assert 0 < port <= 65535
    original = _result(_worker(kind, "produce", port, tmp_path))
    restored = _result(_worker(kind, "load", port, tmp_path))
    assert len({original["pid"], restored["pid"], os.getpid()}) == 3
    assert original["integrity"] is restored["integrity"] is True
    assert restored["event_count"] == 0  # Native database load, not replay through the live queue.
    assert restored["snapshot"] == original["snapshot"]
    assert restored["accounts"] == original["accounts"] == ["BITFINEX-269312", "MT5-12345678"]
    assert restored["account_balances"] == original["account_balances"]
    assert {key: Decimal(value) for key, value in restored["account_balances"].items()} == {
        "BITFINEX-269312": Decimal(1_000_000), "MT5-12345678": Decimal(1_000_000),
    }
    assert restored["trader_id"] == original["trader_id"] == f"PY000-{kind.upper()}-LIVE-001"
    assert restored["strategy_id"] == original["strategy_id"]
    orders, positions = restored["snapshot"]["orders"], restored["snapshot"]["positions"]
    assert set(orders) == {"W6A-SOURCE", "W6A-HEDGE", "W6A-CLOSE"}
    assert set(restored["order_traders"].values()) == {restored["trader_id"]}
    assert {order["strategy"] for order in orders.values()} == {restored["strategy_id"]}
    assert {position["strategy"] for position in positions.values()} == {restored["strategy_id"]}
    assert len(positions) == 2
    source_position = positions[orders["W6A-SOURCE"]["position_id"]]
    assert Decimal(source_position["signed_qty"]) == Decimal("0.5")
    assert Decimal(positions["900000101"]["signed_qty"]) == -1
    assert Decimal(orders["W6A-SOURCE"]["filled"]) == Decimal("0.5")
    assert orders["W6A-SOURCE"]["status"] == "PARTIALLY_FILLED"
    assert orders["W6A-SOURCE"]["trades"] == ["800000101"]
    assert orders["W6A-SOURCE"]["fill_info"] == [{
        "bitfinex_fill_source": "te_paper", "bitfinex_fee_status": "pending",
    }]
    close = orders["W6A-CLOSE"]
    assert close["position_id"] == "900000101" and close["client_id"] == "MT5"
    assert close["reduce_only"] is True and close["status"] == "ACCEPTED"
    assert Decimal(close["filled"]) == 0 and close["trades"] == []
    assert restored["source_ready"] is False  # Gate isolation has separate unit positive controls.

    duplicate = _result(_worker(kind, "duplicate", port, tmp_path))
    assert duplicate["snapshot"] == restored["snapshot"]
    assert duplicate["event_count"] == 1
    increment = _result(_worker(kind, "new-trade", port, tmp_path))
    assert Decimal(increment["snapshot"]["orders"]["W6A-SOURCE"]["filled"]) == 1
    assert Decimal(increment["snapshot"]["positions"][orders["W6A-SOURCE"]["position_id"]]
                   ["signed_qty"]) == 1
    reloaded = _result(_worker(kind, "load", port, tmp_path))
    assert reloaded["snapshot"] == increment["snapshot"]  # The new native update also persisted.

    wrong = _worker(kind, "wrong-account", port, tmp_path)
    assert wrong.returncode != 0
    assert "native cache contains an account outside" in wrong.stderr
    assert _result(_worker(kind, "load", port, tmp_path))["snapshot"] == reloaded["snapshot"]
    missing_position = _result(_worker(kind, "omit-position", port, tmp_path))
    incomplete_close = missing_position["snapshot"]["orders"]["W6A-NO-POSITION"]
    assert missing_position["integrity"] is True
    assert incomplete_close["client_id"] == "MT5" and incomplete_close["position_id"] is None
    assert incomplete_close["reduce_only"] is True and incomplete_close["status"] == "ACCEPTED"
    incomplete = _worker(kind, "load", port, tmp_path)
    assert incomplete.returncode != 0
    assert ("native cache MT5 reduce-only order requires a position index: W6A-NO-POSITION"
            in incomplete.stderr)
    assert _result(_worker(kind, "restore-fixture-position", port, tmp_path)) == {
        "fixture_position_restored": True,
    }
    repaired = _result(_worker(kind, "load", port, tmp_path))
    missing_position["snapshot"]["orders"]["W6A-NO-POSITION"]["position_id"] = "900000101"
    assert repaired["snapshot"] == missing_position["snapshot"]
    _result(_worker(kind, "omit-client", port, tmp_path))
    incomplete = _worker(kind, "load", port, tmp_path)
    assert incomplete.returncode != 0
    assert "native cache order client index mismatch: W6A-NO-CLIENT" in incomplete.stderr
    with socket.socket() as unavailable:
        # Reserved, but not listening; never hit an existing Redis.
        unavailable.bind(("127.0.0.1", 0))
        failed = _worker(kind, "load", unavailable.getsockname()[1], tmp_path)
    assert failed.returncode != 0 and "W6A_JSON=" not in failed.stdout
    assert "CacheDatabaseAdapter.__init__" in failed.stderr
