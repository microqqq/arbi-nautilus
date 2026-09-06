"""Use real Nautilus order/fill event types for source-to-hedge behavior."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from msgspec.structs import replace
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.data import BookOrder, FundingRateUpdate, QuoteTick
from nautilus_trader.model.enums import (
    AccountType,
    BookType,
    OmsType,
    OrderSide,
    OrderStatus,
    TimeInForce,
)
from nautilus_trader.model.events import OrderDenied, OrderExpired, OrderRejected
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, TradeId, VenueOrderId
from nautilus_trader.model.instruments import Cfd
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from py000_nautilus.app import (
    BITFINEX,
    MT5,
    _book_snapshot,
    _hedge_instrument,
    _maker_strategy_config,
    _quote,
    _source_instrument,
    _strategy_config,
)
from py000_nautilus.models import (
    BookTop,
    BusinessOrderSide,
    HedgeAccount,
    HedgeIntent,
    ObligationStatus,
    SourceAccount,
    SourceDirection,
)
from py000_nautilus.strategies.maker import MakerStrategy, SourceTerminalQuery, SourceTerminalResult
from py000_nautilus.strategies.taker import TakerStrategy, _reference_book


class RecordingTakerStrategy(TakerStrategy):
    def __init__(
        self, state_path: Path, *, source_terminal_query: SourceTerminalQuery | None = None,
    ) -> None:
        super().__init__(_strategy_config(state_path), source_terminal_query=source_terminal_query)
        self.recorded_intents: list[HedgeIntent] = []

    def _submit_hedge_intent(self, intent: HedgeIntent) -> None:
        self.recorded_intents.append(intent)
        self.state_store.bind_hedge_order(
            intent.intent_id,
            f"H-RECORDED-{len(self.recorded_intents)}",
        )


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

    assert [intent.source_trade_id for intent in strategy.state_store.intents()] == [
        "T-PARTIAL",
        "T-FINAL",
    ]
    assert [intent.source_trade_id for intent in strategy.recorded_intents] == ["T-PARTIAL"]
    assert all(
        intent.hedge_quantity_ounces == Decimal(1)
        for intent in strategy.state_store.intents()
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


def test_one_shot_is_disarmed_until_armed_with_a_fresh_store(tmp_path: Path) -> None:
    strategy = TakerStrategy(_strategy_config(tmp_path / "one-shot.json"), one_shot=True)
    hedge_tick = _quote(_hedge_instrument(), "2400", "2401", "10", 1_000_000_000)

    assert (strategy.one_shot_armed, strategy.one_shot_claimed) == (False, False)
    assert not strategy._inputs_are_fresh(1_000_000_000, hedge_tick, 1_000_000_000)

    strategy.arm_one_shot()

    assert (strategy.one_shot_armed, strategy.one_shot_claimed) == (True, False)
    assert strategy._inputs_are_fresh(1_000_000_000, hedge_tick, 1_000_000_000)
    with pytest.raises(RuntimeError, match="already armed"):
        strategy.arm_one_shot()


def test_one_shot_refuses_completed_history_even_when_store_gate_is_open(
    tmp_path: Path,
) -> None:
    strategy = TakerStrategy(_strategy_config(tmp_path / "history.json"), one_shot=True)
    strategy.state_store.begin_source("O-HISTORY", BusinessOrderSide.BUY, Decimal(1))
    strategy.state_store.update_source_status("O-HISTORY", "REJECTED")
    assert strategy.state_store.can_submit_source()

    with pytest.raises(RuntimeError, match="fresh state store"):
        strategy.arm_one_shot()


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
    _account_handle = None
    _account_topics: tuple[str, ...] = ()

    def __init__(self, active: str, *, fail_cancel: bool = False) -> None:
        self.state_store = _StopStore(active)
        self.cache = _StopCache(active)
        self.canceled: list[_WorkingOrder] = []
        self.fail_cancel = fail_cancel
        self._source_terminal_stopped = False
        self._source_terminal_generation = 0
        self._source_terminal_inflight: set[str] = set()

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


def test_restart_pending_stop_does_not_cancel_an_unverified_old_order() -> None:
    harness = _StopHarness("OLD")
    TakerStrategy.bind_restart_gate(cast(Any, harness), lambda: True)
    TakerStrategy.on_stop(cast(Any, harness))
    assert not harness.cache.requested and not harness.canceled
    assert not harness.state_store.unknown
    assert harness._source_terminal_stopped


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("leg", ["source", "hedge"])
def test_restart_gate_skips_business_order_callbacks_until_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, leg: str,
) -> None:
    strategy: Any = (
        MakerStrategy(_maker_strategy_config(tmp_path / kind)) if kind == "maker"
        else TakerStrategy(_strategy_config(tmp_path / kind))
    )
    view = strategy._stores[SourceDirection.LONG] if kind == "maker" else strategy.state_store
    view.begin_source("OLD-SOURCE", BusinessOrderSide.BUY, Decimal(1),
                      source_account_id="BITFINEX-001", hedge_account_id="MT5-001")
    instrument = _source_instrument() if leg == "source" else _hedge_instrument()
    client_order_id = "OLD-SOURCE"
    if leg == "hedge":
        intent = view.reserve_source_fill(
            fill_key="OLD-SOURCE|SOURCE-V|SOURCE-T", client_order_id="OLD-SOURCE",
            trade_id="SOURCE-T", source_side=BusinessOrderSide.BUY, fill_ounces=Decimal(1),
        )
        assert intent is not None
        client_order_id = "OLD-HEDGE"
        view.bind_hedge_order(intent.intent_id, client_order_id)
    order = TestExecStubs.limit_order(
        instrument=instrument, client_order_id=ClientOrderId(client_order_id),
        order_side=OrderSide.BUY if leg == "source" else OrderSide.SELL,
        quantity=instrument.make_qty(1), price=instrument.make_price(2400),
    )
    filled = TestEventStubs.order_filled(
        order, instrument, venue_order_id=VenueOrderId("OLD-V"), trade_id=TradeId("OLD-T"),
        last_qty=instrument.make_qty(1), commission=Money(0, instrument.quote_currency),
    )
    pending = True
    strategy.bind_restart_gate(lambda: pending)
    strategy._live_submission_ready = lambda: False
    dispatched: list[object] = []
    canceled: list[object] = []
    monkeypatch.setattr(strategy, "_submit_hedge" if kind == "maker" else "_submit_hedge_intent",
                        lambda *args: dispatched.append(args))
    monkeypatch.setattr(strategy, "cancel_order", lambda *args, **kwargs: canceled.append(args))
    before = deepcopy(view._to_payload())
    file_before = view.path.read_bytes()
    for callback, event in (
        (strategy.on_order_submitted, TestEventStubs.order_submitted(order)),
        (strategy.on_order_accepted, TestEventStubs.order_accepted(order)),
        (strategy.on_order_denied, OrderDenied(
            trader_id=order.trader_id, strategy_id=order.strategy_id,
            instrument_id=order.instrument_id, client_order_id=order.client_order_id,
            reason="risk denied", event_id=UUID4(), ts_init=0,
        )),
        (strategy.on_order_rejected, _rejected(order, "UNKNOWN")),
        (strategy.on_order_rejected, _rejected(order, "venue rejected")),
        (strategy.on_order_canceled, TestEventStubs.order_canceled(order)),
        (strategy.on_order_expired, TestEventStubs.order_expired(order)),
        (strategy.on_order_filled, filled),
    ):
        callback(event)
        assert view._to_payload() == before and view.path.read_bytes() == file_before
    strategy._submit_next_pending_hedge()
    assert not dispatched and not canceled and not strategy._source_terminal_inflight
    pending = False
    strategy.on_order_filled(filled)
    assert view._to_payload() != before
    if leg == "source":
        assert len(dispatched) == 1  # Source-health HOLD does not replace the restart gate.
    else:
        assert view.intents()[0].status is ObligationStatus.COMPLETED


@pytest.mark.parametrize("kind", ["maker", "taker"])
def test_restart_gate_blocks_an_otherwise_dispatchable_pending_hedge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    strategy: Any = (
        MakerStrategy(_maker_strategy_config(tmp_path / kind)) if kind == "maker"
        else TakerStrategy(_strategy_config(tmp_path / kind))
    )
    view = strategy._stores[SourceDirection.LONG] if kind == "maker" else strategy.state_store
    view.begin_source("OLD", BusinessOrderSide.BUY, Decimal(1),
                      source_account_id="BITFINEX-001", hedge_account_id="MT5-001")
    intent = view.reserve_source_fill(
        fill_key="OLD|V|T", client_order_id="OLD", trade_id="T",
        source_side=BusinessOrderSide.BUY, fill_ounces=Decimal(1),
    )
    assert intent is not None and intent.status is ObligationStatus.PENDING
    pending = True
    strategy.bind_restart_gate(lambda: pending)
    dispatched: list[object] = []
    monkeypatch.setattr(strategy, "_submit_hedge" if kind == "maker" else "_submit_hedge_intent",
                        lambda *args: dispatched.append(args))
    before = deepcopy(view._to_payload())
    strategy._submit_next_pending_hedge()
    if kind == "maker":
        MakerStrategy._submit_hedge(strategy, SourceDirection.LONG, AccountId("MT5-001"),
                                    None, intent)
    else:
        TakerStrategy._submit_hedge_intent(strategy, intent)
    assert not dispatched and view._to_payload() == before
    pending = False
    strategy._submit_next_pending_hedge()
    assert len(dispatched) == 1


class _QuoteTriggeredTaker(TakerStrategy):
    def __init__(
        self,
        state_path: Path,
        *,
        base_book_quantity: int = 1,
        hedge_quantity_ready: Callable[[Decimal], bool] | None = None,
    ) -> None:
        config = _strategy_config(state_path)
        super().__init__(
            replace(
                config,
                max_quote_age_ns=200_000_000,
                economics=replace(config.economics, base_book_quantity=Decimal(base_book_quantity)),
            ),
            hedge_quantity_ready=hedge_quantity_ready,
        )
        self.input_observations: list[tuple[int, int, int]] = []

    def _inputs_are_fresh(self, source_ts_ns: int, hedge_tick: QuoteTick, now_ns: int) -> bool:
        self.input_observations.append((source_ts_ns, hedge_tick.ts_event, now_ns))
        return super()._inputs_are_fresh(source_ts_ns, hedge_tick, now_ns)


@contextmanager
def _event_engine(strategy: TakerStrategy) -> Iterator[BacktestEngine]:
    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True), run_analysis=False)
    )
    try:
        engine.add_venue(
            venue=BITFINEX,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            book_type=BookType.L2_MBP,
            starting_balances=[Money(1_000_000, USDT)],
            base_currency=USDT,
        )
        engine.add_venue(
            venue=MT5,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(1_000_000, USD)],
            base_currency=USD,
        )
        engine.add_instrument(_source_instrument())
        engine.add_instrument(_hedge_instrument())
        engine.add_strategy(strategy)
        yield engine
    finally:
        engine.dispose()


def _seed_terminal_source(engine: BacktestEngine, strategy: Any, *, maker: bool = False) -> Any:
    """Apply actual source events through the native engine and its strategy subscription."""
    if not engine.kernel.exec_engine.is_running:
        engine.kernel.exec_engine.start()
    instrument = _source_instrument()
    order = strategy.order_factory.limit(
        instrument_id=instrument.id, order_side=OrderSide.BUY,
        quantity=instrument.make_qty(4), price=instrument.make_price(2400),
        time_in_force=TimeInForce.GTC if maker else TimeInForce.IOC, post_only=maker,
    )
    store = strategy._stores[SourceDirection.LONG] if maker else strategy.state_store
    store.begin_source(
        order.client_order_id.value, BusinessOrderSide.BUY, Decimal(4),
        source_account_id="BITFINEX-001", hedge_account_id="MT5-001",
    )
    engine.cache.add_order(order)
    for event in (
        TestEventStubs.order_submitted(order, account_id=AccountId("BITFINEX-001"), ts_event=1),
        TestEventStubs.order_accepted(
            order, account_id=AccountId("BITFINEX-001"),
            venue_order_id=VenueOrderId("V-TERMINAL"), ts_event=2,
        ),
    ):
        engine.kernel.exec_engine.process(event)
    assert order.status == OrderStatus.ACCEPTED
    return order


def _process_source_terminal(engine: BacktestEngine, order: Any, *, expired: bool = False) -> Any:
    if expired:
        event = OrderExpired(
            trader_id=order.trader_id, strategy_id=order.strategy_id,
            instrument_id=order.instrument_id, client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id, account_id=order.account_id,
            event_id=UUID4(), ts_event=10, ts_init=10,
        )
    else:
        event = TestEventStubs.order_canceled(order, account_id=order.account_id, ts_event=10)
    engine.kernel.exec_engine.process(event)
    assert order.is_closed
    return event


def _terminal_report(order: Any, **overrides: Any) -> OrderStatusReport:
    values = dict(
        account_id=order.account_id, instrument_id=order.instrument_id,
        client_order_id=order.client_order_id, venue_order_id=order.venue_order_id,
        order_side=order.side, order_type=order.order_type, time_in_force=order.time_in_force,
        order_status=order.status, quantity=order.quantity, filled_qty=order.filled_qty,
        price=order.price,
        avg_px=None if order.filled_qty.as_decimal() == 0 else Decimal(str(order.avg_px)),
        post_only=bool(order.is_post_only), reduce_only=bool(order.is_reduce_only),
        report_id=UUID4(), ts_accepted=2, ts_last=10, ts_init=10,
    )
    values.update(overrides)
    return OrderStatusReport(**values)


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("late_fill", [False, True])
def test_native_taker_terminal_reconciles_only_actual_fills_then_allows_next_source(
    tmp_path: Path, expired: bool, late_fill: bool,
) -> None:
    completions: list[Any] = []
    strategy = RecordingTakerStrategy(
        tmp_path / "native-terminal.json",
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy)
        if not (expired and late_fill):
            _process_source_terminal(engine, order, expired=expired)
            assert len(completions) == 1
            assert not strategy.state_store.can_submit_source()
        if late_fill:
            fill = TestEventStubs.order_filled(
                order=order, instrument=_source_instrument(), account_id=order.account_id,
                venue_order_id=order.venue_order_id, trade_id=TradeId("T-LATE"),
                last_qty=_source_instrument().make_qty(1), ts_event=8,
                last_px=_source_instrument().make_price("2400.10"),
                commission=Money(0, USDT),
            )
            engine.kernel.exec_engine.process(fill)
            _process_source_terminal(engine, order, expired=expired)
            assert len(completions) == 1  # Reconciliation event does not spawn another query.
            assert len(strategy.state_store.intents()) == 1
        completions[0](_terminal_report(order))
        completions[0](_terminal_report(order))  # Duplicate completion is inert.
        assert strategy.state_store.active_source_order_id is None
        assert strategy.state_store.halt_reason is None
        if late_fill:
            assert not strategy.state_store.can_submit_source()
            intent = strategy.state_store.intents()[0]
            hedge = TestExecStubs.market_order(
                instrument=_hedge_instrument(), order_side=OrderSide.SELL,
                quantity=_hedge_instrument().make_qty(1), strategy_id=strategy.id,
                client_order_id=ClientOrderId(cast(str, intent.hedge_client_order_id)),
            )
            engine.cache.add_order(hedge)
            engine.kernel.exec_engine.process(TestEventStubs.order_submitted(
                hedge, account_id=AccountId("MT5-001"), ts_event=11,
            ))
            engine.kernel.exec_engine.process(TestEventStubs.order_filled(
                order=hedge, instrument=_hedge_instrument(), account_id=AccountId("MT5-001"),
                trade_id=TradeId("H-DONE"), last_qty=_hedge_instrument().make_qty(1),
                commission=Money(0, USD), ts_event=12,
            ))
            assert strategy.state_store.intents()[0].status is ObligationStatus.COMPLETED
        assert strategy.state_store.can_submit_source()
        # Completion releases eligibility; a later actual market event chooses the next order.
        engine.kernel.clock.set_time(1_100_000_000)
        strategy.clock.set_time(1_100_000_000)
        engine.kernel.data_engine.process(_book_snapshot(
            _source_instrument(), "2399", "2401", "5", 1_100_000_000,
        ))
        engine.kernel.data_engine.process(_quote(
            _hedge_instrument(), "2405", "2406", "10", 1_100_000_000,
        ))
        assert len(strategy.state_store.source_orders()) == 2
        assert strategy.state_store.active_source_order_id != order.client_order_id.value
        assert len(engine.cache.orders(instrument_id=_source_instrument().id)) == 2
        next_id = strategy.state_store.active_source_order_id
        completions[0](_terminal_report(order))
        assert strategy.state_store.active_source_order_id == next_id


@pytest.mark.parametrize("fault", ["missing", "identity", "quantity", "price", "status"])
def test_native_taker_terminal_mismatch_keeps_gate(
    tmp_path: Path, fault: str,
) -> None:
    strategy = RecordingTakerStrategy(tmp_path / "terminal-mismatch.json")
    completions: list[Any] = []
    strategy._source_terminal_query = lambda _cid, _vid, complete: completions.append(complete)
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy)
        _process_source_terminal(engine, order)
        assert len(completions) == 1
        overrides: dict[str, Any] = {
            "identity": {"account_id": AccountId("BITFINEX-OTHER")},
            "quantity": {"filled_qty": _source_instrument().make_qty(1)},
            "price": {"price": _source_instrument().make_price(2401)},
            "status": {"order_status": OrderStatus.ACCEPTED},
        }.get(fault, {})
        assert completions[0](
            None if fault == "missing" else _terminal_report(order, **overrides),
        ) is False
        assert not strategy.state_store.can_submit_source()
        assert strategy.state_store.active_source_order_id == order.client_order_id.value
        assert strategy._source_terminal_inflight == {order.client_order_id.value}
        assert completions[0](_terminal_report(order)) is True
        assert strategy._source_terminal_inflight == set()
        assert strategy.state_store.can_submit_source()


def test_native_taker_stop_invalidates_late_terminal_completion(tmp_path: Path) -> None:
    strategy = RecordingTakerStrategy(tmp_path / "terminal-stop.json")
    completions: list[Any] = []
    strategy._source_terminal_query = lambda _cid, _vid, complete: completions.append(complete)
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy)
        _process_source_terminal(engine, order)
        assert len(completions) == 1
        engine.trader.stop()
        assert completions[0](_terminal_report(order)) is False
        assert strategy.state_store.active_source_order_id == order.client_order_id.value
        assert strategy.state_store.halt_reason is not None
        assert not strategy.state_store.can_submit_source()


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("hold_timing", ["before_terminal", "during_query"])
def test_native_source_query_keeps_independent_unknown_hold(
    tmp_path: Path, kind: str, hold_timing: str,
) -> None:
    completions: list[Any] = []

    def query(_cid: ClientOrderId, _vid: VenueOrderId, complete: SourceTerminalResult) -> None:
        completions.append(complete)

    strategy: Any = (
        MakerStrategy(_maker_strategy_config(tmp_path / kind), source_terminal_query=query)
        if kind == "maker"
        else RecordingTakerStrategy(tmp_path / kind, source_terminal_query=query)
    )
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=kind == "maker")
        store = strategy._stores[SourceDirection.LONG] if kind == "maker" else strategy.state_store
        if hold_timing == "before_terminal":
            store.mark_source_unknown(order.client_order_id.value, "independent execution HOLD")
        _process_source_terminal(engine, order)
        assert len(completions) == 1
        if hold_timing == "during_query":
            store.mark_source_unknown(order.client_order_id.value, "independent execution HOLD")
        completions[0](_terminal_report(order))
        assert store.halt_reason == "independent execution HOLD"
        assert not store.can_submit_source()
        if hold_timing == "during_query":
            assert store.source_order(order.client_order_id.value).status == "UNKNOWN"


def test_native_taker_terminal_keeps_unknown_hedge_obligation(tmp_path: Path) -> None:
    completions: list[Any] = []
    strategy = RecordingTakerStrategy(
        tmp_path / "unknown-hedge",
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy)
        engine.kernel.exec_engine.process(TestEventStubs.order_filled(
            order=order, instrument=_source_instrument(), account_id=order.account_id,
            venue_order_id=order.venue_order_id, trade_id=TradeId("T-PARTIAL"),
            last_qty=_source_instrument().make_qty(1), ts_event=8, commission=Money(0, USDT),
        ))
        intent = strategy.state_store.intents()[0]
        strategy.state_store.update_hedge_status(
            cast(str, intent.hedge_client_order_id), ObligationStatus.UNKNOWN,
        )
        hold = strategy.state_store.halt_reason
        _process_source_terminal(engine, order)
        completions[0](_terminal_report(order))
        assert strategy.state_store.halt_reason == hold
        assert strategy.state_store.intents()[0].status is ObligationStatus.UNKNOWN
        assert not strategy.state_store.can_submit_source()


def test_native_expired_then_late_fill_requires_matching_native_quantity(tmp_path: Path) -> None:
    """NT can retain EXPIRED while forwarding a late fill: do not accept unequal cache/state."""
    completions: list[Any] = []
    strategy = RecordingTakerStrategy(
        tmp_path / "expired-late",
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy)
        _process_source_terminal(engine, order, expired=True)
        engine.kernel.exec_engine.process(TestEventStubs.order_filled(
            order=order, instrument=_source_instrument(), account_id=order.account_id,
            venue_order_id=order.venue_order_id, trade_id=TradeId("T-LATE-EXPIRED"),
            last_qty=_source_instrument().make_qty(1), ts_event=8, commission=Money(0, USDT),
        ))
        assert order.filled_qty.as_decimal() == 0
        record = strategy.state_store.source_order(order.client_order_id.value)
        assert record is not None and record.filled_ounces == 1
        completions[0](_terminal_report(order))
        assert strategy.state_store.active_source_order_id == order.client_order_id.value
        assert not strategy.state_store.can_submit_source()
        assert len(strategy.state_store.intents()) == 1


@pytest.mark.parametrize("kind", ["maker", "taker"])
def test_restart_gate_keeps_exact_terminal_completion_pending(
    tmp_path: Path, kind: str,
) -> None:
    completions: list[Any] = []
    def query(_cid: ClientOrderId, _vid: VenueOrderId, complete: SourceTerminalResult) -> None:
        completions.append(complete)

    strategy: Any = (
        MakerStrategy(_maker_strategy_config(tmp_path / kind), source_terminal_query=query)
        if kind == "maker"
        else RecordingTakerStrategy(tmp_path / kind, source_terminal_query=query)
    )
    pending = False
    strategy.bind_restart_gate(lambda: pending)
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=kind == "maker")
        _process_source_terminal(engine, order)
        assert len(completions) == 1
        view = strategy._stores[SourceDirection.LONG] if kind == "maker" else strategy.state_store
        before = deepcopy(view._to_payload())
        pending = True
        assert completions[0](_terminal_report(order)) is False
        assert view._to_payload() == before
        assert strategy._source_terminal_inflight == {order.client_order_id.value}
        pending = False
        assert completions[0](_terminal_report(order)) is True
        assert not strategy._source_terminal_inflight


@pytest.mark.parametrize("kind", ["maker", "taker"])
def test_old_terminal_callback_cannot_consume_a_new_strategy_generation(
    tmp_path: Path, kind: str,
) -> None:
    completions: list[Any] = []

    def query(_cid: ClientOrderId, _vid: VenueOrderId, complete: SourceTerminalResult) -> None:
        completions.append(complete)

    strategy: Any = (
        MakerStrategy(_maker_strategy_config(tmp_path / kind), source_terminal_query=query)
        if kind == "maker"
        else RecordingTakerStrategy(tmp_path / kind, source_terminal_query=query)
    )
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=kind == "maker")
        _process_source_terminal(engine, order)
        assert len(completions) == 1
        store = strategy._stores[SourceDirection.LONG] if kind == "maker" else strategy.state_store
        store.mark_source_unknown(order.client_order_id.value, "external HOLD before restart")
        strategy.stop()
        strategy.reset()
        strategy.start()
        _process_source_terminal(engine, order)
        assert len(completions) == 2
        assert completions[0](_terminal_report(order)) is False
        assert strategy._source_terminal_inflight == {order.client_order_id.value}
        assert completions[1](_terminal_report(order)) is True
        assert strategy._source_terminal_inflight == set()
        store = strategy._stores[SourceDirection.LONG] if kind == "maker" else strategy.state_store
        assert store.halt_reason == "external HOLD before restart"
        assert not store.can_submit_source()


def test_native_taker_query_submission_failure_is_bounded(tmp_path: Path) -> None:
    attempts: list[str] = []

    def query(cid: ClientOrderId, _vid: VenueOrderId, _complete: SourceTerminalResult) -> None:
        attempts.append(cid.value)
        raise OSError("injected query submission failure")

    strategy = RecordingTakerStrategy(tmp_path / "query-error", source_terminal_query=query)
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy)
        _process_source_terminal(engine, order)
        assert attempts == [order.client_order_id.value]
        assert strategy._source_terminal_inflight == set()
        assert strategy.state_store.active_source_order_id == order.client_order_id.value
        assert not strategy.state_store.can_submit_source()


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("failure", ["missing", "identity", "persistence"])
def test_native_terminal_completion_ack_retains_same_callback_until_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, failure: str,
) -> None:
    completions: list[Any] = []

    def query(_cid: ClientOrderId, _vid: VenueOrderId, complete: SourceTerminalResult) -> None:
        completions.append(complete)

    strategy: Any = (
        MakerStrategy(_maker_strategy_config(tmp_path / kind), source_terminal_query=query)
        if kind == "maker"
        else RecordingTakerStrategy(tmp_path / kind, source_terminal_query=query)
    )
    with _event_engine(strategy) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=kind == "maker")
        _process_source_terminal(engine, order)
        store = strategy._stores[SourceDirection.LONG] if kind == "maker" else strategy.state_store
        original_confirm = store.confirm_source_reconciled

        def fail_persistence(_cid: str) -> None:
            raise OSError("injected terminal confirmation persistence failure")

        if failure == "persistence":
            monkeypatch.setattr(store, "confirm_source_reconciled", fail_persistence)
        bad_report = None if failure == "missing" else _terminal_report(
            order, **({"account_id": AccountId("BITFINEX-OTHER")} if failure == "identity" else {}),
        )
        assert completions[0](bad_report) is False
        assert strategy._source_terminal_inflight == {order.client_order_id.value}
        assert not store.can_submit_source()
        assert store.active_source_order_id == order.client_order_id.value
        monkeypatch.setattr(store, "confirm_source_reconciled", original_confirm)
        assert completions[0](_terminal_report(order)) is True
        assert strategy._source_terminal_inflight == set()
        assert store.active_source_order_id is None and store.halt_reason is None
        assert completions[0](_terminal_report(order)) is False


def test_native_taker_terminal_preserves_one_shot_freeze(tmp_path: Path) -> None:
    completions: list[Any] = []
    strategy = TakerStrategy(
        _strategy_config(tmp_path / "one-shot-terminal"), one_shot=True,
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(strategy) as engine:
        engine.trader.start()
        strategy.arm_one_shot()
        engine.kernel.clock.set_time(1_100_000_000)
        strategy.clock.set_time(1_100_000_000)
        engine.kernel.exec_engine.start()
        engine.kernel.data_engine.process(_book_snapshot(
            _source_instrument(), "2399", "2401", "5", 1_100_000_000,
        ))
        engine.kernel.data_engine.process(_quote(
            _hedge_instrument(), "2405", "2406", "10", 1_100_000_000,
        ))
        order = engine.cache.orders(instrument_id=_source_instrument().id)[0]
        assert strategy.one_shot_claimed
        engine.kernel.exec_engine.process(TestEventStubs.order_accepted(
            order, account_id=AccountId("BITFINEX-001"), venue_order_id=VenueOrderId("V-ONCE"),
        ))
        _process_source_terminal(engine, order)
        freeze = strategy.state_store.source_freeze_reason
        assert freeze is not None
        completions[0](_terminal_report(order))
        assert strategy.state_store.active_source_order_id is None
        assert strategy.state_store.source_freeze_reason == freeze
        assert not strategy.state_store.can_submit_source()


def _swap_instrument(timestamp: int, **changes: Any) -> Cfd:
    values = Cfd.to_dict(_hedge_instrument())
    values["ts_event"] = values["ts_init"] = timestamp
    values["info"] = {
        "canonical_quantity": "ounce",
        "mt5_contract_size_ounces": "100",
        "mt5_volume_step_lots": "0.01",
        "point": "0.01",
        "server_timezone": "Europe/Athens",
        "swap_long": "-6.30",
        "swap_short": "-2.30",
        "swap_mode": 1,
        "swap_rates": ("0", "1", "1", "3", "1", "1", "0"),
    }
    for key, value in changes.items():
        if key in values["info"]:
            values["info"][key] = value
        else:
            values[key] = value
    return Cfd.from_dict(values)


@pytest.mark.parametrize("available", [False, True])
def test_live_account_taker_defers_merges_and_uses_dynamic_quantity(
    tmp_path: Path, available: bool,
) -> None:
    async def run() -> None:
        strategy = TakerStrategy(_strategy_config(tmp_path / "dynamic"))
        reads: list[tuple[int, int, bool]] = []
        ready = False

        def reader(_source: BookTop, source_ts: int, _hedge: BookTop, hedge_ts: int,
                   new_source: bool) -> tuple[SourceAccount, HedgeAccount, int, bool] | None:
            reads.append((source_ts, hedge_ts, new_source))
            if not ready:
                return None
            return (
                SourceAccount(AccountId("BITFINEX-001"), None, Decimal(0), Decimal(1),
                              Decimal(1), Decimal(200)),
                HedgeAccount(Decimal(0), Decimal(1), Decimal(1)),
                2_000_000_000, available,
            )

        strategy.bind_live_account_reader(reader)
        with _event_engine(strategy) as engine:
            engine.trader.start()
            engine.kernel.exec_engine.start()
            strategy.clock.set_time(1_100_000_000)
            engine.kernel.data_engine.process(_book_snapshot(
                _source_instrument(), "2399", "2401", "5", 1_100_000_000,
            ))
            engine.kernel.data_engine.process(_quote(
                _hedge_instrument(), "2405", "2406", "10", 1_100_000_000,
            ))
            assert reads and not engine.cache.orders()
            reads.clear()
            ready = True
            for account in ("BITFINEX-001", "MT5-001", "BITFINEX-001"):
                engine.kernel.msgbus.publish(f"events.account.{account}", object())
            assert reads == [] and not engine.cache.orders()
            await asyncio.sleep(0)
            assert reads == [(1_100_000_000, 1_100_000_000, True)]
            orders = engine.cache.orders(instrument_id=_source_instrument().id)
            assert len(orders) == int(available)
            if orders:
                assert orders[0].quantity.as_decimal() == Decimal(1)
                assert strategy._last_source_attempt_market_ts_ns == 1_100_000_000
            reads.clear()
            engine.kernel.msgbus.publish("events.account.OTHER-001", object())
            await asyncio.sleep(0)
            assert reads == []
            engine.trader.stop()
    asyncio.run(run())


@contextmanager
def _live_cost_event_engine(
    kind: str, state_path: Path, *, maker_type: type[MakerStrategy] = MakerStrategy,
) -> Iterator[tuple[BacktestEngine, Any]]:
    config: Any = (
        _maker_strategy_config(state_path) if kind == "maker" else _strategy_config(state_path)
    )
    config = replace(
        config, initial_cost_ts_ns=100, initial_session_ts_ns=100,
        max_cost_age_ns=100, max_session_age_ns=1_000, max_quote_age_ns=1_000,
    )
    strategy = (
        maker_type(config, live_costs_from_adapters=True)
        if kind == "maker" else TakerStrategy(config, live_costs_from_adapters=True)
    )
    with _event_engine(cast(Any, strategy)) as engine:
        engine.kernel.data_engine.process(_swap_instrument(100))
        engine.kernel.clock.set_time(100)
        strategy.clock.set_time(100)
        engine.trader.start()
        assert strategy.is_running
        try:
            yield engine, strategy
        finally:
            engine.trader.stop()


def _live_inputs_ready(strategy: Any, timestamp: int) -> bool:
    source = _quote(_source_instrument(), "2399", "2401", "5", timestamp)
    hedge = _quote(_hedge_instrument(), "2399", "2401", "5", timestamp)
    return bool(strategy._inputs_are_fresh(
        source if isinstance(strategy, MakerStrategy) else timestamp, hedge, timestamp,
    ))


def _pending_live_hedge(strategy: Any, kind: str) -> Any:
    store = strategy._stores[SourceDirection.LONG] if kind == "maker" else strategy.state_store
    coordinator = (
        strategy._hedges[SourceDirection.LONG] if kind == "maker" else strategy._hedges
    )
    order = TestExecStubs.limit_order(
        instrument=_source_instrument(), order_side=OrderSide.BUY,
        quantity=_source_instrument().make_qty(2), price=_source_instrument().make_price(2400),
    )
    store.begin_source(order.client_order_id.value, BusinessOrderSide.BUY, Decimal(2))
    coordinator.on_source_filled(TestEventStubs.order_filled(
        order=order, instrument=_source_instrument(),
        last_qty=_source_instrument().make_qty(1), ts_event=100,
    ))
    assert len(store.intents()) == 1
    return store


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("via_funding", [False, True])
@pytest.mark.parametrize("timestamp", [150, 301])
def test_invalid_new_cost_observation_does_not_replace_last_good(
    tmp_path: Path, kind: str, via_funding: bool, timestamp: int,
) -> None:
    with _live_cost_event_engine(kind, tmp_path / kind) as (engine, strategy):
        store = _pending_live_hedge(strategy, kind)
        obligations = store.intents()
        carry, fx, previous_ts = strategy._carry, strategy._fx, strategy._cost_ts_ns
        strategy.clock.set_time(300)
        if via_funding:
            engine.kernel.data_engine.process(FundingRateUpdate(
                instrument_id=_source_instrument().id, rate=Decimal("0.25"),
                ts_event=timestamp, ts_init=300,
            ))
        else:
            changed = type(carry)(bitfinex_long=Decimal("0.25"))
            assert not strategy.update_cost_snapshot(changed, fx, timestamp)
        assert strategy._carry == carry
        assert strategy._fx == fx
        assert strategy._cost_ts_ns == previous_ts
        assert not strategy._cost_snapshot_valid
        assert not _live_inputs_ready(strategy, 300)
        assert store.intents() == obligations
        # Invalid future observations do not move the recovery watermark into
        # the future. A genuine current observation can repair this input.
        engine.kernel.data_engine.process(FundingRateUpdate(
            instrument_id=_source_instrument().id, rate=Decimal("0.01"),
            ts_event=300, ts_init=300,
        ))
        assert strategy._cost_ts_ns == 300
        assert strategy._cost_snapshot_valid
        assert store.intents() == obligations


@pytest.mark.parametrize("kind", ["maker", "taker"])
def test_native_instrument_callback_replaces_last_good_reference_without_refreshing_funding(
    tmp_path: Path, kind: str,
) -> None:
    with _live_cost_event_engine(kind, tmp_path / kind) as (engine, strategy):
        before = strategy._hedge_instrument
        carry, funding_ts = strategy._carry, strategy._cost_ts_ns
        for timestamp, swap in ((110, "-7.30"), (110, "-7.30"), (120, "-7.300")):
            strategy.clock.set_time(timestamp)
            updated = _swap_instrument(timestamp, swap_long=swap)
            engine.kernel.data_engine.process(updated)
            assert engine.cache.instrument(updated.id) is updated
            assert strategy._hedge_instrument is updated
            assert strategy._hedge_instrument is not before
            assert strategy._cost_ts_ns == funding_ts
            assert strategy._carry == carry
            assert _live_inputs_ready(strategy, timestamp)
        tick = _quote(_hedge_instrument(), "2399", "2401", "5", 120)
        actual = strategy._carry_for_hedge_tick(tick, 120)
        assert actual.mt5_long_swap == Decimal("-7.3") * Decimal("0.01") / Decimal(2401)


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize(
    "bad_kind", ["future", "older", "same_time_conflict", "stale", "invalid_swap",
                 "invalid_decimal", "structure"],
)
def test_native_bad_instrument_keeps_last_good_and_blocks_until_new_legal_observation(
    tmp_path: Path, kind: str, bad_kind: str,
) -> None:
    with _live_cost_event_engine(kind, tmp_path / kind) as (engine, strategy):
        strategy.clock.set_time(110)
        good = _swap_instrument(110)
        engine.kernel.data_engine.process(good)
        now = 220 if bad_kind == "stale" else 120
        strategy.clock.set_time(now)
        bad_ts = {"future": 121, "older": 109, "same_time_conflict": 110}.get(bad_kind, 120)
        if bad_kind == "stale":
            bad_ts = 119
        changes = {"swap_long": "-7.3"}
        if bad_kind == "invalid_swap":
            changes["swap_long"] = "NaN"
        if bad_kind == "invalid_decimal":
            changes["swap_long"] = "not-a-number"
        if bad_kind == "structure":
            changes["mt5_contract_size_ounces"] = "200"
        bad = _swap_instrument(bad_ts, **changes)
        engine.kernel.data_engine.process(bad)
        assert engine.cache.instrument(good.id) is bad  # Native engine itself has no guard.
        assert strategy._hedge_instrument is good
        assert not _live_inputs_ready(strategy, now)
        strategy.cache.add_quote_tick(_quote(_hedge_instrument(), "2399", "2401", "5", now))
        if kind == "taker":
            assert not strategy.source_independent_inputs_ready()
        assert strategy._cost_ts_ns == 100
        engine.kernel.data_engine.process(good)
        assert not _live_inputs_ready(strategy, now)  # Old replay cannot clear invalidity.
        if now > 200:
            engine.kernel.data_engine.process(FundingRateUpdate(
                instrument_id=_source_instrument().id, rate=Decimal(0),
                ts_event=now, ts_init=now,
            ))
        recovered = _swap_instrument(now, swap_long="-8.3")
        engine.kernel.data_engine.process(recovered)
        assert strategy._hedge_instrument is recovered
        assert _live_inputs_ready(strategy, now)
        if kind == "taker":
            assert strategy.source_independent_inputs_ready()


@pytest.mark.parametrize("kind", ["maker", "taker"])
def test_swap_observation_cannot_clear_invalid_funding_or_existing_hedge_obligation(
    tmp_path: Path, kind: str,
) -> None:
    with _live_cost_event_engine(kind, tmp_path / kind) as (engine, strategy):
        store = _pending_live_hedge(strategy, kind)
        obligations = store.intents()
        assert len(obligations) == 1
        engine.kernel.data_engine.process(FundingRateUpdate(
            instrument_id=_source_instrument().id, rate=Decimal(0), ts_event=101, ts_init=101,
        ))
        assert not strategy._cost_snapshot_valid
        strategy.clock.set_time(110)
        engine.kernel.data_engine.process(_swap_instrument(110, swap_long="bad"))
        strategy.clock.set_time(120)
        recovered = _swap_instrument(120)
        engine.kernel.data_engine.process(recovered)
        assert strategy._hedge_instrument is recovered
        assert not _live_inputs_ready(strategy, 120)
        assert strategy._cost_ts_ns == 100
        assert not strategy._cost_snapshot_valid
        assert store.intents() == obligations


@pytest.mark.parametrize("kind", ["maker", "taker"])
def test_live_instrument_and_funding_age_independently(tmp_path: Path, kind: str) -> None:
    with _live_cost_event_engine(kind, tmp_path / kind) as (engine, strategy):
        store = _pending_live_hedge(strategy, kind)
        obligations = store.intents()
        strategy.clock.set_time(201)
        engine.kernel.data_engine.process(_swap_instrument(201))
        assert strategy._cost_ts_ns == 100
        assert not _live_inputs_ready(strategy, 201)
        assert store.intents() == obligations
        engine.kernel.data_engine.process(FundingRateUpdate(
            instrument_id=_source_instrument().id, rate=Decimal(0), ts_event=201, ts_init=201,
        ))
        assert _live_inputs_ready(strategy, 201)
        strategy.clock.set_time(302)
        engine.kernel.data_engine.process(FundingRateUpdate(
            instrument_id=_source_instrument().id, rate=Decimal(0), ts_event=302, ts_init=302,
        ))
        assert not _live_inputs_ready(strategy, 302)
        assert store.intents() == obligations
        engine.kernel.data_engine.process(_swap_instrument(302))
        assert _live_inputs_ready(strategy, 302)


@pytest.mark.parametrize(
    ("hedge_bid", "hedge_ask", "source_side"),
    [("2405", "2406", OrderSide.BUY), ("2394", "2395", OrderSide.SELL)],
)
def test_mt5_quote_alone_triggers_either_direction_with_original_source_timestamp(
    tmp_path: Path, hedge_bid: str, hedge_ask: str, source_side: OrderSide
) -> None:
    strategy = _QuoteTriggeredTaker(tmp_path / "quote-only.json")
    source, hedge = _source_instrument(), _hedge_instrument()
    with _event_engine(strategy) as engine:
        engine.add_data(
            [
                _quote(hedge, "2399", "2401", "10", 1_000_000_000),
                _book_snapshot(source, "2399", "2401", "5", 1_000_000_000),
                _quote(hedge, hedge_bid, hedge_ask, "10", 1_100_000_000),
            ]
        )
        engine.run()

        source_orders = [o for o in engine.cache.orders() if o.instrument_id == source.id]
        assert len(source_orders) == 1
        assert source_orders[0].side == source_side
        assert source_orders[0].ts_init == 1_100_000_000
        assert len(engine.cache.orders()) == 2
        assert (1_000_000_000, 1_100_000_000, 1_100_000_000) in strategy.input_observations
        assert engine.cache.order_book(source.id).ts_last == 1_000_000_000
        assert strategy._cost_ts_ns == strategy._session_ts_ns == 1_000_000_000


@pytest.mark.parametrize(
    ("source_size", "quote_time"), [("1", 1_100_000_000), ("5", 1_300_000_000)]
)
def test_mt5_quote_cannot_make_shallow_or_stale_source_book_actionable(
    tmp_path: Path, source_size: str, quote_time: int
) -> None:
    strategy = _QuoteTriggeredTaker(tmp_path / "invalid-source.json", base_book_quantity=2)
    source, hedge = _source_instrument(), _hedge_instrument()
    with _event_engine(strategy) as engine:
        engine.add_data(
            [
                _quote(hedge, "2399", "2401", "10", 1_000_000_000),
                _book_snapshot(source, "2399", "2401", source_size, 1_000_000_000),
                _quote(hedge, "2405", "2406", "10", quote_time),
            ]
        )
        engine.run()

        assert engine.cache.orders() == []
        assert strategy.last_decision_gate == (
            "reference_depth_insufficient" if source_size == "1" else "inputs_not_fresh"
        )
        assert engine.cache.order_book(source.id).ts_last == 1_000_000_000
        assert strategy._cost_ts_ns == strategy._session_ts_ns == 1_000_000_000
        assert strategy._last_source_attempt_market_ts_ns is None


@pytest.mark.parametrize("include_next_timestamp", [False, True])
def test_same_time_quote_and_book_events_cannot_submit_duplicate_source_orders(
    tmp_path: Path, include_next_timestamp: bool
) -> None:
    strategy = _QuoteTriggeredTaker(tmp_path / "same-turn.json")
    source, hedge = _source_instrument(), _hedge_instrument()
    with _event_engine(strategy) as engine:
        engine.add_data(
            [
                _quote(hedge, "2399", "2401", "10", 1_000_000_000),
                _book_snapshot(source, "2399", "2401", "5", 1_000_000_000),
                _quote(hedge, "2405", "2406", "10", 1_100_000_000),
                _book_snapshot(source, "2399", "2401", "5", 1_100_000_000),
                _quote(hedge, "2405", "2406", "10", 1_100_000_000),
            ]
        )
        if include_next_timestamp:
            engine.add_data([_quote(hedge, "2405", "2406", "10", 1_200_000_000)])
        engine.run()

        source_orders = [o for o in engine.cache.orders() if o.instrument_id == source.id]
        expected_timestamps = (
            [1_100_000_000, 1_200_000_000] if include_next_timestamp else [1_100_000_000]
        )
        assert sorted(o.ts_init for o in source_orders) == expected_timestamps
        assert len(strategy.state_store.intents()) == len(expected_timestamps)
        assert engine.portfolio.net_position(source.id) == Decimal(len(expected_timestamps))
        assert engine.portfolio.net_position(hedge.id) == -Decimal(len(expected_timestamps))


def test_blocked_source_admission_does_not_claim_same_market_timestamp(tmp_path: Path) -> None:
    ready_checks: list[Decimal] = []

    def ready(quantity: Decimal) -> bool:
        ready_checks.append(quantity)
        return len(ready_checks) > 1

    strategy = _QuoteTriggeredTaker(
        tmp_path / "admission-recovery.json", hedge_quantity_ready=ready
    )
    source, hedge = _source_instrument(), _hedge_instrument()
    with _event_engine(strategy) as engine:
        engine.add_data(
            [
                _quote(hedge, "2399", "2401", "10", 1_000_000_000),
                _book_snapshot(source, "2399", "2401", "5", 1_000_000_000),
                _quote(hedge, "2405", "2406", "10", 1_100_000_000),
                _book_snapshot(source, "2399", "2401", "5", 1_100_000_000),
                _quote(hedge, "2405", "2406", "10", 1_100_000_000),
            ]
        )
        engine.run()

        assert ready_checks == [Decimal(1), Decimal(1)]
        assert len(strategy.state_store.source_orders()) == 1
        assert strategy._last_source_attempt_market_ts_ns == 1_100_000_000


@pytest.mark.parametrize("hedge_event_time", [500_000_000, 1_300_000_000])
def test_expired_or_future_mt5_quote_does_not_claim_market_timestamp(
    tmp_path: Path, hedge_event_time: int
) -> None:
    strategy = _QuoteTriggeredTaker(tmp_path / "invalid-hedge-time.json")
    source, hedge = _source_instrument(), _hedge_instrument()
    quote = _quote(hedge, "2405", "2406", "10", hedge_event_time)
    received_quote = QuoteTick(
        instrument_id=quote.instrument_id,
        bid_price=quote.bid_price,
        ask_price=quote.ask_price,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        ts_event=hedge_event_time,
        ts_init=1_100_000_000,
    )
    with _event_engine(strategy) as engine:
        engine.add_data(
            [
                _quote(hedge, "2399", "2401", "10", 1_000_000_000),
                _book_snapshot(source, "2399", "2401", "5", 1_000_000_000),
                received_quote,
            ]
        )
        engine.run()

        assert engine.cache.orders() == []
        assert strategy._last_source_attempt_market_ts_ns is None
        assert strategy.last_decision_gate == "inputs_not_fresh"
        assert (1_000_000_000, hedge_event_time, 1_100_000_000) in strategy.input_observations


def test_mt5_quote_does_not_open_new_source_while_hedge_is_unresolved(tmp_path: Path) -> None:
    strategy = RecordingTakerStrategy(tmp_path / "pending-hedge.json")
    source, hedge = _source_instrument(), _hedge_instrument()
    with _event_engine(strategy) as engine:
        engine.add_data(
            [
                _quote(hedge, "2399", "2401", "10", 1_000_000_000),
                _book_snapshot(source, "2399", "2401", "5", 1_000_000_000),
                _quote(hedge, "2405", "2406", "10", 1_100_000_000),
                _quote(hedge, "2406", "2407", "10", 1_200_000_000),
                _book_snapshot(source, "2399", "2401", "5", 1_200_000_000),
            ]
        )
        engine.run()

        assert len(engine.cache.orders()) == 1
        assert engine.cache.orders()[0].ts_init == 1_100_000_000
        assert len(strategy.recorded_intents) == 1
        assert len(strategy.state_store.intents()) == 1
        assert strategy.state_store.has_unresolved_hedges()
        assert not strategy.state_store.can_submit_source()
        assert strategy.last_decision_gate == "state_store_closed"
