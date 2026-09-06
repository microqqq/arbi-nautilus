"""Ordinary SIGKILL/restart representatives, not external venue/crash certification.

Opt in with PY000_RESTART_PROCESS_TESTS=1. Each case owns one disposable local
Redis, never flushes a namespace, and keeps the synthetic venue outside native
state. The default uses the same source tree as pytest. Optional
PY000_RESTART_PYTHON selects an installed wheel's interpreter and removes
PYTHONPATH. Both run outside the repository; source mode is not artifact proof.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

_REDIS = "sha256:49898aa0365c1577275e77a57b9ffa8a6ebd9d13eb981610a679e64185eaf0f2"
_WORKER = Path(__file__).with_name("ordinary_restart_worker.py")
_HELD = {"source-reserved", "request-pending", "old-hold", "recovery-killed",
         "hedge-rejected-held", "hedge-rejected-no-quote"}


@pytest.fixture
def restart_redis() -> Iterator[int]:
    if os.environ.get("PY000_RESTART_PROCESS_TESTS") != "1":
        pytest.skip("explicit ordinary process/temporary Redis tests not selected")
    context = subprocess.check_output(["docker", "context", "show"], text=True).strip()
    assert context == "desktop-linux", "restart tests require the local Docker Desktop context"
    details = json.loads(subprocess.check_output(
        ["docker", "context", "inspect", context], text=True,
    ))
    assert details[0]["Endpoints"]["docker"]["Host"].startswith("unix://")
    subprocess.run(["docker", "image", "inspect", _REDIS], check=True, capture_output=True)
    container = subprocess.check_output([
        "docker", "run", "--detach", "--rm", "--pull", "never",
        "--publish", "127.0.0.1::6379",
        "--tmpfs", "/data:rw,noexec,nosuid,size=32m", "--memory", "128m", _REDIS,
        "redis-server", "--save", "", "--appendonly", "no", "--protected-mode", "no",
    ], text=True).strip()
    print(f"ordinary restart Redis created: {container}")
    try:
        bound = subprocess.check_output(
            ["docker", "port", container, "6379/tcp"], text=True,
        ).strip()
        host, port = bound.rsplit(":", 1)
        assert host == "127.0.0.1"
        yield int(port)
    finally:
        subprocess.run(["docker", "stop", container], check=True, capture_output=True, timeout=20)
        print(f"ordinary restart Redis removed: {container}")


def _command(kind: str, cut: str, phase: str, port: int, directory: Path) -> list[str]:
    return [os.environ.get("PY000_RESTART_PYTHON", sys.executable), str(_WORKER),
            kind, cut, phase, str(port), str(directory)]


def _environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if "PY000_RESTART_PYTHON" not in os.environ:
        environment["PYTHONPATH"] = str(_WORKER.parent.parent / "src")
    return environment


def _wait_file(path: Path, process: subprocess.Popen[str], *, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert process.poll() is None, f"worker exited {process.returncode}; inspect {path.parent}"
        errors = list(path.parent.glob("*-error.json"))
        assert not errors, [error.read_text() for error in errors]
        assert time.monotonic() < deadline, f"worker checkpoint timeout: {path}"
        time.sleep(.02)
    return json.loads(path.read_text())


def _probe(kind: str, cut: str, port: int, directory: Path) -> Any:
    process = subprocess.run(_command(kind, cut, "probe", port, directory),
                             cwd=directory, env=_environment(), text=True, capture_output=True,
                             timeout=15)
    assert process.returncode == 0, (process.stdout + process.stderr)[-8000:]
    rows = [line.removeprefix("RESTART_JSON=") for line in process.stdout.splitlines()
            if line.startswith("RESTART_JSON=")]
    assert len(rows) == 1
    return json.loads(rows[0])


def _intents(observation: Any) -> list[Any]:
    business = observation["business"]
    views = business["directions"].values() if "directions" in business else (business,)
    return [intent for view in views for intent in view["hedge_intents"].values()]


def _assert_settled(observation: Any, *, cycles: int, net: int | None = None,
                    hedge_fills: int | None = None, rejected_cid: str | None = None) -> None:
    assert not observation["restart_pending"], observation
    assert not observation["source_while_unresolved"]
    positions = list(observation["native"]["positions"].values())
    source = [item for item in positions if item["instrument"].endswith(".BITFINEX")]
    hedge = [item for item in positions if item["instrument"].endswith(".MT5")]
    net = 2 * cycles if net is None else net
    hedge_fills = cycles if hedge_fills is None else hedge_fills
    assert len(source) == 1 and Decimal(source[0]["quantity"]) == net
    assert sum((Decimal(item["quantity"]) for item in hedge), Decimal(0)) == -net
    assert all(abs(Decimal(item["quantity"])) in {0, 2} for item in hedge)
    assert len(positions) == 1 + len(hedge)  # MT5 tickets are never collapsed into a net leg.
    orders = list(observation["native"]["orders"].values())
    assert sum(order["status"] == "FILLED" and order["instrument"].endswith(".BITFINEX")
               for order in orders) == cycles
    assert sum(order["status"] == "FILLED" and order["instrument"].endswith(".MT5")
               for order in orders) == hedge_fills
    assert all(order["status"] == "FILLED" or (
        order["instrument"].endswith(".BITFINEX") and order["status"] == "CANCELED"
        and Decimal(order["filled"]) == 0
    ) or (cid == rejected_cid and order["status"] == "REJECTED"
          and Decimal(order["filled"]) == 0 and order["trades"] == order["fills"] == [])
        for cid, order in observation["native"]["orders"].items())
    # Ordinary Maker may have placed/canceled a next passive quote. A rejection
    # is allowed only for the exact retained old attempt explicitly under test.
    assert len(observation["source_request_ids"]) == len(set(observation["source_request_ids"]))
    assert len(observation["hedge_request_ids"]) == len(set(observation["hedge_request_ids"]))
    intents = _intents(observation)
    assert len(intents) == cycles and all(intent["status"] == "COMPLETED" for intent in intents)
    assert all(Decimal(intent["hedge_filled_ounces"]) == Decimal(intent["hedge_quantity_ounces"])
               for intent in intents)


def _loaded_facts(native: Any) -> Any:
    expected = deepcopy(native)
    for order in expected["orders"].values():
        if (order["instrument"].endswith(".BITFINEX") and order["index"] == "None"
                and Decimal(order["filled"]) > 0):
            assert order["position"] != "None"
            order["index"] = order["position"]  # Native load rebuilds this NETTING side-index.
    return expected


@pytest.mark.parametrize("kind", ["taker", "maker"])
@pytest.mark.parametrize("cut", [
    "settled", "hedge-callback", "source-reserved", "source-callback", "between-legs",
    "request-pending", "old-hold", "recovery-killed",
    "hedge-rejected-held", "hedge-rejected-retry", "hedge-rejected-no-quote",
])
def test_ordinary_process_kill_then_original_entry_recovers_or_holds(
    kind: str, cut: str, tmp_path: Path, restart_redis: int,
) -> None:
    port = restart_redis
    assert _probe(kind, cut, port, tmp_path) == {"orders": {}, "positions": {}}
    processes: list[subprocess.Popen[str]] = []
    with ((tmp_path / "produce.log").open("w") as output,
          (tmp_path / "recover.log").open("w") as restored):
        try:
            first = subprocess.Popen(_command(kind, cut, "produce", port, tmp_path), cwd=tmp_path,
                                     env=_environment(), stdout=output, stderr=subprocess.STDOUT,
                                     text=True)
            processes.append(first)
            checkpoint = _wait_file(tmp_path / "checkpoint.json", first)
            assert checkpoint["node_running"] and checkpoint["strategy_running"]
            assert checkpoint["connected"]
            # A native enqueue is not durability. Observe the actual server while
            # the process remains paused before the chosen business callback.
            deadline = time.monotonic() + 10
            while _probe(kind, cut, port, tmp_path) != checkpoint["native"]:
                assert time.monotonic() < deadline, "native writes did not reach Redis"
            if cut == "source-reserved":
                assert checkpoint["native"] == {"orders": {}, "positions": {}}
                assert checkpoint["source_requests"] == checkpoint["hedge_requests"] == 0
                assert checkpoint["business_bytes"] is not None
            elif cut in {"settled", "hedge-callback", "old-hold", "recovery-killed"}:
                assert sum(order["status"] == "FILLED"
                           for order in checkpoint["native"]["orders"].values()) == 2
                assert checkpoint["source_requests"] >= 1 and checkpoint["hedge_requests"] == 1
            elif cut == "source-callback":
                assert len(checkpoint["native"]["orders"]) == 1
                assert not _intents(checkpoint) and checkpoint["hedge_requests"] == 0
            elif cut == "between-legs":
                pending = next(intent for intent in _intents(checkpoint)
                               if intent["status"] == "PENDING")
                assert pending["hedge_leg_index"] == 1 and len(pending["hedge_plan"]) == 2
                assert len(pending["hedge_order_ids"]) == 1
                assert pending["hedge_client_order_id"] is None
                assert checkpoint["hedge_requests"] == checkpoint["close_requests"] == 1
            elif cut == "request-pending":
                assert checkpoint["hedge_requests"] == 1 and checkpoint["close_requests"] == 0
                pending_order = checkpoint["native"]["orders"][checkpoint["hedge_request_ids"][0]]
                assert pending_order["status"] == "SUBMITTED" and pending_order["filled"] == "0"
                assert pending_order["venue"] == "None"
                assert checkpoint["journal_types"][-1] == "submission_reserved"
                assert "order_filled" not in checkpoint["journal_types"]
            elif cut.startswith("hedge-rejected-"):
                assert checkpoint["source_requests"] >= 1 and checkpoint["hedge_requests"] == 1
                assert checkpoint["close_requests"] == 0
                assert checkpoint["mt5_quote"]["bid_size"] == "0"
                assert checkpoint["mt5_quote"]["ask_size"] == "0"
                old_cid, = checkpoint["hedge_request_ids"]
                old_order = checkpoint["native"]["orders"][old_cid]
                assert old_order["status"] == "REJECTED" and old_order["filled"] == "0"
                assert old_order["venue"] == "None" and not old_order["trades"]
                assert checkpoint["journal_types"] == [
                    "stream_started", "submission_reserved", "order_rejected",
                ]
                original_intent, = _intents(checkpoint)
                assert original_intent["status"] == "REJECTED"
                assert original_intent["hedge_order_ids"] == [old_cid]
                assert original_intent["rejected_attempt"] is None
                venue = json.loads((tmp_path / "venue.json").read_text())
                assert venue["mt5_snapshot"]["authority_flags"]["mql_trade_allowed"] is True
            first.kill()
            assert first.wait(timeout=5) == -signal.SIGKILL
            # No graceful cleanup/rewrite is inserted between the kill and next process.
            if cut == "recovery-killed":
                interrupted = subprocess.Popen(_command(kind, cut, "interrupt", port, tmp_path),
                                               cwd=tmp_path, env=_environment(), stdout=restored,
                                               stderr=subprocess.STDOUT, text=True)
                processes.append(interrupted)
                pause = _wait_file(tmp_path / "checkpoint-interrupt.json", interrupted)
                assert pause["native"] == _loaded_facts(checkpoint["native"])
                assert pause["production"] == checkpoint["production"]
                assert any(intent["status"] == "UNKNOWN" for intent in _intents(pause))
                interrupted.kill()
                assert interrupted.wait(timeout=5) == -signal.SIGKILL
            second = subprocess.Popen(_command(kind, cut, "recover", port, tmp_path), cwd=tmp_path,
                                      env=_environment(), stdout=restored, stderr=subprocess.STDOUT,
                                      text=True)
            processes.append(second)
            recovered = _wait_file(tmp_path / "recovered.json", second)
            assert len({os.getpid(), checkpoint["pid"], recovered["pid"]}) == 3
            assert recovered["production"] == checkpoint["production"]
            assert recovered["loaded"]["event_count"] == 0
            assert recovered["loaded"]["native"] == _loaded_facts(checkpoint["native"])
            assert recovered["native"] == recovered["loaded"]["native"]
            assert recovered["source_requests"] == checkpoint["source_requests"]
            assert recovered["hedge_requests"] == checkpoint["hedge_requests"]
            assert recovered["close_requests"] == checkpoint["close_requests"]
            if cut in {"hedge-rejected-held", "hedge-rejected-retry"}:
                before_pub = _wait_file(tmp_path / "before-pub.json", second)
                assert before_pub["restart_pending"] and before_pub["mt5_quote"] is None
                assert before_pub["business"] == checkpoint["business"]
                assert before_pub["native"] == recovered["loaded"]["native"]
                assert before_pub["source_requests"] == checkpoint["source_requests"]
                assert before_pub["hedge_requests"] == checkpoint["hedge_requests"]
                assert before_pub["close_requests"] == checkpoint["close_requests"]
                assert recovered["mt5_pub_count"] == 1
                assert recovered["mt5_quote"]["bid_size"] == "0"
                assert recovered["mt5_quote"]["ask_size"] == "0"
            elif cut == "hedge-rejected-no-quote":
                assert recovered["mt5_pub_count"] == 0 and recovered["mt5_quote"] is None
                assert not recovered["reconciler_busy"]
                assert recovered["failure"] == "startup recovery: TimeoutError"
            if cut in _HELD:
                final = _wait_file(tmp_path / "final.json", second)
                assert final["restart_pending"]
                assert final["failure"] or (cut == "request-pending" and final["hedge_hold"])
                assert final["native"] == recovered["native"]
                assert final["source_requests"] == checkpoint["source_requests"]
                assert final["hedge_requests"] == checkpoint["hedge_requests"]
                assert final["close_requests"] == checkpoint["close_requests"]
                if cut in {"old-hold", "hedge-rejected-held", "hedge-rejected-no-quote"}:
                    assert final["business"] == checkpoint["business"]
                if cut == "hedge-rejected-no-quote":
                    assert final["mt5_pub_count"] == 0 and final["mt5_quote"] is None
                    assert not final["reconciler_busy"]
                if cut == "request-pending":
                    assert not final["hedge_admitted"]
                    assert "journal contains dangling reservation(s):" in final["hedge_hold"]
                    assert checkpoint["hedge_request_ids"][0] in final["hedge_hold"]
                    assert final["journal_types"] == checkpoint["journal_types"]
            else:
                if cut in {"source-callback", "between-legs", "hedge-rejected-retry"}:
                    pending = [intent for intent in _intents(recovered)
                               if intent["status"] == "PENDING"]
                    assert len(pending) == 1 and pending[0]["hedge_client_order_id"] is None
                    assert not any(recovered["source_can_submit"])
                    if cut == "between-legs":
                        assert pending[0]["hedge_order_ids"] == next(
                            intent["hedge_order_ids"] for intent in _intents(checkpoint)
                            if intent["intent_id"] == pending[0]["intent_id"])
                    elif cut == "hedge-rejected-retry":
                        assert pending[0]["intent_id"] == original_intent["intent_id"]
                        assert pending[0]["hedge_plan"] == original_intent["hedge_plan"]
                        assert pending[0]["hedge_order_ids"] == [old_cid]
                        assert pending[0]["rejected_attempt"] == {
                            "client_order_id": old_cid, "leg_index": 0,
                        }
                else:
                    _assert_settled(recovered, cycles=1)
                if cut == "settled":
                    assert recovered["business"] == checkpoint["business"]
                (tmp_path / "advance").touch()
                final = _wait_file(tmp_path / "final.json", second)
                if cut in {"source-callback", "between-legs", "hedge-rejected-retry"}:
                    continued = _wait_file(tmp_path / "continued.json", second)
                    assert not continued["source_while_unresolved"]
                    assert continued["hedge_requests"] == checkpoint["hedge_requests"] + 1
                    assert continued["close_requests"] == checkpoint["close_requests"]
                    _assert_settled(continued, cycles=2 if cut == "between-legs" else 1,
                                    net=2, hedge_fills=3 if cut == "between-legs" else 1,
                                    rejected_cid=old_cid if cut == "hedge-rejected-retry" else None)
                    if cut == "hedge-rejected-retry":
                        completed, = _intents(continued)
                        assert completed["intent_id"] == original_intent["intent_id"]
                        assert completed["hedge_order_ids"][0] == old_cid
                        assert len(completed["hedge_order_ids"]) == 2
                        assert len(set(completed["hedge_order_ids"])) == 2
                        assert completed["rejected_attempt"] == pending[0]["rejected_attempt"]
                        assert continued["native"]["orders"][old_cid] == old_order
                        if kind == "taker":
                            assert continued["source_requests"] == checkpoint["source_requests"]
                        else:
                            # A completed Maker may quote again before this
                            # observation. Prove the obligation was COMPLETED at
                            # each such actual send, and no source fill was added.
                            for raw_cid in (set(continued["source_request_ids"])
                                            - set(checkpoint["source_request_ids"])):
                                assert continued["source_attempt_states"][str(raw_cid)][
                                    original_intent["intent_id"]
                                ] == "COMPLETED"
                            for cid, order in continued["native"]["orders"].items():
                                if (cid not in checkpoint["native"]["orders"]
                                        and order["instrument"].endswith(".BITFINEX")):
                                    assert order["status"] == "CANCELED" and order["filled"] == "0"
                                    assert order["trades"] == order["fills"] == []
                        for pid, position in checkpoint["native"]["positions"].items():
                            assert continued["native"]["positions"][pid] == position
                _assert_settled(final, cycles=3 if cut == "between-legs" else 2,
                                net=0 if cut == "between-legs" else 4,
                                hedge_fills=4 if cut == "between-legs" else 2,
                                rejected_cid=old_cid if cut == "hedge-rejected-retry" else None)
                assert final["source_requests"] > checkpoint["source_requests"]
                assert final["hedge_requests"] == (3 if cut == "hedge-rejected-retry" else 2)
                if cut == "hedge-rejected-retry":
                    for cid, order in _loaded_facts(checkpoint["native"])["orders"].items():
                        assert final["native"]["orders"][cid] == order
                    completed = next(intent for intent in _intents(final)
                                     if intent["intent_id"] == original_intent["intent_id"])
                    assert completed["rejected_attempt"] == pending[0]["rejected_attempt"]
                assert set(checkpoint["native"]["orders"]) < set(final["native"]["orders"])
                for position_id, position in checkpoint["native"]["positions"].items():
                    if position["instrument"].endswith(".MT5") and cut != "between-legs":
                        assert final["native"]["positions"][position_id] == position
            second.send_signal(signal.SIGTERM)
            exit_code = second.wait(timeout=8)
            restored.flush()
            lines = (tmp_path / "recover.log").read_text().splitlines()
            result = next(json.loads(line) for line in reversed(lines)
                          if line.startswith('{"outcome"'))
            assert result["drain_complete"] is (cut not in _HELD)
            report_pending = cut in {"between-legs", "request-pending"}
            assert result["accounting"]["status"] == ("PENDING" if report_pending else "FINAL")
            if cut == "between-legs":
                assert result["accounting"]["pending_reasons"] == [
                    "native_position_history_incomplete",
                ]
            incomplete = cut in _HELD or report_pending
            assert exit_code == (1 if incomplete else 0)
            assert result["outcome"] == (
                "PAPER_INCOMPLETE" if incomplete else "PAPER_STOPPED"
            )
            if cut in _HELD:
                assert result["reason"].startswith("drain_timeout") and result["pending"]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()  # Cleanup after a failed test, never counted as a graceful stop.
                    process.wait(timeout=5)
