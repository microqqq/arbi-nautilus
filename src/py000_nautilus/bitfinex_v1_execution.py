"""Minimal same-process Bitfinex derivative execution for the PY000 source leg."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import isclose
from typing import Protocol, cast

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveExecClientConfig
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
from nautilus_trader.model.currencies import USDT
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

from py000_nautilus.bitfinex_v1_cids import BitfinexV1CidError, BitfinexV1CidStore
from py000_nautilus.bitfinex_v1_data import INSTRUMENT_ID as SUPPORTED_INSTRUMENT_ID
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL
from py000_nautilus.bitfinex_v1_data import RAW_SYMBOL as PRODUCTION_RAW_SYMBOL
from py000_nautilus.bitfinex_v1_protocol import (
    MAX_AUTH_NONCE,
    POST_ONLY_FLAG,
    Notification,
    OrderEvent,
    OrderSnapshot,
    OrderState,
    PositionEvent,
    TradeUpdate,
    WalletEvent,
    auth_message,
    cancel_order_op,
    parse_private_message,
    submit_order_op,
    update_order_op,
    validate_interim_trade_message,
)
from py000_nautilus.bitfinex_v1_reports import (
    map_fill_reports,
    map_order_status_reports,
    map_position_status_reports,
)
from py000_nautilus.bitfinex_v1_rest import BitfinexV1RestClient
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport

BITFINEX = Venue("BITFINEX")
_WALLET_BY_RAW_SYMBOL = {
    PRODUCTION_RAW_SYMBOL: "USTF0",
    PAPER_RAW_SYMBOL: "TESTUSDTF0",
}
_REPORT_PAGE_LIMIT = 2_500
_MAX_REPORT_LOOKBACK_MS = 14 * 24 * 60 * 60 * 1_000


class BitfinexV1ExecutionError(RuntimeError):
    """The bounded private execution stream cannot prove a safe outcome."""


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
    terminal_emitted: bool = False
    rejection_key: tuple[str, int | None, str] | None = None


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
        self._seen_trades: dict[int, tuple[int, Decimal, Decimal, Decimal, str]] = {}
        self._ack_deadlines: dict[tuple[int, str], asyncio.TimerHandle] = {}

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
        if any(
            live.terminal is not None and not live.terminal_emitted
            for live in self._by_cid.values()
        ):
            return "Bitfinex terminal order is waiting for authoritative TU fills"
        if not self._running or not self._account_ready:
            return "Bitfinex execution is not connected and account-ready"
        return None

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
                raise BitfinexV1ExecutionError(
                    "configured Bitfinex instrument is unavailable"
                )
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
        try:
            while self._running:
                self._consume_private_frame(await self._transport.recv_json())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._running = False
            self._stop_ack_deadlines(mark_unknown=True)
            self._record_fatal("private reader", exc)
            await self._transport.close()
            self._set_connected(False)

    def _consume_private_frame(self, frame: dict[str, object] | list[object]) -> None:
        if isinstance(frame, dict):
            raise BitfinexV1ExecutionError("unexpected private control event after authentication")
        if len(frame) == 2 and type(frame[0]) is int and frame == [0, "hb"]:
            return
        if len(frame) == 3 and type(frame[0]) is int and frame[:2] == [0, "te"]:
            validate_interim_trade_message(frame)
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
        elif isinstance(message, OrderSnapshot | PositionEvent):
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
        try:
            await self._transport.send_json(payload)
        except BaseException as exc:
            live.unknown_operations.add(operation)
            self._last_failure = f"{operation} send UNKNOWN: {type(exc).__name__}: {exc}"[:300]
            raise
        if not self._running or not self._account_ready:
            live.unknown_operations.add(operation)
            self._last_failure = (
                f"{operation} sent while the private stream closed; outcome UNKNOWN"
            )
            return
        if self._operation_is_pending(live, operation):
            try:
                self._start_ack_deadline(live, operation)
            except BaseException as exc:
                live.unknown_operations.add(operation)
                self._last_failure = (
                    f"{operation} acknowledgment tracking UNKNOWN: "
                    f"{type(exc).__name__}: {exc}"
                )[:300]
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
        self._ack_deadlines[key] = self._loop.call_later(
            self._bfx_config.mutation_ack_timeout_ms / 1_000,
            self._expire_ack_deadline,
            live,
            operation,
        )

    def _expire_ack_deadline(self, live: _LiveOrder, operation: str) -> None:
        key = (live.cid, operation)
        if self._ack_deadlines.pop(key, None) is None:
            return
        live.unknown_operations.add(operation)
        self._last_failure = f"{operation} acknowledgment deadline expired; outcome UNKNOWN"

    def _resolve_mutation(self, live: _LiveOrder, operation: str) -> None:
        deadline = self._ack_deadlines.pop((live.cid, operation), None)
        if deadline is not None:
            deadline.cancel()
        live.unknown_operations.discard(operation)

    def _resolve_all_mutations(self, live: _LiveOrder) -> None:
        for operation in ("submit", "modify", "cancel"):
            self._resolve_mutation(live, operation)

    def _stop_ack_deadlines(self, *, mark_unknown: bool) -> None:
        pending = list(self._ack_deadlines.items())
        self._ack_deadlines.clear()
        for (cid, operation), deadline in pending:
            if mark_unknown:
                live = self._by_cid.get(cid)
                if live is not None:
                    live.unknown_operations.add(operation)
            deadline.cancel()
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
        if event.operation == "oc":
            _terminal_disposition(state)
        rejection = (
            _initial_rejection(state.status)
            if not live.accepted and _terminal_filled(state) == 0
            else None
        )
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
            self._finish_terminal_if_ready(live)

    def _handle_trade(self, trade: TradeUpdate) -> None:
        live = self._resolve_live(trade.client_order_id, trade.venue_order_id)
        if live is None:
            return
        if live.rejection_key is not None:
            raise BitfinexV1ExecutionError("Bitfinex trade followed a definitive rejection")
        self._validate_trade(live, trade)
        fingerprint = (
            trade.venue_order_id,
            trade.execution_qty,
            trade.execution_price,
            trade.fee,
            trade.fee_currency,
        )
        previous = self._seen_trades.get(trade.trade_id)
        if previous is not None:
            if previous != fingerprint:
                raise BitfinexV1ExecutionError("Bitfinex trade ID changed its execution facts")
            return
        if TradeId(str(trade.trade_id)) in live.order.trade_ids:
            return
        next_filled = live.filled_qty + abs(trade.execution_qty)
        if next_filled > Decimal(str(live.order.quantity)):
            raise BitfinexV1ExecutionError("Bitfinex fills exceed the submitted quantity")
        if live.terminal is not None:
            terminal_filled = _terminal_filled(live.terminal)
            if next_filled > terminal_filled:
                raise BitfinexV1ExecutionError(
                    "authoritative TU fill exceeds the terminal order quantity"
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
        self._seen_trades[trade.trade_id] = fingerprint
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
            Money(-trade.fee, USDT),
            LiquiditySide.MAKER if trade.maker else LiquiditySide.TAKER,
            trade.ts_event_ms * 1_000_000,
            info={
                "bitfinex_fee": format(trade.fee, "f"),
                "bitfinex_fee_currency": trade.fee_currency,
            },
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
                "authoritative TU fills exceed the Bitfinex terminal order quantity"
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
        expected_flags = POST_ONLY_FLAG if cast(bool, live.order.is_post_only) else 0
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

    def _validate_trade(self, live: _LiveOrder, trade: TradeUpdate) -> None:
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
            or trade.fee_currency.upper() != self._bfx_config.wallet_currency.upper()
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
        if order.is_reduce_only or order.is_quote_quantity or order.exec_algorithm_id is not None:
            return "Bitfinex v1 source execution does not support additional order semantics"
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
        if (
            instrument is not None
            and instrument.raw_symbol.value != self._bfx_config.raw_symbol
        ):
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
            or order.venue_order_id is None
            or order.price is None
        ):
            raise BitfinexV1ExecutionError(
                "cached Bitfinex order cannot be safely reconstructed"
            )
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
        self._last_failure = f"{context}: {type(exc).__name__}: {exc}"[:300]
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
        for order in self._cache.orders_open(
            instrument_id=self._bfx_config.instrument_id,
        ):
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
            self._validate_cached_open_report(
                order,
                cached_report,
                mass_status.fill_reports.get(cached_report.venue_order_id, []),
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
        cached_position_qty = sum(
            (
                position.signed_decimal_qty()
                for position in self._cache.positions_open(
                    instrument_id=self._bfx_config.instrument_id,
                    account_id=self.account_id,
                )
            ),
            Decimal(),
        )
        missing_fill_delta = Decimal()
        for fill_reports in mass_status.fill_reports.values():
            for fill_report in fill_reports:
                cached_order = (
                    self._cache.order(fill_report.client_order_id)
                    if fill_report.client_order_id is not None
                    else None
                )
                if (
                    cached_order is not None
                    and fill_report.trade_id in cached_order.trade_ids
                ):
                    continue
                order_report = (
                    reported.get(fill_report.client_order_id)
                    if fill_report.client_order_id is not None
                    else None
                )
                cached_open_order_can_advance = (
                    cached_order is not None
                    and cached_order.is_open
                    and order_report is not None
                    and order_report.filled_qty > cached_order.filled_qty
                )
                if not cached_open_order_can_advance:
                    continue
                signed_qty = fill_report.last_qty.as_decimal()
                if fill_report.order_side == OrderSide.SELL:
                    signed_qty = -signed_qty
                missing_fill_delta += signed_qty
        if (
            cached_position_qty + missing_fill_delta
            != position_report.signed_decimal_qty
        ):
            raise BitfinexV1ExecutionError(
                "cached Bitfinex position differs from the venue NETTING position"
            )
        return mass_status

    @staticmethod
    def _validate_cached_open_report(
        order: Order,
        report: OrderStatusReport,
        fills: list[FillReport],
        *,
        allow_missing_post_only: bool = False,
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
            allow_missing_post_only
            and cast(bool, order.is_post_only)
            and not report.post_only
        )
        if post_only_mismatch and not missing_post_only_is_opaque:
            mismatched.append("post_only")
        if cast(bool, order.is_reduce_only) != report.reduce_only:
            mismatched.append("reduce_only")
        if order.quantity != report.quantity:
            mismatched.append("quantity")
        if report.trigger_price is not None:
            mismatched.append("trigger_price")
        if mismatched:
            raise BitfinexV1ExecutionError(
                "cached open Bitfinex order differs from venue report: "
                + ", ".join(mismatched)
            )

        if report.filled_qty < order.filled_qty:
            raise BitfinexV1ExecutionError(
                "venue reconciliation regressed the cached filled quantity"
            )
        same_filled_quantity = order.filled_qty == report.filled_qty

        cached_fills = {
            event.trade_id: event
            for event in order.events
            if isinstance(event, OrderFilled)
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
            if event.commission != fill.commission:
                fill_mismatches.append("commission")
            if event.liquidity_side != fill.liquidity_side:
                fill_mismatches.append("liquidity_side")
            if event.ts_event != fill.ts_event:
                fill_mismatches.append("ts_event")
            if fill_mismatches:
                raise BitfinexV1ExecutionError(
                    "cached Bitfinex fill differs from venue report: "
                    + ", ".join(fill_mismatches)
                )

        if same_filled_quantity:
            cached_avg_px = float(order.avg_px)
            reported_avg_px = 0.0 if report.avg_px is None else float(report.avg_px)
            if not isclose(cached_avg_px, reported_avg_px):
                raise BitfinexV1ExecutionError(
                    "cached open Bitfinex order average price differs from venue report"
                )

        nautilus_will_deduplicate = (
            order.status == report.order_status and same_filled_quantity
        )
        if not nautilus_will_deduplicate:
            return
        if order.venue_order_id is None:
            raise BitfinexV1ExecutionError(
                "cached open Bitfinex order is missing its venue order identity"
            )
        if order.price != report.price:
            raise BitfinexV1ExecutionError(
                "cached open Bitfinex order price differs from venue report"
            )

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        if command.client_order_id is None and command.venue_order_id is None:
            raise ValueError("client_order_id and venue_order_id cannot both be None")
        if command.instrument_id not in {None, self._bfx_config.instrument_id}:
            return None
        pending_submit: _LiveOrder | None = None
        if command.client_order_id is not None:
            cid = self._cid_by_client.get(command.client_order_id)
            candidate = self._by_cid.get(cid) if cid is not None else None
            if (
                candidate is not None
                and candidate.submitted_in_process
                and (
                    not candidate.accepted
                    or candidate.order.status == OrderStatus.SUBMITTED
                )
                and candidate.terminal is None
                and candidate.rejection_key is None
            ):
                pending_submit = candidate
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
            if pending_submit is not None and not self._handle_pending_submit_report(
                pending_submit,
                report,
            ):
                return None
            return report
        return None

    def _handle_pending_submit_report(
        self,
        live: _LiveOrder,
        report: OrderStatusReport,
    ) -> bool:
        """Validate a targeted report, binding it or suppressing a private-stream race."""
        if live.terminal is not None or live.rejection_key is not None:
            return False
        venue_text = report.venue_order_id.value
        if not venue_text.isdigit() or int(venue_text) <= 0:
            raise BitfinexV1ExecutionError("targeted Bitfinex venue order ID is invalid")
        venue_order_id = int(venue_text)
        if live.venue_order_id not in {None, venue_order_id}:
            raise BitfinexV1ExecutionError("Bitfinex order changed venue ID")
        existing_cid = self._cid_by_venue.get(venue_order_id)
        if existing_cid not in {None, live.cid}:
            raise BitfinexV1ExecutionError("Bitfinex venue order ID changed CID")
        report_is_unfilled_active = report.order_status == OrderStatus.ACCEPTED
        report_can_bind = report.order_status in {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        }
        paper_omits_post_only = (
            report_is_unfilled_active
            and self._bfx_config.raw_symbol == PAPER_RAW_SYMBOL
            and live.submitted_in_process
            and cast(bool, live.order.is_post_only)
            and live.order.order_type == OrderType.LIMIT
            and live.order.time_in_force == TimeInForce.GTC
            and not report.post_only
        )
        self._validate_cached_open_report(
            live.order,
            report,
            [],
            allow_missing_post_only=paper_omits_post_only,
        )
        if report.price != live.order.price:
            raise BitfinexV1ExecutionError(
                "pending Bitfinex submit differs from targeted venue report: price"
            )
        if live.accepted:
            if live.venue_order_id is None:
                raise BitfinexV1ExecutionError(
                    "accepted Bitfinex order has no authoritative venue ID"
                )
            # A private-stream event arrived during the REST await and has already
            # been queued for Nautilus. Suppress the now-validated report so
            # strategies cannot observe the same transition twice.
            return False
        if not report_can_bind:
            return True

        # The caller publishes the returned report to Nautilus. Keep that as the
        # sole framework event while synchronizing the adapter before it can cancel.
        live.venue_order_id = venue_order_id
        self._cid_by_venue[venue_order_id] = live.cid
        live.accepted = True
        self._resolve_mutation(live, "submit")
        return True

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
        reports = map_fill_reports(
            rows=rows,
            instrument=instrument,
            account_id=self.account_id,
            cid_lookup=self._client_order_id_for_cid,
            fee_currency=self._bfx_config.wallet_currency,
            ts_init=self._clock.timestamp_ns(),
        )
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
    if config.account_id.get_issuer() != "BITFINEX":
        raise ValueError("Bitfinex account ID must use the BITFINEX issuer")
    if not config.api_key or not config.api_secret:
        raise ValueError("Bitfinex API credentials must be non-empty")
    if type(config.user_id) is not int or config.user_id <= 0:
        raise ValueError("Bitfinex user ID must be a positive exact integer")
    if config.wallet_currency != expected_wallet:
        raise ValueError(
            f"Bitfinex {config.raw_symbol} wallet currency must be {expected_wallet}"
        )
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
