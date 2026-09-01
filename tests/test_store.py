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
    assert store.net_unhedged_ounces == 0


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
