"""Minimal Taker strategy using Nautilus lifecycle, orders, and fill events."""

from decimal import Decimal
from typing import cast

from nautilus_trader.common.events import TimeEvent
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
)
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy

from py000_nautilus.config import CarryConfig, FxConfig, TakerStrategyConfig
from py000_nautilus.economics import evaluate_taker, market_inputs_are_fresh
from py000_nautilus.hedge import HedgeCoordinator
from py000_nautilus.models import (
    BookTop,
    BusinessOrderSide,
    HedgeAccount,
    HedgeIntent,
    ObligationStatus,
    Opportunity,
    SourceAccount,
    SourceDirection,
)
from py000_nautilus.store import JsonStateStore


class TakerStrategy(Strategy):
    """One Bitfinex-source/MT5-hedge Taker slice, with no parallel lifecycle."""

    def __init__(self, config: TakerStrategyConfig) -> None:
        super().__init__(config)
        self._config = config
        self.state_store = JsonStateStore(config.store_path)
        self._hedges = HedgeCoordinator(config.source_instrument_id, self.state_store)
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
        """Accept a newer per-loop funding/swap/FX snapshot from live composition."""
        if ts_event_ns >= self._cost_ts_ns:
            self._carry = carry
            self._fx = fx
            self._cost_ts_ns = ts_event_ns

    def update_hedge_session(self, is_open: bool, ts_event_ns: int) -> None:
        """Accept a newer MT5 session fact without creating a session subsystem."""
        if ts_event_ns >= self._session_ts_ns:
            self._hedge_session_open = is_open
            self._session_ts_ns = ts_event_ns

    def on_start(self) -> None:
        self._source_instrument = self.cache.instrument(self._config.source_instrument_id)
        self._hedge_instrument = self.cache.instrument(self._config.hedge_instrument_id)
        if self._source_instrument is None or self._hedge_instrument is None:
            self.log.error("Taker instruments were not found in the Nautilus cache")
            self.stop()
            return
        reason = self.state_store.recover_for_start()
        if reason is not None:
            self.log.error(reason)
        self.subscribe_quote_ticks(self._config.source_instrument_id)
        self.subscribe_quote_ticks(self._config.hedge_instrument_id)

    def on_stop(self) -> None:
        """Cancel only the exact active source and retain its durable gate."""
        client_order_id = self.state_store.active_source_order_id
        if client_order_id is None:
            return
        order = self.cache.order(ClientOrderId(client_order_id))
        if order is None:
            self.state_store.mark_source_unknown(
                client_order_id,
                "strategy stop could not resolve the active Nautilus order",
            )
            return
        if order.is_closed:
            return
        try:
            self.cancel_order(order)
        except Exception as exc:
            self.state_store.mark_source_unknown(
                client_order_id,
                f"source cancel on stop raised {type(exc).__name__}",
            )

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if tick.instrument_id not in {
            self._config.source_instrument_id,
            self._config.hedge_instrument_id,
        }:
            return
        if not self.state_store.can_submit_source():
            return
        source_tick = self.cache.quote_tick(self._config.source_instrument_id)
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if source_tick is None or hedge_tick is None:
            return
        if not self._inputs_are_fresh(source_tick, hedge_tick):
            return
        opportunity = evaluate_taker(
            source_book=_book_top(source_tick),
            hedge_book=_book_top(hedge_tick),
            accounts=self._source_accounts(),
            hedge=self._hedge_account(),
            config=self._config.economics,
            carry=self._carry,
            fx=self._fx,
        )
        if opportunity is not None:
            self._submit_source(opportunity)

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        self._update_order_status(event.client_order_id.value, "SUBMITTED")

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self._update_order_status(event.client_order_id.value, "ACCEPTED")

    def on_order_denied(self, event: OrderDenied) -> None:
        self._finish_or_reject(event.client_order_id.value, "DENIED")

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._finish_or_reject(event.client_order_id.value, "REJECTED")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self._finish_or_reject(event.client_order_id.value, "CANCELED")

    def on_order_expired(self, event: OrderExpired) -> None:
        self._finish_or_reject(event.client_order_id.value, "EXPIRED")

    def on_order_filled(self, event: OrderFilled) -> None:
        if event.instrument_id == self._config.source_instrument_id:
            intent = self._hedges.on_source_filled(event)
            if intent is not None:
                self._submit_hedge_intent(intent)
            return
        if event.instrument_id == self._config.hedge_instrument_id:
            self.state_store.apply_hedge_fill(
                client_order_id=event.client_order_id.value,
                trade_id=event.trade_id.value,
                fill_ounces=Decimal(str(event.last_qty)),
            )

    def _submit_source(self, opportunity: Opportunity) -> None:
        instrument = self._required_source_instrument()
        side = (
            OrderSide.BUY
            if opportunity.direction is SourceDirection.LONG
            else OrderSide.SELL
        )
        order = self.order_factory.limit(
            instrument_id=self._config.source_instrument_id,
            order_side=side,
            quantity=instrument.make_qty(opportunity.source_quantity_ounces),
            price=instrument.make_price(opportunity.source_price_usdt),
            time_in_force=TimeInForce.GTC,
            tags=[
                "py000=taker-source",
                f"legacy_leverage={opportunity.leverage}",
                f"net_return={opportunity.net_return}",
            ],
        )
        business_side = (
            BusinessOrderSide.BUY if side is OrderSide.BUY else BusinessOrderSide.SELL
        )
        self.state_store.begin_source(
            order.client_order_id.value,
            business_side,
            Decimal(str(order.quantity)),
        )
        try:
            self.submit_order(
                order,
                client_id=opportunity.source_account.client_id,
                params={"leverage": opportunity.leverage},
            )
            self.clock.set_time_alert_ns(
                name=_cancel_timer_name(order.client_order_id.value),
                alert_time_ns=self.clock.timestamp_ns() + self._config.source_cancel_after_ns,
                callback=self._cancel_source_remainder,
            )
        except Exception as exc:
            self.state_store.mark_source_unknown(
                order.client_order_id.value,
                f"source submission raised {type(exc).__name__}",
            )
            raise

    def _submit_hedge_intent(self, intent: HedgeIntent) -> None:
        instrument = self._required_hedge_instrument()
        side = OrderSide.BUY if intent.hedge_side is BusinessOrderSide.BUY else OrderSide.SELL
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if hedge_tick is None or not self._quote_is_fresh(hedge_tick):
            self.log.error(
                f"hedge quote unavailable for {intent.intent_id}; source remains blocked"
            )
            return
        limit_price = hedge_tick.ask_price if side is OrderSide.BUY else hedge_tick.bid_price
        order = self.order_factory.limit(
            instrument_id=self._config.hedge_instrument_id,
            order_side=side,
            quantity=instrument.make_qty(intent.hedge_quantity_ounces),
            price=instrument.make_price(Decimal(str(limit_price))),
            time_in_force=TimeInForce.IOC,
            tags=[f"py000={intent.intent_id}", f"source_trade={intent.source_trade_id}"],
        )
        self.state_store.bind_hedge_order(intent.intent_id, order.client_order_id.value)
        try:
            self.submit_order(order, client_id=self._config.hedge_client_id)
        except Exception as exc:
            self.state_store.update_hedge_status(
                order.client_order_id.value,
                ObligationStatus.UNKNOWN,
            )
            self.log.error(f"hedge submission raised {type(exc).__name__}")
            raise

    def _source_accounts(self) -> tuple[SourceAccount, ...]:
        accounts = []
        for route in self._config.source_accounts:
            accounts.append(
                SourceAccount(
                    account_id=route.account_id,
                    client_id=route.client_id,
                    position_ounces=self._position(
                        self._config.source_instrument_id,
                        route.account_id,
                    ),
                    max_long_ounces=route.max_long_ounces,
                    max_short_ounces=route.max_short_ounces,
                    base_margin_level=route.base_margin_level,
                )
            )
        return tuple(accounts)

    def _hedge_account(self) -> HedgeAccount:
        return HedgeAccount(
            position_ounces=self._position(
                self._config.hedge_instrument_id,
                self._config.hedge_account_id,
            ),
            max_long_ounces=self._config.hedge_max_long_ounces,
            max_short_ounces=self._config.hedge_max_short_ounces,
        )

    def _position(self, instrument_id: InstrumentId, account_id: AccountId) -> Decimal:
        return cast(
            Decimal,
            self.portfolio.net_position(instrument_id=instrument_id, account_id=account_id),
        )

    def _update_order_status(self, client_order_id: str, status: str) -> None:
        if self.state_store.knows_source_order(client_order_id):
            self.state_store.update_source_status(client_order_id, status)
            return
        obligation_status = {
            "SUBMITTED": ObligationStatus.SUBMITTED,
            "ACCEPTED": ObligationStatus.ACCEPTED,
        }[status]
        self.state_store.update_hedge_status(client_order_id, obligation_status)

    def _finish_or_reject(self, client_order_id: str, status: str) -> None:
        if self.state_store.knows_source_order(client_order_id):
            self.state_store.update_source_status(client_order_id, status)
        else:
            self.state_store.update_hedge_status(client_order_id, ObligationStatus.REJECTED)

    def _cancel_source_remainder(self, event: TimeEvent) -> None:
        client_order_id = _timer_target_if_active(
            cast(str, event.name),
            self.state_store.active_source_order_id,
        )
        if client_order_id is None:
            return
        order = self.cache.order(ClientOrderId(client_order_id))
        if order is None:
            self.state_store.mark_source_unknown(
                client_order_id,
                "source cancel timer could not resolve the Nautilus order",
            )
            return
        if not order.is_closed:
            self.cancel_order(order)

    def _inputs_are_fresh(self, source_tick: QuoteTick, hedge_tick: QuoteTick) -> bool:
        now_ns = cast(int, self.clock.timestamp_ns())
        return market_inputs_are_fresh(
            now_ns=now_ns,
            source_ts_ns=source_tick.ts_event,
            hedge_ts_ns=hedge_tick.ts_event,
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

    def _required_source_instrument(self) -> Instrument:
        if self._source_instrument is None:
            raise RuntimeError("source instrument unavailable before strategy start")
        return self._source_instrument

    def _required_hedge_instrument(self) -> Instrument:
        if self._hedge_instrument is None:
            raise RuntimeError("hedge instrument unavailable before strategy start")
        return self._hedge_instrument


def _book_top(tick: QuoteTick) -> BookTop:
    return BookTop(
        bid=Decimal(str(tick.bid_price)),
        ask=Decimal(str(tick.ask_price)),
        bid_size=Decimal(str(tick.bid_size)),
        ask_size=Decimal(str(tick.ask_size)),
    )


def _cancel_timer_name(client_order_id: str) -> str:
    return f"cancel-source:{client_order_id}"


def _timer_target_if_active(timer_name: str, active_order_id: str | None) -> str | None:
    prefix = "cancel-source:"
    if active_order_id is None or not timer_name.startswith(prefix):
        return None
    timer_order_id = timer_name.removeprefix(prefix)
    return timer_order_id if timer_order_id == active_order_id else None
