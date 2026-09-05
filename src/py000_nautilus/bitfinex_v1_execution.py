"""Minimal same-process Bitfinex derivative execution for the PY000 source leg."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from math import isclose
from typing import Protocol, cast

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    BatchCancelOrders,
    CancelAllOrders,
    CancelOrder,
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    ModifyOrder,
    SubmitOrder,
    SubmitOrderList,
)
from nautilus_trader.execution.reports import (
    ExecutionMassStatus,
    FillReport,
    OrderStatusReport,
    PositionStatusReport,
)
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    TradeId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import AccountBalance, Money
from nautilus_trader.model.orders import Order
from nautilus_trader.model.position import Position

from py000_nautilus.bitfinex_v1_cids import (
    BitfinexFeeMetadata,
    BitfinexFeeTrade,
    BitfinexNativeFill,
    BitfinexV1CidError,
    BitfinexV1CidStore,
    BitfinexV1FeeConflict,
)
from py000_nautilus.bitfinex_v1_data import INSTRUMENT_ID as SUPPORTED_INSTRUMENT_ID
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL
from py000_nautilus.bitfinex_v1_data import RAW_SYMBOL as PRODUCTION_RAW_SYMBOL
from py000_nautilus.bitfinex_v1_protocol import (
    MAX_AUTH_NONCE,
    POST_ONLY_FLAG,
    REDUCE_ONLY_FLAG,
    Notification,
    OrderEvent,
    OrderSnapshot,
    OrderState,
    PositionEvent,
    TradeExecution,
    TradeUpdate,
    WalletEvent,
    auth_message,
    cancel_order_op,
    parse_interim_trade_message,
    parse_private_message,
    submit_order_op,
    update_order_op,
)
from py000_nautilus.bitfinex_v1_reports import (
    BitfinexFeeSummary,
    map_fill_reports,
    map_order_status_reports,
    map_position_status_reports,
    native_fill_matches_trade,
    summarize_fees,
    usd_commission,
)
from py000_nautilus.bitfinex_v1_rest import BitfinexV1RestClient
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport

BITFINEX = Venue("BITFINEX")
_WALLET_BY_RAW_SYMBOL = {
    PRODUCTION_RAW_SYMBOL: "USTF0",
    PAPER_RAW_SYMBOL: "TESTUSDTF0",
}
_FEE_CURRENCY_BY_RAW_SYMBOL = {
    PRODUCTION_RAW_SYMBOL: "USD",
    PAPER_RAW_SYMBOL: "USD",
}
_REPORT_PAGE_LIMIT = 2_500
_MAX_REPORT_LOOKBACK_MS = 14 * 24 * 60 * 60 * 1_000
_MAX_UNBOUND_PAPER_INTERIM_FILLS = 256
_PRIVATE_FRAME_TYPES = frozenset(
    {
        "hb",
        "n",
        "oc",
        "on",
        "os",
        "ou",
        "pc",
        "pn",
        "ps",
        "pu",
        "te",
        "tu",
        "ws",
        "wu",
    }
)


class BitfinexV1ExecutionError(RuntimeError):
    """The bounded private execution stream cannot prove a safe outcome."""


class BitfinexV1AccountingError(BitfinexV1ExecutionError):
    """Final fee facts conflict without changing the already known execution."""


class BitfinexV1ExecClientConfig(LiveExecClientConfig, kw_only=True, frozen=True):
    url: str
    api_key: str
    api_secret: str
    user_id: int
    account_id: AccountId
    instrument_id: InstrumentId
    raw_symbol: str
    cid_store_path: str
    rest_url: str = "https://api.bitfinex.com"
    wallet_currency: str = "USTF0"
    fee_currency: str = "USD"
    allow_cold_position_reconciliation: bool = False
    auth_timeout_ms: int = 10_000
    open_timeout_ms: int = 10_000
    mutation_ack_timeout_ms: int = 10_000
    rest_timeout_secs: int = 10


class _PrivateTransport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def send_json(self, payload: dict[str, object] | list[object]) -> None: ...
    async def recv_json(self) -> dict[str, object] | list[object]: ...


class _ReconciliationRest(Protocol):
    async def user_info(self) -> object: ...
    async def active_orders_by_symbol(self, symbol: str) -> object: ...
    async def order_history_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = _REPORT_PAGE_LIMIT,
    ) -> object: ...
    async def trades_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = _REPORT_PAGE_LIMIT,
    ) -> object: ...
    async def positions(self) -> object: ...


@dataclass(slots=True)
class _LiveOrder:
    order: Order
    instrument: Instrument
    cid: int
    current_price: Decimal
    known_prices: set[Decimal]
    unknown_operations: set[str]
    submitted_in_process: bool = False
    venue_order_id: int | None = None
    accepted: bool = False
    filled_qty: Decimal = Decimal(0)
    pending_modify_price: Decimal | None = None
    pending_cancel: bool = False
    terminal: OrderState | None = None
    reconciled_terminal: OrderStatusReport | None = None
    reconciled_working: OrderStatusReport | None = None
    terminal_emitted: bool = False
    rejection_key: tuple[str, int | None, str] | None = None
    venue_flags_verified: bool = False
    terminal_reconciliation_pending: bool = False
    terminal_reconciliation_deadline: float | None = None


class BitfinexV1ExecutionClient(LiveExecutionClient):
    """One-account, one-instrument LIMIT execution client with bounded REST reports."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        name: str | None,
        config: BitfinexV1ExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: InstrumentProvider,
        transport: _PrivateTransport | None = None,
        rest: _ReconciliationRest | None = None,
    ) -> None:
        _validate_config(config)
        client_id = ClientId(name or "BITFINEX")
        if config.account_id.get_issuer() != client_id.value:
            raise ValueError("Bitfinex account issuer must match the execution client ID")
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=BITFINEX,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USDT,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._set_account_id(config.account_id)
        self._bfx_config = config
        self._transport: _PrivateTransport = transport or BitfinexV1Transport(
            config.url,
            open_timeout_ms=config.open_timeout_ms,
        )
        self._rest: _ReconciliationRest = rest or BitfinexV1RestClient(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.rest_url,
            timeout_secs=config.rest_timeout_secs,
        )
        self._running = False
        self._reader_task: asyncio.Task[None] | None = None
        self._account_ready = False
        self._fatal_failure: str | None = None
        self._last_failure: str | None = None
        self._last_auth_nonce = 0
        self._cid_store = BitfinexV1CidStore(
            config.cid_store_path,
            account_id=config.account_id.value,
        )
        self._by_cid: dict[int, _LiveOrder] = {}
        self._cid_by_client: dict[ClientOrderId, int] = {}
        self._cid_by_venue: dict[int, int] = {}
        self._seen_trades: dict[int, TradeUpdate] = {}
        self._paper_interim_fills: dict[int, TradeExecution] = {}
        self._unbound_paper_interim_fills: dict[int, TradeExecution] = {}
        self._ack_deadlines: dict[tuple[int, str], asyncio.TimerHandle] = {}
        self._accounting_failure: str | None = None
        # The Engine publishes this topic after applying the order event. Keep
        # observing through disconnect/drain; the subscription shares node lifetime.
        self._msgbus.subscribe("events.order.*", self._capture_native_fee_evidence)

    @property
    def accounting_incomplete(self) -> bool:
        """A local accounting fault, separate from execution and hedge readiness."""
        return (
            self._accounting_failure is not None or self._cid_store.accounting_conflict
            or not self._cid_store.fees_durable
        )

    @property
    def accounting_ready(self) -> bool:
        return not self.accounting_incomplete

    def fee_summary(self, client_order_id: ClientOrderId | None = None) -> BitfinexFeeSummary:
        bindings = self._cid_store.bindings if client_order_id is None else tuple(
            row for row in self._cid_store.bindings if row.client_order_id == client_order_id.value
        )
        metadata = tuple(
            row for binding in bindings
            if (row := self._cid_store.fee_metadata_for_cid(binding.cid)) is not None
        )
        proven_empty = sum(
            self._cid_store.fee_metadata_for_cid(binding.cid) is None
            and self._cached_zero_fill_terminal(binding.client_order_id)
            for binding in bindings
        )
        return summarize_fees(
            metadata,
            unknown_orders=len(bindings) - len(metadata) - proven_empty + int(
                client_order_id is not None and not bindings
            ),
            durable=not self.accounting_incomplete,
        )

    def _cached_zero_fill_terminal(self, client_order_id: str) -> bool:
        order = self._cache.order(ClientOrderId(client_order_id))
        return (
            order is not None and order.account_id == self.account_id
            and order.instrument_id == self._bfx_config.instrument_id
            and order.status in {OrderStatus.CANCELED, OrderStatus.REJECTED}
            and order.filled_qty.as_decimal() == 0
        )

    def _fee_scope(self, cid: int, venue_order_id: int) -> BitfinexFeeMetadata:
        return BitfinexFeeMetadata(
            cid, venue_order_id, self._bfx_config.instrument_id.value, self._bfx_config.raw_symbol,
        )

    def _record_fee_trade(self, cid: int, trade: TradeExecution | TradeUpdate) -> None:
        try:
            self._cid_store.record_venue_trade(
                self._fee_scope(cid, trade.venue_order_id), _fee_trade_evidence(trade),
            )
        except OSError as exc:
            # The in-memory observation remains dirty and can be retried by TU/REST.
            # Never turn auxiliary fee I/O into a stopped reader or a blocked hedge.
            self._log.error(f"Bitfinex fee metadata write failed: {type(exc).__name__}")
        except BitfinexV1CidError as exc:
            self._record_accounting_conflict("Bitfinex fee metadata has conflicting facts")
            if isinstance(exc, BitfinexV1FeeConflict):
                raise BitfinexV1AccountingError(str(exc)) from exc
            raise BitfinexV1ExecutionError(str(exc)) from exc

    def _record_accounting_conflict(self, reason: str) -> None:
        self._accounting_failure = reason
        try:
            self._cid_store.mark_accounting_conflict()
        except OSError as exc:
            # Preserve the semantic conflict even when its marker cannot be synced.
            # This auxiliary I/O must not stop publication or the known-fill hedge.
            self._log.error(f"Bitfinex accounting conflict write failed: {type(exc).__name__}")

    def _capture_native_fee_evidence(self, event: object) -> None:
        if not isinstance(event, OrderFilled) or (
            event.account_id != self.account_id
            or event.instrument_id != self._bfx_config.instrument_id
        ):
            return
        binding = self._cid_store.binding_for_client(event.client_order_id.value)
        if binding is None:
            return
        order = self._cache.order(event.client_order_id)
        cached = None if order is None else next(
            (fill for fill in order.events if isinstance(fill, OrderFilled)
             and fill.trade_id == event.trade_id), None,
        )
        if cached is None or OrderFilled.to_dict(cached) != OrderFilled.to_dict(event):
            self._record_accounting_conflict("native fill publication lacks applied cache evidence")
            return
        origin = event.info.get("bitfinex_fill_source", "unknown")
        if origin not in {"te_paper", "tu"}:
            # This adapter's real venue IDs are positive integers; native inferred
            # reconciliation IDs occupy a separate UUID namespace.
            origin = (
                "inferred" if event.reconciliation and not event.trade_id.value.isdecimal()
                else "unknown"
            )
        if origin == "te_paper" and (
            self._bfx_config.raw_symbol != PAPER_RAW_SYMBOL
            or event.info.get("bitfinex_fee_status") != "pending"
            or event.commission != Money(0, USD)
        ):
            self._record_accounting_conflict("invalid Paper provisional native fill evidence")
            return
        evidence = _native_fill_evidence(event, origin)
        metadata = self._cid_store.fee_metadata_for_cid(binding.cid)
        previous = None if metadata is None else next(
            (fill for fill in metadata.native_fills if fill.trade_id == event.trade_id.value), None,
        )
        if previous is not None and "bitfinex_fill_source" not in event.info:
            evidence = replace(evidence, native_fill_origin=previous.native_fill_origin)
        try:
            self._cid_store.record_native_fill(
                self._fee_scope(binding.cid, int(event.venue_order_id.value)), evidence,
            )
        except OSError as exc:
            self._log.error(f"Bitfinex native fee evidence write failed: {type(exc).__name__}")
        except (BitfinexV1CidError, ValueError) as exc:
            # A subscriber must not abort publication to the strategy/hedge subscriber.
            self._record_accounting_conflict(f"native fee evidence conflict: {type(exc).__name__}")
        live = self._by_cid.get(binding.cid)
        if live is not None and (
            live.reconciled_working is not None or live.reconciled_terminal is not None
        ):
            self._sync_reconciled_native_quantity(live)

    def _sync_reconciled_native_quantity(self, live: _LiveOrder) -> None:
        """Advance only from real, already-applied native fills; never from enqueue."""
        order = self._cache.order(live.order.client_order_id)
        metadata = self._cid_store.fee_metadata_for_cid(live.cid)
        if order is None or metadata is None:
            return
        trades = {str(row.trade_id): row for row in metadata.venue_trades}
        fills = [event for event in order.events if isinstance(event, OrderFilled)]
        quantity = sum((fill.last_qty.as_decimal() for fill in fills), Decimal())
        if quantity != order.filled_qty.as_decimal() or any(
            fill.trade_id.value not in trades or not native_fill_matches_trade(
                _native_fill_evidence(fill, "unknown"), trades[fill.trade_id.value],
            ) for fill in fills
        ):
            self._record_fatal("native Bitfinex quantity lacks exact real fills", ValueError())
            return
        # WS fills may already be queued past the native cache's current event.
        live.filled_qty = max(live.filled_qty, quantity)

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    @property
    def execution_hold_reason(self) -> str | None:
        if self._fatal_failure is not None:
            return self._fatal_failure
        unknown = [live for live in self._by_cid.values() if live.unknown_operations]
        if unknown:
            return "Bitfinex mutation has an UNKNOWN wire outcome"
        if self._unbound_paper_interim_fills:
            return "Bitfinex paper interim fill is waiting for its venue order mapping"
        if any(
            live.accepted
            and cast(bool, live.order.is_reduce_only)
            and not live.venue_flags_verified
            for live in self._by_cid.values()
        ):
            return "Bitfinex reduce-only order is waiting for exact venue flags"
        if any(
            live.terminal is not None and not live.terminal_emitted
            for live in self._by_cid.values()
        ):
            return "Bitfinex terminal order is waiting for matching execution fills"
        if any(self._silent_terminal_reconciliation_due(live) for live in self._by_cid.values()):
            return "Bitfinex paper order is waiting for authenticated terminal reconciliation"
        if any(live.reconciled_working is not None for live in self._by_cid.values()):
            return "Bitfinex working fills are waiting for native reconciliation confirmation"
        if not self._running or not self._account_ready:
            return "Bitfinex execution is not connected and account-ready"
        return None

    @property
    def terminal_reconciliation_required(self) -> bool:
        """Whether an owned terminal needs authenticated mass reconciliation."""
        return any(
            (
                live.terminal is not None
                and not live.terminal_emitted
                and _terminal_filled(live.terminal) > live.filled_qty
            )
            or (live.reconciled_terminal is not None and not live.terminal_emitted)
            or self._silent_terminal_reconciliation_due(live)
            for live in self._by_cid.values()
        )

    def confirm_terminal_reconciliation(self) -> None:
        """Retire terminals only after Nautilus applied an exact venue report delta."""
        pending = [
            live
            for live in self._by_cid.values()
            if not live.terminal_emitted
            and (live.terminal is not None or live.reconciled_terminal is not None)
        ]
        for live in pending:
            self._retire_reconciled_terminal(live, self._confirmed_terminal_quantity(live))
        for live in tuple(self._by_cid.values()):
            if live.reconciled_working is not None:
                if live.terminal is not None:
                    self._retire_reconciled_terminal(live, self._confirmed_terminal_quantity(live))
                else:
                    self._confirmed_terminal_quantity(live, working=True)
                    live.reconciled_working = None

    def _same_run_paper_maker(self, live: _LiveOrder) -> bool:
        return (
            self._bfx_config.raw_symbol == PAPER_RAW_SYMBOL and live.submitted_in_process
            and live.accepted and live.order.time_in_force == TimeInForce.GTC
            and cast(bool, live.order.is_post_only) and live.pending_modify_price is None
            and "modify" not in live.unknown_operations
        )

    def _working_observation_candidate(self, live: _LiveOrder) -> bool:
        return (
            self._same_run_paper_maker(live) and live.venue_order_id is not None
            and live.terminal is None and live.reconciled_terminal is None
            and live.rejection_key is None and not live.pending_cancel
            and not live.unknown_operations and live.order.is_open
        )

    @property
    def has_working_orders(self) -> bool:
        return any(self._working_observation_candidate(live) for live in self._by_cid.values())

    async def check_working_orders(self) -> bool:
        """Observe an active list; return a discrepancy hint, never apply reports."""
        candidates = [
            (live, live.venue_order_id, live.current_price, live.order.ts_last)
            for live in self._by_cid.values() if self._working_observation_candidate(live)
        ]
        if not candidates:
            return False
        reports = await self.generate_order_status_reports(GenerateOrderStatusReports(
            instrument_id=self._bfx_config.instrument_id, start=None, end=None, open_only=True,
            command_id=UUID4(), ts_init=self._clock.timestamp_ns(),
        ))
        reported = {report.client_order_id: report for report in reports}
        for live, venue_id, price, ts_last in candidates:
            if (
                self._by_cid.get(live.cid) is not live or live.venue_order_id != venue_id
                or self._cache.order(live.order.client_order_id) is not live.order
                or not self._working_observation_candidate(live)
            ):
                continue  # WS retired/closed the order or a mutation now owns its resolution.
            if live.current_price != price:
                return True  # An old snapshot cannot acknowledge a new modify price.
            report = reported.get(live.order.client_order_id)
            if live.order.ts_last != ts_last:
                continue  # Observe the updated native order on the next healthy cycle.
            if report is None:
                return True
            try:
                self._validate_cached_open_report(
                    live.order, report, [],
                    allow_missing_post_only=report.order_status == OrderStatus.ACCEPTED,
                )
            except BitfinexV1ExecutionError:
                return True
            if report.filled_qty != live.order.filled_qty or not report.is_open:
                return True
        return False

    def _terminal_report(self, live: _LiveOrder) -> OrderStatusReport:
        state = live.terminal
        if state is None:
            if live.reconciled_terminal is None:
                raise BitfinexV1ExecutionError("Bitfinex order has no authoritative terminal")
            return live.reconciled_terminal
        filled = _terminal_filled(state)
        return OrderStatusReport(
            account_id=self.account_id, instrument_id=live.order.instrument_id,
            client_order_id=live.order.client_order_id,
            venue_order_id=VenueOrderId(str(state.venue_order_id)),
            order_side=OrderSide.BUY if state.original_qty > 0 else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC if state.order_type == "IOC" else TimeInForce.GTC,
            order_status=(
                OrderStatus.FILLED if _terminal_disposition(state) == "executed"
                else OrderStatus.CANCELED
            ),
            quantity=live.instrument.make_qty(abs(state.original_qty)),
            filled_qty=live.instrument.make_qty(filled),
            price=live.instrument.make_price(state.price),
            avg_px=state.average_price if filled else None,
            post_only=state.flags == POST_ONLY_FLAG, reduce_only=state.flags == REDUCE_ONLY_FLAG,
            cancel_reason=state.status if _terminal_disposition(state) == "canceled" else None,
            report_id=UUID4(), ts_accepted=state.ts_created_ms * 1_000_000,
            ts_last=state.ts_updated_ms * 1_000_000, ts_init=self._clock.timestamp_ns(),
        )

    def _terminal_post_only_is_opaque(self, live: _LiveOrder) -> bool:
        # This exact streamed terminal already passed _validate_order_state while
        # the same-run pending cancel/accepted authority was still available.
        return (
            live.terminal is not None and live.terminal.flags == 0
            and self._bfx_config.raw_symbol == PAPER_RAW_SYMBOL
            and live.submitted_in_process and cast(bool, live.order.is_post_only)
        )

    def _confirmed_terminal_quantity(self, live: _LiveOrder, *, working: bool = False) -> Decimal:
        report = live.reconciled_working if working else self._terminal_report(live)
        if report is None:
            raise BitfinexV1ExecutionError("Bitfinex working order has no reconciled report")
        order = self._cache.order(live.order.client_order_id)
        reported_avg = None if report.avg_px is None else float(report.avg_px)
        cached_avg = None if order is None or order.avg_px is None else float(order.avg_px)
        # Native zero-fill canceled orders retain their initial average of 0.0.
        if order is not None and order.filled_qty.as_decimal() == 0 and cached_avg == 0:
            cached_avg = None
        if (
            order is None or order.account_id != report.account_id
            or order.instrument_id != report.instrument_id
            or order.client_order_id != report.client_order_id
            or (order.venue_order_id != report.venue_order_id
                and not self._silent_rejection_has_no_native_venue(live, order, report))
            or order.side != report.order_side
            or order.order_type != report.order_type or order.time_in_force != report.time_in_force
            or (cast(bool, order.is_post_only) != report.post_only
                and not self._terminal_post_only_is_opaque(live))
            or cast(bool, order.is_reduce_only) != report.reduce_only or order.price != report.price
            or order.quantity != report.quantity or order.filled_qty != report.filled_qty
            or (not working and (order.status != report.order_status or not order.is_closed))
            or (working and (order.is_closed or order.status not in {
                OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_CANCEL,
            }))
            or (cached_avg is None) != (reported_avg is None)
            or (cached_avg is not None and reported_avg is not None
                and not isclose(cached_avg, reported_avg))
        ):
            raise BitfinexV1ExecutionError(
                "Nautilus cache does not prove the Bitfinex terminal reconciliation"
            )
        return cast(Decimal, report.filled_qty.as_decimal())

    def _silent_rejection_has_no_native_venue(
        self, live: _LiveOrder, order: Order, report: OrderStatusReport,
    ) -> bool:
        # NT OrderRejected has no venue ID. Only the already-validated same-run
        # zero-fill REST rejection can supply that one missing native field.
        binding = self._cid_store.binding_for_cid(live.cid)
        return (
            self._can_reconcile_silent_terminal(live, "submit") and not live.accepted
            and live.reconciled_terminal is report and order.venue_order_id is None
            and order.status == report.order_status == OrderStatus.REJECTED
            and live.filled_qty == order.filled_qty.as_decimal() == 0
            and report.filled_qty.as_decimal() == 0
            and not any(isinstance(event, OrderFilled) for event in order.events)
            and binding is not None and binding.client_order_id == order.client_order_id.value
            and self._cid_store.account_id == self.account_id.value
            and self._cid_by_client.get(order.client_order_id) == live.cid
            and live.venue_order_id is not None
            and str(live.venue_order_id) == report.venue_order_id.value
            and self._cid_by_venue.get(live.venue_order_id) == live.cid
        )

    def _retire_reconciled_terminal(self, live: _LiveOrder, filled_qty: Decimal) -> None:
        live.filled_qty = filled_qty
        live.terminal_emitted = True
        live.terminal_reconciliation_pending = False
        self._resolve_all_mutations(live)
        self._by_cid.pop(live.cid, None)
        self._cid_by_client.pop(live.order.client_order_id, None)
        if live.venue_order_id is not None:
            self._cid_by_venue.pop(live.venue_order_id, None)

    async def _connect(self) -> None:
        await self._await_instrument_profile()
        if any(
            live.rejection_key is None and not live.terminal_emitted
            for live in self._by_cid.values()
        ):
            reason = "Bitfinex stream gap with unresolved orders requires reconciliation"
            self._fatal_failure = reason
            raise BitfinexV1ExecutionError(reason)
        self._fatal_failure = None
        self._account_ready = False
        try:
            await self._verify_rest_account_profile()
            await self._transport.open()
            nonce = max(
                cast(int, self._clock.timestamp_ns()) // 1_000,
                self._last_auth_nonce + 1,
            )
            if not 0 < nonce <= MAX_AUTH_NONCE:
                raise BitfinexV1ExecutionError("clock cannot produce a valid Bitfinex auth nonce")
            self._last_auth_nonce = nonce
            auth = auth_message(
                self._bfx_config.api_key,
                self._bfx_config.api_secret,
                nonce=nonce,
            )
            auth["filter"] = [
                f"trading-{self._bfx_config.raw_symbol}",
                "wallet",
                "notify",
            ]
            await self._transport.send_json(auth)
            await asyncio.wait_for(
                self._await_authenticated_wallet(),
                timeout=self._bfx_config.auth_timeout_ms / 1_000,
            )
        except BaseException:
            await self._transport.close()
            raise
        self._running = True
        self._reader_task = self.create_task(self._reader(), log_msg="bitfinex-v1-private")

    async def _await_instrument_profile(self) -> None:
        deadline = self._loop.time() + self._bfx_config.auth_timeout_ms / 1_000
        while self._instrument(self._bfx_config.instrument_id) is None:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise BitfinexV1ExecutionError("configured Bitfinex instrument is unavailable")
            await asyncio.sleep(min(0.01, remaining))

    async def _verify_rest_account_profile(self) -> None:
        info = await self._rest.user_info()
        if not isinstance(info, list) or len(info) <= 21:
            raise BitfinexV1ExecutionError("Bitfinex user info is incomplete")
        user_id = info[0]
        paper_enabled = info[21]
        if type(user_id) is not int or user_id != self._bfx_config.user_id:
            raise BitfinexV1ExecutionError("Bitfinex REST account identity does not match")
        if type(paper_enabled) is not int or paper_enabled not in {0, 1}:
            raise BitfinexV1ExecutionError("Bitfinex paper-trading flag is invalid")
        expected_paper = self._bfx_config.raw_symbol == PAPER_RAW_SYMBOL
        if bool(paper_enabled) != expected_paper:
            raise BitfinexV1ExecutionError("Bitfinex account and symbol environments differ")

    async def _await_authenticated_wallet(self) -> None:
        authenticated = False
        while not (authenticated and self._account_ready):
            frame = await self._transport.recv_json()
            if isinstance(frame, dict):
                event = frame.get("event")
                if event == "info":
                    continue
                channel_id = frame.get("chanId")
                user_id = frame.get("userId")
                if (
                    event != "auth"
                    or frame.get("status") != "OK"
                    or type(channel_id) is not int
                    or channel_id != 0
                    or type(user_id) is not int
                    or user_id != self._bfx_config.user_id
                ):
                    raise BitfinexV1ExecutionError("Bitfinex private authentication failed")
                authenticated = True
                continue
            if not authenticated:
                raise BitfinexV1ExecutionError("private data arrived before authentication")
            self._consume_private_frame(frame)

    async def _disconnect(self) -> None:
        self._running = False
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._stop_ack_deadlines(mark_unknown=True)
        await self._transport.close()
        self._account_ready = False

    async def _reader(self) -> None:
        frame_type = "receive"
        try:
            while self._running:
                frame = await self._transport.recv_json()
                frame_type = _private_frame_type(frame)
                try:
                    self._consume_private_frame(frame)
                except BitfinexV1AccountingError:
                    self._log.error("Bitfinex final fee conflict; accounting remains incomplete")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._running = False
            self._stop_ack_deadlines(mark_unknown=True)
            context = f"private reader frame_type={frame_type}"
            self._record_fatal(context, exc)
            self._log.error(cast(str, self._fatal_failure))
            await self._transport.close()
            self._set_connected(False)

    def _consume_private_frame(self, frame: dict[str, object] | list[object]) -> None:
        if isinstance(frame, dict):
            raise BitfinexV1ExecutionError("unexpected private control event after authentication")
        if len(frame) == 2 and type(frame[0]) is int and frame == [0, "hb"]:
            return
        if len(frame) == 3 and type(frame[0]) is int and frame[:2] == [0, "te"]:
            self._handle_interim_trade(parse_interim_trade_message(frame))
            return
        message = parse_private_message(frame)
        if isinstance(message, WalletEvent):
            self._handle_wallet(message)
        elif isinstance(message, OrderEvent):
            self._handle_order(message)
        elif isinstance(message, TradeUpdate):
            self._handle_trade(message)
        elif isinstance(message, Notification):
            self._handle_notification(message)
        elif isinstance(message, OrderSnapshot):
            self._handle_order_snapshot(message)
        elif isinstance(message, PositionEvent):
            return

    def _handle_wallet(self, event: WalletEvent) -> None:
        for wallet in event.wallets:
            if (
                wallet.wallet_type != "margin"
                or wallet.currency.upper() != self._bfx_config.wallet_currency.upper()
            ):
                continue
            total = wallet.balance
            free = wallet.available_balance if wallet.available_balance is not None else Decimal(0)
            self.generate_account_state(
                balances=[
                    AccountBalance(
                        Money(total, USDT),
                        Money(total - free, USDT),
                        Money(free, USDT),
                    )
                ],
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
                info={"bitfinex_wallet_currency": wallet.currency},
            )
            if event.message_type == "ws":
                self._account_ready = True
            return

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        reason = self._submit_rejection(order, command.params)
        if reason is not None:
            self._deny(order, reason)
            return
        try:
            instrument = self._instrument(order.instrument_id)
        except BitfinexV1ExecutionError as exc:
            self._deny(order, str(exc))
            return
        if instrument is None:
            self._deny(order, "configured Bitfinex instrument is unavailable")
            return
        leverage = _leverage(command.params)
        self._submission_payload(order, leverage=leverage, cid=1)
        try:
            binding = self._cid_store.allocate(
                order.client_order_id.value,
                epoch_ms=cast(int, self._clock.timestamp_ns()) // 1_000_000,
            )
        except (BitfinexV1CidError, OSError):
            self._deny(order, "Bitfinex CID persistence failed")
            return
        cid = binding.cid
        payload = self._submission_payload(order, leverage=leverage, cid=cid)
        live = _LiveOrder(
            order=order,
            instrument=instrument,
            cid=cid,
            current_price=Decimal(str(order.price)),
            known_prices={Decimal(str(order.price))},
            unknown_operations=set(),
            submitted_in_process=True,
        )
        self._by_cid[cid] = live
        self._cid_by_client[order.client_order_id] = cid
        self.generate_order_submitted(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            self._clock.timestamp_ns(),
        )
        await self._send_mutation(live, "submit", payload)

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        for order in command.order_list.orders:
            self._deny(order, "Bitfinex v1 execution does not support order lists")

    async def _modify_order(self, command: ModifyOrder) -> None:
        live = self._live_for_client(command.client_order_id)
        reason: str | None = None
        if self.execution_hold_reason is not None:
            reason = self.execution_hold_reason
        elif live is None or live.venue_order_id is None or not live.accepted:
            reason = "order has no authoritative Bitfinex venue ID"
        elif live.terminal is not None or live.pending_cancel:
            reason = "order is already terminal or pending cancel"
        elif live.pending_modify_price is not None:
            reason = "order already has a pending modify"
        elif command.quantity is not None or command.trigger_price is not None:
            reason = "Bitfinex v1 Maker modify supports price-only changes"
        elif command.price is None:
            reason = "Bitfinex v1 Maker modify requires a price"
        else:
            try:
                leverage = _leverage(command.params)
            except BitfinexV1ExecutionError as exc:
                reason = str(exc)
        if reason is not None:
            self._reject_modify(command, reason)
            return
        assert live is not None and live.venue_order_id is not None and command.price is not None
        price = Decimal(str(command.price))
        live.pending_modify_price = price
        await self._send_mutation(
            live,
            "modify",
            update_order_op(
                venue_order_id=live.venue_order_id,
                leverage=leverage,
                price=price,
            ),
        )

    async def _cancel_order(self, command: CancelOrder) -> None:
        live = self._live_for_client(command.client_order_id)
        reason: str | None = None
        if live is None or live.venue_order_id is None or not live.accepted:
            reason = "order has no authoritative Bitfinex venue ID"
        elif live.terminal is not None:
            reason = "order is already terminal"
        elif live.pending_cancel:
            reason = "order already has a pending cancel"
        if reason is not None:
            self._reject_cancel(command, reason)
            return
        assert live is not None and live.venue_order_id is not None
        live.pending_cancel = True
        await self._send_mutation(
            live,
            "cancel",
            cancel_order_op(venue_order_id=live.venue_order_id),
        )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        raise BitfinexV1ExecutionError("Bitfinex v1 execution does not support cancel-all")

    async def _batch_cancel_orders(self, command: BatchCancelOrders) -> None:
        raise BitfinexV1ExecutionError("Bitfinex v1 execution does not support batch cancel")

    async def _send_mutation(
        self,
        live: _LiveOrder,
        operation: str,
        payload: list[object],
    ) -> None:
        key = (live.cid, operation)
        if key in self._ack_deadlines:
            raise BitfinexV1ExecutionError(
                f"Bitfinex {operation} already has a pending acknowledgment"
            )
        self._arm_terminal_reconciliation(live, operation)
        try:
            await self._transport.send_json(payload)
        except BaseException as exc:
            self._mark_mutation_unknown(
                live,
                operation,
                f"{operation} send UNKNOWN: {type(exc).__name__}: {exc}",
            )
            raise
        if not self._running or not self._account_ready:
            self._mark_mutation_unknown(
                live,
                operation,
                f"{operation} sent while the private stream closed; outcome UNKNOWN",
            )
            return
        if self._operation_is_pending(live, operation):
            try:
                self._start_ack_deadline(live, operation)
            except BaseException as exc:
                self._mark_mutation_unknown(
                    live,
                    operation,
                    f"{operation} acknowledgment tracking UNKNOWN: {type(exc).__name__}: {exc}",
                )
                raise

    @staticmethod
    def _operation_is_pending(live: _LiveOrder, operation: str) -> bool:
        if operation == "submit":
            return not live.accepted and live.rejection_key is None and not live.terminal_emitted
        if operation == "modify":
            return live.pending_modify_price is not None
        if operation == "cancel":
            return live.pending_cancel and live.terminal is None
        raise BitfinexV1ExecutionError(f"unsupported Bitfinex mutation {operation!r}")

    def _start_ack_deadline(self, live: _LiveOrder, operation: str) -> None:
        key = (live.cid, operation)
        delay = self._bfx_config.mutation_ack_timeout_ms / 1_000
        deadline = self._loop.call_later(
            delay,
            self._expire_ack_deadline,
            live,
            operation,
        )
        self._ack_deadlines[key] = deadline

    def _expire_ack_deadline(self, live: _LiveOrder, operation: str) -> None:
        key = (live.cid, operation)
        if self._ack_deadlines.pop(key, None) is None:
            return
        self._mark_mutation_unknown(
            live,
            operation,
            f"{operation} acknowledgment deadline expired; outcome UNKNOWN",
        )

    def _can_reconcile_silent_terminal(self, live: _LiveOrder, operation: str) -> bool:
        owned_pending = (
            self._bfx_config.raw_symbol == PAPER_RAW_SYMBOL
            and live.submitted_in_process
            and live.terminal is None
            and live.rejection_key is None
            and not live.terminal_emitted
            and live.filled_qty < live.order.quantity.as_decimal()
            and live.pending_modify_price is None
            and "modify" not in live.unknown_operations
        )
        return owned_pending and (
            (operation == "submit" and live.order.time_in_force == TimeInForce.IOC)
            or (operation == "cancel" and live.accepted and live.pending_cancel
                and live.order.time_in_force == TimeInForce.GTC
                and cast(bool, live.order.is_post_only))
        )

    def _arm_terminal_reconciliation(self, live: _LiveOrder, operation: str) -> None:
        if self._can_reconcile_silent_terminal(live, operation):
            live.terminal_reconciliation_pending = True
            if live.terminal_reconciliation_deadline is None:
                live.terminal_reconciliation_deadline = (
                    self._loop.time() + self._bfx_config.mutation_ack_timeout_ms / 1_000
                )

    def _mark_mutation_unknown(
        self,
        live: _LiveOrder,
        operation: str,
        reason: str,
    ) -> None:
        live.unknown_operations.add(operation)
        self._arm_terminal_reconciliation(live, operation)
        self._last_failure = reason[:300]

    def _silent_terminal_reconciliation_due(self, live: _LiveOrder) -> bool:
        deadline = live.terminal_reconciliation_deadline
        return (
            live.terminal_reconciliation_pending
            and deadline is not None
            and self._loop.time() >= deadline
            and (self._can_reconcile_silent_terminal(live, "submit")
                 or self._can_reconcile_silent_terminal(live, "cancel"))
        )

    def _resolve_mutation(self, live: _LiveOrder, operation: str) -> None:
        deadline = self._ack_deadlines.pop((live.cid, operation), None)
        if deadline is not None:
            deadline.cancel()
        live.unknown_operations.discard(operation)

    def _resolve_all_mutations(self, live: _LiveOrder) -> None:
        for operation in ("submit", "modify", "cancel"):
            self._resolve_mutation(live, operation)
        live.terminal_reconciliation_pending = False
        live.terminal_reconciliation_deadline = None

    def _stop_ack_deadlines(self, *, mark_unknown: bool) -> None:
        pending = list(self._ack_deadlines.items())
        self._ack_deadlines.clear()
        for (cid, operation), deadline in pending:
            if mark_unknown:
                live = self._by_cid.get(cid)
                if live is not None:
                    self._mark_mutation_unknown(
                        live,
                        operation,
                        "connection closed with unacknowledged mutation; outcome UNKNOWN",
                    )
            deadline.cancel()
        if mark_unknown:
            for live in tuple(self._by_cid.values()):
                if self._can_reconcile_silent_terminal(live, "submit"):
                    self._mark_mutation_unknown(
                        live,
                        "submit",
                        "private stream closed before exact terminal; outcome UNKNOWN",
                    )
        if mark_unknown and pending:
            self._last_failure = "connection closed with unacknowledged mutation; outcome UNKNOWN"

    def _handle_order(self, event: OrderEvent) -> None:
        state = event.order
        live = self._resolve_live(state.client_order_id, state.venue_order_id)
        if live is None:
            return
        if live.terminal is not None:
            if event.operation == "oc" and state == live.terminal:
                return
            raise BitfinexV1ExecutionError("Bitfinex order update followed its terminal event")
        self._validate_order_state(live, state, event.operation)
        staged_interims = self._validated_unbound_paper_interims(
            live,
            state,
            operation=event.operation,
        )
        if event.operation == "oc":
            _terminal_disposition(state)
        rejection = (
            _initial_rejection(state.status)
            if not live.accepted and _terminal_filled(state) == 0
            else None
        )
        if rejection is not None and staged_interims:
            raise BitfinexV1ExecutionError(
                "Bitfinex rejected an order which already has a buffered interim fill"
            )
        live.venue_flags_verified = True
        if live.rejection_key is not None:
            if rejection is not None and live.rejection_key == ("status", None, state.status):
                return
            raise BitfinexV1ExecutionError("Bitfinex success followed a definitive rejection")
        if rejection is not None:
            self.generate_order_rejected(
                live.order.strategy_id,
                live.order.instrument_id,
                live.order.client_order_id,
                state.status,
                state.ts_updated_ms * 1_000_000,
                due_post_only=rejection == "POSTONLY CANCELED",
            )
            live.terminal = state
            live.terminal_emitted = True
            live.rejection_key = ("status", None, state.status)
            self._resolve_all_mutations(live)
            return
        self._accept(live, state.venue_order_id, state.ts_updated_ms * 1_000_000)
        self._ack_pending_price(live, state.price, state.ts_updated_ms * 1_000_000)
        if event.operation == "oc":
            live.pending_cancel = False
            live.terminal = state
            self._resolve_all_mutations(live)
        self._release_unbound_paper_interims(live, staged_interims)
        if event.operation == "oc":
            self._finish_terminal_if_ready(live)

    def _handle_order_snapshot(self, snapshot: OrderSnapshot) -> None:
        """Authenticate owned open-order state without weakening restart semantics."""
        validated: list[tuple[_LiveOrder, OrderState, tuple[TradeExecution, ...]]] = []
        seen_cids: set[int] = set()
        seen_venues: set[int] = set()
        for state in snapshot.orders:
            cid = state.client_order_id
            if cid is None or self._cid_store.binding_for_cid(cid) is None:
                raise BitfinexV1ExecutionError(
                    "Bitfinex order snapshot contains an active order without an owned CID"
                )
            if cid in seen_cids or state.venue_order_id in seen_venues:
                raise BitfinexV1ExecutionError(
                    "Bitfinex order snapshot repeats a CID or venue order ID"
                )
            seen_cids.add(cid)
            seen_venues.add(state.venue_order_id)
            live = self._resolve_live(cid, state.venue_order_id)
            if live is None:
                raise BitfinexV1ExecutionError(
                    "owned Bitfinex snapshot order cannot be reconstructed from cache"
                )
            if live.terminal is not None or live.rejection_key is not None:
                raise BitfinexV1ExecutionError(
                    "active Bitfinex snapshot order conflicts with a local terminal"
                )
            self._validate_order_state(live, state, "os")
            upper_status = state.status.upper()
            if upper_status != "ACTIVE" and not upper_status.startswith("PARTIALLY FILLED"):
                raise BitfinexV1ExecutionError(
                    "Bitfinex order snapshot contains a non-open order status"
                )
            staged_interims = self._validated_unbound_paper_interims(
                live,
                state,
                operation="os",
            )
            validated.append((live, state, staged_interims))

        for live, state, staged_interims in validated:
            live.venue_flags_verified = True
            self._accept(live, state.venue_order_id, state.ts_updated_ms * 1_000_000)
            self._ack_pending_price(live, state.price, state.ts_updated_ms * 1_000_000)
            self._release_unbound_paper_interims(live, staged_interims)

    def _handle_interim_trade(self, trade: TradeExecution) -> None:
        """Use paper ``te`` execution facts promptly when its fee update is absent."""
        live = self._resolve_live(trade.client_order_id, trade.venue_order_id)
        if live is None:
            self._stage_unbound_paper_interim(trade)
            return
        if live.rejection_key is not None:
            raise BitfinexV1ExecutionError("Bitfinex trade followed a definitive rejection")
        self._validate_trade(live, trade)
        if self._bfx_config.raw_symbol != PAPER_RAW_SYMBOL:
            return
        if (
            live.submitted_in_process
            and not live.accepted
            and not live.venue_flags_verified
            and live.terminal is None
            and live.order.time_in_force == TimeInForce.IOC
            and cast(bool, live.order.is_reduce_only)
            and trade.client_order_id == live.cid
        ):
            self._buffer_paper_interim(trade)
            return
        previous = self._paper_interim_fills.get(trade.trade_id)
        if previous is not None:
            if previous != trade:
                raise BitfinexV1ExecutionError(
                    "duplicate Bitfinex interim trade changed its execution facts"
                )
            self._record_fee_trade(live.cid, trade)
            return
        final = self._seen_trades.get(trade.trade_id)
        if final is not None:
            if not _same_execution(trade, final):
                raise BitfinexV1ExecutionError(
                    "Bitfinex interim trade differs from its final update"
                )
            self._record_fee_trade(live.cid, trade)
            return
        self._record_fee_trade(live.cid, trade)
        if self._cached_trade_already_applied(live, trade):
            return
        self._apply_trade_fill(
            live,
            trade=trade,
            commission=Money(Decimal(0), USD),
            info={
                "bitfinex_fill_source": "te_paper",
                "bitfinex_fee_status": "pending",
            },
        )
        self._paper_interim_fills[trade.trade_id] = trade

    def _stage_unbound_paper_interim(self, trade: TradeExecution) -> None:
        if self._bfx_config.raw_symbol != PAPER_RAW_SYMBOL or trade.client_order_id is not None:
            return
        if (
            trade.symbol != self._bfx_config.raw_symbol
            or trade.order_type != "IOC"
            or trade.order_price <= 0
            or trade.maker
        ):
            raise BitfinexV1ExecutionError(
                "unbound Bitfinex paper interim trade is not a recoverable IOC taker fill"
            )
        self._buffer_paper_interim(trade)

    def _buffer_paper_interim(self, trade: TradeExecution) -> None:
        final = self._seen_trades.get(trade.trade_id)
        if final is not None:
            if not _same_execution(trade, final):
                raise BitfinexV1ExecutionError(
                    "unbound Bitfinex interim trade differs from its final update"
                )
            return
        applied = self._paper_interim_fills.get(trade.trade_id)
        if applied is not None:
            if applied != trade:
                raise BitfinexV1ExecutionError(
                    "duplicate Bitfinex interim trade changed its execution facts"
                )
            return
        previous = self._unbound_paper_interim_fills.get(trade.trade_id)
        if previous is not None:
            if previous != trade:
                raise BitfinexV1ExecutionError(
                    "duplicate unbound Bitfinex interim trade changed its execution facts"
                )
            return
        if len(self._unbound_paper_interim_fills) >= _MAX_UNBOUND_PAPER_INTERIM_FILLS:
            raise BitfinexV1ExecutionError("unbound Bitfinex paper interim trade buffer is full")
        self._unbound_paper_interim_fills[trade.trade_id] = trade

    def _validated_unbound_paper_interims(
        self,
        live: _LiveOrder,
        state: OrderState,
        *,
        operation: str,
    ) -> tuple[TradeExecution, ...]:
        staged = tuple(
            sorted(
                (
                    trade
                    for trade in self._unbound_paper_interim_fills.values()
                    if (
                        trade.client_order_id is None
                        and trade.venue_order_id == state.venue_order_id
                    )
                    or (
                        operation in {"on", "oc"}
                        and (
                            trade.client_order_id == live.cid
                            or trade.venue_order_id == state.venue_order_id
                        )
                    )
                ),
                key=lambda trade: (trade.ts_event_ms, trade.trade_id),
            )
        )
        if not staged:
            return ()
        if (
            operation not in {"on", "oc", "os"}
            or self._bfx_config.raw_symbol != PAPER_RAW_SYMBOL
            or not cast(bool, live.order.is_reduce_only)
            or state.flags != REDUCE_ONLY_FLAG
        ):
            raise BitfinexV1ExecutionError(
                "unbound Bitfinex paper interim fill requires exact reduce-only order evidence"
            )
        next_filled = live.filled_qty
        expected_qty = Decimal(str(live.order.quantity))
        for trade in staged:
            positive_cid_interim = trade.client_order_id is not None
            if (
                (positive_cid_interim and operation not in {"on", "oc"})
                or (not positive_cid_interim and operation not in {"on", "os"})
                or (positive_cid_interim and not live.submitted_in_process)
                or live.order.time_in_force != TimeInForce.IOC
                or trade.client_order_id not in {None, live.cid}
                or trade.venue_order_id != state.venue_order_id
            ):
                raise BitfinexV1ExecutionError(
                    "unbound Bitfinex paper interim trade changed order identity"
                )
            self._validate_trade(live, trade)
            if trade.ts_event_ms < state.ts_created_ms:
                raise BitfinexV1ExecutionError(
                    "unbound Bitfinex paper interim trade predates its order"
                )
            next_filled += abs(trade.execution_qty)
            if next_filled > expected_qty:
                raise BitfinexV1ExecutionError(
                    "buffered Bitfinex fills exceed the submitted quantity"
                )
            if operation == "oc" and next_filled > _terminal_filled(state):
                raise BitfinexV1ExecutionError(
                    "buffered Bitfinex fills exceed the terminal order quantity"
                )
            try:
                fill_qty = live.instrument.make_qty(abs(trade.execution_qty))
                fill_price = live.instrument.make_price(trade.execution_price)
            except ValueError as exc:
                raise BitfinexV1ExecutionError(
                    "buffered Bitfinex fill loses instrument precision"
                ) from exc
            if (
                Decimal(str(fill_qty)) != abs(trade.execution_qty)
                or Decimal(str(fill_price)) != trade.execution_price
            ):
                raise BitfinexV1ExecutionError("buffered Bitfinex fill loses instrument precision")
        return staged

    def _release_unbound_paper_interims(
        self,
        live: _LiveOrder,
        staged: tuple[TradeExecution, ...],
    ) -> None:
        for trade in staged:
            self._record_fee_trade(live.cid, trade)
            self._apply_trade_fill(
                live,
                trade=trade,
                commission=Money(Decimal(0), USD),
                info={
                    "bitfinex_fill_source": "te_paper",
                    "bitfinex_fee_status": "pending",
                },
            )
            self._paper_interim_fills[trade.trade_id] = trade
            self._unbound_paper_interim_fills.pop(trade.trade_id, None)

    def _handle_trade(self, trade: TradeUpdate) -> None:
        live = self._resolve_live(self._fee_trade_cid(trade), trade.venue_order_id)
        if live is None:
            self._record_retired_fee_trade(trade)
            return
        if live.rejection_key is not None:
            raise BitfinexV1ExecutionError("Bitfinex trade followed a definitive rejection")
        self._validate_trade(live, trade)
        self._record_fee_trade(live.cid, trade)
        previous = self._seen_trades.get(trade.trade_id)
        if previous is not None:
            if previous != trade:
                raise BitfinexV1ExecutionError("Bitfinex trade ID changed its execution facts")
            return
        interim = self._paper_interim_fills.get(trade.trade_id)
        if interim is not None:
            if not _same_execution(interim, trade):
                raise BitfinexV1ExecutionError(
                    "Bitfinex TU differs from its paper interim execution"
                )
            self._finish_terminal_if_ready(live)
            self._seen_trades[trade.trade_id] = trade
            return
        if self._cached_trade_already_applied(live, trade):
            return
        self._apply_trade_fill(
            live,
            trade=trade,
            commission=usd_commission(trade.fee),
            info={
                "bitfinex_fill_source": "tu",
                "bitfinex_fee": format(trade.fee, "f"),
                "bitfinex_fee_currency": trade.fee_currency,
            },
        )
        self._seen_trades[trade.trade_id] = trade

    def _cached_trade_already_applied(
        self, live: _LiveOrder, trade: TradeExecution | TradeUpdate,
    ) -> bool:
        event = next(
            (fill for fill in live.order.events if isinstance(fill, OrderFilled)
             and fill.trade_id.value == str(trade.trade_id)), None,
        )
        if event is None:
            if not live.order.is_closed:
                return False
            self._confirmed_terminal_quantity(live)
            inferred = [
                fill for fill in live.order.events if isinstance(fill, OrderFilled)
                and fill.reconciliation and not fill.trade_id.value.isdecimal()
            ]
            metadata = self._cid_store.fee_metadata_for_cid(live.cid)
            real_ids = {fill.trade_id.value for fill in live.order.events
                        if isinstance(fill, OrderFilled) and fill.trade_id.value.isdecimal()}
            remaining = [] if metadata is None else [
                row for row in metadata.venue_trades if str(row.trade_id) not in real_ids
            ]
            inferred_qty = sum((fill.last_qty.as_decimal() for fill in inferred), Decimal())
            real_qty = sum((abs(row.execution_qty) for row in remaining), Decimal())
            if (
                not inferred or real_qty > inferred_qty
                or any(row.ts_event_ms * 1_000_000 > max(fill.ts_event for fill in inferred)
                       for row in remaining)
                or (real_qty == inferred_qty and sum(
                    (abs(row.execution_qty) * row.execution_price for row in remaining), Decimal(),
                ) != sum((fill.last_qty.as_decimal() * fill.last_px.as_decimal()
                          for fill in inferred), Decimal()))
            ):
                raise BitfinexV1ExecutionError("late Bitfinex trade conflicts with native fills")
            return True  # Real IDs remain fee evidence, never replace the inferred UUID.
        if not native_fill_matches_trade(
            _native_fill_evidence(event, "unknown"), _fee_trade_evidence(trade),
        ):
            raise BitfinexV1ExecutionError("cached Bitfinex trade changed its execution facts")
        self._capture_native_fee_evidence(event)
        return True

    def _record_retired_fee_trade(self, trade: TradeUpdate) -> None:
        """Late final facts for an already applied closed order never publish a fill."""
        cid = self._fee_trade_cid(trade)
        if cid is None:
            return
        binding = self._cid_store.binding_for_cid(cid)
        if binding is None:
            return
        order = self._cache.order(ClientOrderId(binding.client_order_id))
        if order is None or not order.is_closed:
            return  # Execution reconstruction remains the responsibility of normal reconciliation.
        expected_sign = 1 if order.side == OrderSide.BUY else -1
        if (
            order.account_id != self.account_id
            or order.instrument_id != self._bfx_config.instrument_id
            or order.venue_order_id != VenueOrderId(str(trade.venue_order_id))
            or trade.symbol != self._bfx_config.raw_symbol
            or trade.fee_currency != self._bfx_config.fee_currency
            or trade.order_type != ("IOC" if order.time_in_force == TimeInForce.IOC else "LIMIT")
            or trade.maker != cast(bool, order.is_post_only)
            or trade.execution_qty * expected_sign <= 0
            or abs(trade.execution_qty) > order.filled_qty.as_decimal()
            or trade.execution_price <= 0 or trade.order_price <= 0
        ):
            raise BitfinexV1ExecutionError("late Bitfinex fee trade differs from its closed order")
        self._record_fee_trade(binding.cid, trade)
        for event in order.events:
            self._capture_native_fee_evidence(event)

    def _fee_trade_cid(self, trade: TradeUpdate) -> int | None:
        if trade.client_order_id is not None:
            return trade.client_order_id
        matches = [
            row.cid for row in self._cid_store.fee_metadata
            if row.venue_order_id == trade.venue_order_id
            and row.instrument_id == self._bfx_config.instrument_id.value
            and row.raw_symbol == trade.symbol == self._bfx_config.raw_symbol
        ]
        return matches[0] if len(matches) == 1 else None

    def _apply_trade_fill(
        self,
        live: _LiveOrder,
        *,
        trade: TradeExecution | TradeUpdate,
        commission: Money,
        info: dict[str, str],
    ) -> None:
        if cast(bool, live.order.is_reduce_only) and not live.venue_flags_verified:
            raise BitfinexV1ExecutionError(
                "Bitfinex reduce-only fill arrived before venue flags were verified"
            )
        next_filled = live.filled_qty + abs(trade.execution_qty)
        if next_filled > Decimal(str(live.order.quantity)):
            raise BitfinexV1ExecutionError("Bitfinex fills exceed the submitted quantity")
        if live.terminal is not None:
            terminal_filled = _terminal_filled(live.terminal)
            if next_filled > terminal_filled:
                raise BitfinexV1ExecutionError(
                    "observed Bitfinex fill exceeds the terminal order quantity"
                )
        try:
            fill_qty = live.instrument.make_qty(abs(trade.execution_qty))
            fill_price = live.instrument.make_price(trade.execution_price)
        except ValueError as exc:
            raise BitfinexV1ExecutionError("Bitfinex fill loses instrument precision") from exc
        if (
            Decimal(str(fill_qty)) != abs(trade.execution_qty)
            or Decimal(str(fill_price)) != trade.execution_price
        ):
            raise BitfinexV1ExecutionError("Bitfinex fill loses instrument precision")
        self._accept(live, trade.venue_order_id, trade.ts_event_ms * 1_000_000)
        self._ack_pending_price(live, trade.order_price, trade.ts_event_ms * 1_000_000)
        live.filled_qty = next_filled
        self._resolve_mutation(live, "submit")
        self.generate_order_filled(
            live.order.strategy_id,
            live.order.instrument_id,
            live.order.client_order_id,
            VenueOrderId(str(trade.venue_order_id)),
            None,
            TradeId(str(trade.trade_id)),
            live.order.side,
            live.order.order_type,
            fill_qty,
            fill_price,
            live.instrument.quote_currency,
            commission,
            LiquiditySide.MAKER if trade.maker else LiquiditySide.TAKER,
            trade.ts_event_ms * 1_000_000,
            info=info,
        )
        if next_filled == Decimal(str(live.order.quantity)):
            live.pending_cancel = False
            self._resolve_mutation(live, "cancel")
        self._finish_terminal_if_ready(live)

    def _handle_notification(self, note: Notification) -> None:
        if note.status == "SUCCESS":
            return
        live = self._resolve_live(note.client_order_id, note.venue_order_id)
        if live is None:
            return
        reason = f"Bitfinex {note.request_type} {note.status} {note.code}: {note.text}"
        ts_event = note.ts_event_ms * 1_000_000
        rejection_key = (note.status, note.code, note.text)
        if live.rejection_key is not None:
            if note.operation == "on" and live.rejection_key == rejection_key:
                return
            raise BitfinexV1ExecutionError("Bitfinex mutation followed a definitive rejection")
        if note.operation == "on":
            if live.accepted:
                raise BitfinexV1ExecutionError("Bitfinex submit failure followed acceptance")
            self.generate_order_rejected(
                live.order.strategy_id,
                live.order.instrument_id,
                live.order.client_order_id,
                reason,
                ts_event,
            )
            live.terminal_emitted = True
            live.rejection_key = rejection_key
            self._resolve_all_mutations(live)
        elif note.operation == "ou":
            if live.pending_modify_price is None and "modify" not in live.unknown_operations:
                raise BitfinexV1ExecutionError("unexpected Bitfinex modify rejection")
            self.generate_order_modify_rejected(
                live.order.strategy_id,
                live.order.instrument_id,
                live.order.client_order_id,
                self._venue_id(live),
                reason,
                ts_event,
            )
            live.pending_modify_price = None
            self._resolve_mutation(live, "modify")
        elif note.operation == "oc":
            if not live.pending_cancel and "cancel" not in live.unknown_operations:
                raise BitfinexV1ExecutionError("unexpected Bitfinex cancel rejection")
            self.generate_order_cancel_rejected(
                live.order.strategy_id,
                live.order.instrument_id,
                live.order.client_order_id,
                self._venue_id(live),
                reason,
                ts_event,
            )
            live.pending_cancel = False
            self._resolve_mutation(live, "cancel")

            if (
                self._bfx_config.raw_symbol == PAPER_RAW_SYMBOL and live.submitted_in_process
                and live.order.time_in_force == TimeInForce.GTC and live.order.is_post_only
            ):
                live.terminal_reconciliation_pending = False
                live.terminal_reconciliation_deadline = None

    def _accept(self, live: _LiveOrder, venue_order_id: int, ts_event: int) -> None:
        if live.venue_order_id not in {None, venue_order_id}:
            raise BitfinexV1ExecutionError("Bitfinex order changed venue ID")
        live.venue_order_id = venue_order_id
        self._cid_by_venue[venue_order_id] = live.cid
        self._resolve_mutation(live, "submit")
        if live.accepted:
            return
        live.accepted = True
        self.generate_order_accepted(
            live.order.strategy_id,
            live.order.instrument_id,
            live.order.client_order_id,
            VenueOrderId(str(venue_order_id)),
            ts_event,
        )

    def _finish_terminal_if_ready(self, live: _LiveOrder) -> None:
        state = live.terminal
        if state is None:
            return
        terminal_filled = _terminal_filled(state)
        if live.filled_qty > terminal_filled:
            raise BitfinexV1ExecutionError(
                "observed Bitfinex fills exceed the terminal order quantity"
            )
        if live.terminal_emitted:
            return
        if terminal_filled > live.filled_qty:
            return
        disposition = _terminal_disposition(state)
        if disposition == "canceled" and terminal_filled < abs(state.original_qty):
            self.generate_order_canceled(
                live.order.strategy_id,
                live.order.instrument_id,
                live.order.client_order_id,
                VenueOrderId(str(state.venue_order_id)),
                state.ts_updated_ms * 1_000_000,
            )
        live.terminal_emitted = True
        live.pending_modify_price = None

    def _ack_pending_price(self, live: _LiveOrder, price: Decimal, ts_event: int) -> None:
        if live.pending_modify_price is None or live.pending_modify_price != price:
            return
        if live.venue_order_id is None:
            raise BitfinexV1ExecutionError("Bitfinex modify has no venue order ID")
        self.generate_order_updated(
            live.order.strategy_id,
            live.order.instrument_id,
            live.order.client_order_id,
            VenueOrderId(str(live.venue_order_id)),
            live.instrument.make_qty(Decimal(str(live.order.quantity))),
            live.instrument.make_price(price),
            None,
            ts_event,
        )
        live.current_price = price
        live.known_prices.add(price)
        live.pending_modify_price = None
        self._resolve_mutation(live, "modify")

    def _resolve_live(
        self,
        client_order_id: int | None,
        venue_order_id: int | None,
    ) -> _LiveOrder | None:
        by_client = self._by_cid.get(client_order_id) if client_order_id is not None else None
        if by_client is None and client_order_id is not None:
            binding = self._cid_store.binding_for_cid(client_order_id)
            if binding is not None:
                by_client = self._rehydrate_open_order(client_order_id)
                if by_client is None:
                    cached = self._cache.order(ClientOrderId(binding.client_order_id))
                    if cached is None:
                        raise BitfinexV1ExecutionError(
                            "known Bitfinex CID arrived before restart state reconstruction"
                        )
                    if cached.is_closed:
                        return None
        venue_cid = self._cid_by_venue.get(venue_order_id) if venue_order_id is not None else None
        by_venue = self._by_cid.get(venue_cid) if venue_cid is not None else None
        if by_client is not None and by_venue is not None and by_client is not by_venue:
            raise BitfinexV1ExecutionError(
                "Bitfinex client and venue IDs identify different orders"
            )
        live = by_client or by_venue
        if live is None:
            return None
        if client_order_id is not None and client_order_id != live.cid:
            raise BitfinexV1ExecutionError("Bitfinex event changed client order ID")
        if (
            venue_order_id is not None
            and live.venue_order_id is not None
            and venue_order_id != live.venue_order_id
        ):
            raise BitfinexV1ExecutionError("Bitfinex event changed venue order ID")
        return live

    def _validate_order_state(
        self,
        live: _LiveOrder,
        state: OrderState,
        operation: str,
    ) -> None:
        expected_qty = Decimal(str(live.order.quantity))
        expected_sign = Decimal(1) if live.order.side == OrderSide.BUY else Decimal(-1)
        expected_type = "IOC" if live.order.time_in_force == TimeInForce.IOC else "LIMIT"
        expected_flags = (
            POST_ONLY_FLAG
            if cast(bool, live.order.is_post_only)
            else REDUCE_ONLY_FLAG
            if cast(bool, live.order.is_reduce_only)
            else 0
        )
        paper_full_fill_terminal = (
            operation == "oc"
            and live.accepted
            and live.filled_qty == expected_qty
            and _terminal_filled(state) == expected_qty
        )
        flags_match = state.flags == expected_flags or (
            self._bfx_config.raw_symbol == PAPER_RAW_SYMBOL
            and live.submitted_in_process
            and expected_flags == POST_ONLY_FLAG
            and live.order.order_type == OrderType.LIMIT
            and live.order.time_in_force == TimeInForce.GTC
            and state.flags == 0
            and (
                operation == "on"
                or (operation == "ou" and live.pending_modify_price is not None)
                or (operation == "oc" and live.accepted and live.pending_cancel)
                or paper_full_fill_terminal
            )
        )
        expected_prices = {live.current_price}
        if live.pending_modify_price is not None:
            expected_prices.add(live.pending_modify_price)
        if (
            state.symbol != self._bfx_config.raw_symbol
            or state.client_order_id != live.cid
            or state.original_qty * expected_sign <= 0
            or state.remaining_qty * expected_sign < 0
            or abs(state.original_qty) != expected_qty
            or abs(state.remaining_qty) > expected_qty
            or state.order_type != expected_type
            or state.tif_expiry_ms is not None
            or not flags_match
            or state.price not in expected_prices
        ):
            raise BitfinexV1ExecutionError("Bitfinex order event differs from local submission")

    def _validate_trade(
        self,
        live: _LiveOrder,
        trade: TradeExecution | TradeUpdate,
    ) -> None:
        expected_sign = Decimal(1) if live.order.side == OrderSide.BUY else Decimal(-1)
        expected_type = "IOC" if live.order.time_in_force == TimeInForce.IOC else "LIMIT"
        expected_maker = cast(bool, live.order.is_post_only)
        if (
            trade.symbol != self._bfx_config.raw_symbol
            or trade.execution_qty * expected_sign <= 0
            or trade.execution_price <= 0
            or trade.order_type != expected_type
            or (
                trade.order_price not in live.known_prices
                and trade.order_price != live.pending_modify_price
            )
            or trade.maker != expected_maker
            or (trade.client_order_id is not None and trade.client_order_id != live.cid)
            or (
                isinstance(trade, TradeUpdate)
                and trade.fee_currency != self._bfx_config.fee_currency
            )
        ):
            raise BitfinexV1ExecutionError("Bitfinex trade differs from local submission")

    def _submit_rejection(self, order: Order, params: dict[str, object]) -> str | None:
        if self.execution_hold_reason is not None:
            return self.execution_hold_reason
        if self._cid_store.binding_for_client(order.client_order_id.value) is not None:
            return "Bitfinex client order ID was already used for this account"
        if order.instrument_id != self._bfx_config.instrument_id:
            return "order instrument is outside the configured Bitfinex boundary"
        if order.order_type != OrderType.LIMIT:
            return "Bitfinex v1 source execution supports LIMIT orders only"
        if order.is_quote_quantity or order.exec_algorithm_id is not None:
            return "Bitfinex v1 source execution does not support additional order semantics"
        if cast(bool, order.is_post_only) and cast(bool, order.is_reduce_only):
            return "Bitfinex post-only and reduce-only are mutually exclusive"
        if cast(bool, order.is_reduce_only) and order.time_in_force != TimeInForce.IOC:
            return "Bitfinex reduce-only requires an IOC order"
        valid_tif = (
            order.time_in_force == TimeInForce.IOC and not cast(bool, order.is_post_only)
        ) or (order.time_in_force == TimeInForce.GTC and cast(bool, order.is_post_only))
        if not valid_tif:
            return "only IOC taker and post-only GTC Maker orders are supported"
        try:
            _leverage(params)
        except BitfinexV1ExecutionError as exc:
            return str(exc)
        return None

    def _instrument(self, instrument_id: InstrumentId) -> Instrument | None:
        instrument = self._instrument_provider.find(instrument_id) or self._cache.instrument(
            instrument_id
        )
        if instrument is not None and instrument.raw_symbol.value != self._bfx_config.raw_symbol:
            raise BitfinexV1ExecutionError(
                "Bitfinex instrument profile differs from the execution raw symbol"
            )
        return instrument

    def _submission_payload(self, order: Order, *, leverage: int, cid: int) -> list[object]:
        signed_qty = Decimal(str(order.quantity))
        if order.side == OrderSide.SELL:
            signed_qty = -signed_qty
        return submit_order_op(
            symbol=self._bfx_config.raw_symbol,
            amount=signed_qty,
            price=Decimal(str(order.price)),
            cid=cid,
            order_type="IOC" if order.time_in_force == TimeInForce.IOC else "LIMIT",
            leverage=leverage,
            post_only=cast(bool, order.is_post_only),
            reduce_only=cast(bool, order.is_reduce_only),
        )

    def _live_for_client(self, client_order_id: ClientOrderId) -> _LiveOrder | None:
        cid = self._cid_by_client.get(client_order_id)
        if cid is None:
            binding = self._cid_store.binding_for_client(client_order_id.value)
            cid = binding.cid if binding is not None else None
        if cid is None:
            return None
        return self._by_cid.get(cid) or self._rehydrate_open_order(cid)

    def _rehydrate_open_order(self, cid: int) -> _LiveOrder | None:
        binding = self._cid_store.binding_for_cid(cid)
        if binding is None:
            return None
        client_order_id = ClientOrderId(binding.client_order_id)
        order = self._cache.order(client_order_id)
        if order is None or not order.is_open:
            return None
        if (
            order.account_id != self.account_id
            or order.instrument_id != self._bfx_config.instrument_id
            or order.order_type != OrderType.LIMIT
            or order.time_in_force not in {TimeInForce.GTC, TimeInForce.IOC}
            or (order.time_in_force == TimeInForce.GTC) != cast(bool, order.is_post_only)
            or (cast(bool, order.is_reduce_only) and order.time_in_force != TimeInForce.IOC)
            or (cast(bool, order.is_reduce_only) and cast(bool, order.is_post_only))
            or order.is_quote_quantity
            or order.exec_algorithm_id is not None
            or order.venue_order_id is None
            or order.price is None
        ):
            raise BitfinexV1ExecutionError("cached Bitfinex order cannot be safely reconstructed")
        venue_text = order.venue_order_id.value
        if not venue_text.isdigit() or int(venue_text) <= 0:
            raise BitfinexV1ExecutionError("cached Bitfinex venue order ID is invalid")
        instrument = self._instrument(order.instrument_id)
        if instrument is None:
            raise BitfinexV1ExecutionError("cached Bitfinex instrument is unavailable")
        venue_order_id = int(venue_text)
        existing_cid = self._cid_by_venue.get(venue_order_id)
        if existing_cid not in {None, cid}:
            raise BitfinexV1ExecutionError("cached Bitfinex venue order ID changed CID")
        price = Decimal(str(order.price))
        live = _LiveOrder(
            order=order,
            instrument=instrument,
            cid=cid,
            current_price=price,
            known_prices={price},
            unknown_operations=set(),
            venue_order_id=venue_order_id,
            accepted=True,
            filled_qty=Decimal(str(order.filled_qty)),
        )
        self._by_cid[cid] = live
        self._cid_by_client[client_order_id] = cid
        self._cid_by_venue[venue_order_id] = cid
        return live

    @staticmethod
    def _venue_id(live: _LiveOrder) -> VenueOrderId | None:
        return VenueOrderId(str(live.venue_order_id)) if live.venue_order_id is not None else None

    def _deny(self, order: Order, reason: str) -> None:
        self.generate_order_denied(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            reason,
            self._clock.timestamp_ns(),
        )

    def _reject_modify(self, command: ModifyOrder, reason: str) -> None:
        self.generate_order_modify_rejected(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id,
            reason,
            self._clock.timestamp_ns(),
        )

    def _reject_cancel(self, command: CancelOrder, reason: str) -> None:
        self.generate_order_cancel_rejected(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id,
            reason,
            self._clock.timestamp_ns(),
        )

    def _record_fatal(self, context: str, exc: Exception) -> None:
        # Private protocol exceptions can contain attacker-controlled event labels.
        # Keep only the bounded frame class and Python exception type; never persist
        # or log authenticated frame content.
        self._last_failure = f"{context}: {type(exc).__name__}"[:300]
        self._fatal_failure = self._last_failure

    async def generate_mass_status(
        self,
        lookback_mins: int | None = None,
    ) -> ExecutionMassStatus | None:
        mass_status = await super().generate_mass_status(lookback_mins)
        if mass_status is None:
            return None
        reported: dict[ClientOrderId, OrderStatusReport] = {}
        for report in mass_status.order_reports.values():
            client_order_id = report.client_order_id
            if client_order_id is None:
                continue
            previous = reported.get(client_order_id)
            if previous is not None and previous.venue_order_id != report.venue_order_id:
                raise BitfinexV1ExecutionError(
                    "venue reconciliation returned conflicting order identities"
                )
            reported[client_order_id] = report
        for live in tuple(self._by_cid.values()):
            if live.reconciled_terminal is not None and not live.terminal_emitted:
                current_report = reported.get(live.order.client_order_id)
                if current_report is None or not _same_order_report_facts(
                    live.reconciled_terminal, current_report,
                ):
                    raise BitfinexV1ExecutionError(
                        "Bitfinex pending terminal is absent or changed its reconciliation facts",
                    )
                fills = self._reconciliation_fills(mass_status, current_report)
                self._validate_terminal_fill_evidence(live, current_report, fills)
                cached_order = self._cache.order(live.order.client_order_id)
                if cached_order is None:
                    raise BitfinexV1ExecutionError("Bitfinex pending terminal has no cached order")
                if self._silent_rejection_has_no_native_venue(
                    live, cached_order, live.reconciled_terminal,
                ):
                    self._confirmed_terminal_quantity(live)
                else:
                    self._validate_cached_open_report(cached_order, current_report, fills)
                continue
            if live.reconciled_working is not None and live.order.is_closed:
                current_report = reported.get(live.order.client_order_id)
                if current_report is None:
                    raise BitfinexV1ExecutionError("Bitfinex working reconciliation lost its order")
                fills = self._reconciliation_fills(mass_status, current_report)
                if live.terminal is not None:
                    self._validate_streamed_terminal_report(live, current_report, fills)
                self._reconcile_live_report(
                    live, current_report, fills, allow_terminal_recovery=True,
                )
                continue
            if (
                not live.submitted_in_process
                or live.accepted
                or live.venue_order_id is not None
                or live.terminal is not None
                or live.rejection_key is not None
            ):
                continue
            pending_report = reported.get(live.order.client_order_id)
            if pending_report is None:
                continue
            self._reconcile_live_report(
                live,
                pending_report,
                self._reconciliation_fills(mass_status, pending_report),
                allow_terminal_recovery=True,
            )
        cached_open_orders = self._cache.orders_open(
            instrument_id=self._bfx_config.instrument_id,
        )
        for order in cached_open_orders:
            binding = self._cid_store.binding_for_client(order.client_order_id.value)
            if binding is None:
                if order.account_id == self.account_id:
                    raise BitfinexV1ExecutionError(
                        "cached open Bitfinex order has no persisted CID binding"
                    )
                continue
            if order.account_id != self.account_id:
                raise BitfinexV1ExecutionError(
                    "persisted Bitfinex CID belongs to a different cached account"
                )
            cached_report = reported.get(order.client_order_id)
            if cached_report is None:
                raise BitfinexV1ExecutionError(
                    "cached open Bitfinex order is absent from venue reconciliation reports"
                )
            cached_live = self._live_for_client(order.client_order_id)
            if cached_live is None:
                raise BitfinexV1ExecutionError(
                    "cached open Bitfinex order cannot be reconstructed for reconciliation"
                )
            fills = self._reconciliation_fills(mass_status, cached_report)
            if cached_live.terminal is not None:
                self._validate_streamed_terminal_report(cached_live, cached_report, fills)
            self._reconcile_live_report(
                cached_live,
                cached_report,
                fills,
                allow_terminal_recovery=True,
            )
        position_reports = mass_status.position_reports.get(
            self._bfx_config.instrument_id,
            [],
        )
        if len(position_reports) != 1:
            raise BitfinexV1ExecutionError(
                "Bitfinex reconciliation requires one NETTING position report"
            )
        position_report = position_reports[0]
        if position_report.account_id != self.account_id:
            raise BitfinexV1ExecutionError(
                "Bitfinex position report belongs to a different account"
            )
        cached_open_positions = self._cache.positions_open(
            instrument_id=self._bfx_config.instrument_id,
            account_id=self.account_id,
        )
        cached_position_qty = sum(
            (position.signed_decimal_qty() for position in cached_open_positions),
            Decimal(),
        )
        missing_fill_delta = Decimal()
        for client_order_id, report in reported.items():
            cached_order = self._cache.order(client_order_id)
            candidate_live = self._by_cid.get(self._cid_by_client.get(client_order_id, -1))
            silent_submit_is_reconcilable = (
                candidate_live is not None
                and self._silent_terminal_reconciliation_due(candidate_live)
                and cached_order is candidate_live.order
                and cached_order.status == OrderStatus.SUBMITTED
            )
            if (
                cached_order is None
                or (not cached_order.is_open and not silent_submit_is_reconcilable)
                or report.filled_qty <= cached_order.filled_qty
            ):
                continue
            signed_qty = report.filled_qty.as_decimal() - cached_order.filled_qty.as_decimal()
            if report.order_side == OrderSide.SELL:
                signed_qty = -signed_qty
            missing_fill_delta += signed_qty
        if (
            cached_position_qty + missing_fill_delta != position_report.signed_decimal_qty
            and not self._can_delegate_cold_position_reconciliation(
                reported=reported,
                cached_open_orders=cached_open_orders,
                cached_open_positions=cached_open_positions,
                position_report=position_report,
                missing_fill_delta=missing_fill_delta,
            )
        ):
            raise BitfinexV1ExecutionError(
                "cached Bitfinex position differs from the venue NETTING position"
            )
        return mass_status

    def _validate_terminal_fill_evidence(
        self, live: _LiveOrder, report: OrderStatusReport, fills: list[FillReport],
        *, allow_working: bool = False,
    ) -> None:
        # NT closes canceled orders before considering missing quantity. Require
        # actual trades for partial cancels and every silent terminal recovery.
        self._validate_silent_terminal_fills(
            report, fills,
            liquidity_side=LiquiditySide.MAKER if live.order.is_post_only else LiquiditySide.TAKER,
            allow_working=allow_working,
        )
        reported_ids = {fill.trade_id for fill in fills}
        if any(
            isinstance(event, OrderFilled) and event.trade_id.value.isdecimal()
            and event.trade_id not in reported_ids
            for event in live.order.events
        ):
            raise BitfinexV1ExecutionError(
                "Bitfinex complete trade set omits a previously applied real fill",
            )
        metadata = self._cid_store.fee_metadata_for_cid(live.cid)
        raw_trades = {} if metadata is None else {
            str(row.trade_id): row for row in metadata.venue_trades
        }
        for fill in fills:
            raw = raw_trades.get(fill.trade_id.value)
            if (
                raw is None
                or raw.order_type != ("IOC" if report.time_in_force == TimeInForce.IOC else "LIMIT")
                or raw.order_price not in live.known_prices | {live.pending_modify_price}
                or not report.ts_accepted <= fill.ts_event <= report.ts_last
            ):
                raise BitfinexV1ExecutionError(
                    "Bitfinex terminal trade differs from order execution terms",
                )

    @staticmethod
    def _reconciliation_fills(
        mass_status: ExecutionMassStatus,
        report: OrderStatusReport,
    ) -> list[FillReport]:
        matching = mass_status.fill_reports.get(report.venue_order_id, [])
        for fills in mass_status.fill_reports.values():
            for fill in fills:
                if (
                    fill.client_order_id == report.client_order_id
                    and fill.venue_order_id != report.venue_order_id
                ):
                    raise BitfinexV1ExecutionError(
                        "Bitfinex reconciliation changes CID/order identity"
                    )
        return matching

    def _can_delegate_cold_position_reconciliation(
        self,
        *,
        reported: dict[ClientOrderId, OrderStatusReport],
        cached_open_orders: list[Order],
        cached_open_positions: list[Position],
        position_report: PositionStatusReport,
        missing_fill_delta: Decimal,
    ) -> bool:
        """Allow Nautilus to synthesize a cold position only from an empty state."""
        return (
            self._bfx_config.allow_cold_position_reconciliation
            and position_report.signed_decimal_qty != 0
            and missing_fill_delta == 0
            and not cached_open_orders
            and not cached_open_positions
            and not self._cache.orders(instrument_id=self._bfx_config.instrument_id)
            and not self._cache.positions(instrument_id=self._bfx_config.instrument_id)
            and not self._by_cid
            and not self._unbound_paper_interim_fills
            and not any(
                report.order_status
                in {
                    OrderStatus.ACCEPTED,
                    OrderStatus.PARTIALLY_FILLED,
                }
                for report in reported.values()
            )
        )

    def _validate_cached_open_report(
        self,
        order: Order,
        report: OrderStatusReport,
        fills: list[FillReport],
        *,
        allow_missing_post_only: bool = False,
        expected_prices: set[Decimal] | None = None,
    ) -> None:
        mismatched: list[str] = []
        if order.account_id != report.account_id:
            mismatched.append("account_id")
        if order.instrument_id != report.instrument_id:
            mismatched.append("instrument_id")
        if order.client_order_id != report.client_order_id:
            mismatched.append("client_order_id")
        if order.venue_order_id is not None and order.venue_order_id != report.venue_order_id:
            mismatched.append("venue_order_id")
        if order.side != report.order_side:
            mismatched.append("side")
        if order.order_type != report.order_type:
            mismatched.append("order_type")
        if order.time_in_force != report.time_in_force:
            mismatched.append("time_in_force")
        post_only_mismatch = cast(bool, order.is_post_only) != report.post_only
        missing_post_only_is_opaque = (
            allow_missing_post_only and cast(bool, order.is_post_only) and not report.post_only
        )
        if post_only_mismatch and not missing_post_only_is_opaque:
            mismatched.append("post_only")
        if cast(bool, order.is_reduce_only) != report.reduce_only:
            mismatched.append("reduce_only")
        if order.quantity != report.quantity:
            mismatched.append("quantity")
        if report.trigger_price is not None:
            mismatched.append("trigger_price")
        allowed_prices = expected_prices or {Decimal(str(order.price))}
        if report.price is None or report.price.as_decimal() not in allowed_prices:
            mismatched.append("price")
        if mismatched:
            raise BitfinexV1ExecutionError(
                "cached open Bitfinex order differs from venue report: " + ", ".join(mismatched)
            )

        if report.filled_qty < order.filled_qty:
            raise BitfinexV1ExecutionError(
                "venue reconciliation regressed the cached filled quantity"
            )
        same_filled_quantity = order.filled_qty == report.filled_qty

        cached_fills = {
            event.trade_id: event for event in order.events if isinstance(event, OrderFilled)
        }
        for fill in fills:
            event = cached_fills.get(fill.trade_id)
            if event is None:
                if same_filled_quantity:
                    raise BitfinexV1ExecutionError(
                        "venue reconciliation contains an unknown fill for a cached open order"
                    )
                continue
            fill_mismatches: list[str] = []
            if event.account_id != fill.account_id:
                fill_mismatches.append("account_id")
            if event.instrument_id != fill.instrument_id:
                fill_mismatches.append("instrument_id")
            if event.client_order_id != fill.client_order_id:
                fill_mismatches.append("client_order_id")
            if event.venue_order_id != fill.venue_order_id:
                fill_mismatches.append("venue_order_id")
            if event.order_side != fill.order_side:
                fill_mismatches.append("side")
            if event.last_qty != fill.last_qty:
                fill_mismatches.append("last_qty")
            if event.last_px != fill.last_px:
                fill_mismatches.append("last_px")
            if (
                event.commission.currency != fill.commission.currency
                or event.commission.as_decimal() != fill.commission.as_decimal()
            ) and not self._provisional_fee_supplement(event, fill):
                fill_mismatches.append("commission")
            if event.liquidity_side != fill.liquidity_side:
                fill_mismatches.append("liquidity_side")
            if event.ts_event != fill.ts_event:
                fill_mismatches.append("ts_event")
            if fill_mismatches:
                raise BitfinexV1ExecutionError(
                    "cached Bitfinex fill differs from venue report: " + ", ".join(fill_mismatches)
                )

        if same_filled_quantity:
            cached_avg_px = float(order.avg_px)
            reported_avg_px = 0.0 if report.avg_px is None else float(report.avg_px)
            if not isclose(cached_avg_px, reported_avg_px):
                raise BitfinexV1ExecutionError(
                    "cached open Bitfinex order average price differs from venue report"
                )

        nautilus_will_deduplicate = order.status == report.order_status and same_filled_quantity
        if not nautilus_will_deduplicate:
            return
        if order.venue_order_id is None:
            raise BitfinexV1ExecutionError(
                "cached open Bitfinex order is missing its venue order identity"
            )

    def _provisional_fee_supplement(self, event: OrderFilled, report: FillReport) -> bool:
        if (
            self._bfx_config.raw_symbol != PAPER_RAW_SYMBOL
            or event.commission != Money(0, USD)
            or report.commission.currency != USD
        ):
            return False
        binding = self._cid_store.binding_for_client(event.client_order_id.value)
        metadata = None if binding is None else self._cid_store.fee_metadata_for_cid(binding.cid)
        if metadata is None:
            return False
        native = next(
            (row for row in metadata.native_fills if row.trade_id == event.trade_id.value), None,
        )
        trade = next(
            (row for row in metadata.venue_trades if str(row.trade_id) == event.trade_id.value),
            None,
        )
        return (
            native is not None and native.native_fill_origin == "te_paper"
            and native == _native_fill_evidence(event, "te_paper")
            and trade is not None and trade.raw_fee is not None
            and native_fill_matches_trade(native, trade)
            and usd_commission(trade.raw_fee) == report.commission
        )

    def _validate_streamed_terminal_report(
        self, live: _LiveOrder, report: OrderStatusReport, fills: list[FillReport],
    ) -> None:
        self._validate_cached_open_report(
            live.order, report, fills,
            allow_missing_post_only=self._terminal_post_only_is_opaque(live),
        )
        streamed = self._terminal_report(live)
        if self._terminal_post_only_is_opaque(live) and report.post_only:
            # Normalize only the already-authenticated missing stream bit.
            streamed.post_only = cast(bool, live.order.is_post_only)
        if not _same_order_report_facts(streamed, report):
            raise BitfinexV1ExecutionError("Bitfinex REST report differs from streamed terminal")
        if report.order_status in {OrderStatus.CANCELED, OrderStatus.EXPIRED} or (
            live.reconciled_working is not None
        ):
            self._validate_terminal_fill_evidence(live, report, fills)

    def _reconcile_live_report(
        self,
        live: _LiveOrder,
        report: OrderStatusReport,
        fills: list[FillReport],
        *,
        allow_missing_post_only: bool = False,
        allow_terminal_recovery: bool = False,
    ) -> bool:
        """Validate one owned report completely before granting adapter authority."""
        if live.terminal is not None or live.rejection_key is not None:
            return False
        venue_text = report.venue_order_id.value
        if not venue_text.isdigit() or int(venue_text) <= 0:
            raise BitfinexV1ExecutionError("reconciled Bitfinex venue order ID is invalid")
        venue_order_id = int(venue_text)
        if live.venue_order_id not in {None, venue_order_id}:
            raise BitfinexV1ExecutionError(
                "cached open Bitfinex order differs from venue report: venue_order_id"
            )
        existing_cid = self._cid_by_venue.get(venue_order_id)
        if existing_cid not in {None, live.cid}:
            raise BitfinexV1ExecutionError("Bitfinex venue order ID changed CID")
        expected_prices = {live.current_price}
        if live.pending_modify_price is not None:
            expected_prices.add(live.pending_modify_price)
        self._validate_cached_open_report(
            live.order,
            report,
            fills,
            allow_missing_post_only=allow_missing_post_only,
            expected_prices=expected_prices,
        )
        working_maker = self._same_run_paper_maker(live)
        if working_maker and (
            report.order_status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}
            or report.filled_qty.as_decimal() > 0
        ):
            self._validate_terminal_fill_evidence(live, report, fills, allow_working=True)
        if report.order_status in {OrderStatus.CANCELED, OrderStatus.EXPIRED}:
            self._validate_terminal_fill_evidence(live, report, fills)
        live.venue_flags_verified = True
        report_can_bind = report.order_status in {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        }
        if not report_can_bind:
            if allow_terminal_recovery and (
                self._silent_terminal_reconciliation_due(live) or working_maker
            ):
                self._validate_terminal_fill_evidence(live, report, fills)
                previous = live.reconciled_terminal
                if previous is not None and not _same_order_report_facts(previous, report):
                    raise BitfinexV1ExecutionError(
                        "Bitfinex REST terminal changed its reconciliation facts"
                    )
                live.venue_order_id = venue_order_id
                self._cid_by_venue[venue_order_id] = live.cid
                live.reconciled_terminal = report
                live.reconciled_working = None
            return True

        if working_maker and report.filled_qty.as_decimal() > 0:
            live.reconciled_working = report
        private_stream_won = live.accepted and live.submitted_in_process
        live.venue_order_id = venue_order_id
        self._cid_by_venue[venue_order_id] = live.cid
        live.accepted = True
        self._resolve_mutation(live, "submit")
        return not private_stream_won

    @staticmethod
    def _validate_silent_terminal_fills(
        report: OrderStatusReport,
        fills: list[FillReport],
        *,
        liquidity_side: LiquiditySide = LiquiditySide.TAKER,
        allow_working: bool = False,
    ) -> None:
        """Require exact authenticated trade evidence for a silent terminal fill."""
        if report.order_status not in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        } and not (allow_working and report.order_status == OrderStatus.PARTIALLY_FILLED):
            raise BitfinexV1ExecutionError(
                "silent Bitfinex terminal recovery received a non-terminal report"
            )
        expected_filled = report.filled_qty.as_decimal()
        seen_trades: set[TradeId] = set()
        reported_filled = Decimal()
        weighted_price = Decimal()
        for fill in fills:
            if fill.trade_id in seen_trades:
                raise BitfinexV1ExecutionError(
                    "silent Bitfinex terminal recovery repeated a trade ID"
                )
            seen_trades.add(fill.trade_id)
            if (
                fill.account_id != report.account_id
                or fill.instrument_id != report.instrument_id
                or fill.client_order_id != report.client_order_id
                or fill.venue_order_id != report.venue_order_id
                or fill.order_side != report.order_side
                or fill.liquidity_side != liquidity_side
            ):
                raise BitfinexV1ExecutionError(
                    "silent Bitfinex terminal trade differs from its order report"
                )
            quantity = fill.last_qty.as_decimal()
            reported_filled += quantity
            weighted_price += quantity * fill.last_px.as_decimal()
        if reported_filled != expected_filled:
            raise BitfinexV1ExecutionError(
                "silent Bitfinex terminal recovery lacks exact trade quantity evidence"
            )
        if expected_filled == 0:
            if report.avg_px is not None:
                raise BitfinexV1ExecutionError(
                    "unfilled Bitfinex terminal unexpectedly has an average price"
                )
            return
        if report.avg_px is None or not isclose(
            float(weighted_price / expected_filled),
            float(report.avg_px),
        ):
            raise BitfinexV1ExecutionError(
                "silent Bitfinex terminal trade average differs from its order report"
            )

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        if command.client_order_id is None and command.venue_order_id is None:
            raise ValueError("client_order_id and venue_order_id cannot both be None")
        if command.instrument_id not in {None, self._bfx_config.instrument_id}:
            return None
        reports = await self._order_reports(start=None, end=None, open_only=False)
        for report in reports:
            if (
                command.client_order_id is not None
                and report.client_order_id != command.client_order_id
            ):
                continue
            if (
                command.venue_order_id is not None
                and report.venue_order_id != command.venue_order_id
            ):
                continue
            if report.client_order_id is not None:
                live = self._live_for_client(report.client_order_id)
                if live is not None and live.terminal is None and live.rejection_key is None:
                    report_is_unfilled_active = report.order_status == OrderStatus.ACCEPTED
                    paper_omits_post_only = (
                        report_is_unfilled_active
                        and self._bfx_config.raw_symbol == PAPER_RAW_SYMBOL
                        and live.submitted_in_process
                        and cast(bool, live.order.is_post_only)
                        and live.order.order_type == OrderType.LIMIT
                        and live.order.time_in_force == TimeInForce.GTC
                        and not report.post_only
                    )
                    if not self._reconcile_live_report(
                        live,
                        report,
                        [],
                        allow_missing_post_only=paper_omits_post_only,
                    ):
                        return None
            return report
        return None

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        if command.instrument_id not in {None, self._bfx_config.instrument_id}:
            return []
        return await self._order_reports(
            start=command.start,
            end=command.end,
            open_only=command.open_only,
        )

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        if command.instrument_id not in {None, self._bfx_config.instrument_id}:
            return []
        instrument = self._report_instrument()
        start_ms, end_ms = self._report_window(command.start, command.end)
        rows = await self._trade_history_rows(start_ms, end_ms)
        observations: list[TradeUpdate] = []
        reports = map_fill_reports(
            rows=rows,
            instrument=instrument,
            account_id=self.account_id,
            cid_lookup=self._client_order_id_for_cid,
            fee_currency=self._bfx_config.fee_currency,
            ts_init=self._clock.timestamp_ns(),
            trade_observations=observations,
        )
        for trade in observations:
            if command.venue_order_id not in {None, VenueOrderId(str(trade.venue_order_id))}:
                continue
            if trade.client_order_id is None:
                continue
            self._record_fee_trade(trade.client_order_id, trade)
            client_order_id = self._client_order_id_for_cid(trade.client_order_id)
            cached = None if client_order_id is None else self._cache.order(client_order_id)
            if cached is not None:
                for event in cached.events:
                    self._capture_native_fee_evidence(event)
        if command.venue_order_id is not None:
            reports = [
                report for report in reports if report.venue_order_id == command.venue_order_id
            ]
        return reports

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        if command.instrument_id not in {None, self._bfx_config.instrument_id}:
            return []
        return map_position_status_reports(
            rows=_rows(await self._rest.positions(), "positions"),
            instrument=self._report_instrument(),
            account_id=self.account_id,
            ts_init=self._clock.timestamp_ns(),
        )

    async def _order_reports(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
        open_only: bool,
    ) -> list[OrderStatusReport]:
        instrument = self._report_instrument()
        active_rows = _rows(
            await self._rest.active_orders_by_symbol(self._bfx_config.raw_symbol),
            "active orders",
        )
        history_rows: list[object] = []
        if not open_only:
            start_ms, end_ms = self._report_window(start, end)
            history_rows = await self._order_history_rows(start_ms, end_ms)
        return map_order_status_reports(
            active_rows=active_rows,
            history_rows=history_rows,
            instrument=instrument,
            account_id=self.account_id,
            cid_lookup=self._client_order_id_for_cid,
            ts_init=self._clock.timestamp_ns(),
        )

    async def _order_history_rows(self, start_ms: int, end_ms: int) -> list[object]:
        pending = [(start_ms, end_ms)]
        result: list[object] = []
        timestamp_axes = {
            4: "order creation timestamp",
            5: "order update timestamp",
        }
        candidate_axes = set(timestamp_axes)
        while pending:
            window_start, window_end = pending.pop()
            page = _rows(
                await self._rest.order_history_by_symbol(
                    self._bfx_config.raw_symbol,
                    start=window_start,
                    end=window_end,
                    limit=_REPORT_PAGE_LIMIT,
                ),
                "order history",
            )
            page_axes = {
                index
                for index in candidate_axes
                if all(
                    window_start <= _row_int(row, index, timestamp_axes[index]) <= window_end
                    for row in page
                )
            }
            candidate_axes.intersection_update(page_axes)
            if not candidate_axes:
                raise BitfinexV1ExecutionError(
                    "Bitfinex order history has no consistent requested-window timestamp"
                )
            if len(page) < _REPORT_PAGE_LIMIT:
                result.extend(page)
                continue
            if window_start == window_end:
                raise BitfinexV1ExecutionError(
                    "Bitfinex order history saturated a one-millisecond window"
                )
            midpoint = (window_start + window_end) // 2
            pending.append((midpoint + 1, window_end))
            pending.append((window_start, midpoint))
        return result

    async def _trade_history_rows(self, start_ms: int, end_ms: int) -> list[object]:
        cursor = start_ms
        result: dict[int, object] = {}
        while True:
            page = _rows(
                await self._rest.trades_by_symbol(
                    self._bfx_config.raw_symbol,
                    start=cursor,
                    end=end_ms,
                    limit=_REPORT_PAGE_LIMIT,
                ),
                "trade history",
            )
            previous_key: tuple[int, int] | None = None
            new_ids = 0
            last_ms = cursor
            for row in page:
                trade_id = _row_int(row, 0, "trade ID")
                event_ms = _row_int(row, 2, "trade timestamp")
                if not cursor <= event_ms <= end_ms:
                    raise BitfinexV1ExecutionError(
                        "Bitfinex trade history escaped its requested window"
                    )
                key = (event_ms, trade_id)
                if previous_key is not None and key < previous_key:
                    raise BitfinexV1ExecutionError("Bitfinex trade history is not ascending")
                previous_key = key
                previous = result.get(trade_id)
                if previous is not None:
                    if previous != row:
                        raise BitfinexV1ExecutionError("Bitfinex trade ID changed report facts")
                else:
                    result[trade_id] = row
                    new_ids += 1
                last_ms = event_ms
            if len(page) < _REPORT_PAGE_LIMIT:
                break
            if new_ids == 0 or last_ms < cursor:
                raise BitfinexV1ExecutionError("Bitfinex trade pagination is saturated")
            cursor = last_ms
        return list(result.values())

    def _report_window(
        self,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[int, int]:
        now_ms = cast(int, self._clock.timestamp_ns()) // 1_000_000
        oldest_ms = max(0, now_ms - _MAX_REPORT_LOOKBACK_MS)
        end_ms = min(_datetime_ms(end), now_ms) if end is not None else now_ms
        start_ms = _datetime_ms(start) if start is not None else oldest_ms
        if start_ms < oldest_ms:
            raise BitfinexV1ExecutionError(
                "Bitfinex closed-order reports cannot exceed the two-week venue window"
            )
        if end_ms < start_ms:
            raise ValueError("report start must not exceed report end")
        return start_ms, end_ms

    def _report_instrument(self) -> Instrument:
        instrument = self._instrument(self._bfx_config.instrument_id)
        if instrument is None:
            raise BitfinexV1ExecutionError("configured Bitfinex instrument is unavailable")
        return instrument

    def _client_order_id_for_cid(self, cid: int) -> ClientOrderId | None:
        binding = self._cid_store.binding_for_cid(cid)
        return ClientOrderId(binding.client_order_id) if binding is not None else None


class BitfinexV1LiveExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LiveExecutionClient:
        if not isinstance(config, BitfinexV1ExecClientConfig):
            raise TypeError("Bitfinex factory requires BitfinexV1ExecClientConfig")
        if name != "BITFINEX":
            raise ValueError("Bitfinex v1 client name must be BITFINEX")
        return BitfinexV1ExecutionClient(
            loop=loop,
            name=name,
            config=config,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(config=config.instrument_provider),
        )


def _rows(value: object, label: str) -> list[object]:
    if not isinstance(value, list) or any(not isinstance(row, list) for row in value):
        raise BitfinexV1ExecutionError(f"Bitfinex {label} response must be an array of arrays")
    return cast(list[object], value)


def _row_int(value: object, index: int, label: str) -> int:
    if not isinstance(value, list) or len(value) <= index or type(value[index]) is not int:
        raise BitfinexV1ExecutionError(f"Bitfinex {label} must be an exact integer")
    return cast(int, value[index])


def _datetime_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("report timestamps must be timezone-aware")
    return int(value.timestamp() * 1_000)


def _leverage(params: dict[str, object]) -> int:
    if set(params) != {"leverage"}:
        raise BitfinexV1ExecutionError("Bitfinex derivative command requires only leverage")
    value = params["leverage"]
    if type(value) is not int or not 1 <= value <= 100:
        raise BitfinexV1ExecutionError("Bitfinex derivative leverage must be an integer 1..100")
    return value


def _initial_rejection(status: str) -> str | None:
    upper = status.upper()
    for rejection in ("POSTONLY CANCELED", "INSUFFICIENT MARGIN", "RSN_DUST", "RSN_PAUSE"):
        if upper.startswith(rejection):
            return rejection
    return None


def _terminal_filled(state: OrderState) -> Decimal:
    return abs(state.original_qty) - abs(state.remaining_qty)


def _same_execution(interim: TradeExecution, update: TradeUpdate) -> bool:
    return (
        interim.trade_id == update.trade_id
        and interim.symbol == update.symbol
        and interim.ts_event_ms == update.ts_event_ms
        and interim.venue_order_id == update.venue_order_id
        and interim.execution_qty == update.execution_qty
        and interim.execution_price == update.execution_price
        and interim.order_type == update.order_type
        and interim.order_price == update.order_price
        and interim.maker == update.maker
        and interim.client_order_id in {None, update.client_order_id}
    )


def _fee_trade_evidence(trade: TradeExecution | TradeUpdate) -> BitfinexFeeTrade:
    return BitfinexFeeTrade(
        trade.trade_id, trade.ts_event_ms, trade.execution_qty, trade.execution_price,
        trade.order_type, trade.order_price, trade.maker,
        trade.fee if isinstance(trade, TradeUpdate) else None,
        trade.fee_currency if isinstance(trade, TradeUpdate) else None,
    )


def _native_fill_evidence(event: OrderFilled, origin: str) -> BitfinexNativeFill:
    return BitfinexNativeFill(
        event.trade_id.value, origin,
        event.last_qty.as_decimal() * (1 if event.order_side == OrderSide.BUY else -1),
        event.last_px.as_decimal(), event.ts_event, event.liquidity_side.name,
        event.commission.as_decimal(), event.commission.currency.code,
    )


def _same_order_report_facts(left: OrderStatusReport, right: OrderStatusReport) -> bool:
    return (
        left.account_id == right.account_id
        and left.instrument_id == right.instrument_id
        and left.client_order_id == right.client_order_id
        and left.venue_order_id == right.venue_order_id
        and left.order_side == right.order_side
        and left.order_type == right.order_type
        and left.time_in_force == right.time_in_force
        and left.order_status == right.order_status
        and left.quantity == right.quantity
        and left.filled_qty == right.filled_qty
        and left.price == right.price
        and left.avg_px == right.avg_px
        and left.post_only == right.post_only
        and left.reduce_only == right.reduce_only
        and left.cancel_reason == right.cancel_reason
        and left.ts_accepted == right.ts_accepted
        and left.ts_last == right.ts_last
    )


def _terminal_disposition(state: OrderState) -> str:
    status = state.status.upper()
    terminal_filled = _terminal_filled(state)
    if status.startswith("EXECUTED"):
        if terminal_filled != abs(state.original_qty):
            raise BitfinexV1ExecutionError(
                "Bitfinex EXECUTED terminal does not report the full order quantity"
            )
        return "executed"
    if status.startswith(
        (
            "CANCELED",
            "IOC CANCELED",
            "POSTONLY CANCELED",
            "INSUFFICIENT MARGIN",
            "RSN_DUST",
            "RSN_PAUSE",
        )
    ):
        return "canceled"
    raise BitfinexV1ExecutionError(f"unsupported terminal order status {state.status!r}")


def _private_frame_type(frame: object) -> str:
    """Return only a bounded event label; never expose authenticated payload values."""
    if isinstance(frame, list) and len(frame) >= 2:
        message_type = frame[1]
        if isinstance(message_type, str) and message_type in _PRIVATE_FRAME_TYPES:
            return message_type
    if isinstance(frame, dict):
        event = frame.get("event")
        if isinstance(event, str) and event in {"auth", "error", "info"}:
            return f"control_{event}"
    return "other"


def _validate_config(config: BitfinexV1ExecClientConfig) -> None:
    if not config.url.startswith("wss://"):
        raise ValueError("Bitfinex private execution URL must use wss://")
    if not config.rest_url.startswith("https://"):
        raise ValueError("Bitfinex private REST URL must use https://")
    if config.instrument_id != SUPPORTED_INSTRUMENT_ID:
        raise ValueError("Bitfinex execution supports only the configured XAUT perpetual")
    expected_wallet = _WALLET_BY_RAW_SYMBOL.get(config.raw_symbol)
    if expected_wallet is None:
        raise ValueError("Bitfinex execution supports only the production or paper XAUT profile")
    expected_fee_currency = _FEE_CURRENCY_BY_RAW_SYMBOL[config.raw_symbol]
    if config.account_id.get_issuer() != "BITFINEX":
        raise ValueError("Bitfinex account ID must use the BITFINEX issuer")
    if not config.api_key or not config.api_secret:
        raise ValueError("Bitfinex API credentials must be non-empty")
    if type(config.user_id) is not int or config.user_id <= 0:
        raise ValueError("Bitfinex user ID must be a positive exact integer")
    if config.wallet_currency != expected_wallet:
        raise ValueError(f"Bitfinex {config.raw_symbol} wallet currency must be {expected_wallet}")
    if config.fee_currency != expected_fee_currency:
        raise ValueError(
            f"Bitfinex {config.raw_symbol} trade fee currency must be {expected_fee_currency}"
        )
    if type(config.allow_cold_position_reconciliation) is not bool:
        raise ValueError("Bitfinex cold position reconciliation opt-in must be an exact bool")
    if not config.cid_store_path:
        raise ValueError("Bitfinex CID path must be non-empty")
    if not 100 <= config.auth_timeout_ms <= 60_000:
        raise ValueError("Bitfinex auth timeout is outside the supported range")
    if not 100 <= config.open_timeout_ms <= 60_000:
        raise ValueError("Bitfinex open timeout is outside the supported range")
    if not 100 <= config.mutation_ack_timeout_ms <= 60_000:
        raise ValueError("Bitfinex mutation acknowledgment timeout is outside the supported range")
    if not 1 <= config.rest_timeout_secs <= 60:
        raise ValueError("Bitfinex REST timeout is outside the supported range")


__all__ = [
    "BITFINEX",
    "BitfinexV1ExecClientConfig",
    "BitfinexV1ExecutionClient",
    "BitfinexV1ExecutionError",
    "BitfinexV1LiveExecClientFactory",
]
