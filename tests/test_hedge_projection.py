"""Held hedge projection; complete native/venue reconciliation remains a prerequisite."""

import os
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TradeId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import MarketOrder, Order
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

import py000_nautilus.maker_store as maker_module
import py000_nautilus.store as store_module
from py000_nautilus.app import _hedge_instrument, _source_instrument
from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent
from py000_nautilus.hedge_projection import project_hedge_fills
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import BusinessOrderSide, HedgeLeg, ObligationStatus, SourceDirection
from py000_nautilus.store import JsonStateStore

D = Decimal
BUY, SELL = BusinessOrderSide.BUY, BusinessOrderSide.SELL
SOURCE, HEDGE = _source_instrument().id, _hedge_instrument().id
TRADER, STRATEGY = TestIdStubs.trader_id(), StrategyId("W6B3-001")
ACCOUNT = AccountId("MT5-001")
REASON = "native hedge recovery remains held"


def _new(path: Path, maker: bool) -> JsonStateStore | MakerStateStore:
    return MakerStateStore(path, SOURCE.value, HEDGE.value) if maker else JsonStateStore(path)


def _view(store: JsonStateStore | MakerStateStore, side: BusinessOrderSide = BUY) -> JsonStateStore:
    return (store.stores[SourceDirection.LONG if side is BUY else SourceDirection.SHORT]
            if isinstance(store, MakerStateStore) else store)


def _seed(
    view: JsonStateStore, *, planned: bool = True, cid: str = "S",
    hedge_cid: str | None = "H-CLOSE", side: BusinessOrderSide = BUY, quantity: str = "2",
    legs: tuple[HedgeLeg, ...] | None = None, bound: bool = False,
) -> str:
    view.begin_source(
        cid, side, D(quantity), hedge_account_id=ACCOUNT.value, hedge_client_id="MT5",
        hedge_position_id="900000101" if bound else None,
        hedge_position_quantity_ounces=D(quantity) if bound else None,
    )
    intent = view.reserve_source_fill(
        fill_key=f"{cid}|V-{cid}|{cid}-T", client_order_id=cid, trade_id=f"{cid}-T",
        source_side=side, fill_ounces=D(quantity),
    )
    assert intent is not None
    if planned:
        hedge_side = SELL if side is BUY else BUY
        view.bind_hedge_plan(intent.intent_id, legs or (
            HedgeLeg(hedge_side, D(1), "900000101", side, D(1)), HedgeLeg(hedge_side, D(1)),
        ))
    if hedge_cid is not None:
        view.bind_hedge_order(intent.intent_id, hedge_cid)
    return intent.intent_id


def _order(
    cid: str = "H-CLOSE", amounts: tuple[str, ...] = ("1",), *, quantity: str = "1",
    side: BusinessOrderSide = SELL, position: str | None = "900000101", close: bool = True,
    account: AccountId = ACCOUNT, state: str = "ACCEPTED", tif: TimeInForce = TimeInForce.FOK,
    quote: bool = False, updated_quantity: str | None = None,
    fill_change: dict[str, Any] | None = None,
) -> Order:
    instrument = _hedge_instrument()
    order = MarketOrder(
        trader_id=TRADER, strategy_id=STRATEGY, instrument_id=HEDGE,
        client_order_id=ClientOrderId(cid),
        order_side=OrderSide.SELL if side is SELL else OrderSide.BUY,
        quantity=instrument.make_qty(D(quantity)), init_id=UUID4(), ts_init=0,
        time_in_force=tif, reduce_only=close, quote_quantity=quote,
    )
    if state == "INITIALIZED":
        assert not amounts
        return order
    order.apply(TestEventStubs.order_submitted(order, account))
    if state == "REJECTED":
        assert not amounts
        order.apply(TestEventStubs.order_rejected(order, account))
        return order
    order.apply(TestEventStubs.order_accepted(order, account, VenueOrderId(f"V-{cid}")))
    if updated_quantity is not None:
        order.apply(TestEventStubs.order_updated(order, instrument.make_qty(D(updated_quantity))))
    for index, amount in enumerate(amounts):
        fill = TestEventStubs.order_filled(
            order, instrument, account_id=account, trade_id=TradeId(f"{cid}-T{index}"),
            position_id=PositionId(position) if position is not None else None,
            last_qty=instrument.make_qty(D(amount)), last_px=instrument.make_price(2400),
            commission=Money(0, USD), ts_event=index + 1,
        )
        if fill_change:
            payload = OrderFilled.to_dict(fill)
            payload.update(fill_change)
            fill = OrderFilled.from_dict(payload)
        order.apply(fill)
    return order


