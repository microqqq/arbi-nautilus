"""Real ordinary both-entry processes, native Redis and SIGKILL; finite venue IO only.

The stop cut kills after the real joint drain completed, not during an unknown
unfinished drain. No native seed, replacement recovery, real venue or power-loss
durability claim is made. Reuse the existing isolated opt-in Redis/worker tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import SubscribeInstrument
from nautilus_trader.model.identifiers import ClientId, Venue
from test_restart_process import (
    _command,
    _environment,
    _intents,
    _loaded_facts,
    _probe,
    _wait_file,
)
from test_restart_process import restart_redis as _restart_redis
from test_shared_strategy import _Joint
from test_strategy_continuity import _pump

from py000_nautilus import maker_store
from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.live_lifecycle import _snapshot
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.models import BusinessOrderSide
from py000_nautilus.mt5_v1_data import instrument_from_snapshot
from py000_nautilus.store import _persist_payload

restart_redis = _restart_redis


def _settled(observation: Any, count: int, expected_net: int) -> None:
    assert not observation["restart_pending"] and observation["failure"] is None
    assert not observation["source_while_unresolved"]
    intents = _intents(observation)
    assert len(intents) == count
    assert all(intent["status"] == "COMPLETED"
               and Decimal(intent["hedge_filled_ounces"])
               == Decimal(intent["hedge_quantity_ounces"]) for intent in intents)
    orders = observation["native"]["orders"]
    assert all(order["status"] in {"FILLED", "CANCELED"} for order in orders.values())
    assert {order["owner"] for order in orders.values()} == set(observation["strategy_ids"])
    positions = list(observation["native"]["positions"].values())
    for venue, net in (("BITFINEX", expected_net), ("MT5", -expected_net)):
        assert sum((Decimal(row["quantity"]) for row in positions
                    if row["instrument"].endswith(f".{venue}")), Decimal(0)) == net
    assert Decimal(observation["source_net"]) == expected_net


def _assert_settled_stop_facts(final: Any, stopped: Any, expected_net: int) -> None:
    """Permit only a completed-cycle Maker quote canceled across the SIGTERM gap."""
    _settled(stopped, 4, expected_net)
    assert stopped["native"]["positions"] == final["native"]["positions"]
    old_orders, orders = final["native"]["orders"], stopped["native"]["orders"]
    for cid, order in old_orders.items():
        assert orders[cid] == order  # Every old UUID, fill, owner and index is unchanged.
    for key in ("hedge_request_ids", "close_request_ids"):
        assert stopped[key] == final[key]
    intents = {intent["intent_id"]: intent for intent in _intents(final)}
    assert len(intents) == 4
    assert {intent["intent_id"]: intent for intent in _intents(stopped)} == intents
    assert stopped["business"]["allocations"] == final["business"]["allocations"]
    added = orders.keys() - old_orders.keys()
    for cid in added:
        order = orders[cid]
        assert order["owner"] == final["strategy_ids"][0]
        assert order["instrument"].endswith(".BITFINEX") and order["client"] == "BITFINEX"
        assert order["status"] == "CANCELED" and Decimal(order["filled"]) == 0
        assert order["fills"] == order["trades"] == []
    before, after = final["source_request_ids"], stopped["source_request_ids"]
    assert after[:len(before)] == before
    appended = after[len(before):]
    assert len(appended) == len(set(appended)) == len(added)
    for cid in appended:
        assert stopped["source_attempt_states"][str(cid)] == {
            intent_id: "COMPLETED" for intent_id in intents
        }


@pytest.mark.parametrize("change", [
    "unchanged", "canceled", "old-order", "filled", "wrong-owner", "wrong-venue",
    "pending-at-send", "position", "intent", "hedge-id", "close-id",
])
def test_settled_stop_fact_comparison_controls(change: str) -> None:
    """Small observation controls, not native execution or process certification."""
    intents = {f"I{i}": {"intent_id": f"I{i}", "status": "COMPLETED",
                         "hedge_filled_ounces": "2", "hedge_quantity_ounces": "2"}
               for i in range(4)}
    final: dict[str, Any] = {
        "restart_pending": False, "failure": None, "source_while_unresolved": [],
        "strategy_ids": ["MakerStrategy-M", "TakerStrategy-T"], "source_net": "8",
        "source_request_ids": [1, 2], "hedge_request_ids": ["H1", "H2"],
        "close_request_ids": ["C1"], "source_attempt_states": {},
        "business": {"directions": {
            "bid": {"hedge_intents": {key: value for key, value in intents.items()
                                      if key in {"I0", "I1"}}},
            "ask": {"hedge_intents": {}},
            "taker": {"hedge_intents": {key: value for key, value in intents.items()
                                        if key in {"I2", "I3"}}},
        }, "allocations": []},
        "native": {"orders": {
            "M": {"owner": "MakerStrategy-M", "status": "FILLED", "events": ["M-fill"]},
            "T": {"owner": "TakerStrategy-T", "status": "FILLED", "events": ["T-fill"]},
        }, "positions": {
            "S": {"instrument": "XAUTUSDT-PERP.BITFINEX", "quantity": "8"},
            "H": {"instrument": "XAUUSD.MT5", "quantity": "-8"},
        }},
    }
    stopped = deepcopy(final)
    if change != "unchanged":
        stopped["native"]["orders"]["NEW"] = {
            "owner": "MakerStrategy-M", "instrument": "XAUTUSDT-PERP.BITFINEX",
            "client": "BITFINEX", "status": "CANCELED", "filled": "0",
            "fills": [], "trades": [],
        }
        stopped["source_request_ids"].append(3)
        stopped["source_attempt_states"]["3"] = {key: "COMPLETED" for key in intents}
    if change == "old-order":
        stopped["native"]["orders"]["M"]["events"].append("changed-old-UUID")
    elif change == "filled":
        stopped["native"]["orders"]["NEW"]["filled"] = "1"
    elif change == "wrong-owner":
        stopped["native"]["orders"]["NEW"]["owner"] = "TakerStrategy-T"
    elif change == "wrong-venue":
        stopped["native"]["orders"]["NEW"]["instrument"] = "XAUUSD.MT5"
    elif change == "pending-at-send":
        stopped["source_attempt_states"]["3"]["I0"] = "PENDING"
    elif change == "position":
        stopped["native"]["positions"]["S"]["events"] = ["new-position-event"]
    elif change == "intent":
        stopped["business"]["directions"]["bid"]["hedge_intents"]["I0"]["extra"] = True
    elif change in {"hedge-id", "close-id"}:
        stopped[change.split("-")[0] + "_request_ids"].append("new-request")
    if change in {"unchanged", "canceled"}:
        _assert_settled_stop_facts(final, stopped, 8)
    else:
        with pytest.raises(AssertionError):
            _assert_settled_stop_facts(final, stopped, 8)


def test_joint_instrument_subscription_replay_requires_original_client_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivery-order control with a real node/data client, not process certification."""
    async def scenario() -> None:
        h = _Joint(tmp_path, monkeypatch)
        try:
            await h.start()
            original = h.hedge_data._instrument
            await asyncio.sleep(.005)  # Force distinct valid snapshot milliseconds.
            sample = await h.wire.snapshot(h.wire.identity.binding())
            now = h.node.kernel.clock.timestamp_ns()
            newer = instrument_from_snapshot(sample, original.id, ts_init=now)
            assert original.ts_event < newer.ts_event <= now
            command = SubscribeInstrument(
                instrument_id=original.id, client_id=ClientId("MT5"), venue=Venue("MT5"),
                command_id=UUID4(), ts_init=now,
            )
            h.node.kernel.data_engine.process(newer)  # The old worker bypassed the client.
            await h.hedge_data._subscribe_instrument(command)  # Replays the actual old reference.
            await _pump()
            assert h.hedge_data._instrument.ts_event == original.ts_event
            for strategy in (h.maker, h.taker):
                assert strategy._hedge_instrument is not None
                assert strategy._hedge_instrument.ts_event == newer.ts_event
                assert not strategy._hedge_instrument_valid  # Rejected the backward replay.
            assert h.maker._source_hold

            await asyncio.sleep(.005)
            await h.hedge_data._refresh_snapshot(allow_rehandshake=False)
            await h.hedge_data._subscribe_instrument(command)
            await _pump()
            assert h.hedge_data._instrument.ts_event > newer.ts_event
            for strategy in (h.maker, h.taker):
                assert strategy._hedge_instrument is not None
                assert strategy._hedge_instrument_valid
                assert strategy._hedge_instrument.ts_event == h.hedge_data._instrument.ts_event
            assert not h.node.cache.orders() and not h.node.cache.positions()
            assert not h.venue.rows and not h.wire.submit_calls and not h.wire.close_calls
        finally:
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cut", ["settled", "between-legs", "joint-stop-fill"])
def test_both_ordinary_process_kill_then_native_load_and_joint_continuation(
    cut: str, tmp_path: Path, restart_redis: int,
) -> None:
    port = restart_redis
    assert _probe("both", cut, port, tmp_path) == {"orders": {}, "positions": {}}
    processes: list[subprocess.Popen[str]] = []
    with ((tmp_path / "produce.log").open("w") as output,
          (tmp_path / "recover.log").open("w") as restored):
        try:
            first = subprocess.Popen(_command("both", cut, "produce", port, tmp_path),
                                     cwd=tmp_path, env=_environment(), stdout=output,
                                     stderr=subprocess.STDOUT, text=True)
            processes.append(first)
            if cut == "joint-stop-fill":
                working = _wait_file(tmp_path / "working-ready.json", first)
                assert len(working["native"]["orders"]) == 2
                assert all(order["status"] == "ACCEPTED" and order["filled"] == "0"
                           for order in working["native"]["orders"].values())
                first.send_signal(signal.SIGTERM)
            checkpoint = _wait_file(tmp_path / "checkpoint.json", first)
            assert checkpoint["node_running"] and checkpoint["all_strategies_running"]
            assert checkpoint["connected"] and checkpoint["accounts_calculated"] == [False, False]
            assert len(checkpoint["strategy_ids"]) == len(set(checkpoint["strategy_ids"])) == 2
            assert set(checkpoint["business"]["directions"]) == {"bid", "ask", "taker"}
            deadline = time.monotonic() + 10
            while _probe("both", cut, port, tmp_path) != checkpoint["native"]:
                assert time.monotonic() < deadline, "native enqueues did not reach Redis"
            if cut == "between-legs":
                pending, = [intent for intent in _intents(checkpoint)
                            if intent["status"] == "PENDING"]
                assert pending["hedge_leg_index"] == 1 and len(pending["hedge_plan"]) == 2
                assert pending["hedge_client_order_id"] is None
                assert len(pending["hedge_order_ids"]) == 1
                assert checkpoint["hedge_requests"] == checkpoint["close_requests"] == 1
                close_cid, = checkpoint["close_request_ids"]
                close = checkpoint["native"]["orders"][close_cid]
                ticket = checkpoint["native"]["positions"][close["position"]]
                assert close["owner"] == checkpoint["strategy_ids"][1]
                assert ticket["owner"] == checkpoint["strategy_ids"][0]
                assert close["status"] == "FILLED" and close["filled"] == "2"
                assert Decimal(ticket["quantity"]) == 0
            else:
                _settled(checkpoint, 2, 2 if cut == "joint-stop-fill" else 4)
                if cut == "joint-stop-fill":
                    assert checkpoint["joint_drain"] == {
                        "complete": True, "reason": "obligations_settled",
                        "pending": [], "residuals": {},
                    }
                    cancels = checkpoint["cancel_observations"]
                    assert len(cancels) == len({item["cid"] for item in cancels}) == 2
                    assert all(item["draining"] == [True, True] and item["connected"]
                               for item in cancels)
                    assert checkpoint["source_request_ids"] == working["source_request_ids"]
                    for cid in working["native"]["orders"]:
                        order = checkpoint["native"]["orders"][cid]
                        assert order["status"] == "CANCELED" and order["filled"] == "1"
            first.kill()
            assert first.wait(timeout=5) == -signal.SIGKILL
            second = subprocess.Popen(_command("both", cut, "recover", port, tmp_path),
                                      cwd=tmp_path, env=_environment(), stdout=restored,
                                      stderr=subprocess.STDOUT, text=True)
            processes.append(second)
            recovered = _wait_file(tmp_path / "recovered.json", second)
            assert len({os.getpid(), checkpoint["pid"], recovered["pid"]}) == 3
            assert recovered["production"] == checkpoint["production"]
            assert recovered["accounts_calculated"] == [False, False]
            assert recovered["loaded"]["event_count"] == 0
            expected = _loaded_facts(checkpoint["native"])
            assert recovered["loaded"]["native"] == recovered["native"] == expected
            assert recovered["strategy_ids"] == checkpoint["strategy_ids"]
            assert not recovered["restart_pending"] and recovered["failure"] is None
            for key in ("source_request_ids", "hedge_request_ids", "close_request_ids"):
                assert recovered[key] == checkpoint[key]
            assert recovered["business"]["allocations"] == checkpoint["business"]["allocations"]
            if cut == "between-legs":
                assert not any(recovered["source_can_submit"])
                recovered_pending = next(item for item in _intents(recovered)
                                         if item["intent_id"] == pending["intent_id"])
                assert recovered_pending["hedge_order_ids"] == pending["hedge_order_ids"]
                assert recovered_pending["hedge_plan"] == pending["hedge_plan"]
                assert recovered_pending["hedge_leg_index"] == 1
            (tmp_path / "advance").touch()
            final = _wait_file(tmp_path / "final.json", second)
            net = -2 if cut == "between-legs" else 6 if cut == "joint-stop-fill" else 8
            _settled(final, 4, net)
            assert final["production"] == recovered["production"]
            for cid, order in expected["orders"].items():
                # Old UUIDs/fills/owners/PIDs untouched.
                assert final["native"]["orders"][cid] == order
            assert set(final["source_request_ids"]) > set(checkpoint["source_request_ids"])
            if cut == "between-legs":
                continued = _wait_file(tmp_path / "continued.json", second)
                _settled(continued, 2, 2)
                assert continued["close_request_ids"] == checkpoint["close_request_ids"]
                assert continued["hedge_requests"] == checkpoint["hedge_requests"] + 1
                for cid, order in expected["orders"].items():
                    assert continued["native"]["orders"][cid] == order
            second.send_signal(signal.SIGTERM)
            exit_code = second.wait(timeout=10)
            restored.flush()
            result = next(json.loads(line) for line in reversed(
                (tmp_path / "recover.log").read_text().splitlines(),
            ) if line.startswith('{"outcome"'))
            assert result["drain_complete"] is True
            assert result["accounting"]["scope"] == (
                "shared_owned_cached_history:native_virtual_realized_trading_pnl_and_commission;"
                "not_venue_realized_cashflow"
            )
            assert result["accounting"]["status"] == "FINAL"
            assert result["accounting"]["pending_reasons"] == []
            assert exit_code == 0 and result["outcome"] == "PAPER_STOPPED"
            stopped = json.loads((tmp_path / "recover-drain-returned.json").read_text())
            assert stopped["joint_drain"]["complete"] is True
            _assert_settled_stop_facts(final, stopped, net)
            if cut == "joint-stop-fill":
                assert stopped["native"] == final["native"]
                assert final["maker_source_hold"] is True
                assert stopped["maker_source_hold"] is True  # Stop does not release admission.
                assert not stopped["pause_publication_failed"]
                assert stopped["business"] == final["business"]
            assert _probe("both", cut, port, tmp_path) == stopped["native"]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)  # Failed-test cleanup, never a qualifying stop.


