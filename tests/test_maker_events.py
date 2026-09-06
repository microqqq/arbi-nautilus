"""Real Nautilus fill identities and deterministic Maker lifecycle edges."""

import asyncio
import json
import os
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import uvloop
from msgspec.structs import replace as struct_replace
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import TestClock as NautilusTestClock
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.data import FundingRateUpdate
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.events import (
    OrderCancelRejected,
    OrderExpired,
    OrderModifyRejected,
    OrderRejected,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    PositionId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Quantity
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from test_taker_events import (
    _event_engine,
    _live_cost_event_engine,
    _process_source_terminal,
    _seed_terminal_source,
    _swap_instrument,
    _terminal_report,
)

from py000_nautilus import store as state_module
from py000_nautilus.app import (
    _hedge_instrument,
    _maker_strategy_config,
    _quote,
    _source_instrument,
    _strategy_config,
)
from py000_nautilus.config import CarryConfig, FxConfig, MakerStrategyConfig
from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent
from py000_nautilus.hedge import HedgeCoordinator
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import (
    BookTop,
    BusinessOrderSide,
    HedgeAccount,
    HedgeIntent,
    MakerAccount,
    MakerQuote,
    ObligationStatus,
    SourceAccount,
    SourceDirection,
)
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.maker import (
    MakerStrategy,
    SourceTerminalQuery,
    SourceTerminalResult,
    _cancel_is_pending,
    _maker_timer_name,
    _maker_timer_target,
)
from py000_nautilus.strategies.taker import TakerStrategy

D = Decimal


def _reload_maker_store(
    strategy: MakerStrategy, direction: SourceDirection = SourceDirection.LONG,
) -> JsonStateStore:
    config = strategy._config
    return MakerStateStore(
        config.store_path_prefix, str(config.source_instrument_id), str(config.hedge_instrument_id),
    ).stores[direction]


def test_maker_directions_share_one_persistent_path(tmp_path: Path) -> None:
    prefix = tmp_path / "atomic-maker"
    strategy = MakerStrategy(_maker_strategy_config(prefix))
    stores = tuple(strategy._stores.values())
    assert {store.path for store in stores} == {Path(f"{prefix}.maker.json")}


def test_maker_first_fill_snapshot_already_freezes_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "atomic-fill")
    strategy._stores[SourceDirection.LONG].begin_source(
        "O-MAKER-EVENTS", BusinessOrderSide.BUY, D(1),
        source_account_id="BITFINEX-001", hedge_account_id="MT5-001",
    )
    snapshots: list[dict[str, Any]] = []
    replace_file = replace_and_sync_parent

    def capture(temporary: Path, destination: Path) -> None:
        replace_file(temporary, destination)
        snapshots.append(json.loads(destination.read_text()))

    monkeypatch.setattr(state_module, "replace_and_sync_parent", capture)
    full, _ = _filled_events(quantity=1)
    strategy.on_order_filled(full)

    first = snapshots[0]
    assert "directions" in first, "fill must persist both directions in its first durable act"
    bid, ask = first["directions"]["bid"], first["directions"]["ask"]
    assert len(bid["seen_source_fills"]) == len(bid["hedge_intents"]) == 1
    assert bid["source_orders"]["O-MAKER-EVENTS"]["filled_ounces"] == "1"
    assert bid["source_freeze_reason"] is not None
    assert ask["source_freeze_reason"] == bid["source_freeze_reason"]


def test_maker_releases_both_freezes_in_one_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = MakerStrategy(_maker_strategy_config(tmp_path / "atomic-release"))
    for store in strategy._stores.values():
        store.freeze_source_submissions("same cycle")
    strategy._source_hold = True
    snapshots: list[dict[str, Any]] = []
    replace_file = replace_and_sync_parent

    def capture(temporary: Path, destination: Path) -> None:
        replace_file(temporary, destination)
        snapshots.append(json.loads(destination.read_text()))

    monkeypatch.setattr(state_module, "replace_and_sync_parent", capture)
    assert strategy._try_release_cycle()
    assert len(snapshots) == 1, "release must not leave an intermediate half-released file"
    assert all(value["source_freeze_reason"] is None
               for value in snapshots[0]["directions"].values())
    assert not strategy._source_hold


@pytest.mark.parametrize("frozen", [False, True])
def test_restart_gate_records_external_freeze_but_prevents_release_cancel_and_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen: bool,
) -> None:
    strategy = MakerStrategy(_maker_strategy_config(tmp_path / "startup-gate"))
    if frozen:
        strategy._state_store.freeze_sources("original external HOLD")
    strategy.bind_restart_gate(lambda: True)
    canceled: list[object] = []
    monkeypatch.setattr(strategy, "cancel_order", lambda *args, **kwargs: canceled.append(args))
    before = deepcopy(strategy._state_store._to_payload())
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()  # on_start must not release the old Maker freeze.
        assert not strategy._try_release_cycle()
        strategy._freeze_and_cancel_all("new stale observation")
        strategy.update_cost_snapshot(strategy._carry, strategy._fx, 0)
        strategy.update_hedge_session(False, strategy._session_ts_ns + 1)
        strategy._evaluate_quotes()
        strategy.stop()
        after = strategy._state_store._to_payload()
        assert after["allocations"] == before["allocations"]
        expected = "original external HOLD" if frozen else "new stale observation"
        assert all(view.source_freeze_reason == expected for view in strategy._stores.values())
        assert not strategy._state_store.cycle_freeze_only
        assert strategy._source_hold and not canceled
        assert strategy._source_terminal_stopped


def test_post_replace_failure_blocks_the_sibling_source_even_without_pause_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "publication-gate")
    refreshed: list[SourceDirection] = []
    monkeypatch.setattr(strategy, "_inputs_are_fresh", lambda *_: True)
    monkeypatch.setattr(strategy, "_carry_for_hedge_tick", lambda *_: CarryConfig())
    monkeypatch.setattr(strategy, "_refresh_direction",
                        lambda direction, *_: refreshed.append(direction))
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        for instrument in (_source_instrument(), _hedge_instrument()):
            engine.cache.add_quote_tick(_quote(instrument, "2399", "2401", "5", 100))
        strategy._evaluate_quotes()
        assert refreshed == [SourceDirection.LONG, SourceDirection.SHORT]
        refreshed.clear()

        def fail(source: Path, destination: Path) -> None:
            os.replace(source, destination)
            raise ParentDirectorySyncError("begin source published without directory sync")

        with monkeypatch.context() as patch:
            patch.setattr(state_module, "replace_and_sync_parent", fail)
            with pytest.raises(ParentDirectorySyncError):
                strategy._stores[SourceDirection.LONG].begin_source(
                    "BID", BusinessOrderSide.BUY, D(1),
                )
        assert not strategy._source_hold
        assert all(view.halt_reason is None and view.source_freeze_reason is None
                   for view in strategy._stores.values())
        assert strategy._stores[SourceDirection.SHORT].can_submit_source()
        strategy._evaluate_quotes()
        assert not refreshed and strategy._global_obligation_block()
        assert not engine.cache.orders() and not strategy.recorded


