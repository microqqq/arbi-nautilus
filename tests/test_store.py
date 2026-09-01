"""Durable exactly-once and restart-stop behavior."""

from decimal import Decimal
from pathlib import Path

import pytest

from py000_nautilus.models import BusinessOrderSide, ObligationStatus
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
