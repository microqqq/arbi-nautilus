"""Pure held source projection; venue completeness and cache routes are prerequisites."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    StrategyId,
    TradeId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import LimitOrder, Order
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

import py000_nautilus.maker_store as maker_module
import py000_nautilus.store as store_module
from py000_nautilus.app import _hedge_instrument, _source_instrument
from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent
from py000_nautilus.maker_migration import migrate_maker_state
from py000_nautilus.maker_store import MakerStateStore, maker_legacy_paths
from py000_nautilus.models import BusinessOrderSide, ObligationStatus, SourceDirection
from py000_nautilus.source_projection import project_source_fills
from py000_nautilus.store import JsonStateStore

D = Decimal
BUY, SELL = BusinessOrderSide.BUY, BusinessOrderSide.SELL
LONG, SHORT = SourceDirection.LONG, SourceDirection.SHORT
TRADER = TestIdStubs.trader_id()
STRATEGY = StrategyId("W6B2-001")
ACCOUNT = AccountId("BITFINEX-001")
SOURCE, HEDGE = _source_instrument().id, _hedge_instrument().id
REASON = "native source recovery remains held"


def _order(
    cid: str = "S",
    amounts: tuple[str, ...] = ("0.4", "0.7", "0.9"),
    *,
    side: BusinessOrderSide = BUY,
    quantity: str = "2",
    fill_change: dict[str, Any] | None = None,
    venue: str | None = None,
    rejected: bool = False,
) -> Order:
    values = CryptoPerpetual.to_dict(_source_instrument())
    values.update(size_precision=1, size_increment="0.1", lot_size="0.1")
    instrument = CryptoPerpetual.from_dict(values)
    order = LimitOrder(
        trader_id=TRADER,
        strategy_id=STRATEGY,
        instrument_id=SOURCE,
        client_order_id=ClientOrderId(cid),
        order_side=OrderSide.BUY if side is BUY else OrderSide.SELL,
        quantity=instrument.make_qty(D(quantity)),
        price=instrument.make_price(2400),
        time_in_force=TimeInForce.GTC,
        init_id=UUID4(),
        ts_init=0,
    )
    order.apply(TestEventStubs.order_submitted(order, ACCOUNT))
    if rejected:
        assert not amounts
        order.apply(TestEventStubs.order_rejected(order, ACCOUNT))
        return order
    order.apply(TestEventStubs.order_accepted(order, ACCOUNT, VenueOrderId(venue or f"V-{cid}")))
    for index, amount in enumerate(amounts):
        fill = TestEventStubs.order_filled(
            order,
            instrument,
            account_id=ACCOUNT,
            trade_id=TradeId(f"{cid}-T{index}"),
            last_qty=instrument.make_qty(D(amount)),
            last_px=instrument.make_price(2400),
            commission=Money(0, USDT),
            ts_event=index + 1,
        )
        if fill_change:
            payload = OrderFilled.to_dict(fill)
            payload.update(fill_change)
            fill = OrderFilled.from_dict(payload)
        order.apply(fill)
    return order


def _new(path: Path, maker: bool) -> JsonStateStore | MakerStateStore:
    return MakerStateStore(path, SOURCE.value, HEDGE.value) if maker else JsonStateStore(path)


def _view(store: JsonStateStore | MakerStateStore, side: BusinessOrderSide = BUY) -> JsonStateStore:
    return (
        store.stores[LONG if side is BUY else SHORT]
        if isinstance(store, MakerStateStore)
        else store
    )


def _begin(view: JsonStateStore, order: Order) -> None:
    view.begin_source(
        order.client_order_id.value,
        BUY if order.side is OrderSide.BUY else SELL,
        order.quantity.as_decimal(),
        source_account_id=ACCOUNT.value,
        source_client_id="BITFINEX",
        hedge_account_id="MT5-001",
        hedge_client_id="MT5",
    )


def _record(view: JsonStateStore, order: Order, count: int) -> None:
    fills = [event for event in order.events if isinstance(event, OrderFilled)]
    for fill in fills[:count]:
        view.reserve_source_fill(
            fill_key=f"{fill.client_order_id.value}|{fill.venue_order_id.value}|{fill.trade_id.value}",
            client_order_id=fill.client_order_id.value,
            trade_id=fill.trade_id.value,
            source_side=BUY if order.side is OrderSide.BUY else SELL,
            fill_ounces=fill.last_qty.as_decimal(),
        )


def _project(store: JsonStateStore | MakerStateStore, *orders: Order, **expected: Any) -> int:
    identities = dict(source_instrument_id=SOURCE, trader_id=TRADER, strategy_id=STRATEGY)
    identities.update(expected)
    return project_source_fills(store, orders, reason=REASON, **identities)


def _reload(store: JsonStateStore | MakerStateStore) -> JsonStateStore | MakerStateStore:
    if isinstance(store, MakerStateStore):
        return MakerStateStore(
            str(store.path).removesuffix(".maker.json"), SOURCE.value, HEDGE.value
        )
    return JsonStateStore(store.path)


def _assert_refused(
    store: JsonStateStore | MakerStateStore, *orders: Order, **expected: Any
) -> None:
    before = deepcopy(store._to_payload())
    disk = store.path.read_bytes()
    with pytest.raises(ValueError, match="source projection|Maker"):
        _project(store, *orders, **expected)
    assert store._to_payload() == before and store.path.read_bytes() == disk


def test_existing_live_entry_is_not_a_held_suffix_transaction(tmp_path: Path) -> None:
    """Observed old-entry gap, not a claim that ordinary live behavior is incorrect."""
    store = JsonStateStore(tmp_path / "live")
    order = _order(amounts=("0.4", "0.6"), quantity="1")
    _begin(store, order)
    store.update_source_status("S", "CANCELED")
    old_reason = store.halt_reason
    assert old_reason is not None
    _record(store, order, 2)
    assert store.halt_reason is None and store.intents()[0].status is ObligationStatus.PENDING
    assert (
        store.reserve_source_fill(
            fill_key="S|V-S|S-T1",
            client_order_id="S",
            trade_id="S-T1",
            source_side=BUY,
            fill_ounces=D(99),
        )
        is None
    )  # Original dedup does not authenticate a changed duplicate's quantity.


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("prior_hold", [None, "CANCELED", "unrelated restart HOLD"])
def test_suffix_publishes_once_with_blocked_intents_and_preserved_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maker: bool,
    prior_hold: str | None,
) -> None:
    store = _new(tmp_path / "held", maker)
    view = _view(store)
    order = _order()
    _begin(view, order)
    _record(view, order, 1)
    if prior_hold == "CANCELED":
        view.update_source_status("S", "CANCELED")
    elif prior_hold:
        view.mark_source_unknown("S", prior_hold)
    original_halt, original_freeze = view.halt_reason, view.source_freeze_reason
    publications: list[dict[str, object]] = []
    original = store_module._persist_payload

    def observe(path: Path, payload: dict[str, object]) -> None:
        publications.append(deepcopy(payload))
        original(path, payload)

    monkeypatch.setattr(store_module, "_persist_payload", observe)
    monkeypatch.setattr(maker_module, "_persist_payload", observe)
    native = list(order.events)
    assert _project(store, order) == 2 and len(publications) == 1
    assert order.events == native
    published = store.path.read_bytes()
    assert _project(store, order) == 0 and _project(_reload(store), order) == 0
    assert len(publications) == 1 and store.path.read_bytes() == published
    for current in (store, _reload(store)):
        target = _view(current)
        record = target.source_order("S")
        assert record is not None and record.filled_ounces == 2
        assert len(target.intents()) == 2
        assert all(intent.status is ObligationStatus.BLOCKED for intent in target.intents())
        assert target.halt_reason == (original_halt or REASON)
        assert target.source_freeze_reason == (original_freeze or REASON)
        assert not target.can_submit_source()
        if isinstance(current, MakerStateStore):
            assert current.next_pending_hedge() is None
            assert all(
                item.halt_reason and item.source_freeze_reason for item in current.stores.values()
            )
            assert len(current._allocations) == 3


@pytest.mark.parametrize("maker", [False, True])
def test_sell_suffix_preserves_signed_residual_and_blocks_buy_obligation(
    tmp_path: Path,
    maker: bool,
) -> None:
    store = _new(tmp_path / "sell", maker)
    order = _order(amounts=("0.5", "0.6"), side=SELL)
    view = _view(store, SELL)
    _begin(view, order)
    assert _project(store, order) == 2
    assert view.rounding_residual_ounces == D("-0.1")
    assert len(view.intents()) == 1
    assert view.intents()[0].hedge_side is BUY
    assert view.intents()[0].status is ObligationStatus.BLOCKED


@pytest.mark.parametrize(
    "field",
    [
        "trader_id",
        "strategy_id",
        "source_instrument_id",
        "account",
        "side",
        "quantity",
        "missing",
        "duplicate",
        "extra",
        "fill_owner",
        "fill_side",
        "fill_venue",
    ],
)
def test_identity_and_complete_order_set_conflicts_do_not_write(tmp_path: Path, field: str) -> None:
    store = JsonStateStore(tmp_path / "identity")
    original = _order()
    _begin(store, original)
    _record(store, original, 1)
    expected: dict[str, Any] = {}
    orders = [original]
    if field in {"trader_id", "strategy_id", "source_instrument_id"}:
        expected[field] = {
            "trader_id": TraderId("OTHER-001"),
            "strategy_id": StrategyId("OTHER-001"),
            "source_instrument_id": InstrumentId.from_str("OTHER.BITFINEX"),
        }[field]
    elif field in {"account", "side", "quantity"}:
        record = store.source_order("S")
        assert record is not None
        changes: dict[str, Any]
        if field == "account":
            changes = {"source_account_id": "BITFINEX-OTHER"}
        elif field == "side":
            changes = {"side": SELL}
        else:
            changes = {"quantity_ounces": D(3)}
        store._state.source_orders["S"] = replace(record, **changes)
    elif field == "missing":
        orders = []
    elif field == "duplicate":
        orders.append(original)
    elif field == "extra":
        orders.append(_order("OTHER"))
    elif field == "fill_venue":
        orders = [_order(venue="OTHER")]
    else:
        values = {"fill_owner": {"strategy_id": "OTHER-001"}, "fill_side": {"order_side": "SELL"}}[
            field
        ]
        orders = [_order(fill_change=values)]
    _assert_refused(store, *orders, **expected)


@pytest.mark.parametrize(
    "fault", ["hole", "changed_trade", "prefix_total", "intent_fill", "residual"]
)
def test_recorded_prefix_facts_and_residual_cannot_be_rewritten(tmp_path: Path, fault: str) -> None:
    store = JsonStateStore(tmp_path / "prefix")
    order = _order()
    _begin(store, order)
    _record(store, order, 2)
    if fault == "hole":
        store._state.seen_source_fills.remove("S|V-S|S-T0")
    elif fault == "changed_trade":
        store._state.seen_source_fills.remove("S|V-S|S-T1")
        store._state.seen_source_fills.add("S|V-S|CHANGED")
    elif fault == "prefix_total":
        store._state.source_orders["S"] = replace(
            store._state.source_orders["S"],
            filled_ounces=D("1.2"),
        )
    elif fault == "intent_fill":
        intent = store.intents()[0]
        store._state.hedge_intents[intent.intent_id] = replace(intent, source_fill_ounces=D("0.8"))
    else:
        store._state.net_unhedged_ounces = D("0.4")  # Prefix1.1 - allocated1 is only0.1.
    _assert_refused(store, order)


@pytest.mark.parametrize("fault", ["duplicate_claim", "intent_key"])
def test_recorded_intent_identity_is_unique_even_with_matching_residual(
    tmp_path: Path,
    fault: str,
) -> None:
    store = JsonStateStore(tmp_path / "intent-id")
    order = _order()
    _begin(store, order)
    _record(store, order, 2)
    intent = store.intents()[0]
    if fault == "duplicate_claim":
        store._state.hedge_intents["other"] = replace(intent, intent_id="other")
        store._state.net_unhedged_ounces -= intent.hedge_quantity_ounces
    else:
        store._state.hedge_intents["other"] = store._state.hedge_intents.pop(intent.intent_id)
    _assert_refused(store, order)


@pytest.mark.parametrize("fault", ["source_key", "active_key"])
def test_taker_source_dictionary_and_active_identity_must_be_exact(
    tmp_path: Path,
    fault: str,
) -> None:
    store = JsonStateStore(tmp_path / "source-identity")
    order = _order()
    _begin(store, order)
    if fault == "source_key":
        store._state.source_orders["WRONG"] = store._state.source_orders.pop("S")
    else:
        store._state.active_source_order_id = "WRONG"
    _assert_refused(store, order)


def test_maker_zero_allocation_fills_are_checked_individually(tmp_path: Path) -> None:
    store = _new(tmp_path / "allocation", True)
    original = _order(amounts=("0.1", "0.4", "1.5"))
    _begin(_view(store), original)
    _record(_view(store), original, 2)
    same_total = _order(amounts=("0.2", "0.3", "1.5"))
    assert _view(store).intents() == ()
    _assert_refused(store, same_total)


def test_maker_same_order_allocation_sequence_must_match_native_prefix(tmp_path: Path) -> None:
    store = MakerStateStore(tmp_path / "reversed", SOURCE.value, HEDGE.value)
    order = _order(amounts=("0.4", "0.2", "0.4"), quantity="1")
    view = store.stores[LONG]
    _begin(view, order)
    for trade, amount in (("S-T1", "0.2"), ("S-T0", "0.4")):
        view.reserve_source_fill(
            fill_key=f"S|V-S|{trade}",
            client_order_id="S",
            trade_id=trade,
            source_side=BUY,
            fill_ounces=D(amount),
        )
    store._validate()  # Conserved totals and a contiguous seen set do not prove order.
    _assert_refused(store, order)


@pytest.mark.parametrize("case", ["crossed", "no_anchor", "two_missing", "anchored"])
def test_maker_suffix_requires_an_unambiguous_whole_owner_tail(tmp_path: Path, case: str) -> None:
    store = MakerStateStore(tmp_path / "tail", SOURCE.value, HEDGE.value)
    first = _order("A", ("0.4", "0.7"))
    second = _order("B", ("0.2", "0.2") if case == "two_missing" else ("0.2",), side=SELL)
    _begin(store.stores[LONG], first)
    _begin(store.stores[SHORT], second)
    if case == "anchored":
        _record(store.stores[SHORT], second, 1)
    if case != "no_anchor":
        _record(store.stores[LONG], first, 1)
    if case != "anchored":
        _record(store.stores[SHORT], second, 1)
    if case == "anchored":
        original_allocations = list(store._allocations)
        assert _project(store, second, first) == 1  # Input/cache traversal order is irrelevant.
        assert store._allocations[:-1] == original_allocations
        assert store._allocations[-1].fill_key == "A|V-A|A-T1"
    else:
        _assert_refused(store, first, second)


def test_same_account_trade_id_cannot_belong_to_two_source_orders(tmp_path: Path) -> None:
    store = MakerStateStore(tmp_path / "trade", SOURCE.value, HEDGE.value)
    first = _order("A", ("0.4",), fill_change={"trade_id": "SAME"})
    second = _order("B", ("0.4",), side=SELL, fill_change={"trade_id": "SAME"})
    _begin(store.stores[LONG], first)
    _begin(store.stores[SHORT], second)
    _assert_refused(store, first, second)


@pytest.mark.parametrize(
    "history", ["filled", "rejected", "partial_cancel", "hedge_pending", "residual"]
)
def test_taker_history_requires_terminal_orders_completed_hedges_and_zero_carry(
    tmp_path: Path,
    history: str,
) -> None:
    store = JsonStateStore(tmp_path / "history")
    old = _order(
        "OLD", () if history == "rejected" else ("1",), quantity="1", rejected=history == "rejected"
    )
    _begin(store, old)
    if history == "rejected":
        store.update_source_status("OLD", "REJECTED")
    else:
        _record(store, old, 1)
        intent = store.intents()[0]
        store.bind_hedge_order(intent.intent_id, "OLD-HEDGE")
        store.apply_hedge_fill(client_order_id="OLD-HEDGE", trade_id="DONE", fill_ounces=D(1))
    order = _order()
    _begin(store, order)
    if history == "partial_cancel":
        store._state.source_orders["OLD"] = replace(
            store._state.source_orders["OLD"],
            status="CANCELED",
        )
    elif history == "hedge_pending":
        intent = store.intents()[0]
        store._state.hedge_intents[intent.intent_id] = replace(
            intent, status=ObligationStatus.PENDING
        )
    elif history == "residual":
        intent = store.intents()[0]
        store._state.hedge_intents[intent.intent_id] = replace(
            intent,
            hedge_quantity_ounces=D("0.9"),
            hedge_filled_ounces=D("0.9"),
        )
        store._state.net_unhedged_ounces = D("0.1")
    if history in {"filled", "rejected"}:
        assert _project(store, order, old) == 3
    else:
        _assert_refused(store, old, order)


def test_taker_native_accepted_history_is_not_a_known_zero_fill_rejection(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "nonterminal")
    old = _order("OLD", (), quantity="1")
    _begin(store, old)
    store.update_source_status("OLD", "REJECTED")
    order = _order()
    _begin(store, order)
    _assert_refused(store, old, order)


@pytest.mark.parametrize("legacy_candidate", [False, True])
def test_v4_checkpoint_is_read_only_and_only_new_orders_can_gain_a_suffix(
    tmp_path: Path,
    legacy_candidate: bool,
) -> None:
    prefix = tmp_path / "legacy"
    legacy = JsonStateStore(maker_legacy_paths(prefix)[0])
    old = _order("OLD", ("0.4", "0.6") if legacy_candidate else ("1",), quantity="1")
    _begin(legacy, old)
    _record(legacy, old, 1)
    JsonStateStore(maker_legacy_paths(prefix)[1]).freeze_source_submissions("legacy ask HOLD")
    owner = migrate_maker_state(prefix, tmp_path / "v4", SOURCE.value, HEDGE.value, stopped=True)
    checkpoint = deepcopy(owner._to_payload()["legacy_checkpoint"])
    if legacy_candidate:
        _assert_refused(owner, old)
    else:
        new = _order("NEW", ("1",), side=SELL, quantity="1")
        owner.stores[SHORT]._state.source_freeze_reason = None
        _begin(owner.stores[SHORT], new)
        assert _project(owner, old, new) == 1
        assert owner._to_payload()["legacy_checkpoint"] == checkpoint
        assert _project(_reload(owner), new, old) == 0


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("after_replace", [False, True])
def test_suffix_publish_failure_rolls_back_or_retains_the_whole_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maker: bool,
    after_replace: bool,
) -> None:
    store = _new(tmp_path / "atomic", maker)
    order = _order()
    _begin(_view(store), order)
    _record(_view(store), order, 1)
    before, disk_before = deepcopy(store._to_payload()), store.path.read_bytes()
    original = replace_and_sync_parent
    publications: list[dict[str, Any]] = []

    def fail(source: Path, destination: Path) -> None:
        publications.append(json.loads(source.read_text()))
        if after_replace:
            os.replace(source, destination)
            raise ParentDirectorySyncError("published candidate; parent sync failed")
        raise OSError("before publication")

    monkeypatch.setattr(store_module, "replace_and_sync_parent", fail)
    with pytest.raises(ParentDirectorySyncError if after_replace else OSError):
        _project(store, order)
    assert len(publications) == 1
    assert store._to_payload() == _reload(store)._to_payload()
    assert (store._to_payload() != before) is after_replace
    assert (store.path.read_bytes() != disk_before) is after_replace
    monkeypatch.setattr(store_module, "replace_and_sync_parent", original)
    assert _project(store, order) == (0 if after_replace else 2)
    assert _project(_reload(store), order) == 0
    assert all(intent.status is ObligationStatus.BLOCKED for intent in _view(store).intents())