class _InstrumentLifecycleMaker(MakerStrategy):
    def __init__(self, config: MakerStrategyConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self.cancel_ids: list[str] = []
        self.new_quote_attempts: list[SourceDirection] = []

    def cancel_order(self, order: Any, **kwargs: Any) -> None:
        self.cancel_ids.append(order.client_order_id.value)
        order.apply(TestEventStubs.order_pending_cancel(order, ts_event=self.clock.timestamp_ns()))
        self.cache.update_order(order)

    def _submit_source(self, quote: MakerQuote) -> None:
        self.new_quote_attempts.append(quote.direction)


def _seed_native_working_quotes(
    strategy: _InstrumentLifecycleMaker, *, exact_route: bool = False,
    directions: tuple[SourceDirection, ...] = (SourceDirection.LONG, SourceDirection.SHORT),
    native_status: OrderStatus = OrderStatus.ACCEPTED,
    quantity: Decimal | None = None,
) -> list[Any]:
    quantity = D(2) if quantity is None else quantity
    orders: list[Any] = []
    instrument = strategy.cache.instrument(_source_instrument().id)
    for direction in directions:
        side = OrderSide.BUY if direction is SourceDirection.LONG else OrderSide.SELL
        order = strategy.order_factory.limit(
            instrument_id=_source_instrument().id, order_side=side,
            quantity=instrument.make_qty(quantity), price=instrument.make_price(2400),
            time_in_force=TimeInForce.GTC, post_only=True,
        )
        account_id = AccountId("BITFINEX-001")
        if native_status is not OrderStatus.INITIALIZED:
            order.apply(TestEventStubs.order_submitted(order, account_id=account_id, ts_event=100))
        if native_status is OrderStatus.ACCEPTED:
            order.apply(TestEventStubs.order_accepted(
                order, account_id=account_id,
                venue_order_id=VenueOrderId(direction.value), ts_event=100,
            ))
        strategy.cache.add_order(order)
        strategy._stores[direction].begin_source(
            order.client_order_id.value,
            BusinessOrderSide.BUY if side is OrderSide.BUY else BusinessOrderSide.SELL,
            quantity,
            source_account_id="BITFINEX-001" if exact_route else None,
            hedge_account_id="MT5-001" if exact_route else None,
        )
        if native_status is not OrderStatus.INITIALIZED:
            strategy._stores[direction].update_source_status(
                order.client_order_id.value, native_status.name,
            )
        bound = replace(_bound_quote(), direction=direction, quantity_ounces=quantity)
        if exact_route:
            bound = replace(
                bound, source_account=replace(bound.source_account, client_id=None),
                hedge_account=replace(bound.hedge_account, client_id=None),
            )
        strategy._working_quotes[order.client_order_id.value] = bound
        orders.append(order)
    for instrument in (_source_instrument(), _hedge_instrument()):
        strategy.cache.add_quote_tick(_quote(instrument, "2399", "2401", "5", 100))
    return orders


@pytest.mark.parametrize(
    "change", ["free", "source_risk", "hedge_capacity", "hedge_capacity_one", "missing"],
)
def test_live_account_maker_maintenance_excludes_own_funds_but_keeps_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    async def run() -> None:
        config = struct_replace(
            _maker_strategy_config(tmp_path / change), initial_cost_ts_ns=100,
            initial_session_ts_ns=100, max_cost_age_ns=1_000, max_quote_age_ns=1_000,
            max_session_age_ns=1_000,
        )
        strategy = _InstrumentLifecycleMaker(config)
        maintained: list[MakerQuote] = []
        # This case exercises native working orders and the strategy timer, not venue modify I/O.
        monkeypatch.setattr(
            strategy, "_requote", lambda _order, desired: maintained.append(desired),
        )
        source = SourceAccount(AccountId("BITFINEX-001"), None, D(0), D(0), D(0), D(200))
        hedge = HedgeAccount(D(0), D(10), D(10))
        if change == "source_risk":
            source = replace(source, position_ounces=config.economics.risk.source_max_abs)
        if change == "hedge_capacity":
            hedge = HedgeAccount(D(0), D(0), D(0))
        if change == "hedge_capacity_one":
            hedge = HedgeAccount(D(0), D(1), D(1))
        reads: list[bool] = []

        def reader(_source: BookTop, _source_ts: int, _hedge: BookTop, _hedge_ts: int,
                   new_source: bool) -> tuple[SourceAccount, HedgeAccount, int, bool] | None:
            reads.append(new_source)
            return None if change == "missing" else (source, hedge, 151, False)

        strategy.bind_live_account_reader(reader)
        with _event_engine(cast(Any, strategy)) as engine:
            engine.kernel.clock.set_time(100)
            strategy.clock.set_time(100)
            engine.trader.start()
            orders = _seed_native_working_quotes(strategy, exact_route=True)
            engine.kernel.msgbus.publish("events.account.BITFINEX-001", object())
            engine.kernel.msgbus.publish("events.account.MT5-001", object())
            assert reads == [] and strategy.cancel_ids == []
            await asyncio.sleep(0)
            assert reads and not any(reads)
            expected = [] if change == "free" else [orders[0].client_order_id.value]
            if change in {"hedge_capacity", "hedge_capacity_one", "missing"}:
                expected = [order.client_order_id.value for order in orders]
            assert strategy.cancel_ids == expected
            if change == "free":
                assert len(maintained) == 2
                assert all(quote.quantity_ounces == 2 for quote in maintained)
                assert strategy._stale_timer_names
                assert all(strategy.clock.next_time_ns(name) == 151
                           for name in strategy._stale_timer_names.values())
                # New quotes cannot disguise the earlier account expiry.
                for instrument in (_source_instrument(), _hedge_instrument()):
                    strategy.cache.add_quote_tick(_quote(instrument, "2399", "2401", "5", 150))
                strategy.clock.set_time(151)
                for callback in strategy.clock.advance_time(151):
                    callback.handle()
                assert strategy.cancel_ids == [order.client_order_id.value for order in orders]
            engine.trader.stop()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("loop_factory", [asyncio.SelectorEventLoop, uvloop.new_event_loop])
def test_live_account_notifications_stop_generation_and_exception_isolation(
    tmp_path: Path, kind: str, loop_factory: Callable[[], asyncio.AbstractEventLoop],
) -> None:
    async def run() -> None:
        strategy: Any = (
            MakerStrategy(_maker_strategy_config(tmp_path / kind)) if kind == "maker" else
            TakerStrategy(_strategy_config(tmp_path / kind))
        )
        strategy.bind_live_account_reader(lambda *_args: None)
        calls: list[int] = []

        def evaluate() -> None:
            calls.append(strategy._source_terminal_generation)
            raise ValueError("injected reader failure")

        if kind == "maker":
            strategy._evaluate_quotes = evaluate
        else:
            strategy._evaluate_and_submit = evaluate
        with _event_engine(strategy) as engine:
            engine.trader.start()
            strategy.cache.add_quote_tick(_quote(_source_instrument(), "2399", "2401", "5", 1))
            engine.kernel.msgbus.publish("events.account.BITFINEX-001", object())
            engine.kernel.msgbus.publish("events.account.MT5-001", object())
            assert not calls
            await asyncio.sleep(0)
            assert calls == [0]  # No exception escapes the account publication or the loop.
            engine.kernel.msgbus.publish("events.account.BITFINEX-001", object())
            old_generation = strategy._source_terminal_generation
            strategy.stop()
            await asyncio.sleep(0)
            assert calls == [0]
            strategy.reset()
            strategy.start()
            assert strategy.is_running
            engine.kernel.msgbus.publish("events.account.MT5-001", object())
            new_handle = strategy._account_handle
            strategy._evaluate_account_update(old_generation)
            assert strategy._account_handle is new_handle
            await asyncio.sleep(0)
            assert calls == [0, 1]
            strategy.stop()
            engine.kernel.msgbus.publish("events.account.MT5-001", object())
            await asyncio.sleep(0)
            assert calls == [0, 1]

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(run())


@pytest.mark.parametrize(
    "state", ["initialized", "submitted", "accepted", "update", "cancel", "budget", "disabled"],
)
def test_live_account_maker_empty_side_first_and_pending_budget_never_nets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    async def run() -> None:
        base = _maker_strategy_config(tmp_path / state)
        config = struct_replace(
            base, initial_cost_ts_ns=100, initial_session_ts_ns=100,
            economics=struct_replace(
                base.economics, ask=struct_replace(
                    base.economics.ask, open_quantity_ounces=D(0 if state == "disabled" else 2),
                ),
            ),
        )
        strategy = _InstrumentLifecycleMaker(config)
        reads: list[bool] = []
        maintained: list[MakerQuote] = []

        def reader(_source: BookTop, _source_ts: int, _hedge: BookTop, _hedge_ts: int,
                   new_source: bool) -> tuple[SourceAccount, HedgeAccount, int, bool]:
            reads.append(new_source)
            return (
                SourceAccount(AccountId("BITFINEX-001"), None, D(0), D(10), D(10), D(100)),
                HedgeAccount(D(0), D(10), D(10)), 1_000, state != "budget",
            )

        strategy.bind_live_account_reader(reader)
        monkeypatch.setattr(
            strategy, "_requote", lambda _order, desired: maintained.append(desired),
        )
        with _event_engine(cast(Any, strategy)) as engine:
            engine.kernel.clock.set_time(100)
            strategy.clock.set_time(100)
            engine.trader.start()
            order = _seed_native_working_quotes(
                strategy, exact_route=True, directions=(SourceDirection.LONG,),
                native_status={
                    "initialized": OrderStatus.INITIALIZED, "submitted": OrderStatus.SUBMITTED,
                }.get(state, OrderStatus.ACCEPTED),
            )[0]
            if state == "update":
                order.apply(TestEventStubs.order_pending_update(order, ts_event=100))
            elif state == "cancel":
                order.apply(TestEventStubs.order_pending_cancel(order, ts_event=100))
            strategy.cache.update_order(order)
            engine.kernel.msgbus.publish("events.account.MT5-001", object())
            await asyncio.sleep(0)
            assert reads == ([False] if state == "disabled" else [True, False])
            assert strategy.new_quote_attempts == (
                [SourceDirection.SHORT] if state == "accepted" else []
            )
            assert len(maintained) == int(state in {"accepted", "disabled"})
            assert not strategy.cancel_ids
            assert strategy._stores[SourceDirection.LONG].active_source_order_id == (
                order.client_order_id.value
            )
            engine.trader.stop()
    asyncio.run(run())


def test_live_account_maker_respects_quote_override_without_new_market_event(
    tmp_path: Path,
) -> None:
    class GuardedMaker(MakerStrategy):
        def on_quote_tick(self, tick: Any) -> None:
            observed.append(tick)

    observed: list[Any] = []

    async def run() -> None:
        strategy = GuardedMaker(_maker_strategy_config(tmp_path / "override"))
        strategy.bind_live_account_reader(lambda *_args: None)
        with _event_engine(cast(Any, strategy)) as engine:
            engine.trader.start()
            original = _quote(_source_instrument(), "2399", "2401", "5", 100)
            engine.cache.add_quote_tick(original)
            engine.kernel.msgbus.publish("events.account.BITFINEX-001", object())
            assert observed == []
            await asyncio.sleep(0)
            assert len(observed) == 1 and observed[0] is original
            assert engine.cache.quote_tick(_source_instrument().id).ts_event == 100
            engine.trader.stop()
    asyncio.run(run())


@pytest.mark.parametrize(
    "change", [{"swap_long": "-7.3"}, {"swap_mode": 0},
               {"swap_rates": ("0", "1", "1", "3", "3", "1", "0")}],
)
def test_native_swap_change_cancels_exact_orders_and_waits_for_cancel_reconciliation(
    tmp_path: Path, change: dict[str, Any],
) -> None:
    with _live_cost_event_engine(
        "maker", tmp_path / "swap-cancel", maker_type=_InstrumentLifecycleMaker,
    ) as (engine, strategy):
        orders = _seed_native_working_quotes(strategy)
        expected = [order.client_order_id.value for order in orders]
        strategy.clock.set_time(110)
        engine.kernel.data_engine.process(_swap_instrument(110, swap_long="-6.300"))
        engine.kernel.data_engine.process(_swap_instrument(110))
        assert strategy.cancel_ids == []
        assert [store.active_source_order_id for store in strategy._stores.values()] == expected
        strategy.clock.set_time(120)
        engine.kernel.data_engine.process(_swap_instrument(120, **change))
        assert strategy.cancel_ids == expected
        assert all(order.is_pending_cancel for order in orders)
        assert strategy._cost_ts_ns == 100
        engine.kernel.data_engine.process(_quote(_hedge_instrument(), "2399", "2401", "5", 120))
        assert strategy.new_quote_attempts == []
        for order in orders:
            canceled = TestEventStubs.order_canceled(
                order, account_id=AccountId("BITFINEX-001"), ts_event=120,
            )
            order.apply(canceled)
            strategy.cache.update_order(order)
            strategy.on_order_canceled(canceled)
        assert strategy._global_obligation_block()  # Acknowledgement alone is not final-fill proof.
        assert strategy.new_quote_attempts == []
        for store, order in zip(strategy._stores.values(), orders, strict=True):
            store.confirm_source_reconciled(order.client_order_id.value)
        engine.kernel.data_engine.process(_quote(_hedge_instrument(), "2399", "2401", "5", 120))
        assert set(strategy.new_quote_attempts) == {SourceDirection.LONG, SourceDirection.SHORT}
        assert strategy.cancel_ids == expected


def test_native_same_swap_refresh_keeps_old_timer_then_expires_on_its_own_deadline(
    tmp_path: Path,
) -> None:
    with _live_cost_event_engine(
        "maker", tmp_path / "swap-timer", maker_type=_InstrumentLifecycleMaker,
    ) as (engine, strategy):
        orders = _seed_native_working_quotes(strategy)
        strategy._schedule_stale_timer(SourceDirection.LONG, orders[0].client_order_id.value)
        timer = _maker_timer_name(SourceDirection.LONG, orders[0].client_order_id.value)
        assert strategy.clock.next_time_ns(timer) == 201
        strategy.clock.set_time(150)
        engine.kernel.data_engine.process(_swap_instrument(150))
        strategy.clock.set_time(200)
        engine.kernel.data_engine.process(_maker_funding("0", 200))
        for instrument in (_source_instrument(), _hedge_instrument()):
            strategy.cache.add_quote_tick(_quote(instrument, "2399", "2401", "5", 200))
        assert strategy.cancel_ids == []
        for callback in strategy.clock.advance_time(201):
            callback.handle()
        assert strategy.clock.next_time_ns(timer) == 251
        assert strategy.cancel_ids == []
        for callback in strategy.clock.advance_time(251):
            callback.handle()
        assert strategy.cancel_ids == [order.client_order_id.value for order in orders]
        assert strategy._cost_ts_ns == 200


def _bound_quote() -> MakerQuote:
    return MakerQuote(
        direction=SourceDirection.LONG,
        source_account=SourceAccount(
            account_id=AccountId("BITFINEX-001"),
            client_id=ClientId("SOURCE-CLIENT"),
            position_ounces=D(0),
            max_long_ounces=D(10),
            max_short_ounces=D(10),
            base_margin_level=D(100),
        ),
        hedge_account=MakerAccount(
            account_id=AccountId("MT5-001"),
            client_id=ClientId("HEDGE-CLIENT"),
            position_ounces=D(0),
            max_long_ounces=D(10),
            max_short_ounces=D(10),
        ),
        source_price_usdt=D(2400),
        hedge_reference_price_usd=D(2401),
        quantity_ounces=D(2),
        adjusted_spread=D("0.001"),
        leverage=16,
    )


class RecordingMakerStrategy(MakerStrategy):
    def __init__(
        self,
        state_prefix: Path,
        *,
        hedge_client_id: ClientId | None = None,
        source_terminal_query: SourceTerminalQuery | None = None,
    ) -> None:
        config = _maker_strategy_config(state_prefix)
        if hedge_client_id is not None:
            route = struct_replace(config.hedge_accounts[0], client_id=hedge_client_id)
            config = struct_replace(config, hedge_accounts=(route,))
        super().__init__(
            config,
            source_terminal_query=source_terminal_query,
        )
        self.recorded: list[
            tuple[SourceDirection, AccountId, ClientId | None, HedgeIntent]
        ] = []
        self.canceled: list[SourceDirection] = []

    def _submit_hedge(
        self,
        direction: SourceDirection,
        hedge_account_id: AccountId,
        hedge_client_id: ClientId | None,
        intent: HedgeIntent,
    ) -> None:
        self.recorded.append((direction, hedge_account_id, hedge_client_id, intent))
        self._stores[direction].bind_hedge_order(
            intent.intent_id,
            f"H-RECORDED-{len(self.recorded)}",
        )

    def _cancel_working(
        self,
        direction: SourceDirection,
        *,
        expected_order_id: str | None = None,
        reason: str,
    ) -> None:
        self.canceled.append(direction)


class CancelFaultMakerStrategy(RecordingMakerStrategy):
    def __init__(self, state_prefix: Path) -> None:
        super().__init__(state_prefix)
        self.actions: list[str] = []

    def _submit_hedge(
        self,
        direction: SourceDirection,
        hedge_account_id: AccountId,
        hedge_client_id: ClientId | None,
        intent: HedgeIntent,
    ) -> None:
        self.actions.append("hedge")
        super()._submit_hedge(
            direction,
            hedge_account_id,
            hedge_client_id,
            intent,
        )

    def _cancel_working(
        self,
        direction: SourceDirection,
        *,
        expected_order_id: str | None = None,
        reason: str,
    ) -> None:
        self.actions.append(f"cancel-{direction.value}")
        if direction is SourceDirection.LONG:
            raise OSError("injected first-side cancel failure")
        super()._cancel_working(
            direction,
            expected_order_id=expected_order_id,
            reason=reason,
        )


class CyclePlacementMakerStrategy(RecordingMakerStrategy):
    def __init__(self, state_prefix: Path) -> None:
        super().__init__(state_prefix)
        self.placed_source_ids: list[str] = []

    def _new_quote(
        self,
        direction: SourceDirection,
        source_book: BookTop,
        hedge_book: BookTop,
    ) -> MakerQuote:
        price = D(2398) if direction is SourceDirection.LONG else D(2402)
        return replace(
            _bound_quote(),
            direction=direction,
            source_price_usdt=price,
            quantity_ounces=D(1),
        )

    def _submit_source(self, quote: MakerQuote) -> None:
        client_order_id = f"O-CYCLE-2-{quote.direction.value}"
        side = (
            BusinessOrderSide.BUY
            if quote.direction is SourceDirection.LONG
            else BusinessOrderSide.SELL
        )
        self._stores[quote.direction].begin_source(
            client_order_id,
            side,
            quote.quantity_ounces,
            source_account_id=quote.source_account.account_id.value,
            source_client_id=(
                quote.source_account.client_id.value
                if quote.source_account.client_id is not None
                else None
            ),
            hedge_account_id=quote.hedge_account.account_id.value,
            hedge_client_id=(
                quote.hedge_account.client_id.value
                if quote.hedge_account.client_id is not None
                else None
            ),
        )
        self._working_quotes[client_order_id] = quote
        self.placed_source_ids.append(client_order_id)


class TerminalQueryMakerStrategy(RecordingMakerStrategy):
    def __init__(
        self,
        state_prefix: Path,
        *,
        source_terminal_query: SourceTerminalQuery,
    ) -> None:
        super().__init__(
            state_prefix,
            source_terminal_query=source_terminal_query,
        )
        self.report_is_exact = True

    def _source_cancel_report_is_exact(
        self,
        direction: SourceDirection,
        event: Any,
        report: OrderStatusReport,
    ) -> bool:
        return self.report_is_exact


def _filled_events(quantity: int = 2) -> tuple[Any, Any]:
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(quantity),
        price=instrument.make_price(2400),
        client_order_id=ClientOrderId("O-MAKER-EVENTS"),
    )
    partial = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("V-MAKER"),
        trade_id=TradeId("T-PARTIAL"),
        last_qty=instrument.make_qty(1),
        commission=Money(0, instrument.quote_currency),
    )
    final = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("V-MAKER"),
        trade_id=TradeId("T-FINAL"),
        last_qty=instrument.make_qty(1),
        commission=Money(0, instrument.quote_currency),
    )
    return partial, final


def _canceled_source_order(
    *,
    quantity: int = 1,
    fill_prices: tuple[str, ...] = (),
    time_in_force: TimeInForce = TimeInForce.GTC,
    post_only: bool = True,
    reduce_only: bool = False,
    expired: bool = False,
) -> tuple[Any, Any]:
    instrument = _source_instrument()
    account_id = AccountId("BITFINEX-001")
    venue_order_id = VenueOrderId("V-CANCEL-QUERY")
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(quantity),
        price=instrument.make_price(2400),
        time_in_force=time_in_force,
        post_only=post_only,
        reduce_only=reduce_only,
        client_order_id=ClientOrderId("O-CANCEL-QUERY"),
    )
    order.apply(TestEventStubs.order_submitted(order, account_id=account_id, ts_event=1))
    order.apply(
        TestEventStubs.order_accepted(
            order,
            account_id=account_id,
            venue_order_id=venue_order_id,
            ts_event=2,
        )
    )
    for index, fill_price in enumerate(fill_prices, start=1):
        order.apply(
            TestEventStubs.order_filled(
                order=order,
                instrument=instrument,
                account_id=account_id,
                venue_order_id=venue_order_id,
                trade_id=TradeId(f"T-CANCEL-QUERY-{index}"),
                last_qty=instrument.make_qty(1),
                last_px=instrument.make_price(fill_price),
                commission=Money(0, instrument.quote_currency),
                ts_event=2 + index,
            )
        )
    if expired:
        template = TestEventStubs.order_expired(order, ts_event=3 + len(fill_prices))
        event = OrderExpired(
            trader_id=template.trader_id,
            strategy_id=template.strategy_id,
            instrument_id=template.instrument_id,
            client_order_id=template.client_order_id,
            venue_order_id=template.venue_order_id,
            account_id=account_id,
            event_id=UUID4(),
            ts_event=template.ts_event,
            ts_init=template.ts_init,
        )
    else:
        event = TestEventStubs.order_canceled(
            order,
            account_id=account_id,
            ts_event=3 + len(fill_prices),
        )
    order.apply(event)
    return order, event


def _cancel_report(order: Any, **overrides: Any) -> OrderStatusReport:
    values: dict[str, Any] = {
        "account_id": order.account_id,
        "instrument_id": order.instrument_id,
        "client_order_id": order.client_order_id,
        "venue_order_id": order.venue_order_id,
        "order_side": order.side,
        "order_type": order.order_type,
        "time_in_force": order.time_in_force,
        "order_status": OrderStatus.CANCELED,
        "quantity": order.quantity,
        "filled_qty": order.filled_qty,
        "price": order.price,
        "avg_px": None if order.filled_qty.as_decimal() == 0 else Decimal(str(order.avg_px)),
        "post_only": bool(order.is_post_only),
        "reduce_only": bool(order.is_reduce_only),
        "report_id": UUID4(),
        "ts_accepted": 2,
        "ts_last": 4,
        "ts_init": 5,
    }
    values.update(overrides)
    return OrderStatusReport(**values)


def _seed_cancel_store(
    strategy: RecordingMakerStrategy,
    *,
    quantity: int,
    filled: int = 0,
    terminal: str = "CANCELED",
) -> JsonStateStore:
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-CANCEL-QUERY",
        BusinessOrderSide.BUY,
        D(quantity),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    if filled:
        store.reserve_source_fill(
            fill_key="O-CANCEL-QUERY|V-CANCEL-QUERY|T-CANCEL-QUERY",
            client_order_id="O-CANCEL-QUERY",
            trade_id="T-CANCEL-QUERY",
            source_side=BusinessOrderSide.BUY,
            fill_ounces=D(filled),
        )
    store.update_source_status("O-CANCEL-QUERY", terminal)
    return store


def test_source_cancel_report_requires_exact_cache_and_store_facts(tmp_path: Path) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "cancel-report-exact.state")
    store = _seed_cancel_store(strategy, quantity=1)
    order, event = _canceled_source_order()
    harness = cast(
        Any,
        SimpleNamespace(
            _stores=strategy._stores,
            _config=strategy._config,
            cache=SimpleNamespace(order=lambda _client_order_id: order),
        ),
    )

    assert MakerStrategy._source_cancel_report_is_exact(
        harness,
        SourceDirection.LONG,
        event,
        _cancel_report(order),
    )
    # Bitfinex paper terminal rows omit the submitted post-only flag. The cache
    # still proves the intent; this gate reconciles terminal exposure only.
    assert MakerStrategy._source_cancel_report_is_exact(
        harness,
        SourceDirection.LONG,
        event,
        _cancel_report(order, post_only=False),
    )
    assert store.halt_reason is not None

    instrument = _source_instrument()
    mismatches = (
        {"account_id": AccountId("BITFINEX-OTHER")},
        {"instrument_id": _hedge_instrument().id},
        {"client_order_id": ClientOrderId("O-OTHER")},
        {"venue_order_id": VenueOrderId("V-OTHER")},
        {"order_side": OrderSide.SELL},
        {"time_in_force": TimeInForce.IOC},
        {"order_status": OrderStatus.ACCEPTED},
        {"quantity": instrument.make_qty(2)},
        {"filled_qty": instrument.make_qty(1)},
        {"price": instrument.make_price(2399)},
        {"avg_px": D(2400)},
        {"reduce_only": True},
    )
    for overrides in mismatches:
        assert not MakerStrategy._source_cancel_report_is_exact(
            harness,
            SourceDirection.LONG,
            event,
            _cancel_report(order, **overrides),
        )


def test_source_cancel_report_accepts_an_already_recorded_partial_fill(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "cancel-report-partial.state")
    _seed_cancel_store(strategy, quantity=3, filled=2)
    order, event = _canceled_source_order(
        quantity=3,
        fill_prices=("2400.1", "2400.2"),
    )
    harness = cast(
        Any,
        SimpleNamespace(
            _stores=strategy._stores,
            _config=strategy._config,
            cache=SimpleNamespace(order=lambda _client_order_id: order),
        ),
    )

    assert MakerStrategy._source_cancel_report_is_exact(
        harness,
        SourceDirection.LONG,
        event,
        _cancel_report(order, avg_px=D("2400.15")),
    )
    assert not MakerStrategy._source_cancel_report_is_exact(
        harness,
        SourceDirection.LONG,
        event,
        _cancel_report(order, filled_qty=_source_instrument().make_qty(0), avg_px=None),
    )