def _record(view: JsonStateStore, order: Order, count: int) -> None:
    for fill in [event for event in order.events if isinstance(event, OrderFilled)][:count]:
        assert view.apply_hedge_fill(client_order_id=fill.client_order_id.value,
                                     trade_id=fill.trade_id.value,
                                     fill_ounces=fill.last_qty.as_decimal())


def _project(store: JsonStateStore | MakerStateStore, *orders: Order, **expected: Any) -> int:
    identities = dict(hedge_instrument_id=HEDGE, trader_id=TRADER, strategy_id=STRATEGY)
    identities.update(expected)
    return project_hedge_fills(store, orders, reason=REASON, **identities)


def _reload(store: JsonStateStore | MakerStateStore) -> JsonStateStore | MakerStateStore:
    if isinstance(store, MakerStateStore):
        return MakerStateStore(
            str(store.path).removesuffix(".maker.json"), SOURCE.value, HEDGE.value,
        )
    return JsonStateStore(store.path)


def _assert_refused(
    store: JsonStateStore | MakerStateStore, *orders: Order, **expected: Any,
) -> None:
    before, disk = deepcopy(store._to_payload()), store.path.read_bytes()

    def unexpected_write(*_: object) -> None:
        pytest.fail("preflight conflict attempted a publication")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_module, "_persist_payload", unexpected_write)
        patch.setattr(maker_module, "_persist_payload", unexpected_write)
        with pytest.raises(ValueError, match="hedge projection|Maker"):
            _project(store, *orders, **expected)
    assert store._to_payload() == before and store.path.read_bytes() == disk


def _observe(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    publications: list[dict[str, object]] = []
    original = store_module._persist_payload

    def observe(path: Path, payload: dict[str, object]) -> None:
        publications.append(deepcopy(payload))
        original(path, payload)

    monkeypatch.setattr(store_module, "_persist_payload", observe)
    monkeypatch.setattr(maker_module, "_persist_payload", observe)
    return publications


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("failure", ["publish", "reducer"])
def test_live_hedge_fill_failure_restores_memory_and_allows_exact_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, failure: str,
) -> None:
    store = _new(tmp_path / "rollback", maker)
    view = _view(store)
    _seed(view)
    before, disk = deepcopy(store._to_payload()), store.path.read_bytes()

    def fail(*_: object) -> None:
        raise OSError("before atomic publication")

    with monkeypatch.context() as patch:
        if failure == "publish":
            patch.setattr(store_module, "_persist_payload", fail)
            patch.setattr(maker_module, "_persist_payload", fail)
        with pytest.raises((OSError, ValueError)):
            view.apply_hedge_fill(
                client_order_id="H-CLOSE", trade_id="H-T",
                fill_ounces=D(1) if failure == "publish" else D("Infinity"),
            )
    assert store.path.read_bytes() == disk
    assert store._to_payload() == before
    assert view.apply_hedge_fill(client_order_id="H-CLOSE", trade_id="H-T", fill_ounces=D(1))
    assert not view.apply_hedge_fill(client_order_id="H-CLOSE", trade_id="H-T", fill_ounces=D(1))


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("prefix", [0, 1])
def test_complete_current_suffix_is_one_held_publication_then_reload_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, planned: bool, prefix: int,
) -> None:
    store = _new(tmp_path / "current", maker)
    view = _view(store)
    identity = _seed(view, planned=planned, legs=(HedgeLeg(SELL, D(2)),))
    order = _order(amounts=("1", "1"), quantity="2", close=False, position="900000202")
    _record(view, order, prefix)
    view.mark_source_unknown("S", "original restart HOLD")
    view.freeze_source_submissions("original source freeze")
    original = view.intent(identity)
    original_freeze = view.source_freeze_reason
    other_freeze = _view(store, SELL).source_freeze_reason
    allocation = deepcopy(store._to_payload().get("allocations"))
    native = list(order.events)
    publications = _observe(monkeypatch)
    assert _project(store, order) == 2 - prefix
    assert len(publications) == 1
    current = view.intent(identity)
    assert current.status is ObligationStatus.BLOCKED
    assert current.hedge_filled_ounces == D(2)
    assert current.hedge_leg_filled_ounces == (D(2) if planned else D(0))
    assert current.hedge_leg_index == original.hedge_leg_index
    assert current.hedge_client_order_id == original.hedge_client_order_id == "H-CLOSE"
    assert current.hedge_order_ids == original.hedge_order_ids
    assert view.halt_reason == "original restart HOLD"
    assert view.source_freeze_reason == original_freeze
    assert store._to_payload().get("allocations") == allocation and order.events == native
    if isinstance(store, MakerStateStore):
        assert _view(store, SELL).halt_reason == REASON
        assert _view(store, SELL).source_freeze_reason == (other_freeze or REASON)
    published = store.path.read_bytes()
    assert _project(store, order) == _project(_reload(store), order) == 0
    assert len(publications) == 1 and store.path.read_bytes() == published


