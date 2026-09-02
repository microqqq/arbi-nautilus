"""Minimal fail-closed Nautilus execution client for the PY000 MT5 v1 EA."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, cast

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
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
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.currencies import USD
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
    InstrumentId,
    PositionId,
    TradeId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order

from py000_nautilus.mt5_v1_protocol import Binding, Identity, JsonObject, RecoveryState
from py000_nautilus.mt5_v1_transport import (
    Mt5V1RemoteError,
    Mt5V1Transport,
    Mt5V1TransportError,
)


class Mt5V1ExecutionError(RuntimeError):
    """The MT5 execution boundary cannot establish a trustworthy fact."""


class Mt5V1ExecClientConfig(LiveExecClientConfig, kw_only=True, frozen=True):
    pub_url: str
    rep_url: str
    instrument_id: InstrumentId
    expected_account_id: str
    expected_symbol: str
    expected_magic: str
    expected_ea_build_id: str
    expected_source_sha256: str
    expected_server_timezone: str = "Europe/Athens"
    request_timeout_ms: int = 1_000
    event_poll_interval_ms: int = 250
    event_page_limit: int = 100


class _ExecutionTransport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def hello(self) -> tuple[Identity, RecoveryState]: ...
    async def snapshot(self, binding: Binding) -> JsonObject: ...
    async def submit_market_delta(
        self,
        binding: Binding,
        *,
        client_request_id: str,
        side: Literal["buy", "sell"],
        quantity_lots: str,
    ) -> JsonObject: ...
    async def execution_events(
        self,
        binding: Binding,
        *,
        after_cursor: str,
        limit: int,
    ) -> JsonObject: ...


@dataclass(slots=True)
class _Pending:
    order: Order
    instrument: Instrument
    quantity_lots: Decimal
    unknown: bool = False
    accepted: bool = False


def quantity_to_lots(
    quantity: Decimal,
    *,
    lot_size: Decimal,
    volume_min: Decimal,
    volume_max: Decimal,
    volume_step: Decimal,
) -> Decimal:
    """Convert canonical ounces to exact MT5 lots without rounding."""
    if min(quantity, lot_size, volume_min, volume_max, volume_step) <= 0:
        raise Mt5V1ExecutionError("quantity and MT5 volume constraints must be positive")
    lots = quantity / lot_size
    if lots < volume_min or lots > volume_max or lots % volume_step != 0:
        raise Mt5V1ExecutionError("canonical quantity is not an exact permitted MT5 lot size")
    return lots


class Mt5V1ExecutionClient(LiveExecutionClient):
    """One-account, one-instrument, MARKET-only MT5 execution client."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        name: str | None,
        config: Mt5V1ExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: InstrumentProvider,
        transport: _ExecutionTransport | None = None,
    ) -> None:
        _validate_config(config)
        client_id = ClientId(name or "MT5")
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=Venue("MT5"),
            oms_type=OmsType.HEDGING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._set_account_id(AccountId(f"{client_id.value}-{config.expected_account_id}"))
        self._mt5_config = config
        self._transport: _ExecutionTransport = transport or Mt5V1Transport(
            pub_url=config.pub_url,
            rep_url=config.rep_url,
            topic=config.expected_symbol,
            request_timeout_ms=config.request_timeout_ms,
        )
        self._identity: Identity | None = None
        self._bound_stream_id: str | None = None
        self._snapshot: JsonObject | None = None
        self._cursor = "0"
        self._pending: dict[str, _Pending] = {}
        self._terminal_requests: set[str] = set()
        self._state_lock = asyncio.Lock()
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None
        self._last_failure: str | None = None

    @property
    def pending_client_order_ids(self) -> tuple[str, ...]:
        return tuple(self._pending)

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    def connect(self) -> None:
        self.create_task(
            self._connect(),
            actions=self._finish_connect,
            success_msg="Connected",
            success_color=LogColor.GREEN,
        )

    async def _connect(self) -> None:
        try:
            if self._pending:
                raise Mt5V1ExecutionError(
                    "MT5 execution cannot reconnect with an unresolved submission"
                )
            await self._transport.open()
            identity, recovery = await self._transport.hello()
            self._validate_identity(identity, recovery)
            if self._bound_stream_id is not None and identity.stream_id != self._bound_stream_id:
                raise Mt5V1ExecutionError("MT5 execution journal stream changed")
            snapshot = await self._transport.snapshot(identity.binding())
            self._validate_snapshot(snapshot, identity)
            self._cursor = await self._seek_event_tail(identity)
            self._bound_stream_id = identity.stream_id
            self._identity = identity
            self._snapshot = snapshot
        except BaseException as exc:
            self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
            await self._transport.close()
            raise

    def _finish_connect(self) -> None:
        self._set_connected(True)
        self._running = True
        self._poll_task = self.create_task(self._poll_events(), log_msg="mt5-v1-events")

    async def _disconnect(self) -> None:
        self._running = False
        if self._poll_task is not None and self._poll_task is not asyncio.current_task():
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        self._poll_task = None
        await self._transport.close()
        self._identity = None
        self._snapshot = None

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        if order.order_type != OrderType.MARKET or order.time_in_force != TimeInForce.FOK:
            self._deny(order, "MT5 v1 execution supports MARKET FOK orders only")
            return
        async with self._state_lock:
            if self._pending:
                self._deny(order, "MT5 execution is blocked by an unresolved UNKNOWN submission")
                return
            try:
                pending = self._prepare(order)
            except Mt5V1ExecutionError as exc:
                self._deny(order, str(exc))
                return
            request_id = str(order.client_order_id)
            if request_id in self._terminal_requests:
                self._deny(order, "MT5 execution already has a terminal outcome for this order")
                return
            self.generate_order_submitted(
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                self._clock.timestamp_ns(),
            )
            self._pending[request_id] = pending
            try:
                data = await self._transport.submit_market_delta(
                    self._require_identity().binding(),
                    client_request_id=request_id,
                    side="buy" if order.side == OrderSide.BUY else "sell",
                    quantity_lots=format(pending.quantity_lots, "f"),
                )
                if Identity.from_wire(data["identity"]) != self._require_identity():
                    raise Mt5V1ExecutionError("submit identity changed")
                outcome = cast(JsonObject, data["outcome"])
                outcome_cursor = cast(str, outcome["event_seq"])
                if int(outcome_cursor) <= int(self._cursor):
                    self._apply_event(outcome)
                else:
                    await self._consume_event_pages(expected_outcome=outcome)
            except asyncio.CancelledError as exc:
                pending.unknown = True
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                raise
            except Exception as exc:
                pending.unknown = True
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                if not isinstance(exc, TimeoutError | Mt5V1TransportError):
                    raise

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        for order in command.order_list.orders:
            self._deny(order, "MT5 v1 execution does not support order lists")

    async def _modify_order(self, command: ModifyOrder) -> None:
        self.generate_order_modify_rejected(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id,
            "MT5 v1 execution does not support modify",
            self._clock.timestamp_ns(),
        )

    async def _cancel_order(self, command: CancelOrder) -> None:
        self.generate_order_cancel_rejected(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id,
            "MT5 v1 execution does not support cancel",
            self._clock.timestamp_ns(),
        )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        raise NotImplementedError("MT5 v1 execution does not support cancel-all")

    async def _batch_cancel_orders(self, command: BatchCancelOrders) -> None:
        raise NotImplementedError("MT5 v1 execution does not support batch cancel")

    async def _poll_events(self) -> None:
        while self._running:
            await asyncio.sleep(self._mt5_config.event_poll_interval_ms / 1_000)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Mt5V1RemoteError as exc:
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                self._running = False
                self._set_connected(False)
                await self._transport.close()
                return
            except Mt5V1TransportError as exc:
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
            except Exception as exc:
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                self._running = False
                self._set_connected(False)
                await self._transport.close()
                return

    async def _poll_once(self) -> None:
        async with self._state_lock:
            await self._consume_event_pages()

    async def _consume_event_pages(self, *, expected_outcome: JsonObject | None = None) -> None:
        identity = self._require_identity()
        expected_sequence = (
            int(cast(str, expected_outcome["event_seq"]))
            if expected_outcome is not None
            else None
        )
        while True:
            page = await self._transport.execution_events(
                identity.binding(),
                after_cursor=self._cursor,
                limit=self._mt5_config.event_page_limit,
            )
            self._validate_page_identity(page, identity)
            for event in cast(list[JsonObject], page["events"]):
                sequence = cast(str, event["event_seq"])
                if (
                    expected_sequence is not None
                    and int(sequence) == expected_sequence
                    and event != expected_outcome
                ):
                    raise Mt5V1ExecutionError(
                        "submit outcome differs from its durable execution event"
                    )
                self._apply_event(event)
                self._cursor = sequence
                if expected_sequence is not None and int(sequence) == expected_sequence:
                    return
            if not cast(bool, page["has_more"]):
                if expected_sequence is not None:
                    raise Mt5V1ExecutionError(
                        "submit outcome is missing from the durable execution stream"
                    )
                return

    def _apply_event(self, event: JsonObject) -> None:
        event_type = cast(str, event["event_type"])
        payload = cast(JsonObject, event["payload"])
        if event_type == "submission_reserved":
            self._match_pending(payload)
        elif event_type == "order_unknown":
            self._match_pending(payload).unknown = True
        elif event_type == "order_rejected":
            pending = self._match_pending(payload)
            self.generate_order_rejected(
                pending.order.strategy_id,
                pending.order.instrument_id,
                pending.order.client_order_id,
                f"{payload['reason']} (retcode={payload['broker_retcode']})",
                int(cast(str, event["event_time_ms"])) * 1_000_000,
            )
            self._pending.pop(str(pending.order.client_order_id))
            self._terminal_requests.add(str(pending.order.client_order_id))
        elif event_type == "order_filled":
            self._apply_fill(event, payload)
        elif event_type == "stream_started":
            raise Mt5V1ExecutionError("MT5 execution stream restarted while connected")
        else:
            raise Mt5V1ExecutionError(f"unsupported execution event {event_type!r}")

    def _apply_fill(self, event: JsonObject, payload: JsonObject) -> None:
        pending = self._match_pending(payload)
        filled_lots = Decimal(cast(str, payload["filled_quantity_lots"]))
        if filled_lots != pending.quantity_lots:
            pending.unknown = True
            raise Mt5V1ExecutionError("partial or mismatched MT5 fill is UNKNOWN")
        ts_event = int(cast(str, event["event_time_ms"])) * 1_000_000
        venue_order_id = VenueOrderId(cast(str, payload["venue_order_id"]))
        if not pending.accepted:
            self.generate_order_accepted(
                pending.order.strategy_id,
                pending.order.instrument_id,
                pending.order.client_order_id,
                venue_order_id,
                ts_event,
            )
            pending.accepted = True
        self.generate_order_filled(
            pending.order.strategy_id,
            pending.order.instrument_id,
            pending.order.client_order_id,
            venue_order_id,
            PositionId(cast(str, payload["venue_position_id"])),
            TradeId(cast(str, payload["venue_deal_id"])),
            pending.order.side,
            OrderType.MARKET,
            pending.instrument.make_qty(filled_lots * Decimal(str(pending.instrument.lot_size))),
            pending.instrument.make_price(Decimal(cast(str, payload["fill_price"]))),
            USD,
            Money(Decimal(cast(str, payload["commission"])), USD),
            LiquiditySide.NO_LIQUIDITY_SIDE,
            ts_event,
        )
        self._pending.pop(str(pending.order.client_order_id))
        self._terminal_requests.add(str(pending.order.client_order_id))

    async def _seek_event_tail(self, identity: Identity) -> str:
        cursor = "0"
        while True:
            page = await self._transport.execution_events(
                identity.binding(),
                after_cursor=cursor,
                limit=self._mt5_config.event_page_limit,
            )
            self._validate_page_identity(page, identity)
            cursor = cast(str, page["next_cursor"])
            if not cast(bool, page["has_more"]):
                return cursor

    def _prepare(self, order: Order) -> _Pending:
        if order.instrument_id != self._mt5_config.instrument_id:
            raise Mt5V1ExecutionError("order instrument is outside the configured MT5 boundary")
        instrument = self._instrument_provider.find(order.instrument_id) or self._cache.instrument(
            order.instrument_id
        )
        if instrument is None or instrument.lot_size is None:
            raise Mt5V1ExecutionError("canonical MT5 instrument or lot_size is unavailable")
        spec = cast(JsonObject, self._require_snapshot()["symbol_spec"])
        lot_size = Decimal(str(instrument.lot_size))
        if lot_size != Decimal(cast(str, spec["contract_size"])):
            raise Mt5V1ExecutionError("instrument lot_size differs from MT5 contract_size")
        lots = quantity_to_lots(
            Decimal(str(order.quantity)),
            lot_size=lot_size,
            volume_min=Decimal(cast(str, spec["volume_min"])),
            volume_max=Decimal(cast(str, spec["volume_max"])),
            volume_step=Decimal(cast(str, spec["volume_step"])),
        )
        return _Pending(order=order, instrument=instrument, quantity_lots=lots)

    def _match_pending(self, payload: JsonObject) -> _Pending:
        request_id = cast(str, payload["client_request_id"])
        pending = self._pending.get(request_id)
        if pending is None:
            raise Mt5V1ExecutionError("execution event has no local pending order")
        side = "buy" if pending.order.side == OrderSide.BUY else "sell"
        quantity_lots = Decimal(cast(str, payload["quantity_lots"]))
        if payload["side"] != side or quantity_lots != pending.quantity_lots:
            pending.unknown = True
            raise Mt5V1ExecutionError("execution event differs from the local submission")
        return pending

    def _deny(self, order: Order, reason: str) -> None:
        self.generate_order_denied(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            reason,
            self._clock.timestamp_ns(),
        )

    def _validate_identity(self, identity: Identity, recovery: RecoveryState) -> None:
        config = self._mt5_config
        expected = (
            (identity.account_id, config.expected_account_id),
            (identity.symbol, config.expected_symbol),
            (identity.magic, config.expected_magic),
            (identity.ea_build_id, config.expected_ea_build_id),
            (identity.declared_source_sha256, config.expected_source_sha256),
            (identity.server_timezone, config.expected_server_timezone),
        )
        if recovery != "ready" or not identity.execution_enabled or any(
            actual != wanted for actual, wanted in expected
        ):
            raise Mt5V1ExecutionError(
                "MT5 execution identity is not configured, enabled, and ready"
            )

    def _validate_snapshot(self, snapshot: JsonObject, identity: Identity) -> None:
        if (
            Identity.from_wire(snapshot["identity"]) != identity
            or snapshot["execution_enabled"] is not True
            or snapshot["recovery_state"] != "ready"
            or cast(JsonObject, snapshot["account"])["currency"] != "USD"
        ):
            raise Mt5V1ExecutionError("MT5 execution snapshot is inconsistent")

    @staticmethod
    def _validate_page_identity(page: JsonObject, identity: Identity) -> None:
        if (
            Identity.from_wire(page["identity"]) != identity
            or page["stream_id"] != identity.stream_id
        ):
            raise Mt5V1ExecutionError("MT5 execution event page identity changed")

    def _require_identity(self) -> Identity:
        if self._identity is None:
            raise Mt5V1ExecutionError("MT5 execution identity is unavailable")
        return self._identity

    def _require_snapshot(self) -> JsonObject:
        if self._snapshot is None:
            raise Mt5V1ExecutionError("MT5 execution snapshot is unavailable")
        return self._snapshot

    async def generate_order_status_report(
        self, command: GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        raise NotImplementedError("MT5 v1 reconciliation is disabled")

    async def generate_order_status_reports(
        self, command: GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        raise NotImplementedError("MT5 v1 reconciliation is disabled")

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        raise NotImplementedError("MT5 v1 reconciliation is disabled")

    async def generate_position_status_reports(
        self, command: GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        raise NotImplementedError("MT5 v1 reconciliation is disabled")


class Mt5V1LiveExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LiveExecutionClient:
        if not isinstance(config, Mt5V1ExecClientConfig):
            raise TypeError("MT5 factory requires Mt5V1ExecClientConfig")
        return Mt5V1ExecutionClient(
            loop=loop,
            name=name,
            config=config,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(config=config.instrument_provider),
        )


def _validate_config(config: Mt5V1ExecClientConfig) -> None:
    if not config.pub_url.startswith("tcp://") or not config.rep_url.startswith("tcp://"):
        raise ValueError("MT5 v1 endpoints must use tcp://")
    if config.instrument_id.venue != Venue("MT5"):
        raise ValueError("MT5 v1 execution instrument venue must be MT5")
    if config.instrument_id.symbol.value != config.expected_symbol:
        raise ValueError("configured MT5 instrument and raw symbol differ")
    if not 50 <= config.request_timeout_ms <= 60_000:
        raise ValueError("request_timeout_ms is outside the supported range")
    if not 50 <= config.event_poll_interval_ms <= 60_000:
        raise ValueError("event_poll_interval_ms is outside the supported range")
    if not 1 <= config.event_page_limit <= 500:
        raise ValueError("event_page_limit is outside the v1 wire range")