@pytest.mark.parametrize(
    ("expired", "terminal", "status"),
    [
        (False, "CANCELED", OrderStatus.CANCELED),
        (True, "EXPIRED", OrderStatus.EXPIRED),
    ],
)
def test_source_terminal_report_accepts_exact_ioc_reduce_only_shape(
    tmp_path: Path,
    expired: bool,
    terminal: str,
    status: OrderStatus,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "cancel-report-ioc.state")
    _seed_cancel_store(strategy, quantity=1, terminal=terminal)
    order, event = _canceled_source_order(
        time_in_force=TimeInForce.IOC,
        post_only=False,
        reduce_only=True,
        expired=expired,
    )
    harness = cast(
        Any,
        SimpleNamespace(
            _stores=strategy._stores,
            _config=strategy._config,
            cache=SimpleNamespace(order=lambda _client_order_id: order),
        ),
    )

    assert MakerStrategy._source_cancel_report_is_exact(
        harness,
        SourceDirection.LONG,
        event,
        _cancel_report(order, order_status=status),
    )
    assert not MakerStrategy._source_cancel_report_is_exact(
        harness,
        SourceDirection.LONG,
        event,
        _cancel_report(order, order_status=status, reduce_only=False),
    )
    assert not MakerStrategy._source_cancel_report_is_exact(
        harness,
        SourceDirection.LONG,
        event,
        _cancel_report(order, order_status=status, post_only=True),
    )


def test_expired_source_queries_once_and_reconciles_only_after_report(tmp_path: Path) -> None:
    requested: list[SourceTerminalResult] = []

    def query(
        _client_order_id: ClientOrderId,
        _venue_order_id: VenueOrderId,
        complete: SourceTerminalResult,
    ) -> None:
        requested.append(complete)

    strategy = TerminalQueryMakerStrategy(
        tmp_path / "expire-query.state", source_terminal_query=query
    )
    order, event = _canceled_source_order(
        time_in_force=TimeInForce.IOC,
        post_only=False,
        reduce_only=True,
        expired=True,
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        order.client_order_id.value,
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    strategy.on_order_expired(event)
    assert len(requested) == 1 and store.active_source_order_id == order.client_order_id.value
    requested[0](_cancel_report(order, order_status=OrderStatus.EXPIRED))
    observed: Any = store
    assert observed.active_source_order_id is None and observed.halt_reason is None


def test_canceled_source_queries_once_and_confirms_only_after_exact_report(
    tmp_path: Path,
) -> None:
    requested: list[tuple[ClientOrderId, VenueOrderId, SourceTerminalResult]] = []
    order, event = _canceled_source_order()
    report = _cancel_report(order)

    def query(
        client_order_id: ClientOrderId,
        venue_order_id: VenueOrderId,
        complete: SourceTerminalResult,
    ) -> None:
        requested.append((client_order_id, venue_order_id, complete))

    strategy = TerminalQueryMakerStrategy(
        tmp_path / "cancel-query-success.state",
        source_terminal_query=query,
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        order.client_order_id.value,
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    strategy.on_order_canceled(event)
    strategy.on_order_canceled(event)

    assert len(requested) == 1
    active_before_completion = store.active_source_order_id
    halt_before_completion = store.halt_reason
    assert active_before_completion == order.client_order_id.value
    assert halt_before_completion is not None
    assert strategy._source_terminal_inflight == {order.client_order_id.value}
    requested[0][2](report)

    assert [(item[0], item[1]) for item in requested] == [
        (order.client_order_id, order.venue_order_id)
    ]
    assert store.active_source_order_id is None
    assert store.halt_reason is None
    assert not strategy._global_obligation_block()
    assert strategy._source_terminal_inflight == set()


@pytest.mark.parametrize("failure", ["none", "exception", "inexact"])
def test_source_terminal_query_failure_keeps_durable_hold(
    tmp_path: Path,
    failure: str,
) -> None:
    completions: list[SourceTerminalResult] = []
    order, event = _canceled_source_order()
    report = _cancel_report(order)

    def query(
        _client_order_id: ClientOrderId,
        _venue_order_id: VenueOrderId,
        complete: SourceTerminalResult,
    ) -> None:
        if failure == "exception":
            raise OSError("injected query failure")
        completions.append(complete)

    strategy = TerminalQueryMakerStrategy(
        tmp_path / f"cancel-query-{failure}.state",
        source_terminal_query=query,
    )
    strategy.report_is_exact = failure != "inexact"
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        order.client_order_id.value,
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    strategy.on_order_canceled(event)
    if failure == "exception":
        assert completions == []
    else:
        assert len(completions) == 1
        assert completions[0](None if failure == "none" else report) is False

    record = store.source_order(order.client_order_id.value)
    assert record is not None and record.status == "CANCELED"
    assert store.active_source_order_id == order.client_order_id.value
    assert store.halt_reason is not None
    if failure == "exception":
        assert strategy._source_terminal_inflight == set()
    else:
        assert strategy._source_terminal_inflight == {order.client_order_id.value}
        strategy.report_is_exact = True
        assert completions[0](report) is True
        assert strategy._source_terminal_inflight == set()
        observed: Any = store
        assert observed.active_source_order_id is None and observed.halt_reason is None


def test_native_maker_stop_invalidates_late_terminal_completion(tmp_path: Path) -> None:
    completions: list[SourceTerminalResult] = []
    strategy = RecordingMakerStrategy(
        tmp_path / "native-stop",
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=True)
        store = strategy._stores[SourceDirection.LONG]
        _process_source_terminal(engine, order)
        assert len(completions) == 1
        engine.trader.stop()
        assert completions[0](_terminal_report(order)) is False
        assert store.active_source_order_id == order.client_order_id.value
        assert store.halt_reason is not None
        assert not store.can_submit_source()


def _cancel_rejected(order: Any, **overrides: Any) -> OrderCancelRejected:
    values = dict(
        trader_id=order.trader_id, strategy_id=order.strategy_id,
        instrument_id=order.instrument_id, client_order_id=order.client_order_id,
        venue_order_id=order.venue_order_id, account_id=order.account_id,
        reason="arbitrary cancel failure", event_id=UUID4(), ts_event=11, ts_init=11,
    )
    values.update(overrides)
    return OrderCancelRejected(**values)


def _modify_rejected(order: Any, **overrides: Any) -> OrderModifyRejected:
    values = dict(
        trader_id=order.trader_id, strategy_id=order.strategy_id,
        instrument_id=order.instrument_id, client_order_id=order.client_order_id,
        venue_order_id=order.venue_order_id, account_id=order.account_id,
        reason="arbitrary modify failure", event_id=UUID4(), ts_event=11, ts_init=11,
    )
    values.update(overrides)
    return OrderModifyRejected(**values)


def _fill_maker_source(
    engine: Any, order: Any, quantity: int | Decimal, *,
    trade_id: str = "S-ACTUAL", ts_event: int = 5,
) -> None:
    instrument = engine.cache.instrument(_source_instrument().id)
    engine.kernel.exec_engine.process(TestEventStubs.order_filled(
        order=order, instrument=instrument, last_qty=instrument.make_qty(quantity),
        last_px=instrument.make_price(2400), trade_id=TradeId(trade_id),
        commission=Money(0, instrument.quote_currency), ts_event=ts_event,
    ))


@pytest.mark.parametrize("first_direction", [SourceDirection.LONG, SourceDirection.SHORT])
@pytest.mark.parametrize("second_quantity", [D("0.5"), D("0.6")])
def test_native_maker_net_residual_before_allocating_new_hedge(
    tmp_path: Path, first_direction: SourceDirection, second_quantity: Decimal,
) -> None:
    completions: list[SourceTerminalResult] = []
    strategy = RecordingMakerStrategy(
        tmp_path / "native-netting",
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(cast(Any, strategy)) as engine:
        values = CryptoPerpetual.to_dict(_source_instrument())
        values.update(size_precision=1, size_increment="0.1", lot_size="0.1")
        engine.add_instrument(CryptoPerpetual.from_dict(values))
        engine.trader.start()
        orders = _seed_native_working_quotes(cast(Any, strategy), exact_route=True)
        if first_direction is SourceDirection.SHORT:
            orders.reverse()
        # Both original orders were already live before either fill. No new source
        # is admitted after the first fill has frozen the strategy.
        _fill_maker_source(engine, orders[0], D("0.5"), trade_id="NET-FIRST")
        assert strategy._global_obligation_block()
        _fill_maker_source(engine, orders[1], second_quantity, trade_id="NET-SECOND")
        assert all(store.intents() == () for store in strategy._stores.values()), (
            "same-route unallocated fills must be netted before rounding a new hedge"
        )
        expected = (D("0.5") - second_quantity) * (
            1 if first_direction is SourceDirection.LONG else -1
        )
        residuals = [store.rounding_residual_ounces for store in strategy._stores.values()]
        assert sorted(residuals) == sorted([D(0), expected])
        assert strategy.recorded == []
        assert not strategy._try_release_cycle(), "a zero residual is not source terminal proof"
        for index, order in enumerate(orders):
            _process_source_terminal(engine, order)
            assert len(completions) == index + 1
            assert completions[index](_terminal_report(order)) is True
            if index == 0:
                assert strategy._global_obligation_block()
        assert strategy._global_obligation_block() is (expected != 0)
        for direction, store in strategy._stores.items():
            assert store.can_submit_source() is (expected == 0)
            reloaded = _reload_maker_store(strategy, direction)
            assert reloaded.rounding_residual_ounces == store.rounding_residual_ounces
            assert reloaded.can_submit_source() is (expected == 0)


@pytest.mark.parametrize("fault", ["none", "no_pending", "native_partial", "identity",
                                   "cancel_rejected", "old_hold", "cancel_completed"])
def test_zero_fill_peer_protective_cancel_keeps_exact_facts_on_obsolete_modify_rejection(
    tmp_path: Path, fault: str,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "zero-peer")
    with _event_engine(cast(Any, strategy)) as engine:
        values = CryptoPerpetual.to_dict(_source_instrument())
        values.update(size_precision=1, size_increment="0.1", lot_size="0.1")
        engine.add_instrument(CryptoPerpetual.from_dict(values))
        engine.trader.start()
        filled, peer = _seed_native_working_quotes(cast(Any, strategy), exact_route=True)
        engine.kernel.exec_engine.process(TestEventStubs.order_pending_update(peer, ts_event=101))
        _fill_maker_source(engine, filled, D("0.5"), ts_event=102)
        if fault != "no_pending":
            engine.kernel.exec_engine.process(
                TestEventStubs.order_pending_cancel(peer, ts_event=103),
            )
        view = strategy._stores[SourceDirection.SHORT]
        if fault == "native_partial":
            # A real native fill not yet applied to the business store is not exact evidence.
            peer.apply(TestEventStubs.order_filled(
                order=peer, instrument=engine.cache.instrument(peer.instrument_id),
                last_qty=Quantity.from_str("0.1"), trade_id=TradeId("UNAPPLIED"), ts_event=104,
            ))
            strategy.cache.update_order(peer)
        if fault == "old_hold":
            view._state.halt_reason = "independent HOLD"
            view._persist()
        if fault == "cancel_completed":
            _process_source_terminal(engine, peer)
            assert not _cancel_is_pending(peer)
        before = view.path.read_bytes()
        event = (_cancel_rejected(peer) if fault == "cancel_rejected" else
                 _modify_rejected(peer, **({"account_id": AccountId("BITFINEX-OTHER")}
                                           if fault == "identity" else {})))
        engine.kernel.exec_engine.process(event)
        record = view.source_order(peer.client_order_id.value)
        assert record is not None and record.filled_ounces == 0
        if fault in {"none", "old_hold", "cancel_completed"}:
            assert view.path.read_bytes() == before
            assert record.status == ("CANCELED" if fault == "cancel_completed" else "ACCEPTED")
        else:
            assert record.status == "UNKNOWN" and view.halt_reason is not None
        assert strategy._source_hold and view.source_freeze_reason is not None


@pytest.mark.parametrize("completion", ["partial", "rejected", "canceled"])
def test_native_partial_fill_preserves_pending_cancel_until_explicit_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completion: str,
) -> None:
    class PendingCancelMaker(RecordingMakerStrategy):
        _cancel_working = MakerStrategy._cancel_working

    async def run() -> None:
        strategy = PendingCancelMaker(tmp_path / completion)
        strategy.bind_live_account_reader(lambda *_args: None)
        cancel_commands: list[str] = []
        with _event_engine(cast(Any, strategy)) as engine:
            engine.kernel.clock.set_time(1_000_000_000)
            strategy.clock.set_time(1_000_000_000)
            engine.trader.start()
            order = _seed_terminal_source(engine, strategy, maker=True)
            store = strategy._stores[SourceDirection.LONG]

            def cancel(current: Any, **_kwargs: Any) -> None:
                cancel_commands.append(current.client_order_id.value)
                engine.kernel.exec_engine.process(TestEventStubs.order_pending_cancel(
                    current, ts_event=strategy.clock.timestamp_ns(),
                ))

            monkeypatch.setattr(strategy, "cancel_order", cancel)
            strategy._cancel_working(SourceDirection.LONG, reason="initial protection")
            assert order.status is OrderStatus.PENDING_CANCEL
            _fill_maker_source(engine, order, 1)
            assert cancel_commands == [order.client_order_id.value]
            assert order.status is OrderStatus.PARTIALLY_FILLED
            assert len(store.intents()) == len(strategy.recorded) == 1
            for instrument in (_source_instrument(), _hedge_instrument()):
                engine.cache.add_quote_tick(_quote(
                    instrument, "2399", "2401", "5", 1_000_000_000,
                ))
            engine.kernel.msgbus.publish("events.account.MT5-001", object())
            engine.kernel.msgbus.publish("events.account.BITFINEX-001", object())
            await asyncio.sleep(0)
            assert len(cancel_commands) == 1
            if completion == "rejected":
                # An actual rejection is not hidden behind the older pending event.
                engine.kernel.exec_engine.process(_cancel_rejected(order))
                assert store.halt_reason == "maker cancel rejected"
                assert len(cancel_commands) == 2
                assert order.status is OrderStatus.PENDING_CANCEL
            elif completion == "canceled":
                _process_source_terminal(engine, order)
                strategy._cancel_working(SourceDirection.LONG, reason="late account protection")
                assert order.status is OrderStatus.CANCELED
                assert len(cancel_commands) == 1
            assert len(store.intents()) == 1 and not store.can_submit_source()
            engine.trader.stop()
    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_native_terminal_event_ends_pending_cancel_history(
    tmp_path: Path, expired: bool,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "cancel-history-terminal")
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=True)
        engine.kernel.exec_engine.process(TestEventStubs.order_pending_cancel(order, ts_event=6))
        assert _cancel_is_pending(order)
        _process_source_terminal(engine, order, expired=expired)
        assert order.is_closed
        assert not _cancel_is_pending(order)


@pytest.mark.parametrize("new_cancel", [False, True])
def test_native_late_partial_fill_does_not_revive_completed_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_cancel: bool,
) -> None:
    completions: list[SourceTerminalResult] = []
    strategy = RecordingMakerStrategy(
        tmp_path / "cancel-history-late-fill",
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=True)
        store = strategy._stores[SourceDirection.LONG]
        _fill_maker_source(engine, order, 1)
        engine.kernel.exec_engine.process(TestEventStubs.order_pending_cancel(order, ts_event=6))
        _process_source_terminal(engine, order)
        assert order.is_closed
        _fill_maker_source(engine, order, 1, trade_id="AFTER-CANCEL", ts_event=12)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        record = store.source_order(order.client_order_id.value)
        assert record is not None and record.status == "PARTIALLY_FILLED"
        assert record.filled_ounces == D(2)
        assert store.active_source_order_id == order.client_order_id.value
        assert len(completions) == 1 and len(store.intents()) == 2
        assert store.source_freeze_reason is not None and strategy._source_hold
        assert not _cancel_is_pending(order)
        assert not strategy._source_action_is_obsolete(_modify_rejected(order))

        if new_cancel:
            cancel_commands: list[str] = []

            def cancel(current: Any, **_kwargs: Any) -> None:
                cancel_commands.append(current.client_order_id.value)
                engine.kernel.exec_engine.process(TestEventStubs.order_pending_cancel(
                    current, ts_event=13,
                ))

            monkeypatch.setattr(strategy, "cancel_order", cancel)
            MakerStrategy._cancel_working(
                strategy, SourceDirection.LONG, reason="late fill needs protection",
            )
            assert cancel_commands == [order.client_order_id.value]
            assert _cancel_is_pending(order)
            assert strategy._source_action_is_obsolete(_modify_rejected(order))

        engine.kernel.exec_engine.process(_modify_rejected(order, ts_event=14, ts_init=14))
        updated = store.source_order(order.client_order_id.value)
        assert updated is not None
        assert updated.status == ("PARTIALLY_FILLED" if new_cancel else "UNKNOWN")
        assert not store.can_submit_source() and strategy._source_hold
        assert len(completions) == 1 and len(store.intents()) == 2
        assert store.net_unhedged_ounces == D(2)


