"""Use real Nautilus order/fill event types for source-to-hedge behavior."""

from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.enums import BookType, OrderSide
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.identifiers import ClientOrderId, TradeId, VenueOrderId
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from py000_nautilus.app import _source_instrument, _strategy_config
from py000_nautilus.models import BookTop, BusinessOrderSide, HedgeIntent
from py000_nautilus.strategies.taker import TakerStrategy, _reference_book


class RecordingTakerStrategy(TakerStrategy):
    def __init__(self, state_path: Path) -> None:
        super().__init__(_strategy_config(state_path))
        self.recorded_intents: list[HedgeIntent] = []

    def _submit_hedge_intent(self, intent: HedgeIntent) -> None:
        self.recorded_intents.append(intent)


def _rejected(order: Any, reason: str) -> OrderRejected:
    template = TestEventStubs.order_rejected(order)
    return OrderRejected(
        trader_id=template.trader_id,
        strategy_id=template.strategy_id,
        instrument_id=template.instrument_id,
        client_order_id=template.client_order_id,
        account_id=template.account_id,
        reason=reason,
        event_id=UUID4(),
        ts_event=template.ts_event,
        ts_init=template.ts_init,
    )


def _native_book(
    bids: tuple[tuple[str, str], ...],
    asks: tuple[tuple[str, str], ...],
) -> OrderBook:
    book = OrderBook(_source_instrument().id, BookType.L2_MBP)
    for index, (price, size) in enumerate(bids):
        book.add(
            BookOrder(OrderSide.BUY, Price.from_str(price), Quantity.from_str(size), index),
            ts_event=1,
        )
    for index, (price, size) in enumerate(asks, start=len(bids)):
        book.add(
            BookOrder(OrderSide.SELL, Price.from_str(price), Quantity.from_str(size), index),
            ts_event=1,
        )
    return book


def test_managed_l2_returns_reference_marginal_prices_and_floored_sizes() -> None:
    book = _native_book(
        bids=(("100", "0.6"), ("99.5", "0.7")),
        asks=(("101", "0.4"), ("101.5", "2.3")),
    )

    assert _reference_book(book, Decimal(1)) == BookTop(
        bid=Decimal("99.5"),
        ask=Decimal("101.5"),
        bid_size=Decimal(1),
        ask_size=Decimal(2),
    )


def test_managed_l2_returns_none_when_either_side_is_shallow() -> None:
    shallow_bid = _native_book(
        bids=(("100", "0.4"), ("99.5", "0.5")),
        asks=(("101", "1.1"),),
    )
    shallow_ask = _native_book(
        bids=(("100", "1.1"),),
        asks=(("101", "0.4"), ("101.5", "0.5")),
    )

    assert _reference_book(shallow_bid, Decimal(1)) is None
    assert _reference_book(shallow_ask, Decimal(1)) is None


def test_real_partial_final_duplicate_and_late_order_filled_events(tmp_path: Path) -> None:
    strategy = RecordingTakerStrategy(tmp_path / "state.json")
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(2400),
        client_order_id=ClientOrderId("O-REAL-EVENTS"),
    )
    strategy.state_store.begin_source(
        order.client_order_id.value,
        BusinessOrderSide.BUY,
        Decimal(2),
    )

    partial = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("V-1"),
        trade_id=TradeId("T-PARTIAL"),
        last_qty=instrument.make_qty(1),
        commission=Money(0, instrument.quote_currency),
    )
    final = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("V-1"),
        trade_id=TradeId("T-FINAL"),
        last_qty=instrument.make_qty(1),
        commission=Money(0, instrument.quote_currency),
    )

    strategy.on_order_filled(partial)
    strategy.on_order_filled(final)
    strategy.on_order_filled(partial)  # duplicate partial
    strategy.on_order_filled(final)  # late duplicate after terminal fill

    assert [intent.source_trade_id for intent in strategy.recorded_intents] == [
        "T-PARTIAL",
        "T-FINAL",
    ]
    assert all(
        intent.hedge_quantity_ounces == Decimal(1)
        for intent in strategy.recorded_intents
    )
    assert strategy.state_store.rounding_residual_ounces == 0
    assert strategy.state_store.net_unhedged_ounces == Decimal(2)


def test_unknown_engine_rejection_keeps_source_live_and_blocked(tmp_path: Path) -> None:
    strategy = RecordingTakerStrategy(tmp_path / "unknown.json")
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(1),
        price=instrument.make_price(2400),
        client_order_id=ClientOrderId("O-UNKNOWN"),
    )
    strategy.state_store.begin_source(
        order.client_order_id.value,
        BusinessOrderSide.BUY,
        Decimal(1),
    )

    strategy.on_order_rejected(_rejected(order, "UNKNOWN"))

    record = strategy.state_store.source_order(order.client_order_id.value)
    assert record is not None and record.status == "UNKNOWN"
    assert strategy.state_store.active_source_order_id == order.client_order_id.value
    assert not strategy.state_store.can_submit_source()


class _StopStore:
    def __init__(self, active: str) -> None:
        self.active_source_order_id = active
        self.unknown: list[tuple[str, str]] = []

    def mark_source_unknown(self, client_order_id: str, reason: str) -> None:
        self.unknown.append((client_order_id, reason))


class _WorkingOrder:
    is_closed = False


class _StopCache:
    def __init__(self, expected: str) -> None:
        self.expected = expected
        self.order_value: _WorkingOrder | None = _WorkingOrder()
        self.requested: list[str] = []

    def order(self, client_order_id: ClientOrderId) -> _WorkingOrder | None:
        self.requested.append(client_order_id.value)
        assert client_order_id.value == self.expected
        return self.order_value


class _StopHarness:
    def __init__(self, active: str, *, fail_cancel: bool = False) -> None:
        self.state_store = _StopStore(active)
        self.cache = _StopCache(active)
        self.canceled: list[_WorkingOrder] = []
        self.fail_cancel = fail_cancel

    def cancel_order(self, order: _WorkingOrder) -> None:
        if self.fail_cancel:
            raise RuntimeError("cancel failed")
        self.canceled.append(order)


def test_stop_cancels_only_exact_active_gtc_without_releasing_gate() -> None:
    harness = _StopHarness("O-B")

    TakerStrategy.on_stop(cast(Any, harness))

    assert harness.cache.requested == ["O-B"]
    assert len(harness.canceled) == 1
    assert harness.state_store.active_source_order_id == "O-B"
    assert harness.state_store.unknown == []


def test_stop_cancel_failure_marks_exact_active_unknown() -> None:
    harness = _StopHarness("O-B", fail_cancel=True)

    TakerStrategy.on_stop(cast(Any, harness))

    assert harness.canceled == []
    assert harness.state_store.active_source_order_id == "O-B"
    assert harness.state_store.unknown[0][0] == "O-B"


def test_stop_missing_cached_order_marks_exact_active_unknown() -> None:
    harness = _StopHarness("O-B")
    harness.cache.order_value = None

    TakerStrategy.on_stop(cast(Any, harness))

    assert harness.canceled == []
    assert harness.state_store.active_source_order_id == "O-B"
    assert harness.state_store.unknown[0][0] == "O-B"