@pytest.mark.parametrize("maker", [False, True])
def test_old_leg_is_checked_without_recounting_it_into_current_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    store = _new(tmp_path / "old", maker)
    view = _view(store)
    identity = _seed(view)
    old = _order()
    _record(view, old, 1)
    view.bind_hedge_order(identity, "H-OPEN")
    current = _order("H-OPEN", close=False, position="900000202")
    original_freeze = view.source_freeze_reason
    publications = _observe(monkeypatch)
    assert _project(store, current, old) == 1  # Native iteration is not the plan order.
    intent = view.intent(identity)
    assert intent.hedge_order_ids == ("H-CLOSE", "H-OPEN")
    assert intent.hedge_leg_index == 1 and intent.hedge_client_order_id == "H-OPEN"
    assert intent.hedge_filled_ounces == 2 and intent.hedge_leg_filled_ounces == 1
    assert intent.status is ObligationStatus.BLOCKED
    assert view.halt_reason == REASON
    assert view.source_freeze_reason == (original_freeze or REASON)
    assert len(publications) == 1 and _project(_reload(store), old, current) == 0


@pytest.mark.parametrize("state", ["INITIALIZED", "ACCEPTED", "REJECTED"])
def test_unfilled_bound_close_has_no_native_position_yet_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    store = JsonStateStore(tmp_path / "unfilled")
    _seed(store)
    order = _order(amounts=(), state=state)
    assert order.position_id is None
    before = deepcopy(store._to_payload())
    publications = _observe(monkeypatch)
    assert _project(store, order) == 0
    assert not publications and store._to_payload() == before and store.halt_reason is None


@pytest.mark.parametrize("stage", ["unbound", "between", "completed", "legacy_completed"])
def test_no_delta_preserves_unbound_between_and_completed_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    store = JsonStateStore(tmp_path / "stage")
    identity = _seed(store, hedge_cid=None if stage == "unbound" else "H-CLOSE",
                     planned=stage != "legacy_completed")
    orders: list[Order] = []
    if stage == "legacy_completed":
        store._state.hedge_intents[identity] = replace(store.intent(identity), hedge_order_ids=())
        orders.append(_order(amounts=("2",), quantity="2", close=False))
        _record(store, orders[0], 1)
        assert store.intent(identity).hedge_client_order_id == "H-CLOSE"
    elif stage != "unbound":
        orders.append(_order())
        _record(store, orders[0], 1)
        if stage == "completed":
            store.bind_hedge_order(identity, "H-OPEN")
            orders.append(_order("H-OPEN", close=False, position="900000202"))
            _record(store, orders[1], 1)
        assert store.intent(identity).hedge_client_order_id is None
    before = deepcopy(store._to_payload())
    publications = _observe(monkeypatch)
    assert _project(store, *orders) == _project(_reload(store), *orders) == 0
    assert not publications and store._to_payload() == before