@pytest.mark.parametrize("fill_after_cancel", [False, True])
@pytest.mark.parametrize("independent_hold", [False, True])
def test_native_modify_rejection_during_partial_fill_cancel_preserves_pending_work(
    tmp_path: Path, fill_after_cancel: bool, independent_hold: bool,
) -> None:
    completions: list[SourceTerminalResult] = []
    strategy = RecordingMakerStrategy(
        tmp_path / "partial-cancel-modify",
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=True)
        store = strategy._stores[SourceDirection.LONG]
        engine.kernel.exec_engine.process(TestEventStubs.order_pending_update(order, ts_event=4))
        _fill_maker_source(engine, order, 1)
        engine.kernel.exec_engine.process(TestEventStubs.order_pending_cancel(order, ts_event=6))
        if fill_after_cancel:
            _fill_maker_source(engine, order, 1, trade_id="S-ACTUAL-2", ts_event=7)
        assert order.status == (OrderStatus.PARTIALLY_FILLED if fill_after_cancel
                                else OrderStatus.PENDING_CANCEL)
        if independent_hold:
            store._state.halt_reason = "independent reconciliation conflict"
            store._persist()
        original = (
            store.path.read_bytes(), order.status, strategy._source_hold,
            set(strategy._source_terminal_inflight), tuple(store.intents()),
        )
        rejection = _modify_rejected(order)

        engine.kernel.exec_engine.process(rejection)

        assert order.last_event == rejection
        assert original == (
            store.path.read_bytes(), order.status, strategy._source_hold,
            set(strategy._source_terminal_inflight), tuple(store.intents()),
        )
        record = store.source_order(order.client_order_id.value)
        assert record is not None and record.status == "PARTIALLY_FILLED"
        assert store.active_source_order_id == order.client_order_id.value
        assert not store.can_submit_source() and completions == []
        assert len(store.intents()) == 1 + int(fill_after_cancel)
        assert store.net_unhedged_ounces == D(1 + int(fill_after_cancel))
        _process_source_terminal(engine, order)
        assert len(completions) == 1
        assert store.active_source_order_id == order.client_order_id.value
        assert completions[0](_terminal_report(order)) is True
        assert strategy._stores[SourceDirection.LONG].active_source_order_id is None
        assert store.halt_reason == (
            "independent reconciliation conflict" if independent_hold else None
        )
        # An exact cancel report still cannot discharge the pending hedge(s).
        assert not store.can_submit_source()
        assert all(intent.status is not ObligationStatus.COMPLETED for intent in store.intents())


@pytest.mark.parametrize("fault", [
    "no_cancel", "cancel_rejected", "cancel_completed", "cancel_event", "not_active",
    "no_hold", "no_freeze", "unknown", "status", "quantity", "filled", "unseen_fill",
    "route", "account", "venue", "instrument",
])
def test_native_partial_modify_rejection_without_exact_protection_keeps_hold(
    tmp_path: Path, fault: str,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "partial-modify-mismatch")
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=True)
        store = strategy._stores[SourceDirection.LONG]
        _fill_maker_source(engine, order, 1)
        if fault != "no_cancel":
            engine.kernel.exec_engine.process(
                TestEventStubs.order_pending_cancel(order, ts_event=6),
            )
        record = store.source_order(order.client_order_id.value)
        assert record is not None
        if fault == "cancel_rejected":
            engine.kernel.exec_engine.process(_cancel_rejected(order))
            # Isolate the old rejected cancel event from the separate UNKNOWN gate.
            store._state.source_orders[order.client_order_id.value] = record
        elif fault == "cancel_completed":
            _process_source_terminal(engine, order)
            store._state.source_orders[order.client_order_id.value] = record
        elif fault == "not_active":
            store._state.active_source_order_id = None
        elif fault == "no_hold":
            strategy._source_hold = False
        elif fault == "no_freeze":
            store._state.source_freeze_reason = None
        elif fault == "unknown":
            store.mark_source_unknown(order.client_order_id.value, "existing UNKNOWN")
        elif fault == "unseen_fill":
            store._state.seen_source_fills.clear()
        changes: dict[str, dict[str, Any]] = {
            "status": {"status": "ACCEPTED"}, "quantity": {"quantity_ounces": D(5)},
            "filled": {"filled_ounces": D(2)}, "route": {"hedge_client_id": "OTHER"},
        }
        if fault in changes:
            store._state.source_orders[order.client_order_id.value] = replace(
                record, **changes[fault],
            )
        overrides: dict[str, Any] = {
            "account": {"account_id": AccountId("BITFINEX-OTHER")},
            "venue": {"venue_order_id": VenueOrderId("OTHER")},
            "instrument": {"instrument_id": _hedge_instrument().id},
        }.get(fault, {})
        rejection = (_cancel_rejected if fault == "cancel_event" else _modify_rejected)(
            order, **overrides,
        )
        invalid_state = fault in {"unseen_fill", "filled", "route", "no_freeze"}
        if invalid_state:
            # Private corruption conflicts with the actual allocation history.
            # The proof and the atomic owner must both reject it, retaining the
            # last-good facts and known obligation instead of writing bad state.
            assert not strategy._source_action_is_obsolete(rejection)
            with pytest.raises(ValueError, match="Maker"):
                engine.kernel.exec_engine.process(rejection)
            assert store.has_seen_source_fill(store.intents()[0].fill_key)
            restored = _reload_maker_store(strategy)
            assert restored.source_order(order.client_order_id.value) == record
            assert restored.has_unresolved_hedges() and not restored.can_submit_source()
        else:
            engine.kernel.exec_engine.process(rejection)
        updated = store.source_order(order.client_order_id.value)
        assert updated is not None
        if not invalid_state:
            assert updated.status == "UNKNOWN" and store.halt_reason is not None
        assert strategy._source_hold
        assert not store.can_submit_source()
        assert len(store.intents()) == 1 and store.net_unhedged_ounces == D(1)


@pytest.mark.parametrize("terminal,filled", [
    ("CANCELED", 0), ("CANCELED", 1), ("EXPIRED", 0), ("EXPIRED", 1), ("FILLED", 4),
])
@pytest.mark.parametrize("after_query", [False, True])
@pytest.mark.parametrize("action", ["cancel", "modify"])
def test_native_late_cancel_rejection_preserves_exact_terminal_and_pending_work(
    tmp_path: Path, terminal: str, filled: int, after_query: bool, action: str,
) -> None:
    completions: list[SourceTerminalResult] = []
    strategy = RecordingMakerStrategy(
        tmp_path / "late-cancel",
        source_terminal_query=lambda _cid, _vid, complete: completions.append(complete),
    )
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=True)
        store = strategy._stores[SourceDirection.LONG]
        if action == "modify":
            engine.kernel.exec_engine.process(
                TestEventStubs.order_pending_update(order, ts_event=4),
            )
            assert order.status == OrderStatus.PENDING_UPDATE
        if filled:
            _fill_maker_source(engine, order, filled)
        if terminal != "FILLED":
            _process_source_terminal(engine, order, expired=terminal == "EXPIRED")
            assert len(completions) == 1
            if after_query:
                assert completions[0](_terminal_report(order)) is True
        else:
            assert completions == []
        original = (
            store.path.read_bytes(), store.active_source_order_id, store.halt_reason,
            store.source_freeze_reason, strategy._source_hold,
            set(strategy._source_terminal_inflight), tuple(store.intents()),
        )

        rejection = (_modify_rejected if action == "modify" else _cancel_rejected)(order)
        engine.kernel.exec_engine.process(rejection)

        assert order.last_event == rejection  # Native Engine applied and dispatched the real event.
        assert order.status.name == terminal
        assert original == (
            store.path.read_bytes(), store.active_source_order_id, store.halt_reason,
            store.source_freeze_reason, strategy._source_hold,
            set(strategy._source_terminal_inflight), tuple(store.intents()),
        )
        if terminal != "FILLED":
            assert len(completions) == 1
            if not after_query:
                assert not store.can_submit_source()
                assert completions[0](_terminal_report(order)) is True
        if filled:
            assert len(store.intents()) == 1
            assert store.intents()[0].status is not ObligationStatus.COMPLETED
            # Ignoring a stale action did not discharge its hedge.
            assert not store.can_submit_source()
        else:
            assert store.can_submit_source()


@pytest.mark.parametrize("fault", [
    "working", "account", "instrument", "venue", "no_venue", "trader",
    "source_account", "source_client", "hedge_account", "hedge_client",
    "record_id", "quantity", "filled", "status", "side", "unknown", "unseen_fill",
])
@pytest.mark.parametrize("action", ["cancel", "modify"])
def test_native_cancel_rejection_without_exact_terminal_proof_keeps_hold(
    tmp_path: Path, fault: str, action: str,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "cancel-mismatch")
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=True)
        store = strategy._stores[SourceDirection.LONG]
        _fill_maker_source(engine, order, 1)
        if fault != "working":
            _process_source_terminal(engine, order)
        record = store.source_order(order.client_order_id.value)
        assert record is not None
        record_changes_by_fault: dict[str, dict[str, Any]] = {
            "source_account": {"source_account_id": "BITFINEX-OTHER"},
            "source_client": {"source_client_id": "OTHER"},
            "hedge_account": {"hedge_account_id": "MT5-OTHER"},
            "hedge_client": {"hedge_client_id": "OTHER"},
            "record_id": {"client_order_id": "OTHER"},
            "quantity": {"quantity_ounces": D(5)},
            "filled": {"filled_ounces": D(2)},
            "status": {"status": "EXPIRED"},
            "side": {"side": BusinessOrderSide.SELL},
        }
        record_changes = record_changes_by_fault.get(fault, {})
        store._state.source_orders[order.client_order_id.value] = replace(record, **record_changes)
        if fault == "unknown":
            store.mark_source_unknown(order.client_order_id.value, "existing UNKNOWN")
        if fault == "unseen_fill":
            store._state.seen_source_fills.clear()
        overrides: dict[str, Any] = {
            "account": {"account_id": AccountId("BITFINEX-OTHER")},
            "instrument": {"instrument_id": _hedge_instrument().id},
            "venue": {"venue_order_id": VenueOrderId("OTHER")},
            "no_venue": {"venue_order_id": None},
            "trader": {"trader_id": type(order.trader_id)("OTHER-001")},
        }.get(fault, {})

        rejection = (_modify_rejected if action == "modify" else _cancel_rejected)(
            order, **overrides,
        )
        invalid_state = fault in {
            "record_id", "side", "unseen_fill", "filled", "source_account", "source_client",
            "hedge_account", "hedge_client",
        }
        if invalid_state:
            assert not strategy._source_action_is_obsolete(rejection)
            with pytest.raises(ValueError, match="Maker"):
                engine.kernel.exec_engine.process(rejection)
            restored = _reload_maker_store(strategy)
            assert restored.source_order(order.client_order_id.value) == record
            assert restored.has_seen_source_fill(store.intents()[0].fill_key)
            assert restored.has_unresolved_hedges() and not restored.can_submit_source()
        else:
            engine.kernel.exec_engine.process(rejection)

        observed = store.source_order(order.client_order_id.value)
        assert observed is not None
        if not invalid_state:
            assert observed.status == "UNKNOWN" and store.halt_reason is not None
        assert strategy._source_hold and not store.can_submit_source()
        assert len(store.intents()) == 1  # Known hedge is never discarded.


@pytest.mark.parametrize("action", ["cancel", "modify"])
def test_native_late_cancel_rejection_does_not_clear_independent_hold(
    tmp_path: Path, action: str,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "cancel-existing-hold")
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        order = _seed_terminal_source(engine, strategy, maker=True)
        _process_source_terminal(engine, order)
        store = strategy._stores[SourceDirection.LONG]
        store._state.halt_reason = "independent reconciliation conflict"
        store.freeze_source_submissions("operator HOLD")
        original = store.path.read_bytes()

        engine.kernel.exec_engine.process(
            (_modify_rejected if action == "modify" else _cancel_rejected)(order),
        )

        assert store.path.read_bytes() == original
        assert store.halt_reason == "independent reconciliation conflict"
        assert store.source_freeze_reason == "operator HOLD"
        assert not store.can_submit_source()


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


def _subunit_fill() -> Any:
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(1),
        price=instrument.make_price(2400),
        client_order_id=ClientOrderId("O-MAKER-EVENTS"),
    )
    return TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("V-MAKER"),
        trade_id=TradeId("T-SUBUNIT"),
        last_qty=Quantity.from_str("0.5"),
        commission=Money(0, instrument.quote_currency),
    )


def test_partial_final_duplicate_and_late_fills_keep_pair_and_freeze_both_sides(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(
        tmp_path / "maker.state",
        hedge_client_id=ClientId("HEDGE-CLIENT"),
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(2),
        source_account_id="BITFINEX-001",
        source_client_id="SOURCE-CLIENT",
        hedge_account_id="MT5-001",
        hedge_client_id="HEDGE-CLIENT",
    )
    strategy._working_quotes["O-MAKER-EVENTS"] = _bound_quote()
    partial, final = _filled_events()

    strategy.on_order_filled(partial)
    assert strategy.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    strategy._finish_or_reject("O-MAKER-EVENTS", "CANCELED")
    assert "O-MAKER-EVENTS" not in strategy._working_quotes
    strategy.on_order_filled(final)  # distinct late fill after cancel
    strategy.on_order_filled(partial)  # duplicate early fill
    strategy.on_order_filled(final)  # duplicate late fill

    assert [entry[3].source_trade_id for entry in strategy.recorded] == ["T-PARTIAL"]
    strategy.on_order_filled(
        cast(
            Any,
            SimpleNamespace(
                instrument_id=_hedge_instrument().id,
                client_order_id=ClientOrderId("H-RECORDED-1"),
                trade_id=TradeId("HEDGE-PARTIAL"),
                last_qty=Quantity.from_int(1),
            ),
        )
    )

    assert [entry[3].source_trade_id for entry in strategy.recorded] == [
        "T-PARTIAL",
        "T-FINAL",
    ]
    assert all(entry[1] == AccountId("MT5-001") for entry in strategy.recorded)
    assert all(entry[2] == ClientId("HEDGE-CLIENT") for entry in strategy.recorded)
    assert store.rounding_residual_ounces == 0
    assert store.net_unhedged_ounces == D(1)


def test_unknown_engine_rejection_freezes_source_without_declaring_failure(
    tmp_path: Path,
) -> None:
    requested: list[tuple[ClientOrderId, VenueOrderId]] = []

    def query(
        client_order_id: ClientOrderId,
        venue_order_id: VenueOrderId,
        _complete: SourceTerminalResult,
    ) -> None:
        requested.append((client_order_id, venue_order_id))

    strategy = TerminalQueryMakerStrategy(
        tmp_path / "unknown.state",
        source_terminal_query=query,
    )
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(1),
        price=instrument.make_price(2400),
        client_order_id=ClientOrderId("O-MAKER-UNKNOWN"),
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        order.client_order_id.value,
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    strategy._working_quotes[order.client_order_id.value] = _bound_quote()

    strategy.on_order_rejected(_rejected(order, "UNKNOWN"))

    record = store.source_order(order.client_order_id.value)
    assert record is not None and record.status == "UNKNOWN"
    assert store.active_source_order_id == order.client_order_id.value
    assert not store.can_submit_source()
    assert strategy.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    assert requested == []


def test_restart_late_fill_uses_durable_pair_and_keeps_reconciliation_hold(
    tmp_path: Path,
) -> None:
    state_prefix = tmp_path / "restart.state"
    original = RecordingMakerStrategy(
        state_prefix,
        hedge_client_id=ClientId("HEDGE-CLIENT"),
    )
    original._stores[SourceDirection.LONG].begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(2),
        source_account_id="BITFINEX-001",
        source_client_id="SOURCE-CLIENT",
        hedge_account_id="MT5-001",
        hedge_client_id="HEDGE-CLIENT",
    )

    restarted = RecordingMakerStrategy(
        state_prefix,
        hedge_client_id=ClientId("HEDGE-CLIENT"),
    )
    store = restarted._stores[SourceDirection.LONG]
    assert store.recover_for_start() is not None
    partial, _ = _filled_events()
    restarted.on_order_filled(partial)

    assert len(restarted.recorded) == 1
    assert restarted.recorded[0][1] == AccountId("MT5-001")
    assert restarted.recorded[0][2] == ClientId("HEDGE-CLIENT")
    assert store.has_unresolved_hedges()
    assert store.net_unhedged_ounces == D(1)
    assert store.halt_reason is not None


