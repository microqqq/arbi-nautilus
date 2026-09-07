"""Thin two-sided Maker strategy built directly on Nautilus order custody."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from functools import partial
from typing import cast

from msgspec.structs import replace as replace_config
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.data import FundingRateUpdate, InstrumentStatus, QuoteTick
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderCancelRejected,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderModifyRejected,
    OrderPendingCancel,
    OrderRejected,
    OrderSubmitted,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
)
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.orders import Order
from nautilus_trader.model.position import Position
from nautilus_trader.trading.strategy import Strategy

from py000_nautilus.config import CarryConfig, FxConfig, MakerStrategyConfig
from py000_nautilus.economics import (
    market_inputs_are_fresh,
    normalize_mt5_points_swap,
    round_hedge_ounces,
)
from py000_nautilus.hedge import (
    HedgeCoordinator,
    HedgePlanningError,
    hedge_order_params,
    plan_hedge_delta,
)
from py000_nautilus.maker_economics import (
    maker_carry_bounds,
    maker_hedge_bounds_allow,
    maker_hedge_quantity_bound,
    maker_quote,
    passive_maker_price,
)
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.margin import LiveAccountReader
from py000_nautilus.models import (
    BookTop,
    BusinessOrderSide,
    HedgeIntent,
    MakerAccount,
    MakerQuote,
    ObligationStatus,
    SourceAccount,
    SourceDirection,
)
from py000_nautilus.mt5_v1_protocol import MAX_OBSERVATION_FUTURE_NS
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies._mt5_costs import (
    mt5_instrument_is_fresh,
    mt5_instrument_structure,
    mt5_swap_spec,
    validate_mt5_instrument_update,
)
from py000_nautilus.strategies._source_terminal import (
    SourceTerminalQuery as SourceTerminalQuery,
)
from py000_nautilus.strategies._source_terminal import (
    SourceTerminalResult as SourceTerminalResult,
)
from py000_nautilus.strategies._source_terminal import (
    source_cancel_report_is_exact,
)

_DIRECTIONS = (SourceDirection.LONG, SourceDirection.SHORT)


class MakerStrategy(Strategy):
    """Maintain one bid and one ask GTC source order, hedging actual fills only."""

    def __init__(
        self,
        config: MakerStrategyConfig,
        *,
        live_submission_ready: Callable[[], bool] | None = None,
        hedge_quantity_ready: Callable[[Decimal], bool] | None = None,
        live_costs_from_adapters: bool = False,
        source_terminal_query: SourceTerminalQuery | None = None,
        source_quote_refresh_paused: Callable[[], bool] | None = None,
        state_store: MakerStateStore | None = None,
        source_admission: (
            Callable[[JsonStateStore, BusinessOrderSide, Decimal], bool] | None
        ) = None,
    ) -> None:
        super().__init__(config)
        self._config = config
        self._live_submission_ready = live_submission_ready
        self._restart_gate: Callable[[], bool] | None = None
        self._draining = False
        self._hedge_quantity_ready = hedge_quantity_ready
        self._live_costs_from_adapters = live_costs_from_adapters
        self._source_terminal_query = source_terminal_query
        self._source_quote_refresh_paused = source_quote_refresh_paused
        self._source_terminal_inflight: set[str] = set()
        self._source_terminal_stopped = False
        self._source_terminal_generation = 0
        self._live_account_reader: LiveAccountReader | None = None
        self._account_loop: asyncio.AbstractEventLoop | None = None
        self._account_handle: asyncio.Handle | None = None
        self._account_topics: tuple[str, ...] = ()
        self._account_deadline_ns: int | None = None
        self._account_budget_waiting = False
        carry_route = None
        if config.residual_mode == "bounded-carry":
            source, hedge = config.source_accounts[0], config.hedge_accounts[0]
            carry_route = (source.account_id.value,
                           source.client_id.value if source.client_id is not None else None,
                           hedge.account_id.value,
                           hedge.client_id.value if hedge.client_id is not None else None)
        self._state_store = state_store if state_store is not None else MakerStateStore(
            config.store_path_prefix,
            str(config.source_instrument_id),
            str(config.hedge_instrument_id),
            residual_limit_ounces=config.residual_limit_ounces, carry_route=carry_route,
        )
        self._source_admission = source_admission
        self._stores = self._state_store.stores
        self._hedges = {
            direction: HedgeCoordinator(config.source_instrument_id, self._stores[direction])
            for direction in _DIRECTIONS
        }
        self._working_quotes: dict[str, MakerQuote] = {}
        self._stale_timer_names: dict[SourceDirection, str] = {}
        self._stale_timer_loop: asyncio.AbstractEventLoop | None = None
        self._source_hold = False
        self._source_instrument: Instrument | None = None
        self._hedge_instrument: Instrument | None = None
        self._hedge_instrument_valid = False
        self._carry = config.economics.carry
        self._quote_carry = self._carry
        self._fx = config.economics.fx
        self._cost_ts_ns = config.initial_cost_ts_ns
        self._cost_snapshot_valid = config.initial_cost_ts_ns > 0
        self._cost_recovery_after_ns = 0
        self._hedge_session_open = config.initial_hedge_session_open
        self._session_ts_ns = config.initial_session_ts_ns

    def update_cost_snapshot(
        self,
        carry: CarryConfig,
        fx: FxConfig,
        ts_event_ns: int,
    ) -> bool:
        """Refresh cost freshness, canceling quotes only when their economics change."""
        if type(ts_event_ns) is not int or ts_event_ns <= 0:
            self._invalidate_cost_snapshot("Maker costs are invalid")
            return False
        now_ns = cast(int, self.clock.timestamp_ns())
        if not 0 <= now_ns - ts_event_ns <= self._config.max_cost_age_ns:
            self._invalidate_cost_snapshot("Maker cost observation is stale or future-dated")
            return False
        if ts_event_ns < self._cost_ts_ns:
            self._invalidate_cost_snapshot("Maker costs moved backwards")
            return False
        if ts_event_ns == self._cost_ts_ns:
            if carry != self._carry or fx != self._fx:
                self._invalidate_cost_snapshot("Maker costs conflict at one timestamp")
                return False
            return self._cost_snapshot_valid
        if ts_event_ns <= self._cost_recovery_after_ns:
            return False
        changed = carry != self._carry or fx != self._fx
        self._carry = carry
        self._quote_carry = carry
        self._fx = fx
        self._cost_ts_ns = ts_event_ns
        self._cost_snapshot_valid = True
        self._cost_recovery_after_ns = 0
        if changed:
            self._freeze_and_cancel_all("Maker costs changed", market_input=True)
        return True

    def _invalidate_cost_snapshot(self, reason: str) -> None:
        self._cost_snapshot_valid = False
        self._cost_recovery_after_ns = max(
            self._cost_recovery_after_ns,
            self._cost_ts_ns,
        )
        self._freeze_and_cancel_all(reason, market_input=True)

    def update_hedge_session(self, is_open: bool, ts_event_ns: int) -> None:
        if ts_event_ns >= self._session_ts_ns:
            self._hedge_session_open = is_open
            self._session_ts_ns = ts_event_ns
            if not is_open or ts_event_ns > (
                cast(int, self.clock.timestamp_ns()) + MAX_OBSERVATION_FUTURE_NS
            ):
                self._freeze_and_cancel_all(
                    "hedge session closed or future-dated", market_input=True,
                )
            else:
                self._reschedule_active_timers()

    def bind_restart_gate(self, pending: Callable[[], bool]) -> None:
        """Bind only the ordinary composition's startup HOLD, not general health."""
        self._restart_gate = pending

    def bind_live_account_reader(self, reader: LiveAccountReader) -> None:
        """Bind the composition's current-account view before strategy start."""
        self._live_account_reader = reader

    def _on_account_update(self, _event: object) -> None:
        # A source fill may publish AccountState before its hedge obligation exists.
        loop = self._account_loop
        if self._source_terminal_stopped or loop is None or loop.is_closed():
            return
        if self._account_handle is None:
            self._account_handle = loop.call_soon(
                self._evaluate_account_update, self._source_terminal_generation,
            )

    def _evaluate_account_update(self, generation: int) -> None:
        if self._source_terminal_stopped or generation != self._source_terminal_generation:
            return
        self._account_handle = None
        try:
            tick = self.cache.quote_tick(self._config.source_instrument_id)
            if tick is not None:
                # Preserve existing subclass admission (including canary arming).
                # Reuse the cached event as-is; do not publish or advance its timestamp.
                self.on_quote_tick(tick)
        except Exception as exc:
            self.log.error(f"Maker account evaluation failed with {type(exc).__name__}")

    def on_start(self) -> None:
        self._draining = False
        self._stale_timer_loop = (
            asyncio.get_running_loop() if isinstance(self.clock, LiveClock) else None
        )
        self._source_terminal_stopped = False
        if self._live_account_reader is not None:
            self._account_loop = asyncio.get_running_loop()
            self._account_topics = tuple({
                f"events.account.{self._config.source_accounts[0].account_id}",
                f"events.account.{self._config.hedge_accounts[0].account_id}",
            })
            for topic in self._account_topics:
                self.msgbus.subscribe(topic, self._on_account_update)
        self._source_instrument = self.cache.instrument(self._config.source_instrument_id)
        self._hedge_instrument = self.cache.instrument(self._config.hedge_instrument_id)
        if self._source_instrument is None or self._hedge_instrument is None:
            self.log.error("Maker instruments were not found in the Nautilus cache")
            self.stop()
            return
        if self._live_costs_from_adapters:
            try:
                self._mt5_swap_spec()
                mt5_instrument_structure(self._hedge_instrument)
                if not mt5_instrument_is_fresh(
                    self._hedge_instrument, self.clock.timestamp_ns(), self._config.max_cost_age_ns,
                ):
                    raise ValueError("MT5 instrument observation is not fresh")
                self._hedge_instrument_valid = True
            except (KeyError, TypeError, ValueError) as exc:
                self.log.error(f"MT5 swap specification is unavailable: {exc}")
                self.stop()
                return
        for store in self._stores.values():
            reason = store.recover_for_start()
            if reason is not None:
                self.log.error(reason)
        self._try_release_cycle()
        self.subscribe_quote_ticks(self._config.source_instrument_id)
        self.subscribe_quote_ticks(self._config.hedge_instrument_id)
        if self._live_costs_from_adapters:
            self.subscribe_instrument(self._config.hedge_instrument_id)
            self.subscribe_funding_rates(self._config.source_instrument_id)
        self.subscribe_instrument_status(self._config.hedge_instrument_id)

    def begin_drain(self) -> None:
        """Freeze quote creation/maintenance, not existing fill or hedge processing."""
        self._draining = True
        for name in self._stale_timer_names.values():
            if name in self.clock.timer_names:
                self.clock.cancel_timer(name)
        self._stale_timer_names.clear()

    def continue_drain(self) -> None:
        self._submit_next_pending_hedge()
        self._try_release_cycle()

    def on_stop(self) -> None:
        self._source_terminal_stopped = True
        self._source_terminal_generation += 1
        self._source_terminal_inflight.clear()
        if self._account_handle is not None:
            self._account_handle.cancel()
            self._account_handle = None
        for topic in self._account_topics:
            self.msgbus.unsubscribe(topic, self._on_account_update)
        self._account_topics = ()
        self._account_loop = None
        if _restart_blocked(self) or self._draining:
            return
        for direction in _DIRECTIONS:
            self._cancel_working(direction, reason="strategy stop")
        for route, residual in self._state_store.residuals().items():
            self.log.warning(
                f"Maker stopped with signed residual {residual} ounces on route {route}",
            )

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if tick.instrument_id not in {
            self._config.source_instrument_id,
            self._config.hedge_instrument_id,
        }:
            return
        if tick.instrument_id == self._config.hedge_instrument_id:
            self._submit_next_pending_hedge()
        self._evaluate_quotes()

    def _evaluate_quotes(self) -> None:
        if _restart_blocked(self) or self._draining:
            return
        source_tick = self.cache.quote_tick(self._config.source_instrument_id)
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if source_tick is None or hedge_tick is None:
            return
        now_ns = cast(int, self.clock.timestamp_ns())
        inputs_fresh = self._inputs_are_fresh(source_tick, hedge_tick, now_ns)
        if inputs_fresh:
            quote_carry = self._carry_for_hedge_tick(hedge_tick, now_ns)
            if quote_carry is None:
                inputs_fresh = False
            else:
                self._quote_carry = quote_carry
        if inputs_fresh:
            self._try_release_cycle(inputs_fresh=True)
        if not inputs_fresh:
            self._freeze_and_cancel_all("stale, closed, or unresolved", market_input=True)
            return
        if self._global_obligation_block():
            self._source_hold = True
            self._cancel_all_best_effort("unresolved Maker obligations")
            return
        # A healthy active-list read pauses quoting, not protection of working
        # orders. Real health failures and obligations have already run above.
        if (
            self._live_account_reader is None
            and self._source_quote_refresh_paused is not None
            and self._source_quote_refresh_paused()
        ):
            return
        source_book = _book_top(source_tick)
        hedge_book = _book_top(hedge_tick)
        directions: tuple[SourceDirection, ...] = _DIRECTIONS
        if self._live_account_reader is not None:
            self._account_budget_waiting = False
            directions = tuple(sorted(
                _DIRECTIONS,
                key=lambda direction: self._stores[direction].active_source_order_id is not None,
            ))
        for direction in directions:
            self._refresh_direction(direction, source_book, hedge_book)

    def on_instrument_status(self, status: InstrumentStatus) -> None:
        if status.instrument_id != self._config.hedge_instrument_id:
            return
        self.update_hedge_session(status.is_trading is True, status.ts_event)

    def on_instrument(self, instrument: Instrument) -> None:
        """Keep a validated MT5 reference; swap never refreshes source funding."""
        if not self._live_costs_from_adapters or instrument.id != self._config.hedge_instrument_id:
            return
        previous = self._required_hedge_instrument()
        try:
            changed = validate_mt5_instrument_update(
                previous, instrument, self.clock.timestamp_ns(), self._config.max_cost_age_ns,
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._hedge_instrument_valid = False
            self.log.error(f"MT5 instrument update rejected: {exc}")
            self._freeze_and_cancel_all("invalid MT5 cost observation", market_input=True)
            return
        if instrument.ts_event == previous.ts_event and not self._hedge_instrument_valid:
            return
        self._hedge_instrument = instrument
        self._hedge_instrument_valid = True
        if changed:
            self._freeze_and_cancel_all("MT5 swap costs changed", market_input=True)

    def on_funding_rate(self, funding_rate: FundingRateUpdate) -> None:
        """Install the source venue's signed next-period funding observation."""
        if (
            not self._live_costs_from_adapters
            or funding_rate.instrument_id != self._config.source_instrument_id
        ):
            return
        ts_event_ns = cast(int, funding_rate.ts_event)
        if ts_event_ns > cast(int, self.clock.timestamp_ns()):
            self._invalidate_cost_snapshot("Bitfinex funding observation is future-dated")
            self.log.error("Bitfinex funding observation is future-dated")
            return
        rate = Decimal(str(funding_rate.rate))
        if not rate.is_finite() or abs(rate) > 1:
            self._invalidate_cost_snapshot("Bitfinex funding observation is invalid")
            self.log.error("Bitfinex funding observation is invalid")
            return
        static = self._config.economics.carry
        self.update_cost_snapshot(
            CarryConfig(
                bitfinex_long=rate,
                bitfinex_short=-rate,
                mt5_long_swap=static.mt5_long_swap,
                mt5_short_swap=static.mt5_short_swap,
                total_trade_fee=static.total_trade_fee,
            ),
            self._config.economics.fx,
            ts_event_ns,
        )

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        self._update_order_status(event.client_order_id.value, "SUBMITTED")

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self._update_order_status(event.client_order_id.value, "ACCEPTED")

    def on_order_denied(self, event: OrderDenied) -> None:
        self._finish_or_reject(event.client_order_id.value, "DENIED")

    def on_order_rejected(self, event: OrderRejected) -> None:
        if _restart_blocked(self):
            return
        if event.reason.strip().upper() == "UNKNOWN":
            client_order_id = event.client_order_id.value
            direction = self._direction_for_source_order(client_order_id)
            if direction is not None:
                self._mark_source_unknown(
                    client_order_id,
                    "Nautilus reported an unknown Maker submission outcome",
                )
            else:
                for store in self._stores.values():
                    store.update_hedge_status(client_order_id, ObligationStatus.UNKNOWN)
                self._freeze_and_cancel_all("unknown Maker hedge submission outcome")
            return
        self._finish_or_reject(event.client_order_id.value, "REJECTED")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        client_order_id = event.client_order_id.value
        direction = self._direction_for_source_order(client_order_id)
        self._finish_or_reject(client_order_id, "CANCELED")
        if direction is not None:
            self._request_source_terminal_query(direction, event)

    def on_order_expired(self, event: OrderExpired) -> None:
        client_order_id = event.client_order_id.value
        direction = self._direction_for_source_order(client_order_id)
        self._finish_or_reject(client_order_id, "EXPIRED")
        if direction is not None:
            self._request_source_terminal_query(direction, event)

    def on_order_modify_rejected(self, event: OrderModifyRejected) -> None:
        if self._source_action_is_obsolete(event):
            return
        self._mark_source_unknown(event.client_order_id.value, "maker modify rejected")

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        if self._source_action_is_obsolete(event):
            return  # Preserve terminal facts, pending reconciliation, and every existing HOLD.
        self._mark_source_unknown(event.client_order_id.value, "maker cancel rejected")

    def _source_action_is_obsolete(
        self, event: OrderCancelRejected | OrderModifyRejected,
    ) -> bool:
        """Preserve exact terminal facts or a pending protective cancel."""
        direction = self._direction_for_source_order(event.client_order_id.value)
        if direction is None or event.venue_order_id is None:
            return False
        record = self._stores[direction].source_order(event.client_order_id.value)
        order = self.cache.order(event.client_order_id)
        terminal = {"CANCELED": OrderStatus.CANCELED, "EXPIRED": OrderStatus.EXPIRED,
                    "FILLED": OrderStatus.FILLED}
        if record is None or order is None:
            return False
        terminal_matches = order.is_closed and terminal.get(record.status) == order.status
        protective_cancel = (
            isinstance(event, OrderModifyRejected)
            and (
                (record.status == "PARTIALLY_FILLED"
                 and 0 < record.filled_ounces < record.quantity_ounces
                 and self._stores[direction].source_freeze_reason is not None)
                or (record.status == "ACCEPTED" and record.filled_ounces == 0
                    and order.status is OrderStatus.PENDING_CANCEL)
            )
            and self._stores[direction].active_source_order_id == event.client_order_id.value
            and self._source_hold
            and order.status in {OrderStatus.PENDING_CANCEL, OrderStatus.PARTIALLY_FILLED}
            and _cancel_is_pending(order)
        )
        if not (terminal_matches or protective_cancel):
            return False
        source_route = next((route for route in self._config.source_accounts
                             if route.account_id.value == record.source_account_id), None)
        hedge_route = next((route for route in self._config.hedge_accounts
                            if route.account_id.value == record.hedge_account_id), None)
        if source_route is None or hedge_route is None:
            return False
        side = OrderSide.BUY if record.side is BusinessOrderSide.BUY else OrderSide.SELL
        fills = [item for item in order.events if isinstance(item, OrderFilled)]
        return (
            event.trader_id == order.trader_id == self.trader_id
            and event.strategy_id == order.strategy_id == self.id
            and event.instrument_id == order.instrument_id == self._config.source_instrument_id
            and event.account_id == order.account_id == source_route.account_id
            and event.client_order_id == order.client_order_id
            and event.client_order_id.value == record.client_order_id
            and event.venue_order_id == order.venue_order_id
            and record.source_client_id == (
                source_route.client_id.value if source_route.client_id is not None else None
            )
            and record.hedge_client_id == (
                hedge_route.client_id.value if hedge_route.client_id is not None else None
            )
            and order.side == side
            and order.quantity.as_decimal() == record.quantity_ounces
            and order.filled_qty.as_decimal() == record.filled_ounces
            and sum((fill.last_qty.as_decimal() for fill in fills), Decimal(0))
            == record.filled_ounces
            and all(
                fill.account_id == event.account_id and fill.instrument_id == event.instrument_id
                and fill.client_order_id == event.client_order_id
                and fill.venue_order_id == event.venue_order_id and fill.order_side == side
                and self._hedges[direction].has_seen_source_fill(fill)
                for fill in fills
            )
        )

    def on_order_filled(self, event: OrderFilled) -> None:
        if _restart_blocked(self):
            return
        if event.instrument_id == self._config.source_instrument_id:
            direction = self._direction_for_source_order(event.client_order_id.value)
            if direction is None:
                return
            if self._hedges[direction].has_seen_source_fill(event):
                return
            freeze_reason = (
                f"Maker fill {event.client_order_id.value}/{event.trade_id.value} "
                "requires authoritative two-sided reconciliation"
            )
            # Current-process HOLD precedes I/O. The first durable act records
            # the actual fill/intent and both directions' freezes together.
            self._source_hold = True
            try:
                intent = self._hedges[direction].on_source_filled(event)
            except Exception:
                self._freeze_all_best_effort(freeze_reason)
                self._cancel_all_best_effort("source fill WAL failed")
                raise
            try:
                if intent is not None:
                    self._submit_next_pending_hedge()
            finally:
                self._cancel_all_best_effort("source fill froze Maker quoting")
            if self._stores[direction].active_source_order_id is None:
                self._working_quotes.pop(event.client_order_id.value, None)
            return
        if event.instrument_id == self._config.hedge_instrument_id:
            applied = any(
                coordinator.on_hedge_filled(event)
                for coordinator in self._hedges.values()
            )
            if applied:
                self._submit_next_pending_hedge()
            self._try_release_cycle()

    def _refresh_direction(
        self,
        direction: SourceDirection,
        source_book: BookTop,
        hedge_book: BookTop,
    ) -> None:
        store = self._stores[direction]
        active_id = store.active_source_order_id
        if active_id is None:
            if store.can_submit_source():
                quote = self._new_quote(direction, source_book, hedge_book)
                if quote is not None:
                    self._submit_source(quote)
            return
        order = self.cache.order(ClientOrderId(active_id))
        if order is None:
            store.mark_source_unknown(active_id, "Maker cache lost the active source order")
            return
        if order.is_closed:
            return
        bound = self._working_quotes.get(active_id)
        if bound is None:
            store.mark_source_unknown(active_id, "Maker account pair missing for active order")
            self._cancel_working(direction, expected_order_id=active_id, reason="route missing")
            return
        if (self._live_account_reader is not None or self._config.residual_mode == "bounded-carry"
        ) and not self._live_working_order_is_exact(
            direction, order, bound,
        ):
            store.mark_source_unknown(active_id, "Maker live order/route evidence differs")
            self._cancel_working(direction, expected_order_id=active_id, reason="route differs")
            return
        desired = self._bound_quote(bound, source_book, hedge_book)
        if desired is None:
            self._cancel_working(direction, expected_order_id=active_id, reason="risk changed")
            return
        if not (
            self._live_account_reader is not None
            and (
                self._account_budget_waiting or self._quote_refresh_paused()
                or order.status is not OrderStatus.ACCEPTED
            )
        ):
            self._requote(order, desired)
            self._working_quotes[active_id] = desired
        self._schedule_stale_timer(direction, active_id)

    def _quote_refresh_paused(self) -> bool:
        return self._source_quote_refresh_paused is not None and self._source_quote_refresh_paused()

    def _live_working_order_is_exact(
        self, direction: SourceDirection, order: Order, bound: MakerQuote,
    ) -> bool:
        record = self._stores[direction].source_order(order.client_order_id.value)
        source_route = self._config.source_accounts[0]
        hedge_route = self._config.hedge_accounts[0]
        return bool(
            record is not None
            and order.strategy_id == self.id
            and self.cache.client_id(order.client_order_id) == source_route.client_id
            and bound.direction is direction
            and bound.source_account.account_id == source_route.account_id
            and bound.source_account.client_id == source_route.client_id
            and bound.hedge_account.account_id == hedge_route.account_id
            and bound.hedge_account.client_id == hedge_route.client_id
            and record.source_account_id == bound.source_account.account_id.value
            and record.hedge_account_id == bound.hedge_account.account_id.value
            and record.source_client_id == (
                source_route.client_id.value if source_route.client_id is not None else None
            )
            and record.hedge_client_id == (
                hedge_route.client_id.value if hedge_route.client_id is not None else None
            )
            and (
                order.account_id == bound.source_account.account_id
                or (order.status is OrderStatus.INITIALIZED and order.account_id is None)
            )
            and order.instrument_id == self._config.source_instrument_id
            and order.side == (
                OrderSide.BUY if direction is SourceDirection.LONG else OrderSide.SELL
            )
            and record.side == (
                BusinessOrderSide.BUY
                if direction is SourceDirection.LONG else BusinessOrderSide.SELL
            )
            and record.quantity_ounces == order.quantity.as_decimal()
            and record.filled_ounces == order.filled_qty.as_decimal() == 0
            and order.leaves_qty.as_decimal() == bound.quantity_ounces
        )

    def _live_quote_accounts(
        self, source_book: BookTop, hedge_book: BookTop, *, new_source: bool,
    ) -> tuple[SourceAccount, MakerAccount] | None:
        assert self._live_account_reader is not None
        source_tick = self.cache.quote_tick(self._config.source_instrument_id)
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if source_tick is None or hedge_tick is None:
            return None
        view = self._live_account_reader(
            source_book, source_tick.ts_event, hedge_book, hedge_tick.ts_event, new_source,
        )
        if view is None:
            self._account_deadline_ns = None
            self._account_budget_waiting |= new_source
            return None
        source, hedge, self._account_deadline_ns, budget_ready = view
        if new_source and not budget_ready:
            self._account_budget_waiting = True
            return None
        route = self._config.hedge_accounts[0]
        return source, MakerAccount(
            route.account_id, route.client_id, hedge.position_ounces,
            hedge.max_long_ounces, hedge.max_short_ounces,
        )

    def _new_quote(
        self,
        direction: SourceDirection,
        source_book: BookTop,
        hedge_book: BookTop,
    ) -> MakerQuote | None:
        side = self._config.economics.bid if direction is SourceDirection.LONG else (
            self._config.economics.ask
        )
        if side.open_quantity_ounces <= 0:
            return None
        sources: tuple[SourceAccount, ...]
        hedges: tuple[MakerAccount, ...]
        if self._live_account_reader is not None:
            view = self._live_quote_accounts(source_book, hedge_book, new_source=True)
            if view is None or self._quote_refresh_paused() or self._global_obligation_block():
                return None
            for store in self._stores.values():
                active = store.active_source_order_id
                if active is not None:
                    order = self.cache.order(ClientOrderId(active))
                    if order is None or order.status not in {OrderStatus.ACCEPTED}:
                        self._account_budget_waiting = True
                        return None
            sources, hedges = (view[0],), (view[1],)
        else:
            sources, hedges = self._source_accounts(), self._hedge_accounts()
        economics = self._config.economics
        hedge_quantity = None
        if self._config.residual_mode == "bounded-carry":
            quantity = self._required_source_instrument().make_qty(
                side.open_quantity_ounces,
            ).as_decimal()
            economics = replace_config(economics, **{
                "bid" if direction is SourceDirection.LONG else "ask":
                    replace_config(side, open_quantity_ounces=quantity),
            })
            hedge_quantity = self._carry_hedge_delta_bound(direction, quantity)
        quote = maker_quote(
            direction,
            hedge_book,
            sources,
            hedges,
            economics,
            carry=self._quote_carry,
            fx=self._fx,
            hedge_quantity_ounces=hedge_quantity,
        )
        return self._passive_quote(quote, source_book)

    def _bound_quote(
        self,
        bound: MakerQuote,
        source_book: BookTop,
        hedge_book: BookTop,
    ) -> MakerQuote | None:
        economics = self._config.economics
        if self._live_account_reader is not None:
            view = self._live_quote_accounts(source_book, hedge_book, new_source=False)
            if view is None:
                return None
            source, hedge = view
            if (source.account_id != bound.source_account.account_id
                    or hedge.account_id != bound.hedge_account.account_id):
                return None
            # This order already owns its source funds. Recheck route/net risk,
            # not the entire old quantity against the remaining new-order budget.
            route = self._config.source_accounts[0]
            maximum = self._config.economics.risk.source_max_abs
            source = replace(
                source,
                max_long_ounces=min(
                    route.max_long_ounces, max(maximum - source.position_ounces, Decimal(0)),
                ),
                max_short_ounces=min(
                    route.max_short_ounces, max(maximum + source.position_ounces, Decimal(0)),
                ),
            )
            side_name = "bid" if bound.direction is SourceDirection.LONG else "ask"
            side = economics.bid if bound.direction is SourceDirection.LONG else economics.ask
            economics = replace_config(economics, **{
                side_name: replace_config(side, open_quantity_ounces=bound.quantity_ounces),
            })
        else:
            source = self._source_account(bound.source_account.account_id)
            hedge = self._hedge_account(bound.hedge_account.account_id)
        hedge_quantity = None
        if self._config.residual_mode == "bounded-carry":
            side_name = "bid" if bound.direction is SourceDirection.LONG else "ask"
            side = economics.bid if bound.direction is SourceDirection.LONG else economics.ask
            economics = replace_config(economics, **{
                side_name: replace_config(side, open_quantity_ounces=bound.quantity_ounces),
            })
            hedge_quantity = self._carry_hedge_delta_bound(bound.direction, bound.quantity_ounces)
        quote = maker_quote(
            bound.direction,
            hedge_book,
            (source,),
            (hedge,),
            economics,
            carry=self._quote_carry,
            fx=self._fx,
            hedge_quantity_ounces=hedge_quantity,
        )
        if (quote is not None and self._config.residual_mode == "bounded-carry"
                and not self._carry_source_is_executable(
                    quote, quote.quantity_ounces,
                    self._stores[bound.direction].active_source_order_id,
                )):
            return None
        return self._passive_quote(quote, source_book)

    def _passive_quote(
        self,
        quote: MakerQuote | None,
        source_book: BookTop,
    ) -> MakerQuote | None:
        if quote is None:
            return None
        price = passive_maker_price(
            quote.direction,
            quote.source_price_usdt,
            source_book,
            Decimal(str(self._required_source_instrument().price_increment)),
            self._config.cross_clamp_ticks,
        )
        return replace(quote, source_price_usdt=price)

    def _submit_source(self, quote: MakerQuote) -> None:
        if _restart_blocked(self) or self._draining:
            return
        instrument = self._required_source_instrument()
        side = OrderSide.BUY if quote.direction is SourceDirection.LONG else OrderSide.SELL
        source_quantity = instrument.make_qty(quote.quantity_ounces)
        business_side = BusinessOrderSide.BUY if side is OrderSide.BUY else BusinessOrderSide.SELL
        if self._source_admission is not None and not self._source_admission(
            self._stores[quote.direction], business_side, source_quantity.as_decimal(),
        ):
            return
        if not self._source_hedge_is_executable(quote, Decimal(str(source_quantity))):
            return
        order = self.order_factory.limit(
            instrument_id=self._config.source_instrument_id,
            order_side=side,
            quantity=source_quantity,
            price=instrument.make_price(quote.source_price_usdt),
            time_in_force=TimeInForce.GTC,
            post_only=True,
            tags=["py000=maker-source", f"direction={quote.direction.value}"],
        )
        store = self._stores[quote.direction]
        store.begin_source(
            order.client_order_id.value,
            BusinessOrderSide.BUY if side is OrderSide.BUY else BusinessOrderSide.SELL,
            Decimal(str(order.quantity)),
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
        self._working_quotes[order.client_order_id.value] = quote
        try:
            self.submit_order(
                order,
                client_id=quote.source_account.client_id,
                params={"leverage": quote.leverage},
            )
            self._schedule_stale_timer(quote.direction, order.client_order_id.value)
        except Exception as exc:
            store.mark_source_unknown(
                order.client_order_id.value,
                f"Maker source submission raised {type(exc).__name__}",
            )
            raise

    def _source_hedge_is_executable(
        self,
        quote: MakerQuote,
        source_quantity_ounces: Decimal,
    ) -> bool:
        if self._config.residual_mode == "bounded-carry":
            return self._carry_source_is_executable(quote, source_quantity_ounces, None)
        hedge_side = (
            BusinessOrderSide.SELL
            if quote.direction is SourceDirection.LONG
            else BusinessOrderSide.BUY
        )
        max_hedge_ounces = Decimal(abs(round_hedge_ounces(source_quantity_ounces)))
        if max_hedge_ounces == 0:
            return True
        try:
            plan = plan_hedge_delta(
                self._hedge_positions(quote.hedge_account.account_id),
                hedge_side,
                max_hedge_ounces,
            )
        except (HedgePlanningError, TypeError, ValueError) as exc:
            self.log.error(f"Maker source admission blocked by MT5 position shape: {exc}")
            return False
        quantity_ready = self._hedge_quantity_ready
        if quantity_ready is None:
            return True
        for leg in plan:
            try:
                admitted = quantity_ready(leg.quantity_ounces)
            except Exception as exc:
                self.log.error(
                    "Maker source admission blocked because MT5 quantity preflight raised "
                    f"{type(exc).__name__}"
                )
                return False
            if not admitted:
                self.log.error(
                    "Maker source admission blocked because MT5 cannot execute hedge leg "
                    f"quantity {leg.quantity_ounces} ounces"
                )
                return False
        return True

    def _carry_hedge_delta_bound(self, direction: SourceDirection, quantity: Decimal) -> Decimal:
        _, buy, sell = maker_carry_bounds(
            self._state_store.carry_residual_ounces,
            quantity if direction is SourceDirection.LONG else Decimal(0),
            quantity if direction is SourceDirection.SHORT else Decimal(0),
        )
        return sell if direction is SourceDirection.LONG else buy

    def _carry_working_leaves(
        self, quote: MakerQuote, quantity: Decimal, exclude_source_id: str | None,
    ) -> tuple[Decimal, Decimal] | None:
        source, hedge = self._config.source_accounts[0], self._config.hedge_accounts[0]
        if (quote.source_account.account_id != source.account_id
                or quote.source_account.client_id != source.client_id
                or quote.hedge_account.account_id != hedge.account_id
                or quote.hedge_account.client_id != hedge.client_id
                or (exclude_source_id is not None and exclude_source_id
                    != self._stores[quote.direction].active_source_order_id)):
            return None
        amounts = {SourceDirection.LONG: Decimal(0), SourceDirection.SHORT: Decimal(0)}
        amounts[quote.direction] = quantity
        for direction, store in self._stores.items():
            active_id = store.active_source_order_id
            if active_id is None:
                continue
            order = self.cache.order(ClientOrderId(active_id))
            bound = self._working_quotes.get(active_id)
            if (order is None or order.is_closed or bound is None
                    or not self._live_working_order_is_exact(direction, order, bound)
                    or (exclude_source_id is None and order.status is not OrderStatus.ACCEPTED)):
                return None
            if active_id == exclude_source_id:
                if quantity != order.leaves_qty.as_decimal():
                    return None
                continue
            amounts[direction] += order.leaves_qty.as_decimal()
        return amounts[SourceDirection.LONG], amounts[SourceDirection.SHORT]

    def _carry_source_is_executable(
        self, quote: MakerQuote, quantity: Decimal, exclude_source_id: str | None,
    ) -> bool:
        if self._global_obligation_block():
            return False
        leaves = self._carry_working_leaves(quote, quantity, exclude_source_id)
        if leaves is None:
            return False
        residual = self._state_store.carry_residual_ounces
        exposure, hedge_buy, hedge_sell = maker_carry_bounds(residual, *leaves)
        budget = self._config.max_unhedged_ounces
        if budget is None or exposure > budget or not maker_hedge_bounds_allow(
            quote.hedge_account, self._config.economics.risk, hedge_buy, hedge_sell,
        ):
            return False
        positions = self._hedge_positions(quote.hedge_account.account_id)
        quantity_ready = self._hedge_quantity_ready
        try:
            for direction, amount, cumulative in (
                (SourceDirection.LONG, leaves[0], hedge_sell),
                (SourceDirection.SHORT, leaves[1], hedge_buy),
            ):
                if amount == 0:
                    continue
                largest = maker_hedge_quantity_bound(
                    residual, amount, direction, bool(leaves[0] and leaves[1]),
                )
                if largest == 0:
                    continue
                side = (BusinessOrderSide.SELL if direction is SourceDirection.LONG
                        else BusinessOrderSide.BUY)
                plan = plan_hedge_delta(positions, side, largest)
                quantities = [Decimal(1), *(leg.quantity_ounces for leg in plan)]
                # Earlier fills can reduce a ticket before a later larger intent.
                quantities.extend(min(largest, position.quantity.as_decimal())
                                  for position in positions
                                  if position.is_long == (side is BusinessOrderSide.SELL))
                signed_net = quote.hedge_account.position_ounces
                future_open = min(largest, max(Decimal(0), cumulative + (
                    -signed_net if side is BusinessOrderSide.SELL else signed_net
                )))
                if future_open > 0:
                    quantities.append(future_open)
                if quantity_ready is not None:
                    try:
                        if any(not quantity_ready(q) for q in quantities):
                            return False
                    except Exception as exc:
                        self.log.error(
                            f"Maker carry quantity preflight raised {type(exc).__name__}",
                        )
                        return False
        except (HedgePlanningError, TypeError, ValueError, ArithmeticError) as exc:
            self.log.error(f"Maker carry hedge preflight failed: {exc}")
            return False
        return True

    def _requote(self, order: Order, desired: MakerQuote) -> None:
        if _restart_blocked(self) or self._draining:
            return
        if cast(bool, order.is_pending_update) or cast(bool, order.is_pending_cancel):
            return
        instrument = self._required_source_instrument()
        current_price = Decimal(str(order.price))
        next_price = instrument.make_price(desired.source_price_usdt)
        threshold = abs(
            self._config.economics.bid.delta
            if desired.direction is SourceDirection.LONG
            else self._config.economics.ask.delta
        )
        if not _requote_required(current_price, Decimal(str(next_price)), threshold):
            return
        quantity = (
            instrument.make_qty(desired.quantity_ounces) if self._config.fixed_amount else None
        )
        self.modify_order(
            order,
            quantity=quantity,
            price=next_price,
            client_id=desired.source_account.client_id,
            params={"leverage": desired.leverage},
        )

    def _submit_next_pending_hedge(self) -> None:
        """Run one MT5 leg globally across both Maker directions."""
        if _restart_blocked(self):
            return
        queued = [
            (direction, intent)
            for direction in _DIRECTIONS
            for intent in self._stores[direction].intents()
        ]
        failed_statuses = {
            ObligationStatus.BLOCKED,
            ObligationStatus.REJECTED,
            ObligationStatus.UNKNOWN,
        }
        if any(intent.status in failed_statuses for _, intent in queued):
            return
        active_statuses = {
            ObligationStatus.SUBMITTING,
            ObligationStatus.SUBMITTED,
            ObligationStatus.ACCEPTED,
        }
        if any(intent.status in active_statuses for _, intent in queued):
            return
        pending = self._state_store.next_pending_hedge()
        if pending is None:
            return
        direction, intent = pending
        route = self._durable_hedge_route(direction, intent.source_client_order_id)
        if route is None:
            reason = "durable Maker hedge route is missing or differs from configuration"
            self._stores[direction].block_hedge_intent(intent.intent_id, reason)
            self.log.error(f"Maker hedge route blocked for {intent.intent_id}: {reason}")
            return
        hedge_account_id, hedge_client_id = route
        self._submit_hedge(
            direction,
            hedge_account_id,
            hedge_client_id,
            intent,
        )

    def _submit_hedge(
        self,
        direction: SourceDirection,
        hedge_account_id: AccountId,
        hedge_client_id: ClientId | None,
        intent: HedgeIntent,
    ) -> None:
        if (_restart_blocked(self)
                or not self._stores[direction].hedge_dispatch_ready(intent.intent_id)):
            return
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if hedge_tick is None or not self._quote_is_fresh(hedge_tick):
            self.log.error(f"Maker hedge quote unavailable for {intent.intent_id}")
            return
        instrument = self._required_hedge_instrument()
        coordinator = self._hedges[direction]
        try:
            leg = coordinator.next_hedge_leg(
                intent.intent_id,
                self._hedge_positions(hedge_account_id),
            )
        except HedgePlanningError as exc:
            self.log.error(f"Maker hedge planning blocked for {intent.intent_id}: {exc}")
            return
        side = OrderSide.BUY if leg.side is BusinessOrderSide.BUY else OrderSide.SELL
        position_id = PositionId(cast(str, leg.position_id)) if leg.is_close else None
        order = self.order_factory.market(
            instrument_id=self._config.hedge_instrument_id,
            order_side=side,
            quantity=instrument.make_qty(leg.quantity_ounces),
            time_in_force=TimeInForce.FOK,
            reduce_only=leg.is_close,
            tags=[
                f"py000={intent.intent_id}",
                f"source_trade={intent.source_trade_id}",
                f"hedge_account={hedge_account_id.value}",
            ],
        )
        store = self._stores[direction]
        coordinator.bind_hedge_leg(intent.intent_id, order.client_order_id.value)
        try:
            self.submit_order(
                order,
                position_id=position_id,
                client_id=hedge_client_id,
                params=hedge_order_params(leg),
            )
        except Exception as exc:
            store.update_hedge_status(order.client_order_id.value, ObligationStatus.UNKNOWN)
            self.log.error(f"Maker hedge submission raised {type(exc).__name__}")
            raise

    def _hedge_positions(self, account_id: AccountId) -> list[Position]:
        return cast(
            list[Position],
            self.cache.positions_open(
                instrument_id=self._config.hedge_instrument_id,
                account_id=account_id,
            ),
        )

    def _cancel_working(
        self,
        direction: SourceDirection,
        *,
        expected_order_id: str | None = None,
        reason: str,
    ) -> None:
        if _restart_blocked(self):
            return
        store = self._stores[direction]
        active_id = store.active_source_order_id
        if active_id is None or (expected_order_id is not None and active_id != expected_order_id):
            return
        order = self.cache.order(ClientOrderId(active_id))
        if order is None:
            store.mark_source_unknown(active_id, f"{reason}: active Maker order missing from cache")
            return
        if order.is_closed or order.is_pending_cancel:
            return
        if _cancel_is_pending(order):
            return
        try:
            self.cancel_order(order)
        except Exception as exc:
            store.mark_source_unknown(
                active_id,
                f"{reason}: Maker cancel raised {type(exc).__name__}",
            )

    def _schedule_stale_timer(self, direction: SourceDirection, order_id: str) -> None:
        if self._draining:
            return
        previous = self._stale_timer_names.pop(direction, None)
        if previous is not None and previous in self.clock.timer_names:
            self.clock.cancel_timer(previous)
        source_tick = self.cache.quote_tick(self._config.source_instrument_id)
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if source_tick is None or hedge_tick is None:
            return
        name = _maker_timer_name(direction, order_id)
        deadline = min(
            source_tick.ts_event + self._config.max_quote_age_ns,
            hedge_tick.ts_event + self._config.max_quote_age_ns,
            self._cost_ts_ns + self._config.max_cost_age_ns,
            self._session_ts_ns + self._config.max_session_age_ns,
        ) + 1
        if self._live_costs_from_adapters:
            deadline = min(
                deadline,
                self._required_hedge_instrument().ts_event + self._config.max_cost_age_ns + 1,
            )
        if self._live_account_reader is not None:
            if self._account_deadline_ns is None:
                return
            deadline = min(deadline, self._account_deadline_ns)
            if deadline <= self.clock.timestamp_ns():
                self._freeze_and_cancel_all("account deadline", market_input=True)
                return
        callback: Callable[[TimeEvent], None] = self._on_stale_timer
        if isinstance(self.clock, LiveClock):
            callback = partial(
                self._dispatch_stale_timer, generation=self._source_terminal_generation,
            )
        self.clock.set_time_alert_ns(
            name=name,
            alert_time_ns=deadline,
            callback=callback,
        )
        self._stale_timer_names[direction] = name

    def _dispatch_stale_timer(self, event: TimeEvent, generation: int) -> None:
        # LiveClock uses a Rust thread; strategy/cache/store mutations belong to the loop.
        loop = self._stale_timer_loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._deliver_stale_timer, event, generation)
        except RuntimeError:
            if not loop.is_closed():
                raise

    def _deliver_stale_timer(self, event: TimeEvent, generation: int) -> None:
        if self._source_terminal_stopped or generation != self._source_terminal_generation:
            return
        self._on_stale_timer(event)

    def _on_stale_timer(self, event: TimeEvent) -> None:
        if _restart_blocked(self) or self._draining:
            return
        target = _maker_timer_target(
            cast(str, event.name),
            self._stores[SourceDirection.LONG].active_source_order_id,
            self._stores[SourceDirection.SHORT].active_source_order_id,
        )
        if target is None:
            return
        direction, order_id = target
        if self._stale_timer_names.get(direction) == event.name:
            self._stale_timer_names.pop(direction, None)
        source_tick = self.cache.quote_tick(self._config.source_instrument_id)
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        now_ns = cast(int, self.clock.timestamp_ns())
        if (
            source_tick is not None
            and hedge_tick is not None
            and self._inputs_are_fresh(source_tick, hedge_tick, now_ns)
            and (
                self._live_account_reader is None
                or self._live_quote_accounts(
                    _book_top(source_tick), _book_top(hedge_tick), new_source=False,
                ) is not None
            )
        ):
            self._schedule_stale_timer(direction, order_id)
        else:
            self._freeze_and_cancel_all("stale timer", market_input=True)

    def _reschedule_active_timers(self) -> None:
        if self._source_instrument is None:
            return
        for direction, store in self._stores.items():
            if store.active_source_order_id is not None:
                self._schedule_stale_timer(direction, store.active_source_order_id)

    def _inputs_are_fresh(
        self,
        source_tick: QuoteTick,
        hedge_tick: QuoteTick,
        now_ns: int,
    ) -> bool:
        if self._live_costs_from_adapters and (
            not self._hedge_instrument_valid or not mt5_instrument_is_fresh(
                self._required_hedge_instrument(), now_ns, self._config.max_cost_age_ns,
            )
        ):
            return False
        if self._live_submission_ready is not None:
            try:
                if not self._live_submission_ready():
                    return False
            except Exception as exc:
                self.log.error(
                    "Maker live submission readiness failed with "
                    f"{type(exc).__name__}"
                )
                return False
        if not self._cost_snapshot_valid:
            return False
        if self._cost_ts_ns > now_ns:
            self._invalidate_cost_snapshot("Maker costs are future-dated")
            return False
        return market_inputs_are_fresh(
            now_ns=now_ns,
            source_ts_ns=cast(int, source_tick.ts_event),
            hedge_ts_ns=cast(int, hedge_tick.ts_event),
            cost_ts_ns=self._cost_ts_ns,
            session_ts_ns=self._session_ts_ns,
            session_open=self._hedge_session_open,
            max_quote_age_ns=self._config.max_quote_age_ns,
            max_cross_leg_skew_ns=self._config.max_cross_leg_skew_ns,
            max_cost_age_ns=self._config.max_cost_age_ns,
            max_session_age_ns=self._config.max_session_age_ns,
        )

    def _carry_for_hedge_tick(
        self,
        hedge_tick: QuoteTick,
        now_ns: int,
    ) -> CarryConfig | None:
        if not self._live_costs_from_adapters:
            return self._carry
        try:
            swap_long_value, swap_short_value, point, mode, swap_rates, timezone = (
                self._mt5_swap_spec()
            )
            swap_long, swap_short = normalize_mt5_points_swap(
                swap_long=swap_long_value,
                swap_short=swap_short_value,
                point=point,
                ask=Decimal(str(hedge_tick.ask_price)),
                native_swap_mode=mode,
                swap_rates=swap_rates,
                now_ns=now_ns,
                server_timezone=timezone,
            )
        except (KeyError, TypeError, ValueError) as exc:
            self.log.error(f"MT5 swap normalization failed: {exc}")
            return None
        return CarryConfig(
            bitfinex_long=self._carry.bitfinex_long,
            bitfinex_short=self._carry.bitfinex_short,
            mt5_long_swap=swap_long,
            mt5_short_swap=swap_short,
            total_trade_fee=self._config.economics.carry.total_trade_fee,
        )

    def _mt5_swap_spec(
        self,
    ) -> tuple[Decimal, Decimal, Decimal, int, tuple[Decimal, ...], str]:
        return mt5_swap_spec(self._required_hedge_instrument())

    def _quote_is_fresh(self, tick: QuoteTick) -> bool:
        now_ns = cast(int, self.clock.timestamp_ns())
        tick_ts_ns = cast(int, tick.ts_event)
        return tick_ts_ns <= now_ns and now_ns - tick_ts_ns <= self._config.max_quote_age_ns

    def _source_accounts(self) -> tuple[SourceAccount, ...]:
        return tuple(
            self._source_account(route.account_id) for route in self._config.source_accounts
        )

    def _source_account(self, account_id: AccountId) -> SourceAccount:
        route = next(
            route for route in self._config.source_accounts if route.account_id == account_id
        )
        return SourceAccount(
            account_id=route.account_id,
            client_id=route.client_id,
            position_ounces=self._position(self._config.source_instrument_id, route.account_id),
            max_long_ounces=route.max_long_ounces,
            max_short_ounces=route.max_short_ounces,
            base_margin_level=route.base_margin_level,
        )

    def _hedge_accounts(self) -> tuple[MakerAccount, ...]:
        return tuple(self._hedge_account(route.account_id) for route in self._config.hedge_accounts)

    def _hedge_account(self, account_id: AccountId) -> MakerAccount:
        route = next(
            route for route in self._config.hedge_accounts if route.account_id == account_id
        )
        return MakerAccount(
            account_id=route.account_id,
            client_id=route.client_id,
            position_ounces=self._position(self._config.hedge_instrument_id, route.account_id),
            max_long_ounces=route.max_long_ounces,
            max_short_ounces=route.max_short_ounces,
        )

    def _position(self, instrument_id: InstrumentId, account_id: AccountId) -> Decimal:
        return cast(
            Decimal,
            self.portfolio.net_position(instrument_id=instrument_id, account_id=account_id),
        )

    def _global_obligation_block(self) -> bool:
        return (self._source_hold or self._state_store._freeze_publication_failed
                or not self._state_store.source_balance_is_admissible() or any(
            store.halt_reason is not None
            or store.source_freeze_reason is not None
            or store.has_unresolved_hedges()
            or not store._source_balance_is_admissible()
            for store in self._state_store.all_views()
        ))

    def _request_source_terminal_query(
        self,
        direction: SourceDirection,
        event: OrderCanceled | OrderExpired,
    ) -> None:
        if _restart_blocked(self):
            return
        query = self._source_terminal_query
        client_order_id = event.client_order_id.value
        venue_order_id = event.venue_order_id
        if query is None or self._source_terminal_stopped:
            return
        if venue_order_id is None:
            self.log.error("Maker source cancel terminal has no venue order ID")
            return
        if client_order_id in self._source_terminal_inflight:
            return
        self._source_terminal_inflight.add(client_order_id)
        generation = self._source_terminal_generation
        try:
            query(
                event.client_order_id,
                venue_order_id,
                lambda report: self._complete_source_terminal_query(
                    direction,
                    event,
                    report,
                ) if generation == self._source_terminal_generation else False,
            )
        except Exception as exc:
            self._source_terminal_inflight.discard(client_order_id)
            self.log.error(
                "Maker source terminal query submission failed with "
                f"{type(exc).__name__}"
            )

    def _complete_source_terminal_query(
        self,
        direction: SourceDirection,
        event: OrderCanceled | OrderExpired,
        report: OrderStatusReport | None,
    ) -> bool:
        if _restart_blocked(self):
            return False
        client_order_id = event.client_order_id.value
        if self._source_terminal_stopped or client_order_id not in self._source_terminal_inflight:
            return False
        try:
            if report is None or not self._source_cancel_report_is_exact(
                direction,
                event,
                report,
            ):
                self.log.error("Maker source cancel terminal query was not exact")
                return False
            self._stores[direction].confirm_source_reconciled(client_order_id)
            self._try_release_cycle()
        except Exception as exc:
            self.log.error(
                "Maker source terminal query failed with "
                f"{type(exc).__name__}"
            )
            return False
        self._source_terminal_inflight.discard(client_order_id)
        return True

    def _source_cancel_report_is_exact(
        self,
        direction: SourceDirection,
        event: OrderCanceled | OrderExpired,
        report: OrderStatusReport,
    ) -> bool:
        return source_cancel_report_is_exact(
            record=self._stores[direction].source_order(event.client_order_id.value),
            order=self.cache.order(event.client_order_id), event=event, report=report,
            source_instrument_id=self._config.source_instrument_id,
            source_accounts=self._config.source_accounts, maker=True,
        )

    def _try_release_cycle(self, *, inputs_fresh: bool = False) -> bool:
        if _restart_blocked(self):
            return False
        owner = self._state_store
        if not owner.cycle_freeze_only:
            if (not inputs_fresh or owner._freeze_publication_failed
                    or not owner.source_balance_is_admissible()
                    or any(view.halt_reason is not None or view.source_freeze_reason is not None
                           or not view.cycle_evidence_complete() for view in owner.all_views())):
                return False
            self._source_hold = False
            return True  # Only this healthy callback clears the instance-only market-input hold.
        if not self._state_store.clear_source_freezes():
            return False
        self._source_hold = False
        return True

    def _freeze_and_cancel_all(self, reason: str, *, market_input: bool = False) -> None:
        self._source_hold = True
        if not market_input:
            self._freeze_all_best_effort(reason)
        if _restart_blocked(self):
            return  # External pauses remain durable; startup still forbids cancellation.
        self._cancel_all_best_effort(reason)

    def _freeze_all_best_effort(self, reason: str) -> None:
        try:
            self._state_store.freeze_sources(reason)
        except Exception as exc:
            self.log.error(
                f"Maker freeze persistence failed with {type(exc).__name__}; "
                "current-process source HOLD remains active"
            )

    def _cancel_all_best_effort(self, reason: str) -> None:
        for direction in _DIRECTIONS:
            try:
                self._cancel_working(direction, reason=reason)
            except Exception as exc:
                self.log.error(
                    f"Maker {direction.value} cancel isolation caught {type(exc).__name__}; "
                    "source HOLD remains active"
                )

    def _direction_for_source_order(self, client_order_id: str) -> SourceDirection | None:
        return next(
            (
                direction
                for direction, store in self._stores.items()
                if store.knows_source_order(client_order_id)
            ),
            None,
        )

    def _update_order_status(self, client_order_id: str, status: str) -> None:
        if _restart_blocked(self):
            return
        direction = self._direction_for_source_order(client_order_id)
        if direction is not None:
            self._stores[direction].update_source_status(client_order_id, status)
            return
        obligation_status = {
            "SUBMITTED": ObligationStatus.SUBMITTED,
            "ACCEPTED": ObligationStatus.ACCEPTED,
        }[status]
        for store in self._stores.values():
            store.update_hedge_status(client_order_id, obligation_status)

    def _finish_or_reject(self, client_order_id: str, status: str) -> None:
        if _restart_blocked(self):
            return
        direction = self._direction_for_source_order(client_order_id)
        if direction is not None:
            self._stores[direction].update_source_status(client_order_id, status)
            self._working_quotes.pop(client_order_id, None)
            self._try_release_cycle()
            return
        for store in self._stores.values():
            store.update_hedge_status(
                client_order_id, ObligationStatus.REJECTED, native_status=status,
            )

    def _mark_source_unknown(self, client_order_id: str, reason: str) -> None:
        if _restart_blocked(self):
            return
        direction = self._direction_for_source_order(client_order_id)
        if direction is not None:
            self._source_hold = True
            try:
                self._stores[direction].mark_source_unknown(client_order_id, reason)
            finally:
                self._freeze_and_cancel_all(reason)

    def _durable_hedge_route(
        self,
        direction: SourceDirection,
        client_order_id: str,
    ) -> tuple[AccountId, ClientId | None] | None:
        record = self._stores[direction].source_order(client_order_id)
        if record is None or record.hedge_account_id is None:
            self._stores[direction].mark_source_unknown(
                client_order_id,
                "Maker source order has no durable hedge route",
            )
            return None
        account_id = AccountId(record.hedge_account_id)
        configured = next(
            (
                route
                for route in self._config.hedge_accounts
                if route.account_id == account_id
            ),
            None,
        )
        if configured is None:
            self._stores[direction].mark_source_unknown(
                client_order_id,
                f"Durable hedge account {account_id} is not configured",
            )
            return None
        configured_client_id = (
            configured.client_id.value if configured.client_id is not None else None
        )
        if record.hedge_client_id != configured_client_id:
            self._stores[direction].mark_source_unknown(
                client_order_id,
                "Durable Maker hedge client differs from the configured account route",
            )
            return None
        return account_id, configured.client_id

    def _required_source_instrument(self) -> Instrument:
        if self._source_instrument is None:
            raise RuntimeError("source instrument unavailable before Maker start")
        return self._source_instrument

    def _required_hedge_instrument(self) -> Instrument:
        if self._hedge_instrument is None:
            raise RuntimeError("hedge instrument unavailable before Maker start")
        return self._hedge_instrument


def _restart_blocked(strategy: object) -> bool:
    gate = getattr(strategy, "_restart_gate", None)
    return gate is not None and bool(gate())


def _cancel_is_pending(order: Order) -> bool:
    # A partial fill changes native status, but does not acknowledge its cancel.
    # Conversely, a late fill cannot reopen a cancel already completed in history.
    last_cancel = next((
        event for event in reversed(order.events)
        if isinstance(event, (
            OrderPendingCancel | OrderCancelRejected | OrderCanceled | OrderExpired | OrderRejected
        ))
    ), None)
    return isinstance(last_cancel, OrderPendingCancel)


def _book_top(tick: QuoteTick) -> BookTop:
    return BookTop(
        bid=Decimal(str(tick.bid_price)),
        ask=Decimal(str(tick.ask_price)),
        bid_size=Decimal(str(tick.bid_size)),
        ask_size=Decimal(str(tick.ask_size)),
    )


def _maker_timer_name(direction: SourceDirection, order_id: str) -> str:
    return f"maker-stale|{direction.value}|{order_id}"


def _requote_required(
    current_price: Decimal,
    next_price: Decimal,
    threshold: Decimal,
) -> bool:
    if current_price <= 0:
        raise ValueError("current Maker price must be positive")
    return abs(next_price / current_price - Decimal(1)) > abs(threshold)


def _maker_timer_target(
    timer_name: str,
    active_bid_id: str | None,
    active_ask_id: str | None,
) -> tuple[SourceDirection, str] | None:
    parts = timer_name.split("|", 2)
    if len(parts) != 3 or parts[0] != "maker-stale":
        return None
    try:
        direction = SourceDirection(parts[1])
    except ValueError:
        return None
    order_id = parts[2]
    active_id = active_bid_id if direction is SourceDirection.LONG else active_ask_id
    if order_id != active_id:
        return None
    return direction, order_id