def test_legacy_single_current_cid_without_history_can_be_held(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "legacy")
    identity = _seed(store, planned=False)
    store._state.hedge_intents[identity] = replace(store.intent(identity), hedge_order_ids=())
    store._persist()
    order = _order(amounts=("2",), quantity="2", close=False)
    assert _project(store, order) == 1
    assert store.intent(identity).hedge_order_ids == ()
    assert store.intent(identity).status is ObligationStatus.BLOCKED
    assert _project(_reload(store), order) == 0


@pytest.mark.parametrize("seen", [False, True])
def test_partial_native_current_is_not_a_complete_fok_suffix(tmp_path: Path, seen: bool) -> None:
    store = JsonStateStore(tmp_path / "partial")
    identity = _seed(store, legs=(HedgeLeg(SELL, D(2)),))
    order = _order(quantity="2", close=False)
    assert order.status is OrderStatus.PARTIALLY_FILLED
    if seen:
        _record(store, order, 1)
        assert store.intent(identity).status is ObligationStatus.BLOCKED
        before = deepcopy(store._to_payload())
        assert _project(store, order) == 0 and store._to_payload() == before
    else:
        _assert_refused(store, order)


@pytest.mark.parametrize("fault", [
    "trader_id", "strategy_id", "hedge_instrument_id", "account", "side", "initial_quantity",
    "current_quantity", "quote", "tif", "reduce_only", "position", "missing_position",
    "fill_owner", "fill_side",
])
def test_order_and_fill_facts_must_match_before_any_write(tmp_path: Path, fault: str) -> None:
    store = JsonStateStore(tmp_path / "facts")
    _seed(store)
    expected: dict[str, Any] = {}
    options: dict[str, Any] = {}
    if fault in {"trader_id", "strategy_id", "hedge_instrument_id"}:
        expected[fault] = {"trader_id": TraderId("OTHER-001"),
                           "strategy_id": StrategyId("OTHER-001"),
                           "hedge_instrument_id": InstrumentId.from_str("OTHER.MT5")}[fault]
    else:
        options = {
            "account": {"account": AccountId("MT5-OTHER")}, "side": {"side": BUY},
            "initial_quantity": {"quantity": "2", "updated_quantity": "1"},
            "current_quantity": {"updated_quantity": "2"}, "quote": {"quote": True},
            "tif": {"tif": TimeInForce.IOC}, "reduce_only": {"close": False},
            "position": {"position": "900000999"}, "missing_position": {"position": None},
            "fill_owner": {"fill_change": {"strategy_id": "OTHER-001"}},
            "fill_side": {"fill_change": {"order_side": "BUY"}},
        }[fault]
    _assert_refused(store, _order(**options), **expected)


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "source_key", "active",
                                       "intent_key", "current", "history", "source_collision"])
def test_exact_owned_cid_set_and_dictionary_identity_are_required(
    tmp_path: Path, fault: str,
) -> None:
    store = JsonStateStore(tmp_path / "identity")
    identity = _seed(store)
    orders = [_order()]
    if fault == "missing":
        orders = []
    elif fault == "extra":
        orders.append(_order("OTHER"))
    elif fault == "duplicate":
        orders.append(orders[0])
    elif fault == "source_key":
        store._state.source_orders["WRONG"] = store._state.source_orders.pop("S")
    elif fault == "active":
        store._state.active_source_order_id = "WRONG"
    elif fault == "intent_key":
        store._state.hedge_intents["WRONG"] = store._state.hedge_intents.pop(identity)
    elif fault == "current":
        store._state.hedge_intents[identity] = replace(store.intent(identity),
                                                     hedge_client_order_id="OTHER")
    elif fault == "history":
        store._state.hedge_intents[identity] = replace(store.intent(identity),
                                                     hedge_order_ids=("OLD", "H-CLOSE"))
    else:
        store._state.hedge_intents[identity] = replace(
            store.intent(identity), hedge_order_ids=("S",), hedge_client_order_id="S",
        )
        orders = [_order("S")]
    _assert_refused(store, *orders)