@pytest.mark.parametrize(
    ("configured_client_id", "persisted_client_id"),
    [
        (None, "HEDGE-OLD"),
        (ClientId("HEDGE-CURRENT"), None),
        (ClientId("HEDGE-CURRENT"), "HEDGE-OLD"),
    ],
    ids=["unexpected-persisted", "persisted-missing", "client-drift"],
)
def test_maker_durable_hedge_client_mismatch_blocks_before_submit(
    tmp_path: Path,
    configured_client_id: ClientId | None,
    persisted_client_id: str | None,
) -> None:
    strategy = RecordingMakerStrategy(
        tmp_path / f"client-route-{persisted_client_id}.state",
        hedge_client_id=configured_client_id,
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-CLIENT-ROUTE",
        BusinessOrderSide.BUY,
        D(1),
        hedge_account_id="MT5-001",
        hedge_client_id=persisted_client_id,
    )
    intent = store.reserve_source_fill(
        fill_key="O-CLIENT-ROUTE|V|T-CLIENT-ROUTE",
        client_order_id="O-CLIENT-ROUTE",
        trade_id="T-CLIENT-ROUTE",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    assert intent is not None

    strategy._submit_next_pending_hedge()

    assert strategy.recorded == []
    assert store.intent(intent.intent_id).status is ObligationStatus.BLOCKED
    source = store.source_order("O-CLIENT-ROUTE")
    assert source is not None and source.status == "UNKNOWN"
    assert "durable Maker hedge route" in cast(str, store.halt_reason)


def test_subunit_fill_with_no_rounded_intent_still_freezes_and_cancels_both(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "subunit.state")
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    strategy.on_order_filled(_subunit_fill())

    assert strategy.recorded == []
    assert strategy.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    assert store.rounding_residual_ounces == D("0.5")
    assert store.net_unhedged_ounces == D("0.5")
    assert all(not item.can_submit_source() for item in strategy._stores.values())
    assert not strategy._try_release_cycle()


def test_hedge_dispatch_precedes_cancel_and_first_cancel_failure_is_isolated(
    tmp_path: Path,
) -> None:
    strategy = CancelFaultMakerStrategy(tmp_path / "cancel-fault.state")
    strategy._stores[SourceDirection.LONG].begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    full, _ = _filled_events(quantity=1)

    strategy.on_order_filled(full)

    assert strategy.actions == ["hedge", "cancel-bid", "cancel-ask"]
    assert len(strategy.recorded) == 1
    assert strategy.canceled == [SourceDirection.SHORT]
    assert strategy._global_obligation_block()


def test_maker_two_direction_obligations_share_one_global_mt5_flight(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(
        tmp_path / "global-flight.state",
        hedge_client_id=ClientId("HEDGE-CLIENT"),
    )
    for direction, side in (
        (SourceDirection.LONG, BusinessOrderSide.BUY),
        (SourceDirection.SHORT, BusinessOrderSide.SELL),
    ):
        store = strategy._stores[direction]
        source_id = f"O-{direction.value}"
        store.begin_source(
            source_id,
            side,
            D(1),
            source_account_id="BITFINEX-001",
            hedge_account_id="MT5-001",
            hedge_client_id="HEDGE-CLIENT",
        )
    # Both quotes are working before either actual fill freezes the whole Maker.
    for direction, side in (
        (SourceDirection.LONG, BusinessOrderSide.BUY),
        (SourceDirection.SHORT, BusinessOrderSide.SELL),
    ):
        store = strategy._stores[direction]
        source_id = f"O-{direction.value}"
        assert store.reserve_source_fill(
            fill_key=f"{source_id}|V|T-{direction.value}",
            client_order_id=source_id,
            trade_id=f"T-{direction.value}",
            source_side=side,
            fill_ounces=D(1),
        ) is not None

    strategy._submit_next_pending_hedge()
    strategy._submit_next_pending_hedge()

    assert [entry[0] for entry in strategy.recorded] == [SourceDirection.LONG]
    strategy.on_order_filled(
        cast(
            Any,
            SimpleNamespace(
                instrument_id=_hedge_instrument().id,
                client_order_id=ClientOrderId("H-RECORDED-1"),
                trade_id=TradeId("HT-FIRST"),
                last_qty=Quantity.from_int(1),
            ),
        )
    )

    assert [entry[0] for entry in strategy.recorded] == [
        SourceDirection.LONG,
        SourceDirection.SHORT,
    ]


@pytest.mark.parametrize("first", [SourceDirection.LONG, SourceDirection.SHORT])
def test_maker_dispatch_preserves_native_interleaved_fill_allocation_order(
    tmp_path: Path, first: SourceDirection,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "ordered-native-fills")
    with _event_engine(cast(Any, strategy)) as engine:
        values = CryptoPerpetual.to_dict(_source_instrument())
        values.update(size_precision=1, size_increment="0.1", lot_size="0.1")
        engine.add_instrument(CryptoPerpetual.from_dict(values))
        engine.trader.start()
        orders = _seed_native_working_quotes(cast(Any, strategy), exact_route=True)
        if first is SourceDirection.SHORT:
            orders.reverse()
        # Source fills enter the real Engine. Hedge wire is the existing recording
        # stub: its first binding remains SUBMITTING while every late fill arrives.
        for index in range(7):
            _fill_maker_source(
                engine, orders[index % 2], D("0.6") if index == 0 else D("0.2"),
                trade_id=f"SEQ-{index}", ts_event=101 + index,
            )
        assert len(strategy.recorded) == 1
        assert sum(len(view.intents()) for view in strategy._stores.values()) == 7
        net_hedge = D(0)
        observed_positions: list[Decimal] = []
        for index in range(7):
            intent = strategy.recorded[index][3]
            net_hedge += (D(1) if intent.hedge_side is BusinessOrderSide.BUY else D(-1))
            strategy.on_order_filled(cast(Any, SimpleNamespace(
                instrument_id=_hedge_instrument().id,
                client_order_id=ClientOrderId(f"H-RECORDED-{index + 1}"),
                trade_id=TradeId(f"HD-{index}"), last_qty=Quantity.from_int(1),
            )))
            observed_positions.append(net_hedge)
        assert [entry[3].source_trade_id for entry in strategy.recorded] == [
            f"SEQ-{index}" for index in range(7)
        ]
        sign = D(-1) if first is SourceDirection.LONG else D(1)
        assert observed_positions == [sign, D(0), sign, D(0), sign, D(0), sign]
        assert all(intent.status is ObligationStatus.COMPLETED
                   for view in strategy._stores.values() for intent in view.intents())


@pytest.mark.parametrize("status", [
    ObligationStatus.SUBMITTING, ObligationStatus.SUBMITTED, ObligationStatus.ACCEPTED,
    ObligationStatus.BLOCKED, ObligationStatus.REJECTED, ObligationStatus.UNKNOWN,
])
def test_maker_ordered_dispatch_keeps_global_failure_and_inflight_gate(
    tmp_path: Path, status: ObligationStatus,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "global-ordered-gate")
    for direction, side in ((SourceDirection.LONG, BusinessOrderSide.BUY),
                            (SourceDirection.SHORT, BusinessOrderSide.SELL)):
        strategy._stores[direction].begin_source(
            direction.value, side, D(1), source_account_id="BITFINEX-001",
            hedge_account_id="MT5-001",
        )
    for direction, side in ((SourceDirection.SHORT, BusinessOrderSide.SELL),
                            (SourceDirection.LONG, BusinessOrderSide.BUY)):
        view = strategy._stores[direction]
        view.reserve_source_fill(
            fill_key=f"{direction.value}|V|T-{direction.value}",
            client_order_id=direction.value, trade_id=f"T-{direction.value}",
            source_side=side, fill_ounces=D(1),
        )
    # Even a later allocation's independent failure/flight blocks all dispatch;
    # switching to allocation order must not turn the global gate into a prefix gate.
    later = strategy._stores[SourceDirection.LONG]
    intent = later.intents()[0]
    if status is ObligationStatus.BLOCKED:
        later.block_hedge_intent(intent.intent_id, "known block")
    else:
        later.bind_hedge_order(intent.intent_id, "OTHER-HEDGE")
        later.update_hedge_status("OTHER-HEDGE", status)
    before = later.path.read_bytes()
    strategy._submit_next_pending_hedge()
    assert strategy.recorded == [] and later.path.read_bytes() == before
    assert later.intent(intent.intent_id).status is status


def test_maker_uses_shared_multi_ticket_planner_for_exact_close_legs(
    tmp_path: Path,
) -> None:
    store = JsonStateStore(tmp_path / "maker-shared-plan.json")
    store.begin_source(
        "O-MAKER-PLAN",
        BusinessOrderSide.BUY,
        D(2),
        hedge_account_id="MT5-001",
        hedge_client_id="HEDGE-CLIENT",
    )
    intent = store.reserve_source_fill(
        fill_key="O-MAKER-PLAN|V|T",
        client_order_id="O-MAKER-PLAN",
        trade_id="T-MAKER-PLAN",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    positions = [
        SimpleNamespace(
            id=PositionId("2"),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        ),
        SimpleNamespace(
            id=PositionId("1"),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        ),
    ]
    calls: list[dict[str, object]] = []
    submissions: list[PositionId | None] = []

    def market(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(client_order_id=ClientOrderId(f"H-MAKER-{len(calls)}"))

    coordinator = HedgeCoordinator(_source_instrument().id, store)
    harness = SimpleNamespace(
        _config=SimpleNamespace(hedge_instrument_id=_hedge_instrument().id),
        _hedges={SourceDirection.LONG: coordinator},
        _stores={SourceDirection.LONG: store},
        cache=SimpleNamespace(
            quote_tick=lambda _instrument_id: object(),
            positions_open=lambda **_kwargs: positions,
        ),
        order_factory=SimpleNamespace(market=market),
        log=SimpleNamespace(error=lambda _message: None),
        _quote_is_fresh=lambda _tick: True,
        _required_hedge_instrument=lambda: _hedge_instrument(),
        _hedge_positions=lambda _account_id: positions,
        submit_order=lambda _order, *, position_id, client_id, params: submissions.append(
            position_id,
        ),
    )

    MakerStrategy._submit_hedge(
        cast(Any, harness),
        SourceDirection.LONG,
        AccountId("MT5-001"),
        ClientId("HEDGE-CLIENT"),
        intent,
    )
    positions.pop()
    assert coordinator.on_hedge_filled(
        cast(
            Any,
            SimpleNamespace(
                client_order_id=ClientOrderId("H-MAKER-1"),
                trade_id=TradeId("HT-MAKER-1"),
                last_qty=Quantity.from_int(1),
            ),
        )
    )
    MakerStrategy._submit_hedge(
        cast(Any, harness),
        SourceDirection.LONG,
        AccountId("MT5-001"),
        ClientId("HEDGE-CLIENT"),
        store.intent(intent.intent_id),
    )

    assert [call["reduce_only"] for call in calls] == [True, True]
    assert submissions == [PositionId("1"), PositionId("2")]


@pytest.mark.parametrize("source_missing", [True, False], ids=["source-missing", "source-hold"])
def test_maker_fresh_hedge_quote_retries_pending_leg_exactly_once(
    tmp_path: Path,
    source_missing: bool,
) -> None:
    prefix = tmp_path / f"quote-retry-{source_missing}.state"
    config = _maker_strategy_config(prefix)
    owner = MakerStateStore(
        prefix, str(config.source_instrument_id), str(config.hedge_instrument_id),
    )
    stores = owner.stores
    hedges = {
        direction: HedgeCoordinator(config.source_instrument_id, stores[direction])
        for direction in (SourceDirection.LONG, SourceDirection.SHORT)
    }
    store = stores[SourceDirection.LONG]
    store.begin_source(
        "O-QUOTE-RETRY",
        BusinessOrderSide.BUY,
        D(2),
        hedge_account_id="MT5-001",
        hedge_client_id=None,
    )
    intent = store.reserve_source_fill(
        fill_key="O-QUOTE-RETRY|V|T-QUOTE-RETRY",
        client_order_id="O-QUOTE-RETRY",
        trade_id="T-QUOTE-RETRY",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    positions = [
        SimpleNamespace(
            id=PositionId("2"),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        ),
        SimpleNamespace(
            id=PositionId("1"),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        ),
    ]
    source_tick = _quote(_source_instrument(), "2399", "2400", "5", 1_000_000_000)
    hedge_tick = _quote(_hedge_instrument(), "2401", "2402", "5", 1_000_000_000)
    fresh = {"value": True}
    market_calls: list[dict[str, object]] = []
    submissions: list[PositionId | None] = []
    frozen: list[str] = []
    canceled: list[str] = []

    def quote_tick(instrument_id: object) -> Any:
        if instrument_id == config.hedge_instrument_id:
            return hedge_tick
        return None if source_missing else source_tick

    def market(**kwargs: object) -> object:
        market_calls.append(kwargs)
        return SimpleNamespace(client_order_id=ClientOrderId(f"H-WAKE-{len(market_calls)}"))

    harness = SimpleNamespace(
        _config=config,
        _state_store=owner,
        _stores=stores,
        _hedges=hedges,
        cache=SimpleNamespace(
            quote_tick=quote_tick,
            positions_open=lambda **_kwargs: positions,
        ),
        order_factory=SimpleNamespace(market=market),
        log=SimpleNamespace(error=lambda _message: None),
        clock=SimpleNamespace(timestamp_ns=lambda: 1_000_000_000),
        _quote_is_fresh=lambda _tick: fresh["value"],
        _carry_for_hedge_tick=lambda _tick, _now_ns: CarryConfig(),
        _quote_carry=CarryConfig(),
        _required_hedge_instrument=lambda: _hedge_instrument(),
        _hedge_positions=lambda _account_id: positions,
        submit_order=lambda _order, *, position_id, client_id, params: submissions.append(
            position_id,
        ),
        _try_release_cycle=lambda: False,
        _inputs_are_fresh=lambda _source, _hedge, _now_ns: True,
        _global_obligation_block=lambda: True,
        _freeze_and_cancel_all=lambda reason: frozen.append(reason),
        _cancel_all_best_effort=lambda reason: canceled.append(reason),
        _refresh_direction=lambda *_args: (_ for _ in ()).throw(
            AssertionError("source quote maintenance must stay blocked")
        ),
    )
    harness._durable_hedge_route = lambda direction, client_order_id: (
        MakerStrategy._durable_hedge_route(
            cast(Any, harness),
            direction,
            client_order_id,
        )
    )
    harness._submit_hedge = lambda direction, account_id, client_id, current: (
        MakerStrategy._submit_hedge(
            cast(Any, harness),
            direction,
            account_id,
            client_id,
            current,
        )
    )
    harness._submit_next_pending_hedge = lambda: MakerStrategy._submit_next_pending_hedge(
        cast(Any, harness)
    )
    harness._evaluate_quotes = lambda: MakerStrategy._evaluate_quotes(cast(Any, harness))

    harness._submit_next_pending_hedge()
    assert submissions == [PositionId("1")]
    positions.pop()
    fresh["value"] = False
    MakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(
            Any,
            SimpleNamespace(
                instrument_id=config.hedge_instrument_id,
                client_order_id=ClientOrderId("H-WAKE-1"),
                trade_id=TradeId("HT-WAKE-1"),
                last_qty=Quantity.from_int(1),
            ),
        ),
    )
    waiting = store.intent(intent.intent_id)
    assert submissions == [PositionId("1")]
    assert waiting.status is ObligationStatus.PENDING
    assert waiting.hedge_client_order_id is None

    fresh["value"] = True
    MakerStrategy.on_quote_tick(cast(Any, harness), hedge_tick)
    MakerStrategy.on_quote_tick(cast(Any, harness), hedge_tick)

    assert submissions == [PositionId("1"), PositionId("2")]
    submitted = store.intent(intent.intent_id)
    assert submitted.status is ObligationStatus.SUBMITTING
    assert submitted.hedge_client_order_id == "H-WAKE-2"
    if not source_missing:
        assert canceled == ["unresolved Maker obligations"] * 2
    assert not frozen and owner.cycle_freeze_only


def _seed_filled_two_sided_cycle(
    strategy: RecordingMakerStrategy,
) -> tuple[JsonStateStore, JsonStateStore, HedgeIntent]:
    bid_store = strategy._stores[SourceDirection.LONG]
    ask_store = strategy._stores[SourceDirection.SHORT]
    bid_store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    ask_store.begin_source(
        "O-SIBLING",
        BusinessOrderSide.SELL,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    full, _ = _filled_events(quantity=1)
    strategy.on_order_filled(full)
    return bid_store, ask_store, bid_store.intents()[0]


def test_complete_source_terminals_and_hedge_evidence_release_next_cycle(
    tmp_path: Path,
) -> None:
    strategy = CyclePlacementMakerStrategy(tmp_path / "cycle-complete.state")
    bid_store, ask_store, intent = _seed_filled_two_sided_cycle(strategy)
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    bid_store.apply_hedge_fill(
        client_order_id=hedge_order_id,
        trade_id="HT-COMPLETE",
        fill_ounces=D(1),
    )
    strategy._finish_or_reject("O-SIBLING", "CANCELED")

    assert not strategy._try_release_cycle()
    ask_store.confirm_source_reconciled("O-SIBLING")
    assert strategy._try_release_cycle()

    assert bid_store.source_freeze_reason is None
    assert ask_store.source_freeze_reason is None
    assert bid_store.can_submit_source()
    assert ask_store.can_submit_source()
    assert not strategy._global_obligation_block()

    source_book = BookTop(D(2399), D(2400), D(5), D(5))
    hedge_book = BookTop(D(2401), D(2402), D(5), D(5))
    for direction in (SourceDirection.LONG, SourceDirection.SHORT):
        strategy._refresh_direction(direction, source_book, hedge_book)

    assert strategy.placed_source_ids == ["O-CYCLE-2-bid", "O-CYCLE-2-ask"]
    assert bid_store.active_source_order_id == "O-CYCLE-2-bid"
    assert ask_store.active_source_order_id == "O-CYCLE-2-ask"

    hedge_count = len(strategy.recorded)
    intent_count = len(bid_store.intents())
    cancel_count = len(strategy.canceled)
    old_duplicate, _ = _filled_events(quantity=1)
    strategy.on_order_filled(old_duplicate)

    assert len(strategy.recorded) == hedge_count
    assert len(bid_store.intents()) == intent_count
    assert len(strategy.canceled) == cancel_count
    assert bid_store.active_source_order_id == "O-CYCLE-2-bid"
    assert ask_store.active_source_order_id == "O-CYCLE-2-ask"
    assert bid_store.source_freeze_reason is None
    assert ask_store.source_freeze_reason is None


def test_missing_sibling_terminal_does_not_release_completed_hedge(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "cycle-missing-sibling.state")
    bid_store, ask_store, intent = _seed_filled_two_sided_cycle(strategy)
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    bid_store.apply_hedge_fill(
        client_order_id=hedge_order_id,
        trade_id="HT-COMPLETE",
        fill_ounces=D(1),
    )

    assert not strategy._try_release_cycle()
    assert ask_store.active_source_order_id == "O-SIBLING"
    assert strategy._global_obligation_block()


@pytest.mark.parametrize("hedge_outcome", ["partial", "rejected"])
def test_partial_or_rejected_hedge_does_not_release_cycle(
    tmp_path: Path,
    hedge_outcome: str,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / f"cycle-{hedge_outcome}.state")
    bid_store, _, intent = _seed_filled_two_sided_cycle(strategy)
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    if hedge_outcome == "partial":
        bid_store.apply_hedge_fill(
            client_order_id=hedge_order_id,
            trade_id="HT-PARTIAL",
            fill_ounces=D("0.5"),
        )
    else:
        bid_store.update_hedge_status(hedge_order_id, ObligationStatus.REJECTED)
    strategy._finish_or_reject("O-SIBLING", "CANCELED")
    ask_store = strategy._stores[SourceDirection.SHORT]
    ask_store.confirm_source_reconciled("O-SIBLING")
    assert not strategy._try_release_cycle()

    assert strategy._global_obligation_block()
    assert bid_store.source_freeze_reason is not None


def test_restart_releases_only_from_persisted_complete_cycle_evidence(
    tmp_path: Path,
) -> None:
    state_prefix = tmp_path / "cycle-restart-complete.state"
    original = RecordingMakerStrategy(state_prefix)
    bid_store, ask_store, intent = _seed_filled_two_sided_cycle(original)
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    bid_store.apply_hedge_fill(
        client_order_id=hedge_order_id,
        trade_id="HT-COMPLETE",
        fill_ounces=D(1),
    )
    ask_store.update_source_status("O-SIBLING", "CANCELED")
    ask_store.confirm_source_reconciled("O-SIBLING")

    restarted = MakerStrategy(_maker_strategy_config(state_prefix))

    assert restarted._try_release_cycle()
    assert not restarted._global_obligation_block()


def test_restart_does_not_release_incomplete_persisted_cycle(tmp_path: Path) -> None:
    state_prefix = tmp_path / "cycle-restart-incomplete.state"
    original = RecordingMakerStrategy(state_prefix)
    _seed_filled_two_sided_cycle(original)

    restarted = MakerStrategy(_maker_strategy_config(state_prefix))

    assert not restarted._try_release_cycle()
    assert restarted._global_obligation_block()


def test_keep_last_accounts_true_is_rejected_until_restart_safe(tmp_path: Path) -> None:
    config = _maker_strategy_config(tmp_path / "unsupported-keep-last.state")

    with pytest.raises(ValueError, match="restart-safe"):
        struct_replace(config, keep_last_accounts=True)


def test_old_timer_order_identity_cannot_target_replacement() -> None:
    old_bid = _maker_timer_name(SourceDirection.LONG, "O-A")
    current_bid = _maker_timer_name(SourceDirection.LONG, "O-B")
    current_ask = _maker_timer_name(SourceDirection.SHORT, "O-ASK")

    assert _maker_timer_target(old_bid, "O-B", "O-ASK") is None
    assert _maker_timer_target(current_bid, "O-B", "O-ASK") == (
        SourceDirection.LONG,
        "O-B",
    )
    assert _maker_timer_target(current_ask, "O-B", "O-ASK") == (
        SourceDirection.SHORT,
        "O-ASK",
    )


class _ModifyInstrument:
    def make_price(self, value: Decimal) -> Decimal:
        return value

    def make_qty(self, value: Decimal) -> Decimal:
        return value


class _ModifyHarness:
    def __init__(self, state_prefix: Path) -> None:
        self._config = _maker_strategy_config(state_prefix)
        self.modified: list[dict[str, Any]] = []

    def _required_source_instrument(self) -> _ModifyInstrument:
        return _ModifyInstrument()

    def modify_order(self, order: Any, **kwargs: Any) -> None:
        self.modified.append({"order": order, **kwargs})


def test_fixed_amount_false_requote_modifies_price_with_quantity_none(tmp_path: Path) -> None:
    harness = _ModifyHarness(tmp_path / "modify.state")
    order = SimpleNamespace(
        is_pending_update=False,
        is_pending_cancel=False,
        price=D(100),
    )
    desired = replace(_bound_quote(), source_price_usdt=D("101.01"))

    MakerStrategy._requote(cast(Any, harness), cast(Any, order), desired)

    assert not harness._config.fixed_amount
    assert len(harness.modified) == 1
    assert harness.modified[0]["price"] == D("101.01")
    assert harness.modified[0]["quantity"] is None


def _registered_clock_maker(tmp_path: Path, on_cancel: Any) -> tuple[MakerStrategy, list[Any]]:
    """Real strategy, clock, cache, orders and store; only outgoing venue IO is captured."""
    clock = LiveClock()
    now = clock.timestamp_ns()
    config = struct_replace(
        _maker_strategy_config(tmp_path / "live-clock"),
        max_quote_age_ns=50_000_000,
        max_cost_age_ns=1_000_000_000,
        max_session_age_ns=1_000_000_000,
        initial_cost_ts_ns=now,
        initial_session_ts_ns=now,
        initial_hedge_session_open=True,
    )
    strategy = MakerStrategy(config)
    cache, msgbus = TestComponentStubs.cache(), TestComponentStubs.msgbus()
    for instrument in (_source_instrument(), _hedge_instrument()):
        cache.add_instrument(instrument)
    msgbus.register(endpoint="DataEngine.execute", handler=lambda _command: None)
    msgbus.register(endpoint="ExecEngine.execute", handler=on_cancel)
    strategy.register(
        trader_id=msgbus.trader_id, portfolio=TestComponentStubs.portfolio(),
        msgbus=msgbus, cache=cache, clock=clock,
    )
    strategy.start()
    orders = _seed_native_working_quotes(cast(Any, strategy))
    now = clock.timestamp_ns()
    strategy._cost_ts_ns = strategy._session_ts_ns = now
    for instrument in (_source_instrument(), _hedge_instrument()):
        cache.add_quote_tick(_quote(instrument, "2399", "2401", "5", now))
    return strategy, orders


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvloop"])
@pytest.mark.parametrize("debug", [False, True])
def test_live_maker_stale_timer_mutates_real_state_only_on_running_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop_kind: str, debug: bool,
) -> None:
    async def scenario() -> None:
        loop, loop_thread = asyncio.get_running_loop(), threading.get_ident()
        completed = asyncio.Event()
        cancellations: list[tuple[int, Any]] = []
        handler_threads: list[int] = []
        persist_threads: list[int] = []

        def cancel(command: Any) -> None:
            cancellations.append((threading.get_ident(), command))
            if len(cancellations) == 2:
                loop.call_soon_threadsafe(completed.set)

        strategy, orders = _registered_clock_maker(tmp_path, cancel)
        original = strategy._on_stale_timer

        def handle(event: Any) -> None:
            handler_threads.append(threading.get_ident())
            original(event)

        monkeypatch.setattr(strategy, "_on_stale_timer", handle)
        persist = strategy._state_store._persist

        def record_persist() -> None:
            persist_threads.append(threading.get_ident())
            persist()

        monkeypatch.setattr(strategy._state_store, "_persist", record_persist)
        try:
            strategy._schedule_stale_timer(SourceDirection.LONG, orders[0].client_order_id.value)
            # Only the real timer may wake this idle loop; no polling or market/event pump.
            await asyncio.wait_for(completed.wait(), 0.5)
            # LiveClock may wake before the deadline and legitimately rearm.
            # Every callback, including an early one, must run on this loop.
            assert handler_threads and all(thread == loop_thread for thread in handler_threads)
            assert persist_threads == [loop_thread]
            assert [thread for thread, _ in cancellations] == [loop_thread, loop_thread]
            assert [cmd.client_order_id for _, cmd in cancellations] == [
                order.client_order_id for order in orders
            ]
            assert all(order.status == OrderStatus.PENDING_CANCEL for order in orders)
            assert strategy._source_hold
            assert all(store.source_freeze_reason == "stale timer"
                       for store in strategy._stores.values())
        finally:
            strategy.stop()
            strategy.dispose()

    factory = uvloop.new_event_loop if loop_kind == "uvloop" else asyncio.SelectorEventLoop
    with asyncio.Runner(loop_factory=factory, debug=debug) as runner:
        runner.run(scenario())


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvloop"])
@pytest.mark.parametrize("restart", [False, True])
def test_live_maker_queued_stale_timer_cannot_cross_stop_or_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop_kind: str, restart: bool,
) -> None:
    async def scenario() -> None:
        loop_thread = threading.get_ident()
        cancellations: list[Any] = []
        handled: list[Any] = []
        timer_threads: list[int] = []
        queued = threading.Event()
        strategy, orders = _registered_clock_maker(tmp_path, cancellations.append)
        original_handler = strategy._on_stale_timer
        original_dispatch = strategy._dispatch_stale_timer

        def handle(event: Any) -> None:
            handled.append(event)
            original_handler(event)

        def dispatch(event: Any, generation: int) -> None:
            timer_threads.append(threading.get_ident())
            original_dispatch(event, generation)
            queued.set()

        monkeypatch.setattr(strategy, "_on_stale_timer", handle)
        monkeypatch.setattr(strategy, "_dispatch_stale_timer", dispatch)
        try:
            strategy._schedule_stale_timer(SourceDirection.LONG, orders[0].client_order_id.value)
            # Deliberately hold this loop until the real Rust callback has queued delivery.
            assert queued.wait(0.5), "real timer did not enqueue loop delivery"
            assert timer_threads and all(thread != loop_thread for thread in timer_threads)
            assert handled == []
            strategy.stop()
            assert len(cancellations) == 2  # Actual stop cancels; late timer must not add work.
            if restart:
                # A surviving working CID makes name filtering insufficient: epoch must win.
                for order in orders:
                    order.apply(_cancel_rejected(order))
                    strategy.cache.update_order(order)
                    assert order.status == OrderStatus.ACCEPTED
                strategy.reset()
                strategy.start()
                assert strategy.is_running
            original = [store.path.read_bytes() for store in strategy._stores.values()]
            await asyncio.sleep(0)
            assert handled == []
            assert len(cancellations) == 2
            assert [store.path.read_bytes() for store in strategy._stores.values()] == original
            if restart:
                assert all(order.status == OrderStatus.ACCEPTED for order in orders)
                assert all(not store.can_submit_source() for store in strategy._stores.values())
        finally:
            if strategy.is_running:
                strategy.stop()
            strategy.dispose()

    factory = uvloop.new_event_loop if loop_kind == "uvloop" else asyncio.SelectorEventLoop
    with asyncio.Runner(loop_factory=factory, debug=True) as runner:
        runner.run(scenario())


class _TimerClock:
    def __init__(self) -> None:
        self.timer_names: set[str] = set()
        self.canceled: list[str] = []

    def cancel_timer(self, name: str) -> None:
        self.canceled.append(name)
        self.timer_names.remove(name)

    def set_time_alert_ns(self, *, name: str, alert_time_ns: int, callback: Any) -> None:
        assert alert_time_ns > 0
        assert callback is not None
        self.timer_names.add(name)


class _TimerCache:
    def quote_tick(self, instrument_id: Any) -> Any:
        return SimpleNamespace(ts_event=100)


def test_each_side_replaces_instead_of_accumulating_stale_timers(tmp_path: Path) -> None:
    harness = SimpleNamespace(
        _stale_timer_names={},
        _live_costs_from_adapters=False,
        _live_account_reader=None,
        _config=_maker_strategy_config(tmp_path / "timer.state"),
        _cost_ts_ns=100,
        _session_ts_ns=100,
        cache=_TimerCache(),
        clock=_TimerClock(),
        _on_stale_timer=lambda event: None,
    )

    MakerStrategy._schedule_stale_timer(cast(Any, harness), SourceDirection.LONG, "O-A")
    MakerStrategy._schedule_stale_timer(cast(Any, harness), SourceDirection.LONG, "O-B")

    assert harness.clock.timer_names == {
        _maker_timer_name(SourceDirection.LONG, "O-B")
    }
    assert harness.clock.canceled == [_maker_timer_name(SourceDirection.LONG, "O-A")]


class _StopStore:
    def __init__(self, active: str) -> None:
        self.active_source_order_id = active
        self.unknown: list[tuple[str, str]] = []
        self.freeze_reason: str | None = None

    def mark_source_unknown(self, client_order_id: str, reason: str) -> None:
        self.unknown.append((client_order_id, reason))

    def freeze_source_submissions(self, reason: str) -> None:
        self.freeze_reason = reason

    def knows_source_order(self, client_order_id: str) -> bool:
        return client_order_id == self.active_source_order_id


class _WorkingOrder:
    events: tuple[object, ...] = ()
    is_closed = False
    is_pending_cancel = False


class _StopCache:
    def __init__(self) -> None:
        self.orders = {"O-BID": _WorkingOrder(), "O-ASK": _WorkingOrder()}
        self.requested: list[str] = []

    def order(self, client_order_id: ClientOrderId) -> _WorkingOrder | None:
        self.requested.append(client_order_id.value)
        return self.orders.get(client_order_id.value)

    def quote_tick(self, instrument_id: Any) -> Any:
        return SimpleNamespace(ts_event=1)


class _StopHarness:
    _account_handle = None
    _account_topics: tuple[str, ...] = ()
    _live_account_reader = None

    def __init__(self) -> None:
        self._live_costs_from_adapters = False
        self._source_terminal_stopped = False
        self._source_terminal_generation = 0
        self._source_terminal_inflight: set[str] = set()
        self._stores = {
            SourceDirection.LONG: _StopStore("O-BID"),
            SourceDirection.SHORT: _StopStore("O-ASK"),
        }
        self._state_store: Any = SimpleNamespace(residuals=lambda: {})
        self.log: Any = SimpleNamespace(warning=lambda _message: None)
        self.cache = _StopCache()
        self.canceled: list[_WorkingOrder] = []
        self._source_hold = False
        self._config = _maker_strategy_config(Path("/tmp/stop-harness.state"))
        self._stale_timer_names: dict[SourceDirection, str] = {}
        self._cost_ts_ns = 0
        self._carry = CarryConfig()
        self._quote_carry = self._carry
        self._fx = FxConfig()
        self._cost_snapshot_valid = True
        self._cost_recovery_after_ns: int = 0
        self._session_ts_ns = 0
        self._hedge_session_open = True
        self.clock: Any = SimpleNamespace(timestamp_ns=lambda: 0)

    def cancel_order(self, order: _WorkingOrder) -> None:
        self.canceled.append(order)

    def _cancel_working(
        self,
        direction: SourceDirection,
        *,
        expected_order_id: str | None = None,
        reason: str,
    ) -> None:
        MakerStrategy._cancel_working(
            cast(Any, self),
            direction,
            expected_order_id=expected_order_id,
            reason=reason,
        )

    def _freeze_all_best_effort(self, reason: str) -> None:
        for store in self._stores.values():
            store.freeze_source_submissions(reason)

    def _freeze_and_cancel_all(self, reason: str) -> None:
        MakerStrategy._freeze_and_cancel_all(cast(Any, self), reason)

    def _cancel_all_best_effort(self, reason: str) -> None:
        for direction in (SourceDirection.LONG, SourceDirection.SHORT):
            self._cancel_working(direction, reason=reason)

    def _inputs_are_fresh(
        self,
        source_tick: Any,
        hedge_tick: Any,
        now_ns: int,
    ) -> bool:
        return False

    def _direction_for_source_order(
        self,
        client_order_id: str,
    ) -> SourceDirection | None:
        return MakerStrategy._direction_for_source_order(cast(Any, self), client_order_id)


def test_stop_cancels_exact_two_active_gtc_orders_without_releasing_gates() -> None:
    harness = _StopHarness()

    MakerStrategy.on_stop(cast(Any, harness))

    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2
    assert harness._stores[SourceDirection.LONG].active_source_order_id == "O-BID"
    assert harness._stores[SourceDirection.SHORT].active_source_order_id == "O-ASK"


def test_current_stale_timer_freezes_and_cancels_both_exact_active_ids() -> None:
    harness = _StopHarness()
    timer_name = _maker_timer_name(SourceDirection.LONG, "O-BID")
    harness._stale_timer_names[SourceDirection.LONG] = timer_name

    MakerStrategy._on_stale_timer(
        cast(Any, harness),
        cast(Any, SimpleNamespace(name=timer_name)),
    )

    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2
    assert all(store.freeze_reason == "stale timer" for store in harness._stores.values())


def test_old_stale_timer_is_a_total_noop_for_replacement_orders() -> None:
    harness = _StopHarness()
    old_timer = _maker_timer_name(SourceDirection.LONG, "O-OLD")

    MakerStrategy._on_stale_timer(
        cast(Any, harness),
        cast(Any, SimpleNamespace(name=old_timer)),
    )

    assert harness.cache.requested == []
    assert harness.canceled == []
    assert all(store.freeze_reason is None for store in harness._stores.values())


def test_source_unknown_immediately_freezes_and_cancels_both_without_tick() -> None:
    harness = _StopHarness()

    MakerStrategy._mark_source_unknown(
        cast(Any, harness),
        "O-BID",
        "injected source unknown",
    )

    assert harness._source_hold
    assert harness._stores[SourceDirection.LONG].unknown == [
        ("O-BID", "injected source unknown")
    ]
    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2


class _SessionHarness:
    def __init__(self) -> None:
        self._session_ts_ns = 1
        self._hedge_session_open = True
        self.canceled: list[SourceDirection] = []

    def _cancel_working(self, direction: SourceDirection, *, reason: str) -> None:
        assert reason == "hedge session closed or future-dated"
        self.canceled.append(direction)

    def _reschedule_active_timers(self) -> None:
        raise AssertionError("a closed session must cancel, not reschedule")

    def _freeze_and_cancel_all(self, reason: str) -> None:
        for direction in (SourceDirection.LONG, SourceDirection.SHORT):
            self._cancel_working(direction, reason=reason)


def test_session_close_immediately_freezes_both_gtc_sides_without_a_new_tick() -> None:
    harness = _SessionHarness()

    MakerStrategy.update_hedge_session(cast(Any, harness), False, 2)

    assert harness.canceled == [SourceDirection.LONG, SourceDirection.SHORT]


def test_cost_change_immediately_cancels_exact_active_ids_without_market_tick() -> None:
    harness = _StopHarness()
    harness.clock = SimpleNamespace(timestamp_ns=lambda: 2)
    harness._cost_ts_ns = 1
    harness._carry = CarryConfig()
    harness._fx = FxConfig()

    MakerStrategy.update_cost_snapshot(
        cast(Any, harness),
        CarryConfig(total_trade_fee=D("0.001")),
        FxConfig(usd_usdt_bid=D("0.999"), usd_usdt_ask=D("1.001")),
        2,
    )

    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2


class _CostTimerHarness(_StopHarness):
    _live_submission_ready = None

    def _inputs_are_fresh(self, source_tick: Any, hedge_tick: Any, now_ns: int) -> bool:
        return MakerStrategy._inputs_are_fresh(cast(Any, self), source_tick, hedge_tick, now_ns)

    def _schedule_stale_timer(self, direction: SourceDirection, order_id: str) -> None:
        MakerStrategy._schedule_stale_timer(cast(Any, self), direction, order_id)

    def _on_stale_timer(self, event: Any) -> None:
        MakerStrategy._on_stale_timer(cast(Any, self), event)


@pytest.mark.parametrize("expires_first", ["cost", "quote", "session"])
def test_same_cost_refresh_preserves_orders_until_actual_input_expiry(
    monkeypatch: pytest.MonkeyPatch,
    expires_first: str,
) -> None:
    harness = _CostTimerHarness()
    harness._config = struct_replace(
        harness._config,
        max_cost_age_ns=100,
        max_quote_age_ns=120 if expires_first == "quote" else 1_000,
        max_session_age_ns=120 if expires_first == "session" else 1_000,
    )
    harness._cost_ts_ns = harness._session_ts_ns = 100
    harness.clock = NautilusTestClock()
    harness.clock.set_time(100)
    monkeypatch.setattr(
        harness.cache, "quote_tick", lambda instrument_id: SimpleNamespace(ts_event=100),
    )
    harness._schedule_stale_timer(SourceDirection.LONG, "O-BID")
    timer_name = _maker_timer_name(SourceDirection.LONG, "O-BID")
    assert harness.clock.next_time_ns(timer_name) == 201

    assert harness.clock.advance_time(150) == []
    assert MakerStrategy.update_cost_snapshot(
        cast(Any, harness), harness._carry, harness._fx, 150,
    )
    assert harness.canceled == []
    assert all(store.freeze_reason is None for store in harness._stores.values())

    handlers = harness.clock.advance_time(201)
    assert len(handlers) == 1
    handlers[0].handle()
    deadline = 251 if expires_first == "cost" else 221
    assert harness.clock.next_time_ns(timer_name) == deadline
    assert harness.canceled == []

    handlers = harness.clock.advance_time(deadline)
    assert len(handlers) == 1
    handlers[0].handle()
    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2
    assert all(store.freeze_reason == "stale timer" for store in harness._stores.values())


class _MakerCostHarness:
    def __init__(self, state_prefix: Path, *, now_ns: int = 100) -> None:
        config = _maker_strategy_config(state_prefix)
        economics = struct_replace(
            config.economics,
            carry=CarryConfig(total_trade_fee=D("0.00065")),
        )
        self._config = struct_replace(config, economics=economics)
        self._live_costs_from_adapters = True
        self._carry = self._config.economics.carry
        self._quote_carry = self._carry
        self._fx = FxConfig()
        self._cost_ts_ns = 0
        self._cost_snapshot_valid = False
        self._cost_recovery_after_ns: int = 0
        self.now_ns = now_ns
        self.clock = SimpleNamespace(timestamp_ns=lambda: self.now_ns)
        self.frozen: list[str] = []
        self.errors: list[str] = []
        self.log = SimpleNamespace(error=self.errors.append)

    def _freeze_and_cancel_all(self, reason: str) -> None:
        self.frozen.append(reason)

    def _invalidate_cost_snapshot(self, reason: str) -> None:
        MakerStrategy._invalidate_cost_snapshot(cast(Any, self), reason)

    def update_cost_snapshot(
        self,
        carry: CarryConfig,
        fx: FxConfig,
        ts_event_ns: int,
    ) -> bool:
        return MakerStrategy.update_cost_snapshot(cast(Any, self), carry, fx, ts_event_ns)

    def on_funding_rate(self, update: FundingRateUpdate) -> None:
        MakerStrategy.on_funding_rate(cast(Any, self), update)


def _maker_funding(rate: str, ts_event: int) -> FundingRateUpdate:
    return FundingRateUpdate(
        instrument_id=_source_instrument().id,
        rate=D(rate),
        ts_event=ts_event,
        ts_init=ts_event,
    )


def test_repeated_same_funding_refreshes_freshness_without_canceling_again(tmp_path: Path) -> None:
    harness = _MakerCostHarness(tmp_path / "same-funding.state")
    harness.on_funding_rate(_maker_funding("0.001", 98))
    harness.frozen.clear()

    harness.on_funding_rate(_maker_funding("0.001", 99))
    harness.on_funding_rate(_maker_funding("0.001", 100))

    assert harness.frozen == []
    assert cast(Any, harness)._cost_ts_ns == 100
    assert cast(Any, harness)._cost_snapshot_valid
    assert cast(Any, harness)._carry.bitfinex_long == D("0.001")


@pytest.mark.parametrize(
    ("rate", "timestamp", "reason"),
    [
        ("0.001", 101, "Bitfinex funding observation is future-dated"),
        ("1.1", 99, "Bitfinex funding observation is invalid"),
    ],
)
def test_future_or_invalid_live_funding_invalidates_and_freezes_maker(
    tmp_path: Path,
    rate: str,
    timestamp: int,
    reason: str,
) -> None:
    harness = _MakerCostHarness(tmp_path / "bad-live-cost.state")

    harness.on_funding_rate(_maker_funding(rate, timestamp))

    assert not cast(Any, harness)._cost_snapshot_valid
    assert harness.frozen == [reason]
    assert harness.errors == [reason]


def test_equal_timestamp_cost_conflict_invalidates_until_fresh_newer_snapshot(
    tmp_path: Path,
) -> None:
    harness = _MakerCostHarness(tmp_path / "cost-recovery.state")

    harness.on_funding_rate(_maker_funding("0.001", 99))
    assert cast(Any, harness)._cost_snapshot_valid

    harness.on_funding_rate(_maker_funding("0.002", 99))
    assert not cast(Any, harness)._cost_snapshot_valid

    harness.on_funding_rate(_maker_funding("0.003", 100))
    assert cast(Any, harness)._cost_snapshot_valid
    assert cast(Any, harness)._cost_ts_ns == 100
    assert cast(Any, harness)._carry.bitfinex_long == D("0.003")
    assert harness.frozen == [
        "Maker costs changed",
        "Maker costs conflict at one timestamp",
        "Maker costs changed",
    ]


@pytest.mark.parametrize("failure", ["false", "raises"])
def test_live_hedge_quantity_failure_precedes_source_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / f"quantity-{failure}.state")
    strategy._source_instrument = _source_instrument()
    monkeypatch.setattr(strategy, "_hedge_positions", lambda _account_id: [])

    def quantity_ready(_quantity: Decimal) -> bool:
        if failure == "raises":
            raise RuntimeError("injected quantity failure")
        return False

    strategy._hedge_quantity_ready = quantity_ready
    strategy._submit_source(_bound_quote())

    assert strategy._stores[SourceDirection.LONG].source_orders() == ()
    assert not strategy._stores[SourceDirection.LONG].path.exists()


def test_live_hedge_quantity_preflight_checks_each_planned_mt5_ticket() -> None:
    checked: list[Decimal] = []
    positions = [
        SimpleNamespace(
            id=PositionId(position_id),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        )
        for position_id in ("2", "1")
    ]

    def quantity_ready(quantity: Decimal) -> bool:
        checked.append(quantity)
        return len(checked) == 1

    harness = SimpleNamespace(
        _config=SimpleNamespace(residual_mode="strict"),
        _hedge_quantity_ready=quantity_ready,
        _hedge_positions=lambda _account_id: positions,
        log=SimpleNamespace(error=lambda _message: None),
    )

    assert not MakerStrategy._source_hedge_is_executable(
        cast(Any, harness),
        _bound_quote(),
        D(2),
    )
    assert checked == [D(1), D(1)]


def _bounded_test_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, residual: str = "0.5",
    positions: list[Any] | None = None, budget: str = "10", quantity: str = "2",
) -> tuple[MakerStrategy, MakerQuote, list[Decimal]]:
    config = struct_replace(
        _maker_strategy_config(tmp_path / "carry"), residual_mode="bounded-carry",
        residual_limit_ounces=D("0.5"), max_unhedged_ounces=D(budget),
    )
    config = struct_replace(config, economics=struct_replace(
        config.economics, bid=struct_replace(config.economics.bid,
                                           open_quantity_ounces=D(quantity)),
    ))
    strategy = MakerStrategy(config)
    if D(residual):
        direction = SourceDirection.LONG if D(residual) > 0 else SourceDirection.SHORT
        side = BusinessOrderSide.BUY if D(residual) > 0 else BusinessOrderSide.SELL
        store = strategy._stores[direction]
        store.begin_source("PRIOR", side, D(1), source_account_id="BITFINEX-001",
                           hedge_account_id="MT5-001")
        assert store.reserve_source_fill(
            fill_key="PRIOR|VENUE|DUST", client_order_id="PRIOR", trade_id="DUST",
            source_side=side, fill_ounces=abs(D(residual)),
        ) is None
        store.update_source_status("PRIOR", "CANCELED")
        store.confirm_source_reconciled("PRIOR")
        assert strategy._state_store.clear_source_freezes()
    observed = positions or []
    monkeypatch.setattr(strategy, "_hedge_positions", lambda _account_id: observed)
    checked: list[Decimal] = []
    def quantity_ready(quantity: Decimal) -> bool:
        checked.append(quantity)
        return quantity <= 2
    strategy._hedge_quantity_ready = quantity_ready
    quote = _bound_quote()
    net = sum((p.quantity.as_decimal() * (1 if p.is_long else -1) for p in observed), D(0))
    quote = replace(quote, source_account=replace(quote.source_account, client_id=None),
                    hedge_account=replace(quote.hedge_account, client_id=None, position_ounces=net))
    return strategy, quote, checked


@pytest.mark.parametrize(("positions", "residual", "expected"), [
    ([2], "0.5", False),  # First .2 closes1; later3.8 would close1 + open3.
    ([2, 2], "0", True),  # Pure reduction remains executable in two2oz legs.
    ([], "0.5", False),
])
def test_bounded_preflight_covers_ticket_evolution_and_preserves_multiticket_reduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, positions: list[int],
    residual: str, expected: bool,
) -> None:
    tickets = [SimpleNamespace(id=PositionId(str(index + 1)), quantity=Quantity.from_int(amount),
                               is_long=True, is_short=False)
               for index, amount in enumerate(positions)]
    strategy, quote, checked = _bounded_test_strategy(
        tmp_path, monkeypatch, residual=residual, positions=tickets,
    )
    assert strategy._source_hedge_is_executable(quote, D(4)) is expected
    assert (D(3) in checked) is (positions == [2])


