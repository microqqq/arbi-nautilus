"""Minimal Taker strategy using Nautilus books, orders, and fill events."""

import asyncio
from collections.abc import Callable
from decimal import ROUND_FLOOR, Decimal
from typing import cast

from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.book import BookLevel, OrderBook
from nautilus_trader.model.data import (
    FundingRateUpdate,
    InstrumentStatus,
    OrderBookDeltas,
    QuoteTick,
)
from nautilus_trader.model.enums import BookType, OrderSide, TimeInForce
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
)
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId, PositionId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.position import Position
from nautilus_trader.trading.strategy import Strategy

from py000_nautilus.config import CarryConfig, FxConfig, TakerStrategyConfig
from py000_nautilus.economics import (
    evaluate_taker,
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
from py000_nautilus.margin import LiveAccountReader
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
from py000_nautilus.strategies._mt5_costs import (
    mt5_instrument_is_fresh,
    mt5_instrument_structure,
    mt5_swap_spec,
    validate_mt5_instrument_update,
)
from py000_nautilus.strategies._source_terminal import (
    SourceTerminalQuery,
    source_cancel_report_is_exact,
)

_ONE_SHOT_FREEZE_REASON = "one-shot source attempt claimed"


class TakerStrategy(Strategy):
    """One Bitfinex-source/MT5-hedge Taker slice, with no parallel lifecycle."""

    def __init__(
        self,
        config: TakerStrategyConfig,
        *,
        live_submission_ready: Callable[[], bool] | None = None,
        hedge_quantity_ready: Callable[[Decimal], bool] | None = None,
        live_costs_from_adapters: bool = False,
        one_shot: bool = False,
        allowed_source_direction: SourceDirection | None = None,
        hedge_must_reduce_only: bool = False,
        source_terminal_query: SourceTerminalQuery | None = None,
    ) -> None:
        super().__init__(config)
        if type(one_shot) is not bool:
            raise TypeError("one_shot must be a bool")
        if allowed_source_direction is not None and not isinstance(
            allowed_source_direction,
            SourceDirection,
        ):
            raise TypeError("allowed_source_direction must be a SourceDirection or None")
        if type(hedge_must_reduce_only) is not bool:
            raise TypeError("hedge_must_reduce_only must be a bool")
        if hedge_must_reduce_only and not one_shot:
            raise ValueError("hedge_must_reduce_only requires one_shot execution")
        self._config = config
        self._live_submission_ready = live_submission_ready
        self._restart_gate: Callable[[], bool] | None = None
        self._hedge_quantity_ready = hedge_quantity_ready
        self._live_costs_from_adapters = live_costs_from_adapters
        self._one_shot = one_shot
        self._allowed_source_direction = allowed_source_direction
        self._hedge_must_reduce_only = hedge_must_reduce_only
        self._source_terminal_query = source_terminal_query
        self._source_terminal_inflight: set[str] = set()
        self._source_terminal_stopped = False
        self._source_terminal_generation = 0
        self._live_account_reader: LiveAccountReader | None = None
        self._account_loop: asyncio.AbstractEventLoop | None = None
        self._account_handle: asyncio.Handle | None = None
        self._account_topics: tuple[str, ...] = ()
        self._one_shot_hedge_position_id: PositionId | None = None
        self._one_shot_hedge_position_quantity_ounces: Decimal | None = None
        self._one_shot_armed = False
        self._one_shot_claimed = False
        self._one_shot_closed = False
        self._source_book_subscribed = False
        self._source_book_callback_count = 0
        self._last_source_attempt_market_ts_ns: int | None = None
        self._last_decision_gate = "not_armed" if one_shot else "not_started"
        self.state_store = JsonStateStore(config.store_path)
        self._hedges = HedgeCoordinator(config.source_instrument_id, self.state_store)
        self._source_instrument: Instrument | None = None
        self._hedge_instrument: Instrument | None = None
        self._hedge_instrument_valid = False
        self._carry = config.economics.carry
        self._fx = config.economics.fx
        self._cost_ts_ns = config.initial_cost_ts_ns
        self._cost_snapshot_valid = config.initial_cost_ts_ns > 0
        self._cost_recovery_after_ns = 0
        self._hedge_session_open = config.initial_hedge_session_open
        self._session_ts_ns = config.initial_session_ts_ns

    def bind_restart_gate(self, pending: Callable[[], bool]) -> None:
        """Bind only the ordinary composition's startup HOLD, not general health."""
        self._restart_gate = pending

    def bind_live_account_reader(self, reader: LiveAccountReader) -> None:
        """Bind the composition's current-account view before strategy start."""
        self._live_account_reader = reader

    def _on_account_update(self, _event: object) -> None:
        # Account publication can be nested inside a fill: never re-enter trading here.
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
            self._evaluate_and_submit()
        except Exception as exc:
            self.log.error(f"Taker account evaluation failed with {type(exc).__name__}")

    @property
    def one_shot_armed(self) -> bool:
        return self._one_shot_armed

    @property
    def one_shot_claimed(self) -> bool:
        return self._one_shot_claimed

    @property
    def source_book_callback_count(self) -> int:
        return self._source_book_callback_count

    @property
    def last_decision_gate(self) -> str:
        return self._last_decision_gate

    def source_independent_inputs_ready(self) -> bool:
        """Check the live inputs which can be ready before source subscription."""
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if hedge_tick is None or not self._cost_snapshot_valid:
            return False
        now_ns = cast(int, self.clock.timestamp_ns())
        if self._live_costs_from_adapters and (
            not self._hedge_instrument_valid or not mt5_instrument_is_fresh(
                self._required_hedge_instrument(), now_ns, self._config.max_cost_age_ns,
            )
        ):
            return False
        return market_inputs_are_fresh(
            now_ns=now_ns,
            source_ts_ns=hedge_tick.ts_event,
            hedge_ts_ns=hedge_tick.ts_event,
            cost_ts_ns=self._cost_ts_ns,
            session_ts_ns=self._session_ts_ns,
            session_open=self._hedge_session_open,
            max_quote_age_ns=self._config.max_quote_age_ns,
            max_cross_leg_skew_ns=self._config.max_cross_leg_skew_ns,
            max_cost_age_ns=self._config.max_cost_age_ns,
            max_session_age_ns=self._config.max_session_age_ns,
        )

    def arm_one_shot(self) -> None:
        """Arm one fresh one-shot store exactly once after external preflight."""
        if not self._one_shot:
            raise RuntimeError("strategy is not configured for one-shot execution")
        if self._one_shot_armed or self._one_shot_claimed or self._one_shot_closed:
            raise RuntimeError("one-shot execution was already armed or claimed")
        if (
            not self.state_store.can_submit_source()
            or self.state_store.source_orders()
            or self.state_store.intents()
        ):
            raise RuntimeError("one-shot execution requires a fresh state store")
        self._one_shot_armed = True
        self._source_book_callback_count = 0
        self._last_decision_gate = "awaiting_source_book"
        if self.is_running:
            self._subscribe_source_book()

    def disarm_one_shot(self) -> None:
        """Close an unclaimed gate synchronously before bounded shutdown."""
        if not self._one_shot or self._one_shot_claimed:
            return
        self._one_shot_armed = False
        self._one_shot_closed = True

    def update_cost_snapshot(
        self,
        carry: CarryConfig,
        fx: FxConfig,
        ts_event_ns: int,
    ) -> bool:
        """Install one monotonic funding/swap/FX snapshot, or fail closed."""
        if type(ts_event_ns) is not int or ts_event_ns <= 0:
            self._invalidate_cost_snapshot()
            return False
        now_ns = cast(int, self.clock.timestamp_ns())
        if not 0 <= now_ns - ts_event_ns <= self._config.max_cost_age_ns:
            self._invalidate_cost_snapshot()
            return False
        if ts_event_ns < self._cost_ts_ns:
            self._invalidate_cost_snapshot()
            return False
        if ts_event_ns == self._cost_ts_ns:
            if carry != self._carry or fx != self._fx:
                self._invalidate_cost_snapshot()
                return False
            return self._cost_snapshot_valid
        if ts_event_ns <= self._cost_recovery_after_ns:
            return False
        self._carry = carry
        self._fx = fx
        self._cost_ts_ns = ts_event_ns
        self._cost_snapshot_valid = True
        self._cost_recovery_after_ns = 0
        return True

    def _invalidate_cost_snapshot(self) -> None:
        self._cost_snapshot_valid = False
        self._cost_recovery_after_ns = max(
            self._cost_recovery_after_ns,
            self._cost_ts_ns,
        )

    def update_hedge_session(self, is_open: bool, ts_event_ns: int) -> None:
        """Accept a newer MT5 session fact without creating a session subsystem."""
        if ts_event_ns >= self._session_ts_ns:
            self._hedge_session_open = is_open
            self._session_ts_ns = ts_event_ns

    def on_start(self) -> None:
        self._source_terminal_stopped = False
        if self._live_account_reader is not None:
            self._account_loop = asyncio.get_running_loop()
            self._account_topics = tuple({
                f"events.account.{self._config.source_accounts[0].account_id}",
                f"events.account.{self._config.hedge_account_id}",
            })
            for topic in self._account_topics:
                self.msgbus.subscribe(topic, self._on_account_update)
        self._source_instrument = self.cache.instrument(self._config.source_instrument_id)
        self._hedge_instrument = self.cache.instrument(self._config.hedge_instrument_id)
        if self._source_instrument is None or self._hedge_instrument is None:
            self.log.error("Taker instruments were not found in the Nautilus cache")
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
        reason = self.state_store.recover_for_start()
        if reason is not None:
            self.log.error(reason)
        if not self._one_shot or self._one_shot_armed:
            self._subscribe_source_book()
        if self._live_costs_from_adapters:
            self.subscribe_instrument(self._config.hedge_instrument_id)
            self.subscribe_funding_rates(self._config.source_instrument_id)
        self.subscribe_quote_ticks(self._config.hedge_instrument_id)
        self.subscribe_instrument_status(self._config.hedge_instrument_id)

    def on_stop(self) -> None:
        """Cancel only the exact active source and retain its durable gate."""
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
        if _restart_blocked(self):
            return
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

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        if deltas.instrument_id != self._config.source_instrument_id:
            return
        self._source_book_callback_count += 1
        self._evaluate_and_submit()

    def _evaluate_and_submit(self) -> None:
        if _restart_blocked(self):
            self._last_decision_gate = "restart_pending"
            return
        if not self.state_store.can_submit_source():
            self._last_decision_gate = "state_store_closed"
            return
        source_book = self.cache.order_book(self._config.source_instrument_id)
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if source_book is None or hedge_tick is None:
            self._last_decision_gate = "market_cache_missing"
            return
        reference_book = _reference_book(
            source_book,
            self._config.economics.base_book_quantity,
        )
        now_ns = cast(int, self.clock.timestamp_ns())
        if reference_book is None:
            self._last_decision_gate = "reference_depth_insufficient"
            return
        if not self._inputs_are_fresh(source_book.ts_last, hedge_tick, now_ns):
            self._last_decision_gate = "inputs_not_fresh"
            return
        accounts: tuple[SourceAccount, ...]
        if self._live_account_reader is not None:
            view = self._live_account_reader(
                reference_book, source_book.ts_last, _book_top(hedge_tick),
                hedge_tick.ts_event, True,
            )
            if view is None or not view[3]:
                self._last_decision_gate = "account_budget_unavailable"
                return
            accounts, hedge = (view[0],), view[1]
        else:
            accounts, hedge = self._source_accounts(), self._hedge_account()
        # Coalesce one market timestamp without changing either freshness clock.
        market_ts_ns = max(source_book.ts_last, hedge_tick.ts_event)
        if market_ts_ns == self._last_source_attempt_market_ts_ns:
            self._last_decision_gate = "market_event_already_claimed"
            return
        carry = self._carry_for_hedge_tick(hedge_tick, now_ns)
        if carry is None:
            self._last_decision_gate = "carry_unavailable"
            return
        opportunity = evaluate_taker(
            source_book=reference_book,
            hedge_book=_book_top(hedge_tick),
            accounts=accounts,
            hedge=hedge,
            config=self._config.economics,
            allowed_direction=self._allowed_source_direction,
            carry=carry,
            fx=self._fx,
        )
        if opportunity is None:
            self._last_decision_gate = "not_qualifying"
            return
        self._last_decision_gate = "source_admission"
        self._last_decision_gate = (
            "source_claimed"
            if self._submit_source(opportunity, market_ts_ns=market_ts_ns)
            else "source_admission_blocked"
        )

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if tick.instrument_id == self._config.hedge_instrument_id:
            self._submit_next_pending_hedge()
            self._evaluate_and_submit()

    def _subscribe_source_book(self) -> None:
        if self._source_book_subscribed:
            return
        self.subscribe_order_book_deltas(
            self._config.source_instrument_id,
            book_type=BookType.L2_MBP,
            depth=25,
            managed=True,
        )
        self._source_book_subscribed = True

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
            validate_mt5_instrument_update(
                previous, instrument, self.clock.timestamp_ns(), self._config.max_cost_age_ns,
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._hedge_instrument_valid = False
            self.log.error(f"MT5 instrument update rejected: {exc}")
            return
        if instrument.ts_event == previous.ts_event and not self._hedge_instrument_valid:
            return
        self._hedge_instrument = instrument
        self._hedge_instrument_valid = True

    def on_funding_rate(self, funding_rate: FundingRateUpdate) -> None:
        """Install the source venue's signed next-period funding observation."""
        if (
            not self._live_costs_from_adapters
            or funding_rate.instrument_id != self._config.source_instrument_id
        ):
            return
        ts_event_ns = cast(int, funding_rate.ts_event)
        if ts_event_ns > cast(int, self.clock.timestamp_ns()):
            self._invalidate_cost_snapshot()
            self.log.error("Bitfinex funding observation is future-dated")
            return
        rate = Decimal(str(funding_rate.rate))
        if not rate.is_finite() or abs(rate) > 1:
            self._invalidate_cost_snapshot()
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
            if self.state_store.knows_source_order(client_order_id):
                self.state_store.mark_source_unknown(
                    client_order_id,
                    "Nautilus reported an unknown source submission outcome",
                )
            else:
                self.state_store.update_hedge_status(
                    client_order_id,
                    ObligationStatus.UNKNOWN,
                )
            return
        self._finish_or_reject(event.client_order_id.value, "REJECTED")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self._finish_or_reject(event.client_order_id.value, "CANCELED")
        self._request_source_terminal_query(event)

    def on_order_expired(self, event: OrderExpired) -> None:
        self._finish_or_reject(event.client_order_id.value, "EXPIRED")
        self._request_source_terminal_query(event)

    def _request_source_terminal_query(self, event: OrderCanceled | OrderExpired) -> None:
        if _restart_blocked(self):
            return
        client_order_id = event.client_order_id.value
        query = self._source_terminal_query
        if (
            query is None or self._source_terminal_stopped
            or not self.state_store.knows_source_order(client_order_id)
            or client_order_id in self._source_terminal_inflight
        ):
            return
        if event.venue_order_id is None:
            self.log.error("Taker source terminal has no venue order ID")
            return
        self._source_terminal_inflight.add(client_order_id)
        generation = self._source_terminal_generation
        try:
            query(
                event.client_order_id, event.venue_order_id,
                lambda report: self._complete_source_terminal_query(event, report)
                if generation == self._source_terminal_generation else False,
            )
        except Exception as exc:
            self._source_terminal_inflight.discard(client_order_id)
            self.log.error(f"Taker source terminal query submission failed: {type(exc).__name__}")

    def _complete_source_terminal_query(
        self, event: OrderCanceled | OrderExpired, report: OrderStatusReport | None,
    ) -> bool:
        if _restart_blocked(self):
            return False
        client_order_id = event.client_order_id.value
        if self._source_terminal_stopped or client_order_id not in self._source_terminal_inflight:
            return False
        try:
            if report is None or not source_cancel_report_is_exact(
                record=self.state_store.source_order(client_order_id),
                order=self.cache.order(event.client_order_id), event=event, report=report,
                source_instrument_id=self._config.source_instrument_id,
                source_accounts=self._config.source_accounts, maker=False,
            ):
                self.log.error("Taker source terminal query was not exact")
                return False
            self.state_store.confirm_source_reconciled(client_order_id)
        except Exception as exc:
            self.log.error(f"Taker source terminal query failed: {type(exc).__name__}")
            return False
        self._source_terminal_inflight.discard(client_order_id)
        return True

    def on_order_filled(self, event: OrderFilled) -> None:
        if _restart_blocked(self):
            return
        if event.instrument_id == self._config.source_instrument_id:
            intent = self._hedges.on_source_filled(event)
            if intent is not None:
                self._submit_next_pending_hedge()
            return
        if event.instrument_id == self._config.hedge_instrument_id:
            applied = self._hedges.on_hedge_filled(event)
            if applied:
                self._submit_next_pending_hedge()

    def _submit_source(self, opportunity: Opportunity, *, market_ts_ns: int | None = None) -> bool:
        if _restart_blocked(self):
            return False
        if (
            self._allowed_source_direction is not None
            and opportunity.direction is not self._allowed_source_direction
        ):
            return False
        instrument = self._required_source_instrument()
        source_quantity = instrument.make_qty(opportunity.source_quantity_ounces)
        if not self._source_hedge_is_executable(
            opportunity,
            Decimal(str(source_quantity)),
        ):
            return False
        if not self._claim_one_shot():
            return False
        side = OrderSide.BUY if opportunity.direction is SourceDirection.LONG else OrderSide.SELL
        source_before = opportunity.source_account.position_ounces
        source_reduction = (
            side is OrderSide.SELL
            and source_before > 0
            and Decimal(str(source_quantity)) <= source_before
        ) or (
            side is OrderSide.BUY
            and source_before < 0
            and Decimal(str(source_quantity)) <= abs(source_before)
        )
        order = self.order_factory.limit(
            instrument_id=self._config.source_instrument_id,
            order_side=side,
            quantity=source_quantity,
            price=instrument.make_price(opportunity.source_price_usdt),
            time_in_force=TimeInForce.IOC,
            reduce_only=source_reduction,
            tags=[
                "py000=taker-source",
                f"legacy_leverage={opportunity.leverage}",
                f"net_return={opportunity.net_return}",
            ],
        )
        business_side = BusinessOrderSide.BUY if side is OrderSide.BUY else BusinessOrderSide.SELL
        self.state_store.begin_source(
            order.client_order_id.value,
            business_side,
            Decimal(str(order.quantity)),
            source_account_id=opportunity.source_account.account_id.value,
            source_client_id=(
                opportunity.source_account.client_id.value
                if opportunity.source_account.client_id is not None
                else None
            ),
            hedge_account_id=self._config.hedge_account_id.value,
            hedge_client_id=(
                self._config.hedge_client_id.value
                if self._config.hedge_client_id is not None
                else None
            ),
            hedge_position_id=(
                self._one_shot_hedge_position_id.value
                if self._one_shot_hedge_position_id is not None
                else None
            ),
            hedge_position_quantity_ounces=(
                self._one_shot_hedge_position_quantity_ounces
                if self._one_shot_hedge_position_id is not None
                else None
            ),
            source_freeze_reason=_ONE_SHOT_FREEZE_REASON if self._one_shot else None,
        )
        if market_ts_ns is not None:
            self._last_source_attempt_market_ts_ns = market_ts_ns
        try:
            self.submit_order(
                order,
                client_id=opportunity.source_account.client_id,
                params={"leverage": opportunity.leverage},
            )
        except Exception as exc:
            self.state_store.mark_source_unknown(
                order.client_order_id.value,
                f"source submission raised {type(exc).__name__}",
            )
            raise
        return True

    def _claim_one_shot(self) -> bool:
        if not self._one_shot:
            return True
        if not self._one_shot_armed or self._one_shot_claimed:
            return False
        self._one_shot_armed = False
        self._one_shot_claimed = True
        self._one_shot_closed = True
        return True

    def _source_hedge_is_executable(
        self,
        opportunity: Opportunity,
        source_quantity_ounces: Decimal,
    ) -> bool:
        hedge_side = (
            BusinessOrderSide.SELL
            if opportunity.direction is SourceDirection.LONG
            else BusinessOrderSide.BUY
        )
        max_hedge_ounces = Decimal(abs(round_hedge_ounces(source_quantity_ounces)))
        if max_hedge_ounces == 0:
            return True
        try:
            plan = plan_hedge_delta(
                self._hedge_positions(),
                hedge_side,
                max_hedge_ounces,
            )
        except (HedgePlanningError, TypeError, ValueError) as exc:
            self.log.error(f"source admission blocked by MT5 position shape: {exc}")
            return False
        quantity_ready = self._hedge_quantity_ready
        if quantity_ready is not None:
            for leg in plan:
                try:
                    admitted = quantity_ready(leg.quantity_ounces)
                except Exception as exc:
                    self.log.error(
                        "source admission blocked because MT5 quantity preflight raised "
                        f"{type(exc).__name__}"
                    )
                    return False
                if not admitted:
                    self.log.error(
                        "source admission blocked because MT5 cannot execute hedge leg "
                        f"quantity {leg.quantity_ounces} ounces"
                    )
                    return False
        if self._hedge_must_reduce_only:
            if len(plan) != 1 or not plan[0].is_close:
                self.log.error(
                    "source admission blocked because exit hedge has no exact MT5 close target"
                )
                return False
            position_id = PositionId(cast(str, plan[0].position_id))
            if (
                self._one_shot_hedge_position_id is not None
                and self._one_shot_hedge_position_id != position_id
            ):
                self.log.error("source admission blocked because MT5 close target changed")
                return False
            self._one_shot_hedge_position_id = position_id
            self._one_shot_hedge_position_quantity_ounces = cast(
                Decimal,
                plan[0].expected_position_quantity_ounces,
            )
        return True

    def _submit_next_pending_hedge(self) -> None:
        if _restart_blocked(self):
            return
        intents = self.state_store.intents()
        failed_statuses = {
            ObligationStatus.BLOCKED,
            ObligationStatus.REJECTED,
            ObligationStatus.UNKNOWN,
        }
        if any(intent.status in failed_statuses for intent in intents):
            return
        active_statuses = {
            ObligationStatus.SUBMITTING,
            ObligationStatus.SUBMITTED,
            ObligationStatus.ACCEPTED,
        }
        if any(intent.status in active_statuses for intent in intents):
            return
        pending = next(
            (
                intent
                for intent in intents
                if intent.status is ObligationStatus.PENDING
                and intent.hedge_client_order_id is None
            ),
            None,
        )
        if pending is not None:
            self._submit_hedge_intent(pending)

    def _submit_hedge_intent(self, intent: HedgeIntent) -> None:
        if _restart_blocked(self):
            return
        source = self.state_store.source_order(intent.source_client_order_id)
        configured_client_id = (
            self._config.hedge_client_id.value
            if self._config.hedge_client_id is not None
            else None
        )
        if (
            source is None
            or source.hedge_account_id != self._config.hedge_account_id.value
            or source.hedge_client_id != configured_client_id
        ):
            reason = "durable Taker hedge route is missing or differs from configuration"
            self.state_store.block_hedge_intent(intent.intent_id, reason)
            self.log.error(f"hedge route blocked for {intent.intent_id}: {reason}")
            return
        instrument = self._required_hedge_instrument()
        hedge_tick = self.cache.quote_tick(self._config.hedge_instrument_id)
        if hedge_tick is None or not self._quote_is_fresh(hedge_tick):
            self.log.error(
                f"hedge quote unavailable for {intent.intent_id}; source remains blocked"
            )
            return
        positions = self._hedge_positions()
        try:
            leg = self._hedges.next_hedge_leg(intent.intent_id, positions)
        except HedgePlanningError as exc:
            self.log.error(f"hedge planning blocked for {intent.intent_id}: {exc}")
            return
        side = OrderSide.BUY if leg.side is BusinessOrderSide.BUY else OrderSide.SELL
        position_id = PositionId(cast(str, leg.position_id)) if leg.is_close else None
        order = self.order_factory.market(
            instrument_id=self._config.hedge_instrument_id,
            order_side=side,
            quantity=instrument.make_qty(leg.quantity_ounces),
            time_in_force=TimeInForce.FOK,
            reduce_only=leg.is_close,
            tags=[f"py000={intent.intent_id}", f"source_trade={intent.source_trade_id}"],
        )
        self._hedges.bind_hedge_leg(intent.intent_id, order.client_order_id.value)
        try:
            self.submit_order(
                order,
                position_id=position_id,
                client_id=self._config.hedge_client_id,
                params=hedge_order_params(leg),
            )
        except Exception as exc:
            self.state_store.update_hedge_status(
                order.client_order_id.value,
                ObligationStatus.UNKNOWN,
            )
            self.log.error(f"hedge submission raised {type(exc).__name__}")
            raise

    def _hedge_positions(self) -> list[Position]:
        return cast(
            list[Position],
            self.cache.positions_open(
                instrument_id=self._config.hedge_instrument_id,
                account_id=self._config.hedge_account_id,
            ),
        )

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
        if _restart_blocked(self):
            return
        if self.state_store.knows_source_order(client_order_id):
            self.state_store.update_source_status(client_order_id, status)
            return
        obligation_status = {
            "SUBMITTED": ObligationStatus.SUBMITTED,
            "ACCEPTED": ObligationStatus.ACCEPTED,
        }[status]
        self.state_store.update_hedge_status(client_order_id, obligation_status)

    def _finish_or_reject(self, client_order_id: str, status: str) -> None:
        if _restart_blocked(self):
            return
        if self.state_store.knows_source_order(client_order_id):
            self.state_store.update_source_status(client_order_id, status)
        else:
            self.state_store.update_hedge_status(client_order_id, ObligationStatus.REJECTED)

    def _inputs_are_fresh(
        self,
        source_ts_ns: int,
        hedge_tick: QuoteTick,
        now_ns: int,
    ) -> bool:
        if self._live_costs_from_adapters and (
            not self._hedge_instrument_valid or not mt5_instrument_is_fresh(
                self._required_hedge_instrument(), now_ns, self._config.max_cost_age_ns,
            )
        ):
            return False
        if self._one_shot and (not self._one_shot_armed or self._one_shot_claimed):
            return False
        if self._live_submission_ready is not None and not self._live_submission_ready():
            return False
        if not self._cost_snapshot_valid:
            return False
        if self._cost_ts_ns > now_ns:
            self._invalidate_cost_snapshot()
            return False
        return market_inputs_are_fresh(
            now_ns=now_ns,
            source_ts_ns=source_ts_ns,
            hedge_ts_ns=hedge_tick.ts_event,
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

    def _required_source_instrument(self) -> Instrument:
        if self._source_instrument is None:
            raise RuntimeError("source instrument unavailable before strategy start")
        return self._source_instrument

    def _required_hedge_instrument(self) -> Instrument:
        if self._hedge_instrument is None:
            raise RuntimeError("hedge instrument unavailable before strategy start")
        return self._hedge_instrument


def _restart_blocked(strategy: object) -> bool:
    gate = getattr(strategy, "_restart_gate", None)
    return gate is not None and bool(gate())


def _book_top(tick: QuoteTick) -> BookTop:
    return BookTop(
        bid=Decimal(str(tick.bid_price)),
        ask=Decimal(str(tick.ask_price)),
        bid_size=Decimal(str(tick.bid_size)),
        ask_size=Decimal(str(tick.ask_size)),
    )


def _reference_book(book: OrderBook, quantity: Decimal) -> BookTop | None:
    if quantity <= 0:
        raise ValueError("base book quantity must be positive")
    bid = _reference_level(book.bids(), quantity)
    ask = _reference_level(book.asks(), quantity)
    if bid is None or ask is None:
        return None
    return BookTop(bid=bid[0], ask=ask[0], bid_size=bid[1], ask_size=ask[1])


def _reference_level(
    levels: list[BookLevel],
    quantity: Decimal,
) -> tuple[Decimal, Decimal] | None:
    cumulative = Decimal(0)
    for level in levels[:25]:
        cumulative += sum(
            (order.size.as_decimal() for order in level.orders()),
            start=Decimal(0),
        )
        if cumulative >= quantity:
            return (
                level.price.as_decimal(),
                cumulative.to_integral_value(rounding=ROUND_FLOOR),
            )
    return None