@pytest.mark.parametrize("fault", ["old_missing_seen", "old_changed_trade", "old_incomplete",
                                       "reversed_history", "current_total", "aggregate", "unknown"])
def test_advanced_history_cannot_be_recounted_or_reassigned_to_current(
    tmp_path: Path, fault: str,
) -> None:
    store = JsonStateStore(tmp_path / "history")
    identity = _seed(store)
    old, current = _order(), _order("H-OPEN", close=False, position="900000202")
    _record(store, old, 1)
    store.bind_hedge_order(identity, "H-OPEN")
    if fault == "old_missing_seen":
        store._state.seen_hedge_fills.clear()
    elif fault == "old_changed_trade":
        old = _order(fill_change={"trade_id": "CHANGED"})
    elif fault == "old_incomplete":
        old = _order(amounts=())
    elif fault == "reversed_history":
        store._state.hedge_intents[identity] = replace(
            store.intent(identity), hedge_order_ids=("H-OPEN", "H-CLOSE"),
            hedge_client_order_id="H-CLOSE",
        )
    elif fault == "current_total":
        store._state.hedge_intents[identity] = replace(
            store.intent(identity), status=ObligationStatus.BLOCKED,
            hedge_leg_filled_ounces=D(1), hedge_filled_ounces=D(2),
        )
    elif fault == "aggregate":
        store._state.hedge_intents[identity] = replace(
            store.intent(identity), status=ObligationStatus.BLOCKED, hedge_filled_ounces=D(0),
        )
    else:
        store._state.seen_hedge_fills.add("UNKNOWN|TRADE")
    _assert_refused(store, old, current)


@pytest.mark.parametrize("fault", ["hole", "changed_trade", "completed_unseen", "many_legacy_ids"])
def test_current_seen_prefix_and_legacy_completion_are_not_rewritten(
    tmp_path: Path, fault: str,
) -> None:
    store = JsonStateStore(tmp_path / "prefix")
    identity = _seed(store, planned=False)
    order = _order(amounts=("1", "1"), quantity="2", close=False)
    _record(store, order, 1)
    if fault in {"hole", "changed_trade"}:
        store._state.seen_hedge_fills = {"H-CLOSE|H-CLOSE-T1" if fault == "hole"
                                        else "H-CLOSE|CHANGED"}
    elif fault == "completed_unseen":
        store._state.hedge_intents[identity] = replace(store.intent(identity),
                                                     status=ObligationStatus.COMPLETED)
    else:
        store._state.hedge_intents[identity] = replace(store.intent(identity),
                                                     hedge_order_ids=("OLD", "H-CLOSE"))
    _assert_refused(store, order)


@pytest.mark.parametrize("fault", [None, "source", "intent", "plan_quantity", "plan_position"])
def test_bound_one_ticket_exit_must_agree_with_source_intent_and_plan(
    tmp_path: Path, fault: str | None,
) -> None:
    store = JsonStateStore(tmp_path / "bound")
    identity = _seed(store, bound=True,
                     legs=(HedgeLeg(SELL, D(2), "900000101", BUY, D(2)),))
    order = _order(amounts=("2",), quantity="2")
    if fault == "source":
        store._state.source_orders["S"] = replace(store._state.source_orders["S"],
                                                  hedge_position_id="900000999")
    elif fault == "intent":
        store._state.hedge_intents[identity] = replace(store.intent(identity),
                                                     hedge_position_id="900000999")
    elif fault in {"plan_quantity", "plan_position"}:
        leg = HedgeLeg(SELL, D(2), "900000999" if fault == "plan_position" else "900000101",
                       BUY, D(3) if fault == "plan_quantity" else D(2))
        store._state.hedge_intents[identity] = replace(store.intent(identity), hedge_plan=(leg,))
    if fault is not None:
        _assert_refused(store, order)
    else:
        assert _project(store, order) == 1 and _project(_reload(store), order) == 0