@pytest.mark.parametrize("fault", ["callback", "unit_step", "source_route", "hedge_route"])
def test_bounded_preflight_rejects_callback_failure_unit_step_and_other_candidate_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    strategy, quote, _ = _bounded_test_strategy(tmp_path, monkeypatch)
    if fault == "callback":
        def unavailable(_quantity: Decimal) -> bool:
            raise RuntimeError("quantity callback unavailable")
        strategy._hedge_quantity_ready = unavailable
    elif fault == "unit_step":
        strategy._hedge_quantity_ready = lambda quantity: quantity == 2
    elif fault == "source_route":
        quote = replace(quote, source_account=replace(quote.source_account,
                                                      client_id=ClientId("OTHER")))
    else:
        quote = replace(quote, hedge_account=replace(quote.hedge_account,
                                                    account_id=AccountId("MT5-OTHER")))
    original = strategy._state_store.path.read_bytes()
    assert not strategy._source_hedge_is_executable(quote, D(2))
    assert strategy._state_store.path.read_bytes() == original


def test_bounded_native_working_leaves_exclude_exact_self_and_do_not_net_two_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy, _, _ = _bounded_test_strategy(tmp_path, monkeypatch, budget="2.5")
    with _event_engine(cast(Any, strategy)) as engine:
        engine.trader.start()
        orders = _seed_native_working_quotes(cast(Any, strategy), exact_route=True)
        bid = orders[0]
        quote = strategy._working_quotes[bid.client_order_id.value]
        assert strategy._carry_source_is_executable(
            quote, D(2), bid.client_order_id.value,
        )
        assert not strategy._carry_source_is_executable(
            quote, D(1), bid.client_order_id.value,
        )  # Maintenance cannot replace actual leaves2 with an invented smaller amount.
        assert not strategy._source_hedge_is_executable(quote, D(2))
        other = orders[1]
        other.apply(TestEventStubs.order_pending_cancel(other, ts_event=101))
        strategy.cache.update_order(other)
        # Pending cancellation retains native leaves during maintenance.
        assert strategy._carry_source_is_executable(
            quote, D(2), bid.client_order_id.value,
        )


