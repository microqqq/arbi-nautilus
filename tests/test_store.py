"""Durable exactly-once and restart-stop behavior."""

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

import py000_nautilus.store as store_module
from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.models import BusinessOrderSide, HedgeLeg, ObligationStatus
from py000_nautilus.store import JsonStateStore

D = Decimal


def test_partial_final_and_duplicate_fills_create_exactly_once_intents(tmp_path: Path) -> None:
    path = _state_path(tmp_path)
    store = JsonStateStore(path)
    store.begin_source("O-1", BusinessOrderSide.BUY, D(2))

    first = store.reserve_source_fill(
        fill_key="O-1|V-1|T-PARTIAL",
        client_order_id="O-1",
        trade_id="T-PARTIAL",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    duplicate = store.reserve_source_fill(
        fill_key="O-1|V-1|T-PARTIAL",
        client_order_id="O-1",
        trade_id="T-PARTIAL",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    final = store.reserve_source_fill(
        fill_key="O-1|V-1|T-FINAL",
        client_order_id="O-1",
        trade_id="T-FINAL",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )

    assert first is not None
    assert final is not None
    assert duplicate is None
    assert len(store.intents()) == 2
    assert store.active_source_order_id is None


def test_completed_hedge_allows_next_source_and_duplicate_hedge_fill_is_ignored(
    tmp_path: Path,
) -> None:
    store = JsonStateStore(_state_path(tmp_path))
    store.begin_source("O-1", BusinessOrderSide.SELL, D(1))
    intent = store.reserve_source_fill(
        fill_key="O-1|V-1|T-1",
        client_order_id="O-1",
        trade_id="T-1",
        source_side=BusinessOrderSide.SELL,
        fill_ounces=D(1),
    )
    assert intent is not None
    store.bind_hedge_order(intent.intent_id, "H-1")
    assert store.apply_hedge_fill(client_order_id="H-1", trade_id="HT-1", fill_ounces=D(1))
    assert not store.apply_hedge_fill(
        client_order_id="H-1",
        trade_id="HT-1",
        fill_ounces=D(1),
    )
    assert store.intent(intent.intent_id).status is ObligationStatus.COMPLETED
    assert store.can_submit_source()


def test_outstanding_exposure_only_declines_on_distinct_real_hedge_fills(
    tmp_path: Path,
) -> None:
    store = JsonStateStore(_state_path(tmp_path))
    store.begin_source("O-EXPOSURE", BusinessOrderSide.BUY, D(2))
    intent = store.reserve_source_fill(
        fill_key="O-EXPOSURE|V-1|T-1",
        client_order_id="O-EXPOSURE",
        trade_id="T-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    assert store.rounding_residual_ounces == 0
    assert store.net_unhedged_ounces == D(2)

    store.bind_hedge_order(intent.intent_id, "H-EXPOSURE")
    assert store.apply_hedge_fill(
        client_order_id="H-EXPOSURE",
        trade_id="HT-PARTIAL",
        fill_ounces=D("0.5"),
    )
    assert store.net_unhedged_ounces == D("1.5")
    assert not store.apply_hedge_fill(
        client_order_id="H-EXPOSURE",
        trade_id="HT-PARTIAL",
        fill_ounces=D("0.5"),
    )
    store.update_hedge_status("H-EXPOSURE", ObligationStatus.REJECTED)
    assert store.net_unhedged_ounces == D("1.5")


def test_multi_ticket_plan_round_trips_and_advances_one_exact_leg(tmp_path: Path) -> None:
    path = _state_path(tmp_path)
    store = JsonStateStore(path)
    store.begin_source("O-PLAN", BusinessOrderSide.BUY, D(3))
    intent = store.reserve_source_fill(
        fill_key="O-PLAN|V-1|T-1",
        client_order_id="O-PLAN",
        trade_id="T-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(3),
    )
    assert intent is not None
    plan = (
        HedgeLeg(
            side=BusinessOrderSide.SELL,
            quantity_ounces=D(1),
            position_id="2",
            expected_position_side=BusinessOrderSide.BUY,
            expected_position_quantity_ounces=D(1),
        ),
        HedgeLeg(side=BusinessOrderSide.SELL, quantity_ounces=D(2)),
    )

    store.bind_hedge_plan(intent.intent_id, plan)
    store.bind_hedge_order(intent.intent_id, "H-CLOSE")
    with pytest.raises(ValueError, match="pending unique leg"):
        store.bind_hedge_order(intent.intent_id, "H-OVERWRITE")
    reloaded = JsonStateStore(path)

    assert reloaded.intent(intent.intent_id).hedge_plan == plan
    assert reloaded.apply_hedge_fill(
        client_order_id="H-CLOSE",
        trade_id="HT-CLOSE",
        fill_ounces=D(1),
    )
    advanced = JsonStateStore(path).intent(intent.intent_id)
    assert advanced.status is ObligationStatus.PENDING
    assert advanced.hedge_client_order_id is None
    assert advanced.hedge_leg_index == 1
    assert advanced.hedge_filled_ounces == D(1)

    after_first = JsonStateStore(path)
    assert not after_first.apply_hedge_fill(
        client_order_id="H-CLOSE",
        trade_id="HT-CLOSE",
        fill_ounces=D(1),
    )
    assert not after_first.apply_hedge_fill(
        client_order_id="H-CLOSE",
        trade_id="HT-IMPOSSIBLE-SECOND",
        fill_ounces=D(1),
    )
    after_first.bind_hedge_order(intent.intent_id, "H-OPEN")
    after_first.update_hedge_status("H-CLOSE", ObligationStatus.REJECTED)
    rebound = JsonStateStore(path).intent(intent.intent_id)
    assert rebound.hedge_client_order_id == "H-OPEN"
    assert rebound.hedge_order_ids == ("H-CLOSE", "H-OPEN")
    assert rebound.status is ObligationStatus.SUBMITTING


@pytest.mark.parametrize("late_fill", [D(1), D("0.5")], ids=["exact", "partial"])
def test_late_fill_after_unknown_stays_blocked_across_restart(
    tmp_path: Path,
    late_fill: Decimal,
) -> None:
    path = _state_path(tmp_path)
    store = JsonStateStore(path)
    store.begin_source("O-UNKNOWN-PLAN", BusinessOrderSide.BUY, D(2))
    intent = store.reserve_source_fill(
        fill_key="O-UNKNOWN-PLAN|V-1|T-1",
        client_order_id="O-UNKNOWN-PLAN",
        trade_id="T-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    store.bind_hedge_plan(
        intent.intent_id,
        (
            HedgeLeg(
                side=BusinessOrderSide.SELL,
                quantity_ounces=D(1),
                position_id="1",
                expected_position_side=BusinessOrderSide.BUY,
                expected_position_quantity_ounces=D(1),
            ),
            HedgeLeg(side=BusinessOrderSide.SELL, quantity_ounces=D(1)),
        ),
    )
    store.bind_hedge_order(intent.intent_id, "H-UNKNOWN")
    store.update_hedge_status("H-UNKNOWN", ObligationStatus.UNKNOWN)

    assert store.apply_hedge_fill(
        client_order_id="H-UNKNOWN",
        trade_id="HT-LATE",
        fill_ounces=late_fill,
    )

    reloaded = JsonStateStore(path)
    held = reloaded.intent(intent.intent_id)
    assert held.status is ObligationStatus.BLOCKED
    assert held.hedge_client_order_id == "H-UNKNOWN"
    assert held.hedge_leg_index == 0
    assert held.hedge_leg_filled_ounces == late_fill
    assert held.hedge_filled_ounces == late_fill

    reason = reloaded.recover_for_start()

    restarted = JsonStateStore(path)
    assert reason is not None
    assert restarted.halt_reason is not None
    assert restarted.intent(intent.intent_id).status is ObligationStatus.BLOCKED
    assert restarted.intent(intent.intent_id).hedge_leg_filled_ounces == late_fill


def test_corrupt_persisted_hedge_plan_index_fails_closed_on_load(tmp_path: Path) -> None:
    path = Path(_state_path(tmp_path))
    store = JsonStateStore(path)
    store.begin_source("O-CORRUPT", BusinessOrderSide.BUY, D(1))
    intent = store.reserve_source_fill(
        fill_key="O-CORRUPT|V-1|T-1",
        client_order_id="O-CORRUPT",
        trade_id="T-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    assert intent is not None
    store.bind_hedge_plan(
        intent.intent_id,
        (HedgeLeg(side=BusinessOrderSide.SELL, quantity_ounces=D(1)),),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["hedge_intents"][intent.intent_id]["hedge_leg_index"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="index is out of bounds"):
        JsonStateStore(path)


def test_source_to_hedge_route_survives_json_round_trip(tmp_path: Path) -> None:
    path = _state_path(tmp_path)
    JsonStateStore(path).begin_source(
        "O-ROUTE",
        BusinessOrderSide.SELL,
        D(1),
        source_account_id="BITFINEX-002",
        source_client_id="SOURCE-CLIENT",
        hedge_account_id="MT5-002",
        hedge_client_id="HEDGE-CLIENT",
    )

    record = JsonStateStore(path).source_order("O-ROUTE")

    assert record is not None
    assert record.source_account_id == "BITFINEX-002"
    assert record.source_client_id == "SOURCE-CLIENT"
    assert record.hedge_account_id == "MT5-002"
    assert record.hedge_client_id == "HEDGE-CLIENT"


def test_completed_schema1_id_only_intent_loads_and_recovers(tmp_path: Path) -> None:
    path = Path(_state_path(tmp_path))
    store = JsonStateStore(path)
    store.begin_source(
        "O-LEGACY-COMPLETED",
        BusinessOrderSide.BUY,
        D(2),
        hedge_position_id="10352076527",
        hedge_position_quantity_ounces=D(2),
    )
    intent = store.reserve_source_fill(
        fill_key="O-LEGACY-COMPLETED|V-1|T-1",
        client_order_id="O-LEGACY-COMPLETED",
        trade_id="T-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    store.bind_hedge_order(intent.intent_id, "H-LEGACY-COMPLETED")
    assert store.apply_hedge_fill(
        client_order_id="H-LEGACY-COMPLETED",
        trade_id="HT-1",
        fill_ounces=D(2),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["source_orders"]["O-LEGACY-COMPLETED"][
        "hedge_position_quantity_ounces"
    ]
    legacy_intent = payload["hedge_intents"][intent.intent_id]
    for field in (
        "hedge_position_quantity_ounces",
        "hedge_plan",
        "hedge_leg_index",
        "hedge_leg_filled_ounces",
    ):
        del legacy_intent[field]
    path.write_text(json.dumps(payload), encoding="utf-8")

    legacy = JsonStateStore(path)

    loaded = legacy.intent(intent.intent_id)
    assert loaded.status is ObligationStatus.COMPLETED
    assert loaded.hedge_position_id == "10352076527"
    assert loaded.hedge_position_quantity_ounces is None
    assert legacy.recover_for_start() is None


def test_source_reservation_atomically_persists_one_shot_freeze(tmp_path: Path) -> None:
    path = _state_path(tmp_path)
    store = JsonStateStore(path)

    store.begin_source(
        "O-ONE-SHOT",
        BusinessOrderSide.BUY,
        D(2),
        source_freeze_reason="one-shot source attempt claimed",
    )

    reloaded = JsonStateStore(path)
    assert [record.client_order_id for record in reloaded.source_orders()] == ["O-ONE-SHOT"]
    assert reloaded.active_source_order_id == "O-ONE-SHOT"
    assert reloaded.source_freeze_reason == "one-shot source attempt claimed"

    reloaded.update_source_status("O-ONE-SHOT", "REJECTED")
    rejected = JsonStateStore(path)
    assert rejected.active_source_order_id is None
    assert rejected.source_orders()[0].status == "REJECTED"
    assert rejected.source_freeze_reason == "one-shot source attempt claimed"
    assert not rejected.can_submit_source()


def test_distinct_late_fill_is_hedged_once_but_its_duplicate_is_not(tmp_path: Path) -> None:
    store = JsonStateStore(_state_path(tmp_path))
    store.begin_source("O-LATE", BusinessOrderSide.BUY, D(2))
    first = store.reserve_source_fill(
        fill_key="O-LATE|V-1|T-EARLY",
        client_order_id="O-LATE",
        trade_id="T-EARLY",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    store.update_source_status("O-LATE", "CANCELED")
    assert not store.can_submit_source()
    assert store.active_source_order_id == "O-LATE"
    late = store.reserve_source_fill(
        fill_key="O-LATE|V-1|T-LATE",
        client_order_id="O-LATE",
        trade_id="T-LATE",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    duplicate_late = store.reserve_source_fill(
        fill_key="O-LATE|V-1|T-LATE",
        client_order_id="O-LATE",
        trade_id="T-LATE",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    assert first is not None
    assert late is not None
    assert duplicate_late is None
    assert len(store.intents()) == 2
    assert store.rounding_residual_ounces == 0
    assert store.net_unhedged_ounces == D(2)


def test_nonzero_rounding_residual_blocks_new_source_after_cancel_reconciliation(
    tmp_path: Path,
) -> None:
    store = JsonStateStore(_state_path(tmp_path))
    store.begin_source("O-DUST", BusinessOrderSide.BUY, D(1))
    assert store.reserve_source_fill(
        fill_key="O-DUST|V-1|T-DUST",
        client_order_id="O-DUST",
        trade_id="T-DUST",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D("0.5"),
    ) is None
    store.update_source_status("O-DUST", "CANCELED")
    store.confirm_source_reconciled("O-DUST")

    assert store.rounding_residual_ounces == D("0.5")
    assert not store.can_submit_source()


def test_source_reconciliation_persist_failure_rolls_back_memory_and_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _state_path(tmp_path)
    store = JsonStateStore(path)
    store.begin_source("O-RECONCILE", BusinessOrderSide.BUY, D(1))
    store.update_source_status("O-RECONCILE", "CANCELED")
    expected_record = store.source_order("O-RECONCILE")
    expected_halt = store.halt_reason

    def fail_persist() -> None:
        raise OSError("injected reconciliation persistence failure")

    monkeypatch.setattr(store, "_persist", fail_persist)

    with pytest.raises(OSError, match="reconciliation persistence"):
        store.confirm_source_reconciled("O-RECONCILE")

    assert store.active_source_order_id == "O-RECONCILE"
    assert store.halt_reason == expected_halt
    assert store.source_order("O-RECONCILE") == expected_record
    assert not store.can_submit_source()

    reloaded = JsonStateStore(path)
    assert reloaded.active_source_order_id == "O-RECONCILE"
    assert reloaded.halt_reason == expected_halt
    assert reloaded.source_order("O-RECONCILE") == expected_record
    assert not reloaded.can_submit_source()


@pytest.mark.parametrize("terminal_status", ["CANCELED", "EXPIRED"])
@pytest.mark.parametrize("prior_hold", [None, "restart", "hedge"])
def test_source_terminal_confirmation_preserves_unrelated_durable_hold(
    tmp_path: Path, terminal_status: str, prior_hold: str | None,
) -> None:
    path = _state_path(tmp_path)
    store = JsonStateStore(path)
    store.begin_source("O-TERMINAL", BusinessOrderSide.BUY, D(2))
    if prior_hold == "restart":
        store.recover_for_start()
    elif prior_hold == "hedge":
        intent = store.reserve_source_fill(
            fill_key="O-TERMINAL|V-1|T-1", client_order_id="O-TERMINAL",
            trade_id="T-1", source_side=BusinessOrderSide.BUY, fill_ounces=D(1),
        )
        assert intent is not None
        store.block_hedge_intent(intent.intent_id, "exact ticket has changed")
    original_reason = store.halt_reason

    store.update_source_status("O-TERMINAL", terminal_status)
    if prior_hold is not None:
        assert store.halt_reason == original_reason
        assert JsonStateStore(path).halt_reason == original_reason
    assert not store.can_submit_source()
    store.confirm_source_reconciled("O-TERMINAL")
    store.confirm_source_reconciled("O-TERMINAL")  # Duplicate proof is harmless.

    for state in (store, JsonStateStore(path)):
        assert state.active_source_order_id is None
        assert state.halt_reason == original_reason
        assert state.can_submit_source() is (prior_hold is None)
        record = state.source_order("O-TERMINAL")
        assert record is not None and record.status == terminal_status
        if prior_hold == "hedge":
            assert state.intents()[0].status is ObligationStatus.BLOCKED
            assert state.net_unhedged_ounces == D(1)


def test_post_replace_sync_failure_keeps_the_source_fill_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(_state_path(tmp_path))
    store = JsonStateStore(path)
    store.begin_source("O-UNCERTAIN", BusinessOrderSide.BUY, D(1))
    original_replace = os.replace

    def replace_then_fail(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        original_replace(source, destination)
        raise ParentDirectorySyncError("replacement completed but parent sync failed")

    monkeypatch.setattr(store_module, "replace_and_sync_parent", replace_then_fail)

    with pytest.raises(ParentDirectorySyncError, match="replacement completed"):
        store.reserve_source_fill(
            fill_key="O-UNCERTAIN|V-1|T-1",
            client_order_id="O-UNCERTAIN",
            trade_id="T-1",
            source_side=BusinessOrderSide.BUY,
            fill_ounces=D(1),
        )

    assert store.has_seen_source_fill("O-UNCERTAIN|V-1|T-1")
    assert len(store.intents()) == 1
    reloaded = JsonStateStore(path)
    assert reloaded.has_seen_source_fill("O-UNCERTAIN|V-1|T-1")
    assert len(reloaded.intents()) == 1


def test_restart_marks_inflight_source_unknown_and_blocks_second_source(tmp_path: Path) -> None:
    path = _state_path(tmp_path)
    JsonStateStore(path).begin_source("O-UNKNOWN", BusinessOrderSide.BUY, D(2))

    restarted = JsonStateStore(path)
    reason = restarted.recover_for_start()

    assert reason is not None
    assert "O-UNKNOWN" in reason
    assert not restarted.can_submit_source()
    assert JsonStateStore(path).halt_reason == reason
    with pytest.raises(RuntimeError, match="blocked"):
        restarted.begin_source("O-2", BusinessOrderSide.SELL, D(1))


def _state_path(tmp_path: Path) -> str:
    return str(tmp_path) + "/taker.state.json"