def _batch(store: JsonStateStore | MakerStateStore) -> tuple[Order, ...]:
    if isinstance(store, MakerStateStore):
        targets = [(_view(store), "A", BUY), (_view(store, SELL), "B", SELL)]
        for view, cid, side in targets:
            view.begin_source(
                cid, side, D(2), hedge_account_id=ACCOUNT.value, hedge_client_id="MT5",
            )
    else:
        store.begin_source("S", BUY, D(4), hedge_account_id=ACCOUNT.value, hedge_client_id="MT5")
        targets = [(store, "S", BUY), (store, "S", BUY)]
    orders = []
    for index, (view, cid, side) in enumerate(targets):
        intent = view.reserve_source_fill(fill_key=f"{cid}|V-{cid}|ST{index}", client_order_id=cid,
                                          trade_id=f"ST{index}", source_side=side, fill_ounces=D(2))
        assert intent is not None
        view.bind_hedge_plan(intent.intent_id, (HedgeLeg(intent.hedge_side, D(2)),))
        hedge_cid = f"H{index}"
        view.bind_hedge_order(intent.intent_id, hedge_cid)
        orders.append(_order(hedge_cid, ("1", "1"), quantity="2", close=False,
                             side=intent.hedge_side, position=f"90000020{index}"))
    return tuple(orders)


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("after_replace", [False, True])
def test_multi_intent_batch_is_atomic_before_and_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, after_replace: bool,
) -> None:
    store = _new(tmp_path / "batch", maker)
    orders = _batch(store)
    before, disk = deepcopy(store._to_payload()), store.path.read_bytes()
    attempts: list[Path] = []

    def fail(source: Path, destination: Path) -> None:
        attempts.append(source)
        if after_replace:
            os.replace(source, destination)
            raise ParentDirectorySyncError("candidate published; parent sync failed")
        raise OSError("before publication")

    monkeypatch.setattr(store_module, "replace_and_sync_parent", fail)
    with pytest.raises(ParentDirectorySyncError if after_replace else OSError):
        _project(store, *reversed(orders))
    assert len(attempts) == 1
    assert store._to_payload() == _reload(store)._to_payload()
    assert (store._to_payload() != before) is after_replace
    assert (store.path.read_bytes() != disk) is after_replace
    monkeypatch.setattr(store_module, "replace_and_sync_parent", replace_and_sync_parent)
    publications = _observe(monkeypatch)
    assert _project(store, *orders) == (0 if after_replace else 4)
    assert len(publications) == (0 if after_replace else 1)
    assert _project(_reload(store), *orders) == 0
    assert store._to_payload().get("allocations") == before.get("allocations")
    for view in (store.stores.values() if isinstance(store, MakerStateStore) else (store,)):
        assert all(intent.status is ObligationStatus.BLOCKED and intent.hedge_leg_index == 0
                   and intent.hedge_filled_ounces == intent.hedge_leg_filled_ounces == 2
                   for intent in view.intents())