def test_bounded_stop_reports_signed_dust_without_changing_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy, _, _ = _bounded_test_strategy(tmp_path, monkeypatch, residual="-0.5")
    messages: list[str] = []
    harness = _StopHarness()
    harness._state_store = strategy._state_store
    harness.log = SimpleNamespace(warning=messages.append)
    before = strategy._state_store.path.read_bytes()
    MakerStrategy.on_stop(cast(Any, harness))
    assert len(messages) == 1 and "signed residual -0.5 ounces" in messages[0]
    assert "FLAT" not in messages[0]
    assert strategy._state_store.path.read_bytes() == before


@pytest.mark.parametrize("normalize", [False, True])
def test_bounded_normal_quote_uses_native_quantity_and_net_hedge_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, normalize: bool,
) -> None:
    strategy, template, checked = _bounded_test_strategy(
        tmp_path, monkeypatch, residual="0" if normalize else "-0.5",
        quantity="1.26" if normalize else "0.5", budget="1.26" if normalize else "1",
    )
    account = (template.hedge_account if normalize else replace(
        template.hedge_account, max_long_ounces=D(0), max_short_ounces=D(0),
    ))
    monkeypatch.setattr(strategy, "_hedge_accounts", lambda: (account,))
    with _event_engine(cast(Any, strategy)) as engine:
        values = CryptoPerpetual.to_dict(_source_instrument())
        values.update(size_precision=1, size_increment="0.1", lot_size="0.1")
        engine.add_instrument(CryptoPerpetual.from_dict(values))
        engine.trader.start()
        book = BookTop(D(2399), D(2401), D(10), D(10))
        quote = strategy._new_quote(SourceDirection.LONG, book, book)
        assert quote is not None
        assert quote.quantity_ounces == (D("1.3") if normalize else D("0.5"))
        if normalize:
            # Native1.3 exceeds U1.26, even though the unnormalized input did not.
            strategy._submit_source(quote)
            assert strategy._stores[SourceDirection.LONG].active_source_order_id is None
        else:
            assert strategy._source_hedge_is_executable(quote, quote.quantity_ounces)
            assert checked == []  # R-.5 + BUY.5 allocates0: no synthetic 1oz requirement.


