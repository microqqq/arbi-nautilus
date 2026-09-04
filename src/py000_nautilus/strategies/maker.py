"""Thin two-sided Maker strategy built directly on Nautilus order custody."""

from dataclasses import replace
from decimal import Decimal
from typing import cast

from nautilus_trader.common.events import TimeEvent
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderCancelRejected,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderModifyRejected,
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
from py000_nautilus.economics import market_inputs_are_fresh
from py000_nautilus.hedge import HedgeCoordinator, HedgePlanningError
from py000_nautilus.maker_economics import maker_quote, passive_maker_price
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
from py000_nautilus.store import JsonStateStore

_DIRECTIONS = (SourceDirection.LONG, SourceDirection.SHORT)


class MakerStrategy(Strategy):
    """Maintain one bid and one ask GTC source order, hedging actual fills only."""

    def __init__(self, config: MakerStrategyConfig) -> None:
        super().__init__(config)
        self._config = config
        self._stores = {
            direction: JsonStateStore(f"{config.store_path_prefix}.{direction.value}.json")
            for direction in _DIRECTIONS
        }
        self._hedges = {
            direction: HedgeCoordinator(config.source_instrument_id, self._stores[direction])
            for direction in _DIRECTIONS
        }
        self._working_quotes: dict[str, MakerQuote] = {}
        self._stale_timer_names: dict[SourceDirection, str] = {}
        self._source_hold = False
        self._source_instrument: Instrument | None = None
        self._hedge_instrument: Instrument | None = None
        self._carry = config.economics.carry
        self._fx = config.economics.fx
        self._cost_ts_ns = config.initial_cost_ts_ns
        self._hedge_session_open = config.initial_hedge_session_open
        self._session_ts_ns = config.initial_session_ts_ns

    def update_cost_snapshot(
        self,
        carry: CarryConfig,
        fx: FxConfig,
        ts_event_ns: int,
    ) -> None:
        if ts_event_ns >= self._cost_ts_ns:
            changed = (
                ts_event_ns > self._cost_ts_ns
                or carry != self._carry
                or fx != self._fx
            )
            self._carry = carry
            self._fx = fx
            self._cost_ts_ns = ts_event_ns
            if changed:
                self._freeze_and_cancel_all("Maker costs changed")

    def update_hedge_session(self, is_open: bool, ts_event_ns: int) -> None:
        if ts_event_ns >= self._session_ts_ns:
            self._hedge_session_open = is_open
            self._session_ts_ns = ts_event_ns
            if not is_open or ts_event_ns > cast(int, self.clock.timestamp_ns()):
                self._freeze_and_cancel_all("hedge session closed or future-dated")
            else:
                self._reschedule_active_timers()

    def on_start(self) -> None:
        self._source_instrument = self.cache.instrument(self._config.source_instrument_id)
        self._hedge_instrument = self.cache.instrument(self._config.hedge_instrument_id)
        if self._source_instrument is None or self._hedge_instrument is None:
            self.log.error("Maker instruments were not found in the Nautilus cache")
            self.stop()
            return
        for store in self._stores.values():
            reason = store.recover_for_start()
            if reason is not None:
                self.log.error(reason)
        self._try_release_cycle()
        self.subscribe_quote_ticks(self._config.source_instrument_id)
        self.subscribe_quote_ticks(self._config.hedge_instrument_id)

    def on_stop(self) -> None:
        for direction in _DIRECTIONS:
            self._cancel_working(direction, reason="strategy stop")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if tick.instrument_id not in {
            self._config.source_instrument_id,
            self._config.hedge_instrument_id,
        }:
            return
        if tick.instrument_id == self._config.hedge_instrument_id:
            self._submit_next_pending_hedge()
        source_tick = self.cache.quote_tick(self._config.source_instrument_id)
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if source_tick is None or hedge_tick is None:
            return
        inputs_fresh = self._inputs_are_fresh(source_tick, hedge_tick)
        if inputs_fresh:
            self._try_release_cycle()
        if self._global_obligation_block() or not inputs_fresh:
            self._freeze_and_cancel_all("stale, closed, or unresolved")
            return
        source_book = _book_top(source_tick)
        hedge_book = _book_top(hedge_tick)
        for direction in _DIRECTIONS:
            self._refresh_direction(direction, source_book, hedge_book)

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        self._update_order_status(event.client_order_id.value, "SUBMITTED")

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self._update_order_status(event.client_order_id.value, "ACCEPTED")

    def on_order_denied(self, event: OrderDenied) -> None:
        self._finish_or_reject(event.client_order_id.value, "DENIED")

    def on_order_rejected(self, event: OrderRejected) -> None:
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
        self._finish_or_reject(event.client_order_id.value, "CANCELED")

    def on_order_expired(self, event: OrderExpired) -> None:
        self._finish_or_reject(event.client_order_id.value, "EXPIRED")

    def on_order_modify_rejected(self, event: OrderModifyRejected) -> None:
        self._mark_source_unknown(event.client_order_id.value, "maker modify rejected")

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        self._mark_source_unknown(event.client_order_id.value, "maker cancel rejected")

    def on_order_filled(self, event: OrderFilled) -> None:
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
            # Current-process HOLD precedes I/O. The first durable act is the
            # authoritative fill/intent reservation in the direction store.
            self._source_hold = True
            try:
                intent = self._hedges[direction].on_source_filled(event)
            except Exception:
                self._freeze_all_best_effort(freeze_reason)
                self._cancel_all_best_effort("source fill WAL failed")
                raise
            self._freeze_all_best_effort(freeze_reason)
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
        desired = self._bound_quote(bound, source_book, hedge_book)
        if desired is None:
            self._cancel_working(direction, expected_order_id=active_id, reason="risk changed")
            return
        self._requote(order, desired)
        self._working_quotes[active_id] = desired
        self._schedule_stale_timer(direction, active_id)

    def _new_quote(
        self,
        direction: SourceDirection,
        source_book: BookTop,
        hedge_book: BookTop,
    ) -> MakerQuote | None:
        quote = maker_quote(
            direction,
            hedge_book,
            self._source_accounts(),
            self._hedge_accounts(),
            self._config.economics,
            carry=self._carry,
            fx=self._fx,
        )
        return self._passive_quote(quote, source_book)

    def _bound_quote(
        self,
        bound: MakerQuote,
        source_book: BookTop,
        hedge_book: BookTop,
    ) -> MakerQuote | None:
        source = self._source_account(bound.source_account.account_id)
        hedge = self._hedge_account(bound.hedge_account.account_id)
        quote = maker_quote(
            bound.direction,
            hedge_book,
            (source,),
            (hedge,),
            self._config.economics,
            carry=self._carry,
            fx=self._fx,
        )
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
        instrument = self._required_source_instrument()
        side = OrderSide.BUY if quote.direction is SourceDirection.LONG else OrderSide.SELL
        order = self.order_factory.limit(
            instrument_id=self._config.source_instrument_id,
            order_side=side,
            quantity=instrument.make_qty(quote.quantity_ounces),
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

    def _requote(self, order: Order, desired: MakerQuote) -> None:
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
        pending = next(
            (
                (direction, intent)
                for direction, intent in queued
                if intent.status is ObligationStatus.PENDING
                and intent.hedge_client_order_id is None
            ),
            None,
        )
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
        try:
            self.cancel_order(order)
        except Exception as exc:
            store.mark_source_unknown(
                active_id,
                f"{reason}: Maker cancel raised {type(exc).__name__}",
            )

    def _schedule_stale_timer(self, direction: SourceDirection, order_id: str) -> None:
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
        self.clock.set_time_alert_ns(
            name=name,
            alert_time_ns=deadline,
            callback=self._on_stale_timer,
        )
        self._stale_timer_names[direction] = name

    def _on_stale_timer(self, event: TimeEvent) -> None:
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
        if (
            source_tick is not None
            and hedge_tick is not None
            and self._inputs_are_fresh(source_tick, hedge_tick)
        ):
            self._schedule_stale_timer(direction, order_id)
        else:
            self._freeze_and_cancel_all("stale timer")

    def _reschedule_active_timers(self) -> None:
        if self._source_instrument is None:
            return
        for direction, store in self._stores.items():
            if store.active_source_order_id is not None:
                self._schedule_stale_timer(direction, store.active_source_order_id)

    def _inputs_are_fresh(self, source_tick: QuoteTick, hedge_tick: QuoteTick) -> bool:
        return market_inputs_are_fresh(
            now_ns=cast(int, self.clock.timestamp_ns()),
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
        return self._source_hold or any(
            store.halt_reason is not None
            or store.source_freeze_reason is not None
            or store.has_unresolved_hedges()
            or store.net_unhedged_ounces != 0
            for store in self._stores.values()
        )

    def confirm_source_reconciled(
        self,
        direction: SourceDirection,
        client_order_id: str,
    ) -> bool:
        """Accept external authoritative cancel/expiry reconciliation evidence."""
        self._stores[direction].confirm_source_reconciled(client_order_id)
        return self._try_release_cycle()

    def _try_release_cycle(self) -> bool:
        frozen = {
            store.source_freeze_reason
            for store in self._stores.values()
            if store.source_freeze_reason is not None
        }
        if not frozen or len(frozen) != 1:
            return False
        if not all(store.cycle_evidence_complete() for store in self._stores.values()):
            return False
        for store in self._stores.values():
            if store.source_freeze_reason is not None:
                store.clear_source_freeze()
        self._source_hold = False
        return True

    def _freeze_and_cancel_all(self, reason: str) -> None:
        self._source_hold = True
        self._freeze_all_best_effort(reason)
        self._cancel_all_best_effort(reason)

    def _freeze_all_best_effort(self, reason: str) -> None:
        for store in self._stores.values():
            try:
                store.freeze_source_submissions(reason)
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
        direction = self._direction_for_source_order(client_order_id)
        if direction is not None:
            self._stores[direction].update_source_status(client_order_id, status)
            self._working_quotes.pop(client_order_id, None)
            self._try_release_cycle()
            return
        for store in self._stores.values():
            store.update_hedge_status(client_order_id, ObligationStatus.REJECTED)

    def _mark_source_unknown(self, client_order_id: str, reason: str) -> None:
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