def test_maker_batch_reducer_failure_restores_both_views_before_any_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MakerStateStore(tmp_path / "reducer", SOURCE.value, HEDGE.value)
    orders = _batch(store)
    before, disk = deepcopy(store._to_payload()), store.path.read_bytes()
    original = JsonStateStore._apply_hedge_fill
    calls = 0

    def fail(view: JsonStateStore, **kwargs: Any) -> bool:
        nonlocal calls
        changed = original(view, **kwargs)
        calls += 1
        if calls == 4:
            raise ValueError("after both views were reduced")
        return changed

    publications = _observe(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(JsonStateStore, "_apply_hedge_fill", fail)
        with pytest.raises(ValueError, match="both views"):
            _project(store, *orders)
    assert calls == 4 and not publications
    assert store._to_payload() == before and store.path.read_bytes() == disk
    assert _project(store, *orders) == 4 and _project(_reload(store), *orders) == 0


@pytest.mark.parametrize("fault", ["same_trade", "same_cid", "other_view_seen"])
def test_cross_intent_identity_cannot_be_shared_or_moved(tmp_path: Path, fault: str) -> None:
    store = MakerStateStore(tmp_path / "ownership", SOURCE.value, HEDGE.value)
    orders = list(_batch(store))
    first = _view(store).intents()[0]
    second_view = _view(store, SELL)
    second = second_view.intents()[0]
    if fault == "same_trade":
        orders = [_order("H0", ("2",), quantity="2", close=False,
                          fill_change={"trade_id": "SAME"}),
                  _order("H1", ("2",), quantity="2", close=False, side=BUY,
                          fill_change={"trade_id": "SAME"})]
    elif fault == "same_cid":
        second_view._state.hedge_intents[second.intent_id] = replace(
            second, hedge_client_order_id=first.hedge_client_order_id,
            hedge_order_ids=first.hedge_order_ids,
        )
    else:
        second_view._state.seen_hedge_fills.add("H0|H0-T0")
    _assert_refused(store, *orders)


@pytest.mark.parametrize("maker", [False, True])
def test_live_after_replace_failure_retains_published_default_leg_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    store = _new(tmp_path / "live-published", maker)
    view = _view(store)
    identity = _seed(view)

    def fail(source: Path, destination: Path) -> None:
        os.replace(source, destination)
        raise ParentDirectorySyncError("published live fill")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "replace_and_sync_parent", fail)
        with pytest.raises(ParentDirectorySyncError):
            view.apply_hedge_fill(client_order_id="H-CLOSE", trade_id="H-T", fill_ounces=D(1))
    assert store._to_payload() == _reload(store)._to_payload()
    assert view.intent(identity).status is ObligationStatus.PENDING
    assert view.intent(identity).hedge_leg_index == 1
    assert not view.apply_hedge_fill(client_order_id="H-CLOSE", trade_id="H-T", fill_ounces=D(1))


def test_projection_requires_owner_scope_and_nonempty_reason(tmp_path: Path) -> None:
    store = MakerStateStore(tmp_path / "owner", SOURCE.value, HEDGE.value)
    with pytest.raises(TypeError, match="whole Maker owner"):
        _project(_view(store))
    with pytest.raises(ValueError, match="HOLD reason"):
        project_hedge_fills(store, (), hedge_instrument_id=HEDGE, trader_id=TRADER,
                            strategy_id=STRATEGY, reason="")


@pytest.mark.parametrize("fault", [None, "swapped_old_ids", "old_seen_missing"])
def test_three_leg_plan_checks_both_old_tickets_before_projecting_open_suffix(
    tmp_path: Path, fault: str | None,
) -> None:
    store = JsonStateStore(tmp_path / "three")
    identity = _seed(store, quantity="3", legs=(
        HedgeLeg(SELL, D(1), "900000101", BUY, D(1)),
        HedgeLeg(SELL, D(1), "900000102", BUY, D(1)), HedgeLeg(SELL, D(1)),
    ))
    first = _order()
    _record(store, first, 1)
    store.bind_hedge_order(identity, "H-MIDDLE")
    second = _order("H-MIDDLE", position="900000102")
    _record(store, second, 1)
    store.bind_hedge_order(identity, "H-OPEN")
    current = _order("H-OPEN", close=False, position="900000202")
    if fault == "swapped_old_ids":
        store._state.hedge_intents[identity] = replace(
            store.intent(identity), hedge_order_ids=("H-MIDDLE", "H-CLOSE", "H-OPEN"),
        )
    elif fault == "old_seen_missing":
        store._state.seen_hedge_fills.remove("H-CLOSE|H-CLOSE-T0")
    if fault is not None:
        _assert_refused(store, first, second, current)
    else:
        assert _project(store, current, second, first) == 1
        intent = store.intent(identity)
        assert intent.hedge_filled_ounces == 3 and intent.hedge_leg_filled_ounces == 1
        assert intent.hedge_leg_index == 2 and intent.hedge_client_order_id == "H-OPEN"
        assert intent.status is ObligationStatus.BLOCKED
        assert _project(_reload(store), first, second, current) == 0
