"""One bounded, paper-only Bitfinex adapter canary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal, Protocol, TextIO, cast

from nautilus_trader.config import RoutingConfig, TradingNodeConfig
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderCancelRejected,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderRejected,
)
from nautilus_trader.model.identifiers import AccountId, ClientId, ClientOrderId, VenueOrderId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.orders import Order
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from py000_nautilus.account_lock import lock_bitfinex_account
from py000_nautilus.bitfinex_v1_cids import BitfinexV1CidStore
from py000_nautilus.bitfinex_v1_data import (
    INSTRUMENT_ID,
    PAPER_RAW_SYMBOL,
    BitfinexV1DataClientConfig,
    BitfinexV1LiveDataClientFactory,
)
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1LiveExecClientFactory,
)
from py000_nautilus.bitfinex_v1_protocol import (
    POST_ONLY_FLAG,
    OrderSnapshot,
    OrderState,
    TradeUpdate,
    parse_private_message,
)
from py000_nautilus.bitfinex_v1_rest import BitfinexV1RestClient

PUBLIC_WS_URL = "wss://api-pub.bitfinex.com/ws/2"
PRIVATE_WS_URL = "wss://api.bitfinex.com/ws/2"
REST_URL = "https://api.bitfinex.com"
WALLET_CURRENCY = "TESTUSDTF0"
CLIENT_NAME = "BITFINEX"
CLIENT_ID = ClientId(CLIENT_NAME)
MIN_QUANTITY = Decimal("2")
PRICE_INCREMENT = Decimal("0.1")
MAX_QUOTE_AGE_NS = 1_000_000_000
DEFAULT_FIRST_CHECKSUM_TIMEOUT = 60.0
type Outcome = Literal["READY", "HOLD", "PASSED", "FAILED", "UNKNOWN", "FILLED_HOLD"]
type PostOnlyAssurance = Literal[
    "VENUE_FLAG_OBSERVED",
    "SUBMITTED_INTENT_ONLY",
    "UNPROVEN",
]
type Record = dict[str, object]


class PaperCanaryError(RuntimeError):
    pass


class _Rest(Protocol):
    async def user_info(self) -> object: ...
    async def permissions(self) -> object: ...
    async def wallets(self) -> object: ...
    async def active_orders_by_symbol(self, symbol: str) -> object: ...
    async def positions(self) -> object: ...
    async def order_history_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2_500,
    ) -> object: ...
    async def trades_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2_500,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class PaperSnapshot:
    user_id: int
    balance: Decimal
    available: Decimal
    active_orders: int
    position_quantity: Decimal
    permissions_ok: bool
    withdrawal_disabled: bool
    paper_enabled: bool

    @property
    def holds(self) -> tuple[str, ...]:
        checks = (
            (not self.paper_enabled, "account_is_not_paper"),
            (not self.permissions_ok, "api_permissions_are_insufficient"),
            (not self.withdrawal_disabled, "api_key_has_withdrawal_permission"),
            (self.active_orders != 0, "target_has_preexisting_active_orders"),
            (self.position_quantity != 0, "target_has_preexisting_position"),
            (self.available <= 0, "target_wallet_has_no_available_balance"),
        )
        return tuple(reason for blocked, reason in checks if blocked)

    def record(self) -> Record:
        return {
            "paper_enabled": self.paper_enabled,
            "user_id": self.user_id,
            "permissions_ok": self.permissions_ok,
            "withdrawal_disabled": self.withdrawal_disabled,
            "wallet_currency": WALLET_CURRENCY,
            "balance": format(self.balance, "f"),
            "available": format(self.available, "f"),
            "active_orders": self.active_orders,
            "position_quantity": format(self.position_quantity, "f"),
            "holds": list(self.holds),
        }


@dataclass(frozen=True, slots=True)
class OwnedOrderEvidence:
    active_venue_ids: tuple[int, ...]
    historical_venue_ids: tuple[int, ...]
    historical_statuses: tuple[str, ...]
    trade_ids: tuple[int, ...]
    fill_indicated: bool
    complete: bool
    error: str | None
    terminal_lifecycle_exact: bool
    historical_flags: tuple[int, ...] = ()
    submitted_intent_fallback_used: bool = False
    post_only_assurance: PostOnlyAssurance = "UNPROVEN"

    def record(self) -> Record:
        return {
            "active_venue_ids": list(self.active_venue_ids),
            "historical_venue_ids": list(self.historical_venue_ids),
            "historical_statuses": list(self.historical_statuses),
            "trade_ids": list(self.trade_ids),
            "fill_indicated": self.fill_indicated,
            "complete": self.complete,
            "error": self.error,
            "terminal_lifecycle_exact": self.terminal_lifecycle_exact,
            "historical_flags": list(self.historical_flags),
            "submitted_intent_fallback_used": self.submitted_intent_fallback_used,
            "post_only_assurance": self.post_only_assurance,
        }


class PaperCanaryStrategyConfig(StrategyConfig, frozen=True):
    max_quote_age_ns: int = MAX_QUOTE_AGE_NS


class PaperCanaryStrategy(Strategy):
    """Wait for explicit arming, submit once, then cancel on acceptance."""

    def __init__(self, config: PaperCanaryStrategyConfig) -> None:
        super().__init__(config)
        self._config = config
        self._instrument: Instrument | None = None
        self._quote: QuoteTick | None = None
        self._available: Decimal | None = None
        self._order: Order | None = None
        self._armed = False
        self._cancel_sent = False
        self._quote_subscribed = False
        self.started = asyncio.Event()
        self.quote_ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.outcome: Literal["PENDING", "CANCELED", "FAILED", "UNKNOWN", "FILLED_HOLD"] = (
            "PENDING"
        )
        self.reason = "not_started"
        self.venue_order_id: VenueOrderId | None = None
        self.same_run_ownership_venue_order_id: VenueOrderId | None = None
        self.filled = Decimal()

    @property
    def order(self) -> Order | None:
        return self._order

    @property
    def submitted(self) -> bool:
        return self._order is not None

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(INSTRUMENT_ID)
        if self._instrument is None or self._instrument.raw_symbol.value != PAPER_RAW_SYMBOL:
            self._finish("FAILED", "paper_instrument_is_unavailable")
            self.stop()
            return
        self.reason = "awaiting_pre_arm_snapshot"
        self.started.set()

    def on_stop(self) -> None:
        if self._quote_subscribed:
            self.unsubscribe_quote_ticks(INSTRUMENT_ID, client_id=CLIENT_ID)

    def start_quote_subscription(self) -> None:
        if self._quote_subscribed:
            raise PaperCanaryError("paper canary quote subscription already started")
        self.subscribe_quote_ticks(INSTRUMENT_ID, client_id=CLIENT_ID)
        self._quote_subscribed = True
        self.reason = "waiting_for_actionable_quote"

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if tick.instrument_id != INSTRUMENT_ID:
            return
        self._quote = tick
        self.quote_ready.set()
        self._try_submit()

    def arm(self, available: Decimal) -> None:
        if self._armed or self.submitted:
            raise PaperCanaryError("paper canary can be armed only once")
        if not available.is_finite() or available <= 0:
            raise PaperCanaryError("paper canary requires positive available balance")
        self._available = available
        self._armed = True
        self.reason = "armed"
        self._try_submit()

    def planned_price(self) -> Decimal:
        if self._quote is None or self._instrument is None:
            raise PaperCanaryError("a current actionable quote is required")
        age = cast(int, self.clock.timestamp_ns()) - self._quote.ts_init
        if age < 0 or age > self._config.max_quote_age_ns:
            raise PaperCanaryError("the actionable quote is stale")
        bid, ask = self._quote.bid_price.as_decimal(), self._quote.ask_price.as_decimal()
        tick = self._instrument.price_increment.as_decimal()
        price = ((bid * Decimal("0.95")) / tick).to_integral_value(ROUND_FLOOR) * tick
        if price <= 0 or not price < bid < ask:
            raise PaperCanaryError("the passive canary price is invalid")
        return cast(Decimal, price)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        if not self._owns(event.client_order_id):
            return
        if self._cancel_sent or self._order is None:
            self._finish("UNKNOWN", "duplicate_or_unbound_acceptance")
            return
        self.venue_order_id = event.venue_order_id
        if (
            self._order.order_type == OrderType.LIMIT
            and self._order.time_in_force == TimeInForce.GTC
            and cast(bool, self._order.is_post_only)
        ):
            self.same_run_ownership_venue_order_id = event.venue_order_id
        self._cancel_sent = True
        self.reason = "cancel_sent"
        try:
            self.cancel_order(self._order, client_id=CLIENT_ID)
        except Exception as exc:
            self._finish("UNKNOWN", f"cancel_send_{type(exc).__name__}")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        if self._owns(event.client_order_id):
            if self.filled:
                self._finish("FILLED_HOLD", "partial_fill_then_canceled")
            else:
                outcome: Literal["CANCELED", "UNKNOWN"] = (
                    "CANCELED" if self._cancel_sent else "UNKNOWN"
                )
                self._finish(outcome, "accepted_then_canceled")

    def on_order_filled(self, event: OrderFilled) -> None:
        if self._owns(event.client_order_id):
            self.filled += event.last_qty.as_decimal()
            self.outcome, self.reason = "FILLED_HOLD", "canary_received_a_fill"
            if self._order is not None and self._order.is_closed:
                self.finished.set()

    def on_order_denied(self, event: OrderDenied) -> None:
        if self._owns(event.client_order_id):
            self._finish("FAILED", "order_denied")

    def on_order_rejected(self, event: OrderRejected) -> None:
        if self._owns(event.client_order_id):
            outcome: Literal["FAILED", "UNKNOWN"] = (
                "UNKNOWN" if "UNKNOWN" in event.reason.upper() else "FAILED"
            )
            self._finish(outcome, "order_rejected")

    def on_order_expired(self, event: OrderExpired) -> None:
        if self._owns(event.client_order_id):
            self._finish("UNKNOWN", "unexpected_order_expiry")

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        if self._owns(event.client_order_id):
            self._finish("UNKNOWN", "cancel_rejected")

    def mark_timeout(self) -> None:
        if self.outcome == "FILLED_HOLD":
            self.reason = "fill_then_terminal_timeout"
            self.finished.set()
        else:
            self._finish("UNKNOWN" if self.submitted else "FAILED", "canary_timeout")

    def _try_submit(self) -> None:
        if not self._armed or self.submitted or self._quote is None:
            return
        try:
            if self._instrument is None or self._available is None:
                raise PaperCanaryError("paper canary is not initialized")
            price = self.planned_price()
            if self._available < MIN_QUANTITY * price * Decimal("1.01"):
                raise PaperCanaryError("available balance cannot cover the 1x canary")
            self._order = self.order_factory.limit(
                instrument_id=INSTRUMENT_ID,
                order_side=OrderSide.BUY,
                quantity=self._instrument.make_qty(MIN_QUANTITY),
                price=self._instrument.make_price(price),
                time_in_force=TimeInForce.GTC,
                post_only=True,
                tags=["py000=bitfinex-paper-canary", "one_shot=true"],
            )
            self.reason = "submit_sent"
            self.submit_order(self._order, client_id=CLIENT_ID, params={"leverage": 1})
        except PaperCanaryError as exc:
            self._finish("FAILED", str(exc))
        except Exception as exc:
            self._finish("UNKNOWN", f"submit_send_{type(exc).__name__}")

    def _owns(self, client_order_id: ClientOrderId) -> bool:
        return self._order is not None and client_order_id == self._order.client_order_id

    def _finish(
        self,
        outcome: Literal["CANCELED", "FAILED", "UNKNOWN", "FILLED_HOLD"],
        reason: str,
    ) -> None:
        if self.outcome == "FILLED_HOLD" and outcome != "FILLED_HOLD":
            self.finished.set()
            return
        if self.finished.is_set():
            if outcome == "FILLED_HOLD":
                self.outcome, self.reason = outcome, reason
            return
        self.outcome, self.reason = outcome, reason
        self.finished.set()


async def read_snapshot(rest: _Rest) -> PaperSnapshot:
    info = _row(await rest.user_info(), "user info", 22)
    permissions = _permission_map(await rest.permissions())
    wallets = _rows(await rest.wallets(), "wallets")
    orders = _rows(await rest.active_orders_by_symbol(PAPER_RAW_SYMBOL), "active orders")
    positions = _rows(await rest.positions(), "positions")
    user_id, paper = _positive_int(info[0], "user ID"), _flag(info[21], "paper flag")
    required = (
        ("account", 0), ("history", 0), ("orders", 0), ("orders", 1),
        ("positions", 0), ("wallets", 0),
    )
    permission_ok = all(permissions.get(scope, (0, 0))[index] for scope, index in required)
    target_wallets = [
        row for value in wallets
        if (row := _row(value, "wallet", 5))[0:2] == ["margin", WALLET_CURRENCY]
    ]
    if len(target_wallets) != 1:
        raise PaperCanaryError("expected one TESTUSDTF0 margin wallet")
    for value in orders:
        if _row(value, "active order", 18)[3] != PAPER_RAW_SYMBOL:
            raise PaperCanaryError("active order response contains the wrong symbol")
    target_positions = [
        row for value in positions
        if (row := _row(value, "position", 1))[0] == PAPER_RAW_SYMBOL
    ]
    if len(target_positions) > 1:
        raise PaperCanaryError("paper target has multiple NETTING positions")
    position = (
        _decimal(_row(target_positions[0], "target position", 16)[2], "position")
        if target_positions else Decimal()
    )
    return PaperSnapshot(
        user_id=user_id,
        balance=_decimal(target_wallets[0][2], "wallet balance"),
        available=_decimal(target_wallets[0][4], "available wallet balance"),
        active_orders=len(orders),
        position_quantity=position,
        permissions_ok=permission_ok,
        withdrawal_disabled=permissions.get("withdraw", (0, 0))[1] == 0,
        paper_enabled=paper == 1,
    )


async def read_owned_evidence(
    rest: _Rest,
    *,
    cid: int,
    venue_order_id: int | None,
    price: Decimal,
    start_ms: int,
    same_run_ownership_venue_order_id: int | None = None,
    post_only_intent_submitted: bool = False,
) -> OwnedOrderEvidence:
    active = parse_private_message(
        [0, "os", _rows(await rest.active_orders_by_symbol(PAPER_RAW_SYMBOL), "active orders")]
    )
    if not isinstance(active, OrderSnapshot):
        raise PaperCanaryError("active-order evidence parser returned the wrong event")
    active_owned = tuple(order for order in active.orders if order.client_order_id == cid)
    try:
        history = parse_private_message(
            [
                0,
                "os",
                _rows(
                    await rest.order_history_by_symbol(PAPER_RAW_SYMBOL, start=start_ms),
                    "order history",
                ),
            ]
        )
        if not isinstance(history, OrderSnapshot):
            raise PaperCanaryError("order-history evidence parser returned the wrong event")
    except Exception as exc:
        return _incomplete_owned_evidence(
            active_owned,
            (),
            error=f"order_history_{type(exc).__name__}",
        )
    historical = tuple(order for order in history.orders if order.client_order_id == cid)
    trade_ids: list[int] = []
    try:
        for row in _rows(
            await rest.trades_by_symbol(PAPER_RAW_SYMBOL, start=start_ms), "trade history"
        ):
            trade = parse_private_message([0, "tu", row])
            if not isinstance(trade, TradeUpdate):
                raise PaperCanaryError("trade evidence parser returned the wrong event")
            if trade.client_order_id == cid or (
                venue_order_id is not None and trade.venue_order_id == venue_order_id
            ):
                trade_ids.append(trade.trade_id)
    except Exception as exc:
        return _incomplete_owned_evidence(
            active_owned,
            historical,
            error=f"trade_history_{type(exc).__name__}",
        )
    exact = len(historical) == 1
    zero_flag_ownership_witness = False
    post_only_assurance: PostOnlyAssurance = "UNPROVEN"
    if exact:
        order = historical[0]
        post_only_exact = post_only_intent_submitted and order.effective_flags == POST_ONLY_FLAG
        if (
            order.effective_flags == 0 and order.post_only_meta is None
            and post_only_intent_submitted
            and same_run_ownership_venue_order_id is not None
            and same_run_ownership_venue_order_id == venue_order_id
            and order.venue_order_id == same_run_ownership_venue_order_id
        ):
            # Paper order events and REST rows may omit the submitted flag. The
            # same-run acceptance proves identity, not venue-enforced post-only.
            post_only_exact = True
            zero_flag_ownership_witness = True
        exact = (
            venue_order_id is not None
            and order.venue_order_id == venue_order_id
            and order.symbol == PAPER_RAW_SYMBOL
            and order.order_type == "LIMIT"
            and order.tif_expiry_ms is None
            and post_only_exact
            and order.original_qty == MIN_QUANTITY
            and order.remaining_qty == MIN_QUANTITY
            and order.price == price
            and order.average_price == 0
            and order.status.upper().startswith("CANCELED")
        )
        if exact:
            post_only_assurance = (
                "SUBMITTED_INTENT_ONLY"
                if zero_flag_ownership_witness
                else "VENUE_FLAG_OBSERVED"
            )
    return OwnedOrderEvidence(
        active_venue_ids=tuple(order.venue_order_id for order in active_owned),
        historical_venue_ids=tuple(order.venue_order_id for order in historical),
        historical_statuses=tuple(order.status for order in historical),
        trade_ids=tuple(trade_ids),
        fill_indicated=any(_order_indicates_fill(order) for order in (*active_owned, *historical)),
        complete=True,
        error=None,
        terminal_lifecycle_exact=exact,
        historical_flags=tuple(order.flags for order in historical),
        submitted_intent_fallback_used=exact and zero_flag_ownership_witness,
        post_only_assurance=post_only_assurance,
    )


def _incomplete_owned_evidence(
    active: tuple[OrderState, ...],
    historical: tuple[OrderState, ...],
    *,
    error: str,
) -> OwnedOrderEvidence:
    return OwnedOrderEvidence(
        active_venue_ids=tuple(order.venue_order_id for order in active),
        historical_venue_ids=tuple(order.venue_order_id for order in historical),
        historical_statuses=tuple(order.status for order in historical),
        trade_ids=(),
        fill_indicated=any(_order_indicates_fill(order) for order in (*active, *historical)),
        complete=False,
        error=error,
        terminal_lifecycle_exact=False,
        historical_flags=tuple(order.flags for order in historical),
    )


def _order_indicates_fill(order: OrderState) -> bool:
    return order.remaining_qty != order.original_qty or order.average_price != 0


def paper_data_config() -> BitfinexV1DataClientConfig:
    return BitfinexV1DataClientConfig(
        url=PUBLIC_WS_URL, instrument_id=INSTRUMENT_ID, raw_symbol=PAPER_RAW_SYMBOL,
        price_precision=1, size_precision=8, price_increment=PRICE_INCREMENT,
        size_increment=Decimal("0.00000001"), min_quantity=MIN_QUANTITY,
        max_quantity=Decimal("10000"), margin_init=Decimal("0.01"),
        margin_maint=Decimal("0.005"), maker_fee=Decimal(0), taker_fee=Decimal("0.0002"),
        routing=RoutingConfig(default=False, venues=frozenset({CLIENT_NAME})),
    )


def build_paper_node(
    key: str,
    secret: str,
    user_id: int,
    cid_path: Path,
    loop: asyncio.AbstractEventLoop,
    timeout: float,
) -> tuple[TradingNode, PaperCanaryStrategy]:
    if not 1 <= timeout <= 60:
        raise ValueError("timeout must be in [1, 60]")
    account_id = _account_id(user_id)
    execution = BitfinexV1ExecClientConfig(
        url=PRIVATE_WS_URL, rest_url=REST_URL, api_key=key, api_secret=secret,
        user_id=user_id, account_id=account_id, instrument_id=INSTRUMENT_ID,
        raw_symbol=PAPER_RAW_SYMBOL, wallet_currency=WALLET_CURRENCY,
        cid_store_path=str(cid_path), auth_timeout_ms=int(timeout * 1_000),
        open_timeout_ms=int(timeout * 1_000), mutation_ack_timeout_ms=int(timeout * 1_000),
        rest_timeout_secs=min(5, max(1, int(timeout / 3))),
        routing=RoutingConfig(default=False, venues=frozenset({CLIENT_NAME})),
    )
    node = TradingNode(
        TradingNodeConfig(
            trader_id="PY000-BFX-PAPER-001",
            data_clients={CLIENT_NAME: paper_data_config()}, exec_clients={CLIENT_NAME: execution},
            exec_engine=LiveExecEngineConfig(reconciliation=True), timeout_connection=timeout,
            timeout_reconciliation=timeout, timeout_portfolio=timeout,
            timeout_disconnection=5.0, timeout_post_stop=0.1, timeout_shutdown=5.0,
        ),
        loop=loop,
    )
    strategy = PaperCanaryStrategy(PaperCanaryStrategyConfig())
    try:
        node.add_data_client_factory(CLIENT_NAME, BitfinexV1LiveDataClientFactory)
        node.add_exec_client_factory(CLIENT_NAME, BitfinexV1LiveExecClientFactory)
        node.build()
        node.trader.add_strategy(strategy)
    except BaseException:
        _dispose(node)
        raise
    return node, strategy


@dataclass(frozen=True, slots=True)
class PaperCanaryResult:
    outcome: Outcome
    reason: str
    transcript_path: Path


def run_paper_canary(
    key: str,
    secret: str,
    output: Path,
    cid_path: Path,
    *,
    execute: bool,
    expected_user_id: int | None = None,
    timeout: float = 10.0,
    quote_timeout: float = DEFAULT_FIRST_CHECKSUM_TIMEOUT,
) -> PaperCanaryResult:
    """Default to REST-only preflight; ``execute`` grants exactly one submit and cancel."""
    if not key or not secret or not 1 <= timeout <= 60:
        raise ValueError("credentials and timeout in [1, 60] are required")
    if not 1 <= quote_timeout <= 60:
        raise ValueError("quote_timeout in [1, 60] is required")
    if execute and (type(expected_user_id) is not int or expected_user_id <= 0):
        raise ValueError("execute requires a positive expected_user_id")
    transcript = _new_transcript(output)
    loop = asyncio.new_event_loop()
    node: TradingNode | None = None
    strategy: PaperCanaryStrategy | None = None
    lock: TextIO | None = None
    final: PaperSnapshot | None = None
    evidence: OwnedOrderEvidence | None = None
    recovery_attempted = False
    started_ms = time.time_ns() // 1_000_000
    rest = BitfinexV1RestClient(api_key=key, api_secret=secret, timeout_secs=int(timeout))

    def finish(outcome: Outcome, reason: str) -> PaperCanaryResult:
        nonlocal node
        if node is not None:
            try:
                _dispose(node)
            except Exception as exc:
                _write(transcript, {
                    "kind": "cleanup_error",
                    "error": type(exc).__name__,
                    "message": str(exc)[:300],
                })
                if strategy is not None and _has_fill_or_position(strategy, final, evidence):
                    outcome = "FILLED_HOLD"
                    reason = _manual_intervention_reason(strategy, final, evidence, reason)
                elif strategy is not None and strategy.submitted:
                    outcome = "UNKNOWN"
                    reason = _manual_intervention_reason(
                        strategy, final, evidence, "node_cleanup_failed"
                    )
                else:
                    outcome, reason = "FAILED", "node_cleanup_failed"
            finally:
                node = None
        return _finish(transcript, output, outcome, reason)

    try:
        _write(transcript, {
            "kind": "started", "execute": execute, "symbol": PAPER_RAW_SYMBOL,
            "quantity": "2", "leverage": 1, "expected_user_id": expected_user_id,
            "quote_timeout_seconds": quote_timeout,
            "ts_utc_ns": time.time_ns(),
        })
        if execute:
            try:
                assert expected_user_id is not None
                lock = _lock_canary(expected_user_id)
            except PaperCanaryError:
                return finish("HOLD", "paper_canary_is_already_running")
        first = loop.run_until_complete(read_snapshot(rest))
        _write(transcript, {"kind": "preflight", "phase": 1, **first.record()})
        if expected_user_id is not None and first.user_id != expected_user_id:
            return finish("HOLD", "paper_account_identity_mismatch")
        if first.holds:
            return finish("HOLD", first.holds[0])
        if not execute:
            return finish("READY", "paper_preflight_is_clean")
        if cid_path.exists():
            return finish("HOLD", "cid_store_requires_operator_review")
        assert expected_user_id is not None
        node, strategy = build_paper_node(
            key, secret, expected_user_id, cid_path, loop, timeout
        )
        second, reconciled = loop.run_until_complete(
            _execute(
                node,
                strategy,
                rest,
                expected_user_id,
                timeout,
                quote_timeout=quote_timeout,
            )
        )
        _write(transcript, {"kind": "preflight", "phase": 2, **second.record()})
        if second.holds:
            return finish("HOLD", second.holds[0])
        binding = None
        if strategy.order is not None:
            binding = BitfinexV1CidStore(
                cid_path, account_id=_account_id(expected_user_id).value
            ).binding_for_client(
                strategy.order.client_order_id.value
            )
        _write(transcript, {
            "kind": "mutation", "outcome": strategy.outcome, "reason": strategy.reason,
            "client_order_id": strategy.order.client_order_id.value if strategy.order else None,
            "cid": binding.cid if binding else None,
            "venue_order_id": strategy.venue_order_id.value if strategy.venue_order_id else None,
            "same_run_ownership_venue_order_id": (
                strategy.same_run_ownership_venue_order_id.value
                if strategy.same_run_ownership_venue_order_id
                else None
            ),
            "post_only_intent_submitted": bool(
                strategy.order is not None and strategy.order.is_post_only
            ),
            "filled": format(strategy.filled, "f"), "reconciled": reconciled,
        })
        if strategy.submitted:
            recovery_attempted = True
            final, evidence = _post_mutation_evidence(
                loop, rest, strategy, binding.cid if binding else None, started_ms, transcript
            )
        if _has_fill_or_position(strategy, final, evidence):
            reason = _manual_intervention_reason(
                strategy, final, evidence, "canary_received_a_fill"
            )
            return finish("FILLED_HOLD", reason)
        if strategy.outcome != "CANCELED":
            reason = _manual_intervention_reason(strategy, final, evidence, strategy.reason)
            outcome = cast(Outcome, strategy.outcome)
            if outcome != "FILLED_HOLD" and _has_active_order(final, evidence):
                outcome = "UNKNOWN"
            return finish(outcome, reason)
        if (
            not reconciled
            or binding is None
            or strategy.venue_order_id is None
            or final is None
            or final.user_id != expected_user_id
            or final.holds
            or evidence is None
            or not evidence.complete
            or evidence.active_venue_ids
            or evidence.trade_ids
            or not evidence.terminal_lifecycle_exact
        ):
            return finish("UNKNOWN", "final_reconciliation_is_not_clean")
        return finish("PASSED", "one_order_canary_reconciled")
    except Exception as exc:
        outcome = (
            "FILLED_HOLD"
            if strategy is not None and strategy.outcome == "FILLED_HOLD"
            else "UNKNOWN"
            if strategy is not None and strategy.submitted
            else "FAILED"
        )
        failure_reason = type(exc).__name__
        _write(transcript, {
            "kind": "error", "error": type(exc).__name__, "message": str(exc)[:300],
            "mutation_sent": bool(strategy and strategy.submitted),
        })
        if strategy and strategy.submitted and not recovery_attempted:
            recovery_attempted = True
            binding = None
            if strategy.order is not None and cid_path.exists() and expected_user_id is not None:
                with suppress(Exception):
                    binding = BitfinexV1CidStore(
                        cid_path, account_id=_account_id(expected_user_id).value
                    ).binding_for_client(strategy.order.client_order_id.value)
            try:
                final, evidence = _post_mutation_evidence(
                    loop, rest, strategy, binding.cid if binding else None, started_ms, transcript
                )
                failure_reason = _manual_intervention_reason(
                    strategy, final, evidence, failure_reason
                )
            except Exception as recovery_exc:
                _write(transcript, {
                    "kind": "recovery_error", "error": type(recovery_exc).__name__,
                    "message": str(recovery_exc)[:300],
                })
        if strategy is not None:
            if _has_fill_or_position(strategy, final, evidence):
                outcome = "FILLED_HOLD"
            failure_reason = _manual_intervention_reason(
                strategy, final, evidence, failure_reason
            )
        return finish(outcome, failure_reason)
    finally:
        try:
            if node is not None:
                _dispose(node)
            elif not loop.is_closed():
                loop.close()
        finally:
            if lock is not None:
                lock.close()
            transcript.close()


async def _execute(
    node: TradingNode,
    strategy: PaperCanaryStrategy,
    rest: _Rest,
    user_id: int,
    timeout: float,
    *,
    quote_timeout: float = DEFAULT_FIRST_CHECKSUM_TIMEOUT,
) -> tuple[PaperSnapshot, bool]:
    run_task = asyncio.create_task(node.run_async())
    try:
        await asyncio.wait_for(strategy.started.wait(), timeout)
        second = await _await_pre_arm_snapshot(
            node,
            strategy,
            rest,
            user_id,
            timeout,
            quote_timeout=quote_timeout,
        )
        if second.holds:
            return second, False
        strategy.arm(second.available)
        with suppress(TimeoutError):
            await asyncio.wait_for(strategy.finished.wait(), timeout * 2)
        if not strategy.finished.is_set():
            strategy.mark_timeout()
        reconciled = (
            strategy.outcome == "CANCELED"
            and await node.kernel.exec_engine.reconcile_execution_state(timeout_secs=timeout)
        )
        return second, reconciled
    finally:
        stop_error: BaseException | None = None
        if node.is_running():
            try:
                await asyncio.wait_for(node.stop_async(), 6.0)
            except BaseException as exc:
                stop_error = exc
        if not run_task.done():
            await asyncio.sleep(0)
        if not run_task.done():
            run_task.cancel()
        run_result = (await asyncio.gather(run_task, return_exceptions=True))[0]
        if isinstance(run_result, Exception) and stop_error is None:
            stop_error = run_result
        if stop_error is not None:
            raise PaperCanaryError("TradingNode shutdown failed") from stop_error


async def _await_pre_arm_snapshot(
    node: TradingNode,
    strategy: PaperCanaryStrategy,
    rest: _Rest,
    user_id: int,
    timeout: float,
    *,
    quote_timeout: float = DEFAULT_FIRST_CHECKSUM_TIMEOUT,
) -> PaperSnapshot:
    if not _paper_node_connected(node):
        raise PaperCanaryError("Nautilus clients disconnected before arming")
    try:
        snapshot = await asyncio.wait_for(read_snapshot(rest), timeout)
    except TimeoutError:
        raise PaperCanaryError("paper pre-arm snapshot timed out") from None
    if snapshot.user_id != user_id:
        raise PaperCanaryError("paper account identity changed")
    if snapshot.holds:
        return snapshot
    if not _paper_node_connected(node):
        raise PaperCanaryError("Nautilus clients disconnected before arming")
    strategy.start_quote_subscription()
    await _await_actionable_quote(strategy, quote_timeout)
    if not _paper_node_connected(node):
        raise PaperCanaryError("Nautilus clients disconnected before arming")
    strategy.planned_price()
    return snapshot


def _paper_node_connected(node: TradingNode) -> bool:
    return (
        node.is_running()
        and node.kernel.data_engine.check_connected()
        and node.kernel.exec_engine.check_connected()
    )


async def _await_actionable_quote(
    strategy: PaperCanaryStrategy,
    timeout: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        strategy.quote_ready.clear()
        try:
            strategy.planned_price()
            return
        except PaperCanaryError:
            pass
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(strategy.quote_ready.wait(), remaining)
        except TimeoutError:
            break
    raise PaperCanaryError("a fresh actionable quote did not arrive")


def _post_mutation_evidence(
    loop: asyncio.AbstractEventLoop,
    rest: _Rest,
    strategy: PaperCanaryStrategy,
    cid: int | None,
    started_ms: int,
    transcript: TextIO,
) -> tuple[PaperSnapshot, OwnedOrderEvidence | None]:
    final = loop.run_until_complete(read_snapshot(rest))
    _write(transcript, {"kind": "preflight", "phase": 3, **final.record()})
    evidence = None
    if cid is not None and strategy.order is not None:
        venue_id = int(strategy.venue_order_id.value) if strategy.venue_order_id else None
        post_only_intent_submitted = (
            strategy.order.order_type == OrderType.LIMIT
            and strategy.order.time_in_force == TimeInForce.GTC
            and cast(bool, strategy.order.is_post_only)
        )
        ownership_venue_id = (
            int(strategy.same_run_ownership_venue_order_id.value)
            if post_only_intent_submitted
            and strategy.same_run_ownership_venue_order_id is not None
            else None
        )
        try:
            evidence = loop.run_until_complete(
                read_owned_evidence(
                    rest,
                    cid=cid,
                    venue_order_id=venue_id,
                    price=strategy.order.price.as_decimal(),
                    start_ms=max(0, started_ms - 1_000),
                    same_run_ownership_venue_order_id=ownership_venue_id,
                    post_only_intent_submitted=post_only_intent_submitted,
                )
            )
            _write(transcript, {"kind": "owned_order_evidence", **evidence.record()})
        except Exception as exc:
            _write(transcript, {
                "kind": "owned_order_evidence_error",
                "error": type(exc).__name__,
                "message": str(exc)[:300],
            })
    return final, evidence


def _manual_intervention_reason(
    strategy: PaperCanaryStrategy,
    final: PaperSnapshot | None,
    evidence: OwnedOrderEvidence | None,
    fallback: str,
) -> str:
    has_position = _has_fill_or_position(strategy, final, evidence)
    has_active_order = _has_active_order(final, evidence)
    if has_position and has_active_order:
        return "manual_cancel_and_position_review_required"
    if has_position:
        return "manual_position_review_required"
    if has_active_order:
        return "manual_cancel_required"
    return fallback


def _has_active_order(
    final: PaperSnapshot | None,
    evidence: OwnedOrderEvidence | None,
) -> bool:
    return (final is not None and final.active_orders != 0) or (
        evidence is not None and bool(evidence.active_venue_ids)
    )


def _has_fill_or_position(
    strategy: PaperCanaryStrategy,
    final: PaperSnapshot | None,
    evidence: OwnedOrderEvidence | None,
) -> bool:
    return (
        strategy.filled != 0
        or (final is not None and final.position_quantity != 0)
        or (evidence is not None and evidence.fill_indicated)
        or (evidence is not None and bool(evidence.trade_ids))
    )


def credentials(path: Path) -> tuple[str, str, int | None]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name, value = name.strip(), value.strip()
            if name in {"BFX_TEST_API_KEY", "BFX_TEST_API_SECRET", "BFX_TEST_USER_ID"}:
                if name in values:
                    raise PaperCanaryError(f"duplicate {name}")
                values[name] = (
                    value[1:-1] if len(value) > 1 and value[0] == value[-1]
                    and value[0] in {'"', "'"} else value
                )
    key = os.environ.get("BFX_TEST_API_KEY", values.get("BFX_TEST_API_KEY", ""))
    secret = os.environ.get("BFX_TEST_API_SECRET", values.get("BFX_TEST_API_SECRET", ""))
    user_id_text = os.environ.get("BFX_TEST_USER_ID", values.get("BFX_TEST_USER_ID", ""))
    if not key or not secret:
        raise PaperCanaryError("BFX_TEST_API_KEY and BFX_TEST_API_SECRET are required")
    if user_id_text and (not user_id_text.isdigit() or int(user_id_text) <= 0):
        raise PaperCanaryError("BFX_TEST_USER_ID must be a positive integer")
    return key, secret, int(user_id_text) if user_id_text else None


def _permission_map(value: object) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for item in _rows(value, "permissions"):
        row = _row(item, "permission", 3)
        if not isinstance(row[0], str) or row[0] in result:
            raise PaperCanaryError("permission scope is invalid or duplicated")
        result[row[0]] = (_flag(row[1], "read permission"), _flag(row[2], "write permission"))
    return result


def _rows(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise PaperCanaryError(f"Bitfinex {label} response must be an array")
    return cast(list[object], value)


def _row(value: object, label: str, minimum: int) -> list[object]:
    if not isinstance(value, list) or len(value) < minimum:
        raise PaperCanaryError(f"Bitfinex {label} row is incomplete")
    return cast(list[object], value)


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise PaperCanaryError(f"Bitfinex {label} must be a positive exact integer")
    return value


def _flag(value: object, label: str) -> int:
    if type(value) is not int or value not in {0, 1}:
        raise PaperCanaryError(f"Bitfinex {label} must be 0 or 1")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise PaperCanaryError(f"Bitfinex {label} must be an exact decimal")
    result = Decimal(value)
    if not result.is_finite():
        raise PaperCanaryError(f"Bitfinex {label} must be finite")
    return result


def _account_id(user_id: int) -> AccountId:
    return AccountId(f"BITFINEX-PAPER-{_positive_int(user_id, 'user ID')}")


def _new_transcript(path: Path) -> TextIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _lock_canary(user_id: int) -> TextIO:
    try:
        return lock_bitfinex_account(user_id)
    except BlockingIOError:
        raise PaperCanaryError("another paper canary holds the account lock") from None


def _write(stream: TextIO, record: Record) -> None:
    stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


def _finish(stream: TextIO, path: Path, outcome: Outcome, reason: str) -> PaperCanaryResult:
    _write(stream, {
        "kind": "finished", "outcome": outcome, "reason": reason, "ts_utc_ns": time.time_ns(),
    })
    return PaperCanaryResult(outcome, reason, path)


def _dispose(node: TradingNode) -> None:
    try:
        if not node.kernel.loop.is_closed():
            node.kernel.cancel_all_tasks()
    finally:
        node.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Bitfinex paper adapter canary")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cid-store", type=Path, default=Path("bitfinex-paper-cids.state.json"))
    parser.add_argument("--expected-user-id", type=int)
    parser.add_argument("--execute", action="store_true", help="allow one submit and one cancel")
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="connection, REST, reconciliation, and mutation timeout",
    )
    parser.add_argument(
        "--quote-timeout",
        type=float,
        default=DEFAULT_FIRST_CHECKSUM_TIMEOUT,
        help="independent wait for the paper book's first actionable quote",
    )
    args = parser.parse_args()
    try:
        key, secret, env_user_id = credentials(args.env_file)
        if args.expected_user_id is not None and env_user_id not in {None, args.expected_user_id}:
            raise PaperCanaryError("CLI and environment user IDs differ")
        expected_user_id = args.expected_user_id or env_user_id
        result = run_paper_canary(
            key, secret, args.output, args.cid_store, execute=args.execute,
            expected_user_id=expected_user_id, timeout=args.timeout,
            quote_timeout=args.quote_timeout,
        )
    except Exception as exc:
        print(json.dumps({"outcome": "FAILED", "reason": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({"outcome": result.outcome, "reason": result.reason}))
    return {
        "READY": 0, "PASSED": 0, "HOLD": 2, "FAILED": 1, "UNKNOWN": 3, "FILLED_HOLD": 4,
    }[result.outcome]


if __name__ == "__main__":
    raise SystemExit(main())