@pytest.mark.parametrize("fault", ["external", "publication", "unfinished"])
def test_shared_soft_hold_drain_gate_still_rejects_other_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    """Gate-only controls on a real empty node; these are not process/trade evidence."""
    async def scenario() -> None:
        h = _Joint(tmp_path, monkeypatch, stop_timeout=.02)
        try:
            await h.start()
            await h.until(lambda: not get_source_terminal_reconciler(h.node).busy)
            h.maker._freeze_and_cancel_all("unusable market input", market_input=True)
            assert h.maker._source_hold and _snapshot(h.node, h.maker).complete
            if fault == "external":
                h.owner.freeze_sources("operator review required")
                expected = "freeze:operator review required"
            elif fault == "publication":
                publish = _persist_payload

                def after_publish(*args: Any, **kwargs: Any) -> None:
                    publish(*args, **kwargs)
                    raise ParentDirectorySyncError("synthetic post-replace directory fsync")

                with monkeypatch.context() as failure:
                    failure.setattr(maker_store, "_persist_payload", after_publish)
                    with pytest.raises(ParentDirectorySyncError):
                        h.owner._persist()
                assert all(view.source_freeze_reason is None for view in h.owner.all_views())
                assert h.owner._freeze_publication_failed
                expected = "maker_pause_publication_failed"
            else:
                # A real durable reservation but no submitted native order is
                # deliberately incomplete. No native event or fill is fabricated.
                h.taker.state_store.begin_source(
                    "J-DRAIN-RESERVED", BusinessOrderSide.BUY, Decimal(2),
                    source_account_id=str(h.source.account_id), source_client_id="BITFINEX",
                    hedge_account_id=str(h.hedge.account_id), hedge_client_id="MT5",
                )
                expected = "maker_source_hold"
                assert not _snapshot(h.node, h.taker).complete
            result = _snapshot(h.node, h.maker)
            assert not result.complete and expected in result.pending
            assert h.maker._source_hold
            assert not h.node.cache.orders() and not h.node.cache.positions()
            assert not h.venue.rows and not h.wire.submit_calls and not h.wire.close_calls
        finally:
            await h.close()

    asyncio.run(scenario())
