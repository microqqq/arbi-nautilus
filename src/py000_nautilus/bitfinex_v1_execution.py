"""Minimal same-process Bitfinex derivative execution for the PY000 source leg."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
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
from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderType,
    TimeInForce,
)
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
from py000_nautilus.bitfinex_v1_data import RAW_SYMBOL as SUPPORTED_RAW_SYMBOL
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
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport

BITFINEX = Venue("BITFINEX")
SUPPORTED_WALLET_CURRENCY = "USTF0"


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
    wallet_currency: str = "USTF0"
    auth_timeout_ms: int = 10_000
    open_timeout_ms: int = 10_000


class _PrivateTransport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def send_json(self, payload: dict[str, object] | list[object]) -> None: ...
    async def recv_json(self) -> dict[str, object] | list[object]: ...


@dataclass(slots=True)
class _LiveOrder:
    order: Order
    instrument: Instrument
    cid: int
    current_price: Decimal
    known_prices: set[Decimal]
    unknown_operations: set[str]
    venue_order_id: int | None = None
    accepted: bool = False
    filled_qty: Decimal = Decimal(0)
    pending_modify_price: Decimal | None = None
    pending_cancel: bool = False
    terminal: OrderState | None = None
    terminal_emitted: bool = False
    rejection_key: tuple[str, int | None, str] | None = None


class BitfinexV1ExecutionClient(LiveExecutionClient):
    """One-account, one-instrument LIMIT execution client without reconciliation."""

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
        if any(
            live.rejection_key is None and not live.terminal_emitted
            for live in self._by_cid.values()
        ):
            reason = "Bitfinex stream gap with unresolved orders requires reconciliation"
            self._fatal_failure = reason
            raise BitfinexV1ExecutionError(reason)
        self._fatal_failure = None
        self._account_ready = False
        await self._transport.open()
        try:
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
        await self._transport.close()
        self._account_ready = False

    async def _reader(self) -> None:
        try:
            while self._running:
                self._consume_private_frame(await self._transport.recv_json())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_fatal("private reader", exc)
            self._running = False
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
        instrument = self._instrument(order.instrument_id)
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
        try:
            await self._transport.send_json(payload)
        except BaseException as exc:
            live.unknown_operations.add(operation)
            self._last_failure = f"{operation} send UNKNOWN: {type(exc).__name__}: {exc}"[:300]
            raise

    def _handle_order(self, event: OrderEvent) -> None:
        state = event.order
        live = self._resolve_live(state.client_order_id, state.venue_order_id)
        if live is None:
            return
        if live.terminal is not None:
            if event.operation == "oc" and state == live.terminal:
                return
            raise BitfinexV1ExecutionError("Bitfinex order update followed its terminal event")
        self._validate_order_state(live, state)
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
            live.unknown_operations.clear()
            return
        self._accept(live, state.venue_order_id, state.ts_updated_ms * 1_000_000)
        self._ack_pending_price(live, state.price, state.ts_updated_ms * 1_000_000)
        if event.operation == "oc":
            live.pending_cancel = False
            live.terminal = state
            live.unknown_operations.clear()
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
        live.unknown_operations.discard("submit")
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
        live.unknown_operations.discard(
            {"on": "submit", "ou": "modify", "oc": "cancel"}[note.operation]
        )

    def _accept(self, live: _LiveOrder, venue_order_id: int, ts_event: int) -> None:
        if live.venue_order_id not in {None, venue_order_id}:
            raise BitfinexV1ExecutionError("Bitfinex order changed venue ID")
        live.venue_order_id = venue_order_id
        self._cid_by_venue[venue_order_id] = live.cid
        if live.accepted:
            return
        live.accepted = True
        live.unknown_operations.discard("submit")
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
        live.unknown_operations.discard("modify")

    def _resolve_live(
        self,
        client_order_id: int | None,
        venue_order_id: int | None,
    ) -> _LiveOrder | None:
        by_client = self._by_cid.get(client_order_id) if client_order_id is not None else None
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

    def _validate_order_state(self, live: _LiveOrder, state: OrderState) -> None:
        expected_qty = Decimal(str(live.order.quantity))
        expected_sign = Decimal(1) if live.order.side == OrderSide.BUY else Decimal(-1)
        expected_type = "IOC" if live.order.time_in_force == TimeInForce.IOC else "LIMIT"
        expected_flags = POST_ONLY_FLAG if cast(bool, live.order.is_post_only) else 0
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
            or state.flags != expected_flags
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
        return self._instrument_provider.find(instrument_id) or self._cache.instrument(
            instrument_id
        )

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
        return self._by_cid.get(cid) if cid is not None else None

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

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        raise BitfinexV1ExecutionError("Bitfinex execution reports require reconciliation")

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        raise BitfinexV1ExecutionError("Bitfinex execution reports require reconciliation")

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        raise BitfinexV1ExecutionError("Bitfinex execution reports require reconciliation")

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        raise BitfinexV1ExecutionError("Bitfinex execution reports require reconciliation")


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
    if (
        config.instrument_id != SUPPORTED_INSTRUMENT_ID
        or config.raw_symbol != SUPPORTED_RAW_SYMBOL
    ):
        raise ValueError("Bitfinex execution supports only the configured XAUT perpetual")
    if config.account_id.get_issuer() != "BITFINEX":
        raise ValueError("Bitfinex account ID must use the BITFINEX issuer")
    if not config.api_key or not config.api_secret:
        raise ValueError("Bitfinex API credentials must be non-empty")
    if type(config.user_id) is not int or config.user_id <= 0:
        raise ValueError("Bitfinex user ID must be a positive exact integer")
    if config.wallet_currency.upper() != SUPPORTED_WALLET_CURRENCY:
        raise ValueError("Bitfinex execution wallet currency must be USTF0")
    if not config.cid_store_path:
        raise ValueError("Bitfinex CID path must be non-empty")
    if not 100 <= config.auth_timeout_ms <= 60_000:
        raise ValueError("Bitfinex auth timeout is outside the supported range")
    if not 100 <= config.open_timeout_ms <= 60_000:
        raise ValueError("Bitfinex open timeout is outside the supported range")


__all__ = [
    "BITFINEX",
    "BitfinexV1ExecClientConfig",
    "BitfinexV1ExecutionClient",
    "BitfinexV1ExecutionError",
]