def test_bounded_preflight_checks_later_initial_ticket_not_only_first_plan_pieces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets = [SimpleNamespace(id=PositionId(str(index + 1)),
                               quantity=Quantity.from_int(amount), is_long=True, is_short=False)
               for index, amount in enumerate((2, 5))]
    strategy, quote, checked = _bounded_test_strategy(
        tmp_path, monkeypatch, residual="0", positions=tickets,
    )
    with _event_engine(cast(Any, strategy)) as engine:
        values = CryptoPerpetual.to_dict(_source_instrument())
        values.update(size_precision=1, size_increment="0.1", lot_size="0.1")
        engine.add_instrument(CryptoPerpetual.from_dict(values))
        engine.trader.start()
        _seed_native_working_quotes(cast(Any, strategy), exact_route=True,
                                    directions=(SourceDirection.SHORT,), quantity=D("0.2"))
        # Initial SELL4 is close2+close2, but BUY1.6/SELL.2/BUY2.4 later closes3 on id2.
        assert not strategy._source_hedge_is_executable(quote, D(4))
        assert checked[:3] == [D(1), D(2), D(2)]
        assert D(4) in checked  # min(K4, original ticket5), not just its first2 slice.


class _NowClock:
    def timestamp_ns(self) -> int:
        return 2


def test_future_session_fact_immediately_cancels_instead_of_scheduling() -> None:
    harness = _StopHarness()
    harness._session_ts_ns = 1
    harness._hedge_session_open = True
    harness.clock = _NowClock()

    MakerStrategy.update_hedge_session(cast(Any, harness), True, 3)

    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2


class _QuoteCache:
    def __init__(self) -> None:
        source = _source_instrument()
        hedge = _hedge_instrument()
        self._ticks = {
            source.id: _quote(source, "2399", "2400", "5", 1_000_000_000),
            hedge.id: _quote(hedge, "2401", "2402", "5", 1_000_000_000),
        }

    def quote_tick(self, instrument_id: Any) -> Any:
        return self._ticks[instrument_id]


class _LiveQuoteGateHarness:
    _source_quote_refresh_paused: Callable[[], bool] | None = None
    _live_account_reader = None

    def _evaluate_quotes(self) -> None:
        MakerStrategy._evaluate_quotes(cast(Any, self))

    def __init__(self, state_prefix: Path, *, now_ns: int) -> None:
        self._config = _maker_strategy_config(state_prefix)
        source = _source_instrument()
        hedge = _hedge_instrument()
        self.source_tick = _quote(source, "3999", "4000", "5", now_ns - 1)
        self.hedge_tick = _quote(hedge, "3999", "4000", "5", now_ns - 1)
        ticks = {source.id: self.source_tick, hedge.id: self.hedge_tick}
        self.cache = SimpleNamespace(quote_tick=lambda instrument_id: ticks[instrument_id])
        self.clock = SimpleNamespace(timestamp_ns=lambda: now_ns)
        self.ready = False
        self._live_submission_ready = lambda: self.ready
        self._live_costs_from_adapters = True
        self._cost_snapshot_valid = True
        self._hedge_instrument_valid = True
        self._cost_recovery_after_ns = 0
        self._cost_ts_ns = now_ns - 1
        self._session_ts_ns = now_ns - 1
        self._hedge_session_open = True
        self._carry = CarryConfig(
            bitfinex_long=D("-0.0007366"),
            bitfinex_short=D("0.0007366"),
            total_trade_fee=D("0.00065"),
        )
        self._quote_carry = self._carry
        self._fx = FxConfig()
        self.canceled: list[SourceDirection] = []
        self.quote_carries: list[CarryConfig] = []
        self.errors: list[str] = []
        self.log = SimpleNamespace(error=self.errors.append)

    def _required_hedge_instrument(self) -> Any:
        return SimpleNamespace(ts_event=self._session_ts_ns)

    def _inputs_are_fresh(
        self,
        source_tick: Any,
        hedge_tick: Any,
        now_ns: int,
    ) -> bool:
        return MakerStrategy._inputs_are_fresh(
            cast(Any, self),
            source_tick,
            hedge_tick,
            now_ns,
        )

    def _invalidate_cost_snapshot(self, reason: str) -> None:
        MakerStrategy._invalidate_cost_snapshot(cast(Any, self), reason)

    def _freeze_and_cancel_all(self, reason: str) -> None:
        self.canceled.extend((SourceDirection.LONG, SourceDirection.SHORT))

    def _global_obligation_block(self) -> bool:
        return False

    def _try_release_cycle(self) -> bool:
        return False

    def _refresh_direction(self, direction: SourceDirection, *_books: Any) -> None:
        self.quote_carries.append(self._quote_carry)

    def _carry_for_hedge_tick(self, hedge_tick: Any, now_ns: int) -> CarryConfig | None:
        return MakerStrategy._carry_for_hedge_tick(cast(Any, self), hedge_tick, now_ns)

    def _mt5_swap_spec(
        self,
    ) -> tuple[Decimal, Decimal, Decimal, int, tuple[Decimal, ...], str]:
        return (
            D("-12.6"),
            D("-4.6"),
            D("0.01"),
            1,
            (D(0), D(1), D(1), D(3), D(1), D(1), D(0)),
            "Europe/Athens",
        )


def test_live_readiness_false_cancels_both_quotes_without_source_submit(
    tmp_path: Path,
) -> None:
    now_ns = 1_788_439_200_000_000_000
    harness = _LiveQuoteGateHarness(tmp_path / "readiness.state", now_ns=now_ns)

    MakerStrategy.on_quote_tick(cast(Any, harness), harness.source_tick)

    assert harness.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    assert harness.quote_carries == []


def test_live_readiness_exception_logs_type_and_cancels_both_quotes(tmp_path: Path) -> None:
    now_ns = 1_788_439_200_000_000_000
    harness = _LiveQuoteGateHarness(tmp_path / "readiness-error.state", now_ns=now_ns)

    def readiness_failure() -> bool:
        raise ConnectionError("injected readiness failure")

    harness._live_submission_ready = readiness_failure

    MakerStrategy.on_quote_tick(cast(Any, harness), harness.source_tick)

    assert harness.errors == [
        "Maker live submission readiness failed with ConnectionError",
    ]
    assert harness.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    assert harness.quote_carries == []


def test_live_mt5_swap_normalization_feeds_both_maker_quote_sides(tmp_path: Path) -> None:
    # 2026-09-03 12:00 UTC is Thursday in Europe/Athens, multiplier one.
    now_ns = 1_788_439_200_000_000_000
    harness = _LiveQuoteGateHarness(tmp_path / "swap.state", now_ns=now_ns)
    harness.ready = True

    MakerStrategy.on_quote_tick(cast(Any, harness), harness.source_tick)

    assert len(harness.quote_carries) == 2
    assert all(carry.mt5_long_swap == D("-0.0000315") for carry in harness.quote_carries)
    assert all(carry.mt5_short_swap == D("-0.0000115") for carry in harness.quote_carries)


class _QuoteGateHarness:
    _live_account_reader = None

    def _evaluate_quotes(self) -> None:
        MakerStrategy._evaluate_quotes(cast(Any, self))

    def __init__(self, state_prefix: Path, *, blocked: bool, fresh: bool) -> None:
        self._config = _maker_strategy_config(state_prefix)
        self.cache = _QuoteCache()
        self.blocked = blocked
        self.fresh = fresh
        self.clock: Any = SimpleNamespace(timestamp_ns=lambda: 1_000_000_000)
        self._quote_carry = CarryConfig()
        self.canceled: list[SourceDirection] = []
        self.refreshed: list[SourceDirection] = []
        self._source_quote_refresh_paused: Callable[[], bool] | None = None
        self.freshness_now_ns: list[int] = []
        self.carry_now_ns: list[int] = []

    def _global_obligation_block(self) -> bool:
        return self.blocked

    def _try_release_cycle(self) -> bool:
        return False

    def _inputs_are_fresh(
        self,
        source_tick: Any,
        hedge_tick: Any,
        now_ns: int,
    ) -> bool:
        self.freshness_now_ns.append(now_ns)
        return self.fresh

    def _carry_for_hedge_tick(self, hedge_tick: Any, now_ns: int) -> CarryConfig:
        self.carry_now_ns.append(now_ns)
        return CarryConfig()

    def _cancel_working(self, direction: SourceDirection, *, reason: str) -> None:
        assert reason in {"stale, closed, or unresolved", "unresolved Maker obligations"}
        self.canceled.append(direction)

    def _cancel_all_best_effort(self, reason: str) -> None:
        for direction in (SourceDirection.LONG, SourceDirection.SHORT):
            self._cancel_working(direction, reason=reason)

    def _freeze_and_cancel_all(self, reason: str) -> None:
        for direction in (SourceDirection.LONG, SourceDirection.SHORT):
            self._cancel_working(direction, reason=reason)

    def _refresh_direction(
        self,
        direction: SourceDirection,
        source_book: Any,
        hedge_book: Any,
    ) -> None:
        self.refreshed.append(direction)


def test_quote_decision_uses_one_clock_sample_for_freshness_and_swap(
    tmp_path: Path,
) -> None:
    harness = _QuoteGateHarness(tmp_path / "one-clock.state", blocked=False, fresh=True)
    clock_values = (100, 102)
    clock_reads: list[int] = []

    def timestamp_ns() -> int:
        value = clock_values[len(clock_reads)]
        clock_reads.append(value)
        return value

    harness.clock = SimpleNamespace(timestamp_ns=timestamp_ns)
    tick = harness.cache.quote_tick(harness._config.source_instrument_id)

    MakerStrategy.on_quote_tick(cast(Any, harness), tick)

    assert clock_reads == [100]
    assert harness.freshness_now_ns == [100]
    assert harness.carry_now_ns == [100]
    assert harness.refreshed == [SourceDirection.LONG, SourceDirection.SHORT]


def test_completed_hedge_before_sibling_cancel_terminal_cannot_reopen_quotes(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "race.state")
    bid_store = strategy._stores[SourceDirection.LONG]
    ask_store = strategy._stores[SourceDirection.SHORT]
    bid_store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    ask_store.begin_source(
        "O-SIBLING",
        BusinessOrderSide.SELL,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    full, _ = _filled_events(quantity=1)

    strategy.on_order_filled(full)
    intent = bid_store.intents()[0]
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    bid_store.apply_hedge_fill(
        client_order_id=hedge_order_id,
        trade_id="HT-COMPLETE",
        fill_ounces=D(1),
    )
    assert bid_store.net_unhedged_ounces == 0
    assert strategy._global_obligation_block()

    harness = _QuoteGateHarness(tmp_path / "gate.state", blocked=True, fresh=True)
    tick = harness.cache.quote_tick(harness._config.source_instrument_id)
    MakerStrategy.on_quote_tick(cast(Any, harness), tick)

    assert harness.refreshed == []
    assert harness.canceled == [SourceDirection.LONG, SourceDirection.SHORT]


def test_stale_market_gate_cancels_both_sides_without_quote_maintenance(
    tmp_path: Path,
) -> None:
    harness = _QuoteGateHarness(tmp_path / "stale.state", blocked=False, fresh=False)
    tick = harness.cache.quote_tick(harness._config.source_instrument_id)

    MakerStrategy.on_quote_tick(cast(Any, harness), tick)

    assert harness.refreshed == []
    assert harness.canceled == [SourceDirection.LONG, SourceDirection.SHORT]


def test_atomic_fill_does_not_call_the_external_freeze_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_prefix = tmp_path / "freeze-fault.state"
    strategy = RecordingMakerStrategy(state_prefix)
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    failures: list[str] = []

    def fail_freeze(reason: str) -> None:
        failures.append(reason)
        raise OSError("injected freeze write failure")

    monkeypatch.setattr(
        strategy._state_store,
        "freeze_sources",
        fail_freeze,
    )
    full, _ = _filled_events(quantity=1)

    strategy.on_order_filled(full)
    strategy.on_order_filled(full)  # duplicate identity remains idempotent

    persisted = _reload_maker_store(strategy)
    assert not failures and strategy._state_store.cycle_freeze_only
    assert len(persisted.intents()) == 1
    assert persisted.net_unhedged_ounces == D(1)
    assert all(_reload_maker_store(strategy, direction).source_freeze_reason is not None
               for direction in (SourceDirection.LONG, SourceDirection.SHORT))
    assert len(strategy.recorded) == 1
    assert strategy._global_obligation_block()

    restarted = MakerStrategy(_maker_strategy_config(state_prefix))
    assert restarted._global_obligation_block()


def test_fill_wal_failure_rolls_back_memory_and_replay_commits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_prefix = tmp_path / "fill-wal-replay.state"
    strategy = RecordingMakerStrategy(state_prefix)
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    persist = store._persist

    def fail_fill_wal() -> None:
        raise OSError("injected fill WAL failure")

    monkeypatch.setattr(store, "_persist", fail_fill_wal)
    full, _ = _filled_events(quantity=1)

    with pytest.raises(OSError, match="fill WAL"):
        strategy.on_order_filled(full)

    assert strategy._source_hold
    assert strategy.recorded == []
    assert strategy.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    assert store.intents() == ()
    assert _reload_maker_store(strategy).intents() == ()

    monkeypatch.setattr(store, "_persist", persist)
    strategy.on_order_filled(full)
    strategy.on_order_filled(full)

    assert len(_reload_maker_store(strategy).intents()) == 1
    assert len(strategy.recorded) == 1
    assert strategy.canceled == [
        SourceDirection.LONG,
        SourceDirection.SHORT,
        SourceDirection.LONG,
        SourceDirection.SHORT,
    ]


def test_restart_after_unreplayed_fill_wal_failure_holds_active_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_prefix = tmp_path / "fill-wal-restart.state"
    strategy = RecordingMakerStrategy(state_prefix)
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    def fail_fill_wal() -> None:
        raise OSError("injected fill WAL failure")

    monkeypatch.setattr(store, "_persist", fail_fill_wal)
    full, _ = _filled_events(quantity=1)
    with pytest.raises(OSError, match="fill WAL"):
        strategy.on_order_filled(full)

    restarted = MakerStrategy(_maker_strategy_config(state_prefix))
    reason = restarted._stores[SourceDirection.LONG].recover_for_start()

    assert reason is not None
    assert restarted._global_obligation_block()
    assert not restarted._stores[SourceDirection.LONG].can_submit_source()
