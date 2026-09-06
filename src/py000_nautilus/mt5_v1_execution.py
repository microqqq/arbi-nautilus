"""Minimal fail-closed Nautilus execution client for the PY000 MT5 v1 EA."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, cast

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
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
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    TradeId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import AccountBalance, Money
from nautilus_trader.model.orders import Order

from py000_nautilus.mt5_v1_protocol import (
    MAX_OBSERVATION_FUTURE_NS,
    Binding,
    Identity,
    JsonObject,
    RecoveryState,
)
from py000_nautilus.mt5_v1_transport import (
    Mt5V1RemoteError,
    Mt5V1RequestTimeout,
    Mt5V1Transport,
    Mt5V1TransportError,
)


class Mt5V1ExecutionError(RuntimeError):
    """The MT5 execution boundary cannot establish a trustworthy fact."""


_REJECTED_ORDER_ID_DOMAIN = "py000-nautilus:mt5-v1:rejected-order"
_MAX_SNAPSHOT_POSITION_TICKETS = 32  # Same fixed new-ticket envelope as the EA.


def _mt5_usd_commission(native_fee: str) -> Money:
    """Book native signed cash flow as a USD cost without changing journal values."""
    return Money.from_decimal(-Decimal(native_fee), USD)


class Mt5V1ExecClientConfig(LiveExecClientConfig, kw_only=True, frozen=True):
    pub_url: str
    rep_url: str
    instrument_id: InstrumentId
    expected_account_id: str
    expected_symbol: str
    expected_magic: str
    expected_ea_build_id: str
    expected_source_sha256: str
    expected_max_order_lots: Decimal
    expected_stream_id: str
    expected_server_timezone: str = "Europe/Athens"
    request_timeout_ms: int = 1_000
    mutation_timeout_ms: int = 15_000
    event_poll_interval_ms: int = 250
    snapshot_refresh_interval_ms: int = 1_000
    event_page_limit: int = 100
    event_pagination_max_pages: int = 1_000
    event_pagination_timeout_ms: int = 30_000


def mt5_v1_execution_account_id(client_id: ClientId, expected_account_id: str) -> AccountId:
    """Return the sole Nautilus account identity used by one MT5 v1 client."""
    return AccountId(f"{client_id.value}-{expected_account_id}")


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

    async def close_position(
        self,
        binding: Binding,
        *,
        client_request_id: str,
        side: Literal["buy", "sell"],
        quantity_lots: str,
        position_ticket: str,
        position_identifier: str,
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
    position_ticket: str | None = None
    position_identifier: str | None = None
    unknown: bool = False
    accepted: bool = False

    @property
    def is_close(self) -> bool:
        return self.position_ticket is not None


@dataclass(frozen=True, slots=True)
class _JournalProjection:
    reservations: dict[str, JsonObject]
    terminals: dict[str, JsonObject]

    @property
    def seen_request_ids(self) -> frozenset[str]:
        return frozenset(self.reservations)

    @property
    def hold_reason(self) -> str | None:
        unknown = sorted(
            request_id
            for request_id, event in self.terminals.items()
            if event["event_type"] == "order_unknown"
        )
        dangling = sorted(set(self.reservations) - set(self.terminals))
        mismatched_fills = sorted(
            request_id
            for request_id, event in self.terminals.items()
            if event["event_type"] == "order_filled"
            and Decimal(cast(str, cast(JsonObject, event["payload"])["filled_quantity_lots"]))
            != Decimal(
                cast(
                    str,
                    cast(JsonObject, self.reservations[request_id]["payload"])["quantity_lots"],
                )
            )
        )
        if unknown:
            return f"journal contains UNKNOWN request(s): {', '.join(unknown)}"
        if dangling:
            return f"journal contains dangling reservation(s): {', '.join(dangling)}"
        if mismatched_fills:
            return f"journal contains mismatched FOK fill(s): {', '.join(mismatched_fills)}"
        return None


def _project_journal(events: list[JsonObject], *, current_boot_id: str) -> _JournalProjection:
    """Validate and project one complete execution journal without emitting events."""
    if not events or events[0]["event_type"] != "stream_started":
        raise Mt5V1ExecutionError("execution journal does not begin with stream_started")
    reservations: dict[str, JsonObject] = {}
    terminals: dict[str, JsonObject] = {}
    boot_ids: set[str] = set()
    active_boot_id: str | None = None

    for event in events:
        event_type = cast(str, event["event_type"])
        event_boot_id = cast(str, event["boot_id"])
        if event_type == "stream_started":
            if event_boot_id in boot_ids:
                raise Mt5V1ExecutionError("execution journal repeats a boot ID")
            boot_ids.add(event_boot_id)
            active_boot_id = event_boot_id
            continue
        if event_boot_id != active_boot_id:
            raise Mt5V1ExecutionError("execution event belongs to an inactive EA boot")

        payload = cast(JsonObject, event["payload"])
        request_id = cast(str, payload["client_request_id"])
        if event_type == "submission_reserved":
            if request_id in reservations or request_id in terminals:
                raise Mt5V1ExecutionError("execution journal repeats a request reservation")
            reservations[request_id] = event
            continue
        if event_type not in {"order_rejected", "order_filled", "order_unknown"}:
            raise Mt5V1ExecutionError(f"unsupported execution event {event_type!r}")
        reservation = reservations.get(request_id)
        if reservation is None:
            raise Mt5V1ExecutionError("terminal execution event has no reservation")
        if request_id in terminals:
            raise Mt5V1ExecutionError("execution journal repeats a terminal outcome")
        if reservation["boot_id"] != event_boot_id:
            raise Mt5V1ExecutionError("terminal execution event changed EA boot")
        reserved_payload = cast(JsonObject, reservation["payload"])
        if _submission_signature(payload) != _submission_signature(reserved_payload):
            raise Mt5V1ExecutionError("terminal execution payload differs from its reservation")
        _validate_close_fill_target(event_type, reserved_payload, payload)
        terminals[request_id] = event

    if active_boot_id is not None and active_boot_id != current_boot_id:
        raise Mt5V1ExecutionError("execution journal does not end at the current EA boot")
    return _JournalProjection(reservations=reservations, terminals=terminals)


def _submission_signature(payload: JsonObject) -> tuple[object, ...]:
    return (
        payload["side"],
        payload["quantity_lots"],
        payload.get("position_ticket"),
        payload.get("position_identifier"),
    )


def _validate_close_fill_target(
    event_type: str,
    reservation: JsonObject,
    terminal: JsonObject,
) -> None:
    position_identifier = reservation.get("position_identifier")
    if (
        event_type == "order_filled"
        and position_identifier is not None
        and terminal["venue_position_id"] != position_identifier
    ):
        raise Mt5V1ExecutionError("MT5 close fill does not match reserved target position")


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
        self._set_account_id(mt5_v1_execution_account_id(client_id, config.expected_account_id))
        self._mt5_config = config
        self._transport: _ExecutionTransport = transport or Mt5V1Transport(
            pub_url=config.pub_url,
            rep_url=config.rep_url,
            topic=config.expected_symbol,
            request_timeout_ms=config.request_timeout_ms,
            mutation_timeout_ms=config.mutation_timeout_ms,
            loop=loop,
        )
        self._identity: Identity | None = None
        self._bound_stream_id: str | None = None
        self._snapshot: JsonObject | None = None
        self._snapshot_refresh_healthy = False
        self._account_sample_observed_ns: int | None = None
        self._account_sample_last_valid_ns: int | None = None
        self._account_sample_valid = False
        self._cursor = "0"
        self._pending: dict[str, _Pending] = {}
        self._seen_request_ids: set[str] = set()
        self._reservations: dict[str, JsonObject] = {}
        self._terminal_events: dict[str, JsonObject] = {}
        self._recovery_state: RecoveryState | None = None
        self._foreign_position_ids: tuple[str, ...] = ()
        self._execution_spec: tuple[object, ...] | None = None
        self._next_snapshot_refresh_at: float | None = None
        self._execution_hold_reason: str | None = "MT5 execution is not connected"
        self._state_lock = asyncio.Lock()
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None
        self._last_failure: str | None = None
        self._transient_request_failure: str | None = None

    @property
    def pending_client_order_ids(self) -> tuple[str, ...]:
        return tuple(self._pending)

    def raw_commission_cashflows(self) -> dict[tuple[ClientOrderId, TradeId], Decimal]:
        """Copy validated journal cash flows, including after ordinary disconnection.

        This does not query the venue, publish fills, or certify other broker fees.
        """
        projection = _JournalProjection(self._reservations, self._terminal_events)
        if self._pending or projection.hold_reason is not None:
            raise Mt5V1ExecutionError("MT5 commission history has unresolved requests")
        result: dict[tuple[ClientOrderId, TradeId], Decimal] = {}
        for request_id, event in projection.terminals.items():
            if event["event_type"] != "order_filled":
                continue
            instrument = self._cache.instrument(self._mt5_config.instrument_id)
            if instrument is None or instrument.lot_size is None:
                raise Mt5V1ExecutionError("canonical MT5 commission instrument is unavailable")
            self._validate_cached_terminal(request_id, event, cached_instrument=instrument)
            payload = cast(JsonObject, event["payload"])
            amount = Decimal(cast(str, payload["commission"]))
            if payload["client_request_id"] != request_id or not amount.is_finite():
                raise Mt5V1ExecutionError("MT5 commission history has conflicting facts")
            result[ClientOrderId(request_id), TradeId(cast(str, payload["venue_deal_id"]))] = amount
        return result

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    @property
    def execution_admitted(self) -> bool:
        return self._identity is not None and self._execution_hold_reason is None

    @property
    def execution_hold_reason(self) -> str | None:
        return self._execution_hold_reason

    def account_capacity_ready(self, max_age_ns: int) -> bool:
        """Qualify the latest account sample, not execution or a future order."""
        if (
            type(max_age_ns) is not int
            or max_age_ns < 0
            or not self.is_connected
            or self._identity is None
            or not self._snapshot_refresh_healthy
            or not self._account_sample_valid
            or self._account_sample_observed_ns is None
        ):
            return False
        age_ns = self._clock.timestamp_ns() - self._account_sample_observed_ns
        return bool(-MAX_OBSERVATION_FUTURE_NS <= age_ns <= max_age_ns)

    def can_execute_quantity(self, quantity_ounces: Decimal) -> bool:
        """Return whether the installed MT5 snapshot can express this ounce quantity."""
        if not self.execution_admitted or type(quantity_ounces) is not Decimal:
            return False
        try:
            self._quantity_to_current_lots(quantity_ounces)
        except (ArithmeticError, KeyError, TypeError, ValueError, Mt5V1ExecutionError):
            return False
        return True

    def connect(self) -> None:
        self.create_task(
            self._connect(),
            actions=self._finish_connect,
            success_msg="Connected",
            success_color=LogColor.GREEN,
        )

    async def _connect(self) -> None:
        try:
            await self._transport.open()
            identity, recovery = await self._transport.hello()
            self._validate_identity(identity, recovery)
            if self._bound_stream_id is not None and identity.stream_id != self._bound_stream_id:
                raise Mt5V1ExecutionError("MT5 execution journal stream changed")
            events, cursor = await self._read_complete_journal(identity)
            snapshot = await self._transport.snapshot(identity.binding())
            self._validate_snapshot(snapshot, identity, recovery)
            await self._verify_unchanged_journal_tail(identity, cursor)
            projection = _project_journal(events, current_boot_id=identity.boot_id)
            self._bound_stream_id = identity.stream_id
            self._identity = identity
            self._execution_spec = self._snapshot_execution_spec(snapshot)
            self._install_snapshot(snapshot)
            self._cursor = cursor
            self._reservations = projection.reservations
            self._terminal_events = projection.terminals
            self._seen_request_ids = set(projection.seen_request_ids) | set(self._pending)
            self._recovery_state = recovery
            self._recover_local_pending_from_journal()
            self._transient_request_failure = None
            self._refresh_execution_hold()
            self._schedule_snapshot_refresh()
            self._last_failure = None
        except BaseException as exc:
            self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
            self._identity = None
            self._snapshot = None
            self._snapshot_refresh_healthy = False
            self._recovery_state = None
            self._execution_spec = None
            self._next_snapshot_refresh_at = None
            self._execution_hold_reason = "MT5 execution is not connected"
            self._transient_request_failure = None
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
        self._snapshot_refresh_healthy = False
        self._recovery_state = None
        self._execution_spec = None
        self._next_snapshot_refresh_at = None
        self._execution_hold_reason = "MT5 execution is not connected"
        self._transient_request_failure = None

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        if order.order_type != OrderType.MARKET or order.time_in_force != TimeInForce.FOK:
            self._deny(order, "MT5 v1 execution supports MARKET FOK orders only")
            return
        is_close = order.is_reduce_only or command.position_id is not None
        if order.is_reduce_only != (command.position_id is not None):
            self._deny(
                order,
                "MT5 close requires both reduce-only and an exact position ID",
            )
            return
        if order.is_quote_quantity or order.exec_algorithm_id is not None:
            self._deny(order, "MT5 v1 execution does not support additional order semantics")
            return
        async with self._state_lock:
            request_id = str(order.client_order_id)
            if request_id in self._seen_request_ids:
                self._deny(order, "MT5 execution journal already contains this order ID")
                return
            try:
                planned, expected_position_ounces = self._hedge_plan_precondition(command)
            except Mt5V1ExecutionError as exc:
                self._deny(order, str(exc))
                return
            try:
                await self._refresh_snapshot_if_due(force=is_close or planned)
            except asyncio.CancelledError:
                raise
            except Mt5V1RequestTimeout as exc:
                self._mark_transient_request_timeout(exc)
                self._deny(order, f"MT5 snapshot refresh failed: {type(exc).__name__}: {exc}")
                return
            except Exception as exc:
                self._deny(order, f"MT5 snapshot refresh failed: {type(exc).__name__}: {exc}")
                return
            if self._execution_hold_reason is not None:
                self._deny(order, f"MT5 execution HOLD: {self._execution_hold_reason}")
                return
            if self._pending:
                self._deny(order, "MT5 execution is blocked by an unresolved UNKNOWN submission")
                return
            try:
                pending = self._prepare(
                    order, position_id=command.position_id, planned=planned,
                    expected_position_ounces=expected_position_ounces,
                )
            except Mt5V1ExecutionError as exc:
                self._deny(order, str(exc))
                return
            self.generate_order_submitted(
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                self._clock.timestamp_ns(),
            )
            self._pending[request_id] = pending
            self._seen_request_ids.add(request_id)
            try:
                side: Literal["buy", "sell"] = "buy" if order.side == OrderSide.BUY else "sell"
                if pending.is_close:
                    assert pending.position_ticket is not None
                    assert pending.position_identifier is not None
                    data = await self._transport.close_position(
                        self._require_identity().binding(),
                        client_request_id=request_id,
                        side=side,
                        quantity_lots=format(pending.quantity_lots, "f"),
                        position_ticket=pending.position_ticket,
                        position_identifier=pending.position_identifier,
                    )
                else:
                    data = await self._transport.submit_market_delta(
                        self._require_identity().binding(),
                        client_request_id=request_id,
                        side=side,
                        quantity_lots=format(pending.quantity_lots, "f"),
                    )
                if Identity.from_wire(data["identity"]) != self._require_identity():
                    raise Mt5V1ExecutionError("submit identity changed")
                outcome = cast(JsonObject, data["outcome"])
                outcome_cursor = cast(str, outcome["event_seq"])
                if int(outcome_cursor) <= int(self._cursor):
                    raise Mt5V1ExecutionError("submit outcome does not advance the journal cursor")
                else:
                    await self._consume_event_pages(expected_outcome=outcome)
            except asyncio.CancelledError as exc:
                pending.unknown = True
                self._refresh_execution_hold()
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                raise
            except Exception as exc:
                pending.unknown = True
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                if isinstance(exc, Mt5V1RequestTimeout):
                    self._transient_request_failure = self._last_failure
                self._refresh_execution_hold()
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
            except Mt5V1RequestTimeout as exc:
                self._mark_transient_request_timeout(exc)
            except Mt5V1RemoteError as exc:
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                if self._identity is not None:
                    await self._fail_closed_disconnect()
                return
            except Mt5V1TransportError as exc:
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                if self._identity is not None:
                    await self._fail_closed_disconnect()
                return
            except Exception as exc:
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                if self._identity is not None:
                    await self._fail_closed_disconnect()
                return

    async def _fail_closed_disconnect(self) -> None:
        self._running = False
        self._set_connected(False)
        await self._transport.close()
        self._identity = None
        self._snapshot = None
        self._snapshot_refresh_healthy = False
        self._recovery_state = None
        self._execution_spec = None
        self._next_snapshot_refresh_at = None
        self._execution_hold_reason = "MT5 execution is not connected"
        self._transient_request_failure = None

    async def _poll_once(self) -> None:
        async with self._state_lock:
            try:
                await self._consume_event_pages()
                await self._refresh_snapshot_if_due()
            except Mt5V1RequestTimeout as exc:
                self._mark_transient_request_timeout(exc)
                return
            except Mt5V1RemoteError as exc:
                if exc.code != "SNAPSHOT_UNAVAILABLE":
                    raise
                return
            self._clear_transient_request_timeout()

    async def _consume_event_pages(self, *, expected_outcome: JsonObject | None = None) -> None:
        identity = self._require_identity()
        expected_sequence = (
            int(cast(str, expected_outcome["event_seq"])) if expected_outcome is not None else None
        )
        target_sequence: int | None = None
        try:
            async with asyncio.timeout(self._mt5_config.event_pagination_timeout_ms / 1_000):
                for _ in range(self._mt5_config.event_pagination_max_pages):
                    current_sequence = int(self._cursor)
                    if target_sequence is not None and current_sequence >= target_sequence:
                        if expected_sequence is not None:
                            raise Mt5V1ExecutionError(
                                "submit outcome is missing from the durable execution stream"
                            )
                        return
                    limit = self._mt5_config.event_page_limit
                    if target_sequence is not None:
                        limit = min(limit, target_sequence - current_sequence)
                    page = await self._transport.execution_events(
                        identity.binding(),
                        after_cursor=self._cursor,
                        limit=limit,
                    )
                    self._validate_page_identity(page, identity)
                    page_last_sequence = int(cast(str, page["last_cursor"]))
                    if target_sequence is None:
                        target_sequence = page_last_sequence
                        if expected_sequence is not None and expected_sequence > target_sequence:
                            raise Mt5V1ExecutionError(
                                "submit outcome exceeds the durable execution stream tail"
                            )
                    elif page_last_sequence < target_sequence:
                        raise Mt5V1ExecutionError(
                            "execution journal tail moved behind the pagination target"
                        )
                    for event in cast(list[JsonObject], page["events"]):
                        sequence = cast(str, event["event_seq"])
                        if int(sequence) != int(self._cursor) + 1:
                            raise Mt5V1ExecutionError(
                                "execution journal cursor is not contiguous"
                            )
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
                    if int(self._cursor) >= target_sequence:
                        if expected_sequence is not None:
                            raise Mt5V1ExecutionError(
                                "submit outcome is missing from the durable execution stream"
                            )
                        return
                    if not cast(bool, page["has_more"]):
                        raise Mt5V1ExecutionError(
                            "execution journal ended before its pagination target"
                        )
                raise Mt5V1ExecutionError(
                    "execution journal pagination exceeded its page budget"
                )
        except TimeoutError as exc:
            raise Mt5V1ExecutionError(
                "execution journal pagination exceeded its time budget"
            ) from exc

    def _apply_event(self, event: JsonObject) -> None:
        event_type = cast(str, event["event_type"])
        payload = cast(JsonObject, event["payload"])
        if event_type == "submission_reserved":
            self._record_reservation(event)
            self._match_pending(payload)
        elif event_type == "order_unknown":
            self._record_terminal(event)
            self._match_pending(payload).unknown = True
            self._refresh_execution_hold()
        elif event_type == "order_rejected":
            self._record_terminal(event)
            pending = self._match_pending(payload)
            self.generate_order_rejected(
                pending.order.strategy_id,
                pending.order.instrument_id,
                pending.order.client_order_id,
                f"{payload['reason']} (retcode={payload['broker_retcode']})",
                int(cast(str, event["event_time_ms"])) * 1_000_000,
            )
            self._pending.pop(str(pending.order.client_order_id))
            self._refresh_execution_hold()
        elif event_type == "order_filled":
            self._record_terminal(event)
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
        if (
            pending.position_identifier is not None
            and payload["venue_position_id"] != pending.position_identifier
        ):
            pending.unknown = True
            raise Mt5V1ExecutionError("MT5 close fill does not match reserved target position")
        ts_event = int(cast(str, event["event_time_ms"])) * 1_000_000
        venue_order_id = VenueOrderId(cast(str, payload["venue_order_id"]))
        position_id = PositionId(cast(str, payload["venue_position_id"]))
        trade_id = TradeId(cast(str, payload["venue_deal_id"]))
        quantity = pending.instrument.make_qty(
            filled_lots * Decimal(str(pending.instrument.lot_size))
        )
        price = pending.instrument.make_price(Decimal(cast(str, payload["fill_price"])))
        commission = _mt5_usd_commission(cast(str, payload["commission"]))
        # The old account's position sample predates this real fill. Do not mutate
        # its published AccountState, or stop delivery of the hedge's execution.
        self._account_sample_valid = False
        if not pending.accepted:
            self.generate_order_accepted(
                pending.order.strategy_id,
                pending.order.instrument_id,
                pending.order.client_order_id,
                venue_order_id,
                ts_event,
            )
            pending.accepted = True
        self._pending.pop(str(pending.order.client_order_id))
        self._refresh_execution_hold()
        self.generate_order_filled(
            pending.order.strategy_id,
            pending.order.instrument_id,
            pending.order.client_order_id,
            venue_order_id,
            position_id,
            trade_id,
            pending.order.side,
            OrderType.MARKET,
            quantity,
            price,
            USD,
            commission,
            LiquiditySide.NO_LIQUIDITY_SIDE,
            ts_event,
        )

    async def _read_complete_journal(
        self,
        identity: Identity,
    ) -> tuple[list[JsonObject], str]:
        cursor = "0"
        events: list[JsonObject] = []
        last_cursor: str | None = None
        try:
            async with asyncio.timeout(self._mt5_config.event_pagination_timeout_ms / 1_000):
                for _ in range(self._mt5_config.event_pagination_max_pages):
                    page = await self._transport.execution_events(
                        identity.binding(),
                        after_cursor=cursor,
                        limit=self._mt5_config.event_page_limit,
                    )
                    self._validate_page_identity(page, identity)
                    if page["first_retained_cursor"] != "0":
                        raise Mt5V1ExecutionError(
                            "execution journal history is not fully retained"
                        )
                    page_last_cursor = cast(str, page["last_cursor"])
                    if last_cursor is None:
                        last_cursor = page_last_cursor
                    elif page_last_cursor != last_cursor:
                        raise Mt5V1ExecutionError(
                            "execution journal tail changed during pagination"
                        )
                    page_events = cast(list[JsonObject], page["events"])
                    for event in page_events:
                        sequence = cast(str, event["event_seq"])
                        if int(sequence) != int(cursor) + 1:
                            raise Mt5V1ExecutionError(
                                "execution journal cursor is not contiguous"
                            )
                        cursor = sequence
                        events.append(event)
                    if page["next_cursor"] != cursor:
                        raise Mt5V1ExecutionError(
                            "execution journal page cursor is inconsistent"
                        )
                    if not cast(bool, page["has_more"]):
                        if cursor != last_cursor:
                            raise Mt5V1ExecutionError(
                                "execution journal ended before its declared tail"
                            )
                        return events, cursor
                raise Mt5V1ExecutionError(
                    "execution journal pagination exceeded its page budget"
                )
        except TimeoutError as exc:
            raise Mt5V1ExecutionError(
                "execution journal pagination exceeded its time budget"
            ) from exc

    async def _verify_unchanged_journal_tail(self, identity: Identity, cursor: str) -> None:
        page = await self._transport.execution_events(
            identity.binding(),
            after_cursor=cursor,
            limit=self._mt5_config.event_page_limit,
        )
        self._validate_page_identity(page, identity)
        if (
            page["first_retained_cursor"] != "0"
            or page["next_cursor"] != cursor
            or page["last_cursor"] != cursor
            or cast(list[JsonObject], page["events"])
        ):
            raise Mt5V1ExecutionError("execution journal changed while snapshot was captured")

    def _schedule_snapshot_refresh(self) -> None:
        self._next_snapshot_refresh_at = (
            self._loop.time() + self._mt5_config.snapshot_refresh_interval_ms / 1_000
        )

    async def _refresh_snapshot_if_due(self, *, force: bool = False) -> None:
        deadline = self._next_snapshot_refresh_at
        if not force and (deadline is None or self._loop.time() < deadline):
            return
        try:
            identity = self._require_identity()
            snapshot = await self._transport.snapshot(identity.binding())
            self._validate_snapshot(
                snapshot,
                identity,
                cast(RecoveryState, self._recovery_state),
                expected_spec=self._execution_spec,
            )
            await self._verify_unchanged_journal_tail(identity, self._cursor)
            self._install_snapshot(snapshot)
            self._clear_transient_request_timeout()
            self._schedule_snapshot_refresh()
        except Mt5V1RequestTimeout as exc:
            self._mark_snapshot_unavailable(exc)
            raise
        except Mt5V1RemoteError as exc:
            if exc.code == "SNAPSHOT_UNAVAILABLE":
                self._mark_snapshot_unavailable(exc)
            else:
                self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
                await self._fail_closed_disconnect()
            raise
        except BaseException as exc:
            self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
            await self._fail_closed_disconnect()
            raise

    def _mark_snapshot_unavailable(self, exc: Mt5V1TransportError) -> None:
        self._snapshot_refresh_healthy = False
        self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
        self._refresh_execution_hold()
        self._schedule_snapshot_refresh()

    def _install_snapshot(self, snapshot: JsonObject) -> None:
        account = cast(JsonObject, snapshot["account"])
        equity = Decimal(cast(str, account["equity"]))
        margin = Decimal(cast(str, account["margin"]))
        margin_free = Decimal(cast(str, account["margin_free"]))
        if margin < 0:
            raise Mt5V1ExecutionError("MT5 account margin cannot be negative")
        try:
            balance = AccountBalance(
                Money(equity, USD),
                Money(margin, USD),
                Money(margin_free, USD),
            )
        except ValueError as exc:
            raise Mt5V1ExecutionError(
                "MT5 account equity, margin, and free margin are inconsistent"
            ) from exc
        positions = cast(list[JsonObject], snapshot["positions"])
        foreign_position_ids = tuple(
            cast(str, position["identifier"])
            for position in positions
            if position["magic"] != self._mt5_config.expected_magic
        )
        contract_size = Decimal(
            cast(str, cast(JsonObject, snapshot["symbol_spec"])["contract_size"])
        )
        net_ounces = sum(
            (
                Decimal(cast(str, position["volume_lots"]))
                * contract_size
                * (1 if position["side"] == "buy" else -1)
                for position in positions
            ),
            Decimal(0),
        )
        observed_ns = (
            int(cast(str, cast(JsonObject, snapshot["time"])["observed_utc_ms"])) * 1_000_000
        )
        sample_valid = observed_ns <= self._clock.timestamp_ns() + MAX_OBSERVATION_FUTURE_NS and (
            self._account_sample_last_valid_ns is None
            or observed_ns >= self._account_sample_last_valid_ns
        )

        self._snapshot = snapshot
        self._foreign_position_ids = foreign_position_ids
        self._account_sample_observed_ns = observed_ns
        self._account_sample_valid = sample_valid
        if sample_valid:
            self._account_sample_last_valid_ns = observed_ns
        self._snapshot_refresh_healthy = True
        self.generate_account_state(
            balances=[balance],
            margins=[],
            reported=True,
            ts_event=observed_ns,
            info={
                "mt5_balance": cast(str, account["balance"]),
                "mt5_equity": cast(str, account["equity"]),
                "mt5_margin": cast(str, account["margin"]),
                "mt5_margin_free": cast(str, account["margin_free"]),
                "mt5_margin_level": cast(str, account["margin_level"]),
                "mt5_leverage": cast(int, account["leverage"]),
                "mt5_positions_complete": True,
                "mt5_net_position_ounces": format(net_ounces, "f"),
                "mt5_position_count": len(positions),
                "mt5_symbol": self._mt5_config.expected_symbol,
                "mt5_stream_id": self._require_identity().stream_id,
                "mt5_account_observed_ns": observed_ns,
                "mt5_account_sample_valid": sample_valid,
            },
        )

    @staticmethod
    def _snapshot_execution_spec(snapshot: JsonObject) -> tuple[object, ...]:
        spec = cast(JsonObject, snapshot["symbol_spec"])
        limits = cast(JsonObject, snapshot["execution_limits"])
        fields = (
            "symbol",
            "contract_size",
            "currency_base",
            "currency_margin",
            "currency_profit",
            "digits",
            "filling_mode",
            "order_mode",
            "point",
            "tick_size",
            "trade_calc_mode",
            "trade_mode",
            "volume_max",
            "volume_min",
            "volume_step",
        )
        return (*tuple(spec[field] for field in fields), limits["max_order_lots"])

    def _record_reservation(self, event: JsonObject) -> None:
        payload = cast(JsonObject, event["payload"])
        request_id = cast(str, payload["client_request_id"])
        if request_id in self._reservations or request_id in self._terminal_events:
            raise Mt5V1ExecutionError("execution journal repeats a request reservation")
        self._reservations[request_id] = event
        self._seen_request_ids.add(request_id)

    def _record_terminal(self, event: JsonObject) -> None:
        payload = cast(JsonObject, event["payload"])
        request_id = cast(str, payload["client_request_id"])
        reservation = self._reservations.get(request_id)
        if reservation is None:
            raise Mt5V1ExecutionError("terminal execution event has no reservation")
        if request_id in self._terminal_events:
            raise Mt5V1ExecutionError("execution journal repeats a terminal outcome")
        if reservation["boot_id"] != event["boot_id"]:
            raise Mt5V1ExecutionError("terminal execution event changed EA boot")
        reserved_payload = cast(JsonObject, reservation["payload"])
        if _submission_signature(payload) != _submission_signature(reserved_payload):
            raise Mt5V1ExecutionError("terminal execution payload differs from its reservation")
        _validate_close_fill_target(cast(str, event["event_type"]), reserved_payload, payload)
        self._terminal_events[request_id] = event

    def _recover_local_pending_from_journal(self) -> None:
        for request_id in tuple(self._pending):
            event = self._terminal_events.get(request_id)
            if event is None:
                continue
            event_type = cast(str, event["event_type"])
            payload = cast(JsonObject, event["payload"])
            if event_type == "order_unknown":
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
                self._pending.pop(request_id)
            elif event_type == "order_filled":
                self._apply_fill(event, payload)

    def _refresh_execution_hold(self) -> None:
        reasons: list[str] = []
        if self._identity is None:
            reasons.append("MT5 execution is not connected")
        elif not self._snapshot_refresh_healthy:
            reasons.append("MT5 execution snapshot is temporarily unavailable")
        if self._recovery_state == "blocked":
            reasons.append("EA recovery state is blocked")
        projection = _JournalProjection(self._reservations, self._terminal_events)
        if projection.hold_reason is not None:
            reasons.append(projection.hold_reason)
        if any(pending.unknown for pending in self._pending.values()):
            reasons.append("a local submission has an UNKNOWN outcome")
        elif self._pending:
            reasons.append("a local submission is unresolved")
        if self._foreign_position_ids:
            reasons.append("snapshot contains foreign-magic position(s)")
        if self._transient_request_failure is not None:
            reasons.append("MT5 REP request path is temporarily unavailable")
        self._execution_hold_reason = "; ".join(reasons) if reasons else None

    def _mark_transient_request_timeout(self, exc: Mt5V1RequestTimeout) -> None:
        self._transient_request_failure = f"{type(exc).__name__}: {exc}"[:300]
        self._last_failure = self._transient_request_failure
        self._refresh_execution_hold()

    def _clear_transient_request_timeout(self) -> None:
        self._transient_request_failure = None
        if self._snapshot_refresh_healthy and not any(
            pending.unknown for pending in self._pending.values()
        ):
            self._last_failure = None
        self._refresh_execution_hold()

    @staticmethod
    def _hedge_plan_precondition(command: SubmitOrder) -> tuple[bool, Decimal | None]:
        params = command.params or {}
        marker = "py000_hedge_plan"
        expected_key = "py000_expected_position_ounces"
        if marker not in params and expected_key not in params:
            return False, None  # Preserve unmarked manual adapter commands.
        if params.get(marker) is not True:
            raise Mt5V1ExecutionError("invalid hedge plan marker")
        if command.position_id is None:
            if expected_key in params:
                raise Mt5V1ExecutionError("planned open cannot contain a close target quantity")
            return True, None
        expected = params.get(expected_key)
        if not isinstance(expected, Decimal) or not expected.is_finite() or expected <= 0:
            raise Mt5V1ExecutionError("planned close requires a positive finite Decimal quantity")
        return True, expected

    def _prepare(
        self, order: Order, *, position_id: PositionId | None, planned: bool = False,
        expected_position_ounces: Decimal | None = None,
    ) -> _Pending:
        if order.instrument_id != self._mt5_config.instrument_id:
            raise Mt5V1ExecutionError("order instrument is outside the configured MT5 boundary")
        instrument = self._instrument_provider.find(order.instrument_id) or self._cache.instrument(
            order.instrument_id
        )
        if instrument is None or instrument.lot_size is None:
            raise Mt5V1ExecutionError("canonical MT5 instrument or lot_size is unavailable")
        lots = self._quantity_to_current_lots(
            Decimal(str(order.quantity)),
            instrument=instrument,
        )
        spec = cast(JsonObject, self._require_snapshot()["symbol_spec"])
        volume_min = Decimal(cast(str, spec["volume_min"]))
        volume_step = Decimal(cast(str, spec["volume_step"]))
        if position_id is None:
            if len(cast(list[JsonObject], self._require_snapshot()["positions"])) >= (
                _MAX_SNAPSHOT_POSITION_TICKETS
            ):
                raise Mt5V1ExecutionError("MT5 new position capacity is exhausted")
            opposite = "sell" if order.side == OrderSide.BUY else "buy"
            if planned and any(
                position["side"] == opposite
                for position in cast(list[JsonObject], self._require_snapshot()["positions"])
            ):
                raise Mt5V1ExecutionError("opposing MT5 ticket appeared before planned open")
            return _Pending(order=order, instrument=instrument, quantity_lots=lots)
        identifier = position_id.value
        matches = [
            position
            for position in cast(list[JsonObject], self._require_snapshot()["positions"])
            if position["identifier"] == identifier
        ]
        if len(matches) != 1:
            raise Mt5V1ExecutionError("close target is absent or duplicated in the live snapshot")
        target = matches[0]
        if target["magic"] != self._mt5_config.expected_magic:
            raise Mt5V1ExecutionError("close target has foreign magic")
        expected_order_side = "sell" if target["side"] == "buy" else "buy"
        actual_order_side = "buy" if order.side == OrderSide.BUY else "sell"
        if actual_order_side != expected_order_side:
            raise Mt5V1ExecutionError("close order side does not oppose the target position")
        target_lots = Decimal(cast(str, target["volume_lots"]))
        if planned and target_lots * Decimal(str(instrument.lot_size)) != expected_position_ounces:
            raise Mt5V1ExecutionError("planned target quantity differs from the live snapshot")
        if lots > target_lots:
            raise Mt5V1ExecutionError("close quantity exceeds the live target position")
        remaining_lots = target_lots - lots
        if remaining_lots and (remaining_lots < volume_min or remaining_lots % volume_step != 0):
            raise Mt5V1ExecutionError("close would leave an invalid MT5 lot remainder")
        return _Pending(
            order=order,
            instrument=instrument,
            quantity_lots=lots,
            position_ticket=cast(str, target["ticket"]),
            position_identifier=identifier,
        )

    def _quantity_to_current_lots(
        self,
        quantity_ounces: Decimal,
        *,
        instrument: Instrument | None = None,
    ) -> Decimal:
        if instrument is None:
            instrument = self._instrument_provider.find(
                self._mt5_config.instrument_id
            ) or self._cache.instrument(self._mt5_config.instrument_id)
        if instrument is None or instrument.lot_size is None:
            raise Mt5V1ExecutionError("canonical MT5 instrument or lot_size is unavailable")
        spec = cast(JsonObject, self._require_snapshot()["symbol_spec"])
        lot_size = Decimal(str(instrument.lot_size))
        if lot_size != Decimal(cast(str, spec["contract_size"])):
            raise Mt5V1ExecutionError("instrument lot_size differs from MT5 contract_size")
        volume_min = Decimal(cast(str, spec["volume_min"]))
        limits = cast(JsonObject, self._require_snapshot()["execution_limits"])
        volume_max = min(
            Decimal(cast(str, spec["volume_max"])),
            Decimal(cast(str, limits["max_order_lots"])),
        )
        volume_step = Decimal(cast(str, spec["volume_step"]))
        return quantity_to_lots(
            quantity_ounces,
            lot_size=lot_size,
            volume_min=volume_min,
            volume_max=volume_max,
            volume_step=volume_step,
        )

    def _match_pending(self, payload: JsonObject) -> _Pending:
        request_id = cast(str, payload["client_request_id"])
        pending = self._pending.get(request_id)
        if pending is None:
            raise Mt5V1ExecutionError("execution event has no local pending order")
        side = "buy" if pending.order.side == OrderSide.BUY else "sell"
        quantity_lots = Decimal(cast(str, payload["quantity_lots"]))
        target = (payload.get("position_ticket"), payload.get("position_identifier"))
        expected_target = (pending.position_ticket, pending.position_identifier)
        if (
            payload["side"] != side
            or quantity_lots != pending.quantity_lots
            or target != expected_target
        ):
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
        if identity.stream_id != config.expected_stream_id:
            raise Mt5V1ExecutionError("MT5 journal stream differs from expected_stream_id")
        expected = (
            (identity.account_id, config.expected_account_id),
            (identity.symbol, config.expected_symbol),
            (identity.magic, config.expected_magic),
            (identity.ea_build_id, config.expected_ea_build_id),
            (identity.declared_source_sha256, config.expected_source_sha256),
            (identity.server_timezone, config.expected_server_timezone),
        )
        if (
            recovery not in {"ready", "blocked"}
            or not identity.execution_enabled
            or any(actual != wanted for actual, wanted in expected)
        ):
            raise Mt5V1ExecutionError("MT5 execution identity is not configured and enabled")

    def _validate_snapshot(
        self,
        snapshot: JsonObject,
        identity: Identity,
        recovery: RecoveryState,
        *,
        expected_spec: tuple[object, ...] | None = None,
    ) -> None:
        if (
            Identity.from_wire(snapshot["identity"]) != identity
            or snapshot["execution_enabled"] is not True
            or snapshot["recovery_state"] != recovery
            or cast(JsonObject, snapshot["account"])["currency"] != "USD"
            or Decimal(
                cast(
                    str,
                    cast(JsonObject, snapshot["execution_limits"])["max_order_lots"],
                )
            )
            != self._mt5_config.expected_max_order_lots
        ):
            raise Mt5V1ExecutionError("MT5 execution snapshot is inconsistent")
        actual_spec = self._snapshot_execution_spec(snapshot)
        if expected_spec is not None and actual_spec != expected_spec:
            raise Mt5V1ExecutionError("MT5 execution symbol spec changed")

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

    def _require_reportable_state(self) -> tuple[JsonObject, _JournalProjection]:
        self._require_identity()
        snapshot = self._require_snapshot()
        if not self._snapshot_refresh_healthy:
            raise Mt5V1ExecutionError("MT5 execution reports require a complete current snapshot")
        projection = _JournalProjection(self._reservations, self._terminal_events)
        if self._pending:
            raise Mt5V1ExecutionError(
                "MT5 execution reports are unavailable while an order is pending"
            )
        if self._recovery_state == "blocked":
            raise Mt5V1ExecutionError(
                "MT5 execution reports are unavailable while EA recovery is blocked"
            )
        if projection.hold_reason is not None:
            raise Mt5V1ExecutionError(
                f"MT5 execution reports are unavailable: {projection.hold_reason}"
            )
        if self._foreign_position_ids:
            raise Mt5V1ExecutionError(
                "MT5 execution reports are unavailable with foreign-magic positions"
            )
        return snapshot, projection

    def _report_instrument(self) -> Instrument:
        instrument = self._instrument_provider.find(
            self._mt5_config.instrument_id
        ) or self._cache.instrument(self._mt5_config.instrument_id)
        if instrument is None or instrument.lot_size is None:
            raise Mt5V1ExecutionError("canonical MT5 instrument or lot_size is unavailable")
        contract_size = Decimal(
            cast(str, cast(JsonObject, self._require_snapshot()["symbol_spec"])["contract_size"])
        )
        if Decimal(str(instrument.lot_size)) != contract_size:
            raise Mt5V1ExecutionError("instrument lot_size differs from MT5 contract_size")
        return instrument

    @staticmethod
    def _event_ts_ns(event: JsonObject) -> int:
        return int(cast(str, event["event_time_ms"])) * 1_000_000

    @staticmethod
    def _datetime_ms(value: datetime) -> int:
        return dt_to_unix_nanos(value) // 1_000_000

    @classmethod
    def _event_in_window(
        cls,
        event: JsonObject,
        start: datetime | None,
        end: datetime | None,
    ) -> bool:
        timestamp_ms = int(cast(str, event["event_time_ms"]))
        return not (
            (start is not None and timestamp_ms < cls._datetime_ms(start))
            or (end is not None and timestamp_ms > cls._datetime_ms(end))
        )

    @staticmethod
    def _order_side(payload: JsonObject) -> OrderSide:
        side = payload["side"]
        if side == "buy":
            return OrderSide.BUY
        if side == "sell":
            return OrderSide.SELL
        raise Mt5V1ExecutionError(f"unsupported MT5 order side {side!r}")

    def _synthetic_rejected_venue_order_id(self, client_order_id: str) -> VenueOrderId:
        identity = self._require_identity()
        material = "\0".join(
            (
                _REJECTED_ORDER_ID_DOMAIN,
                identity.stream_id,
                identity.account_id,
                identity.symbol,
                identity.magic,
                client_order_id,
            )
        ).encode()
        return VenueOrderId(f"PY000_REJ_{hashlib.sha256(material).hexdigest()}")

    def _validate_cached_terminal(
        self, request_id: str, event: JsonObject, *, cached_instrument: Instrument | None = None,
    ) -> None:
        """Reject known native/journal conflicts before the engine can overwrite facts."""
        client_order_id = ClientOrderId(request_id)
        reserved = cast(JsonObject, self._reservations[request_id]["payload"])
        payload = cast(JsonObject, event["payload"])
        filled = event["event_type"] == "order_filled"
        venue_order_id = (
            VenueOrderId(cast(str, payload["venue_order_id"]))
            if filled else self._synthetic_rejected_venue_order_id(request_id)
        )
        indexed_cid = self._cache.client_order_id(venue_order_id)
        if indexed_cid is not None and indexed_cid != client_order_id:
            raise Mt5V1ExecutionError(f"cached MT5 venue order index conflict: {request_id}")
        order = self._cache.order(client_order_id)
        if order is None:
            return
        # Only retained-fee observation supplies the cached instrument after disconnect.
        # Live report callers still require the complete current snapshot/contract check.
        instrument = self._report_instrument() if cached_instrument is None else cached_instrument
        quantity = Decimal(cast(str, reserved["quantity_lots"])) * Decimal(str(instrument.lot_size))
        side = self._order_side(reserved)
        reduce_only = "position_identifier" in reserved
        indexed_client = self._cache.client_id(client_order_id)
        if (
            (order.account_id is not None and order.account_id != self.account_id)
            or (indexed_client is not None and indexed_client != self.id)
            or order.instrument_id != self._mt5_config.instrument_id
            or order.side != side
            or order.quantity.as_decimal() != quantity
            or order.order_type != OrderType.MARKET
            or order.time_in_force != TimeInForce.FOK
            or order.is_reduce_only != reduce_only
            or order.is_quote_quantity
        ):
            raise Mt5V1ExecutionError(f"cached MT5 order facts conflict: {request_id}")
        for known_venue_id in (order.venue_order_id, self._cache.venue_order_id(client_order_id)):
            if known_venue_id is not None and known_venue_id != venue_order_id:
                raise Mt5V1ExecutionError(f"cached MT5 venue order conflict: {request_id}")
        journal_position = (
            PositionId(cast(str, payload["venue_position_id"]))
            if filled else (
                PositionId(cast(str, reserved["position_identifier"])) if reduce_only else None
            )
        )
        indexed_position = self._cache.position_id(client_order_id)
        known_positions = {
            value for value in (indexed_position, order.position_id, journal_position)
            if value is not None
        }
        if len(known_positions) > 1 or (
            reduce_only
            and indexed_position != PositionId(cast(str, reserved["position_identifier"]))
        ):
            raise Mt5V1ExecutionError(f"cached MT5 position index conflict: {request_id}")
        status = OrderStatus.FILLED if filled else OrderStatus.REJECTED
        if order.is_closed and order.status != status:
            raise Mt5V1ExecutionError(f"cached MT5 closed order conflicts: {request_id}")
        price = Decimal(cast(str, payload["fill_price"])) if filled else None
        if price is not None and instrument.make_price(price).as_decimal() != price:
            raise Mt5V1ExecutionError(f"cached MT5 fill price loses precision: {request_id}")
        fills = [item for item in order.events if isinstance(item, OrderFilled)]
        if order.filled_qty == 0 and not fills and not order.trade_ids:
            return  # A first complete FOK report may fill an existing unfilled order.
        if not filled or order.filled_qty.as_decimal() != quantity or len(fills) != 1:
            raise Mt5V1ExecutionError(f"cached MT5 filled quantity conflicts: {request_id}")
        trade_id = TradeId(cast(str, payload["venue_deal_id"]))
        recorded = fills[0]
        # W1 currency quantization is intentional for fees, not quantity or price facts.
        if (
            order.trade_ids != [trade_id]
            or recorded.account_id != self.account_id
            or recorded.instrument_id != self._mt5_config.instrument_id
            or recorded.venue_order_id != venue_order_id
            or recorded.position_id != journal_position
            or recorded.order_side != side
            or recorded.trade_id != trade_id
            or recorded.last_qty.as_decimal() != quantity
            or recorded.last_px.as_decimal() != price
            or recorded.commission != _mt5_usd_commission(cast(str, payload["commission"]))
            or recorded.liquidity_side != LiquiditySide.NO_LIQUIDITY_SIDE
            or recorded.ts_event != self._event_ts_ns(event)
        ):
            raise Mt5V1ExecutionError(f"cached MT5 recorded fill conflicts: {request_id}")

    def _order_report(self, request_id: str, event: JsonObject) -> OrderStatusReport:
        self._validate_cached_terminal(request_id, event)
        reservation = self._reservations[request_id]
        reserved = cast(JsonObject, reservation["payload"])
        payload = cast(JsonObject, event["payload"])
        instrument = self._report_instrument()
        contract_size = Decimal(str(instrument.lot_size))
        quantity = instrument.make_qty(
            Decimal(cast(str, reserved["quantity_lots"])) * contract_size
        )
        timestamp = self._event_ts_ns(event)
        event_type = cast(str, event["event_type"])
        if event_type == "order_filled":
            venue_order_id = VenueOrderId(cast(str, payload["venue_order_id"]))
            venue_position_id = PositionId(cast(str, payload["venue_position_id"]))
            filled_qty = instrument.make_qty(
                Decimal(cast(str, payload["filled_quantity_lots"])) * contract_size
            )
            status = OrderStatus.FILLED
            avg_px = Decimal(cast(str, payload["fill_price"]))
            cancel_reason = None
        elif event_type == "order_rejected":
            venue_order_id = self._synthetic_rejected_venue_order_id(request_id)
            position_identifier = reserved.get("position_identifier")
            venue_position_id = (
                PositionId(cast(str, position_identifier))
                if position_identifier is not None
                else None
            )
            filled_qty = instrument.make_qty(0)
            status = OrderStatus.REJECTED
            avg_px = None
            cancel_reason = f"{payload['reason']} (retcode={payload['broker_retcode']})"
        else:  # pragma: no cover - guarded by _require_reportable_state
            raise Mt5V1ExecutionError(f"unsupported report terminal {event_type!r}")
        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=self._mt5_config.instrument_id,
            venue_order_id=venue_order_id,
            client_order_id=ClientOrderId(request_id),
            venue_position_id=venue_position_id,
            order_side=self._order_side(reserved),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.FOK,
            order_status=status,
            quantity=quantity,
            filled_qty=filled_qty,
            avg_px=avg_px,
            reduce_only="position_identifier" in reserved,
            cancel_reason=cancel_reason,
            report_id=UUID4(),
            ts_accepted=timestamp,
            ts_last=timestamp,
            ts_init=self._clock.timestamp_ns(),
        )

    def _fill_report(self, request_id: str, event: JsonObject) -> FillReport:
        self._validate_cached_terminal(request_id, event)
        payload = cast(JsonObject, event["payload"])
        instrument = self._report_instrument()
        contract_size = Decimal(str(instrument.lot_size))
        timestamp = self._event_ts_ns(event)
        price = instrument.make_price(Decimal(cast(str, payload["fill_price"])))
        return FillReport(
            account_id=self.account_id,
            instrument_id=self._mt5_config.instrument_id,
            venue_order_id=VenueOrderId(cast(str, payload["venue_order_id"])),
            trade_id=TradeId(cast(str, payload["venue_deal_id"])),
            client_order_id=ClientOrderId(request_id),
            venue_position_id=PositionId(cast(str, payload["venue_position_id"])),
            order_side=self._order_side(payload),
            last_qty=instrument.make_qty(
                Decimal(cast(str, payload["filled_quantity_lots"])) * contract_size
            ),
            last_px=price,
            avg_px=price.as_decimal(),
            commission=_mt5_usd_commission(cast(str, payload["commission"])),
            liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
            report_id=UUID4(),
            ts_event=timestamp,
            ts_init=self._clock.timestamp_ns(),
        )

    async def generate_mass_status(
        self,
        lookback_mins: int | None = None,
    ) -> ExecutionMassStatus | None:
        async with self._state_lock:
            self.reconciliation_active = True
            try:
                await self._consume_event_pages()
                await self._refresh_snapshot_if_due(force=True)
                try:
                    _, projection = self._require_reportable_state()
                except Mt5V1ExecutionError:
                    pass  # Preserve the native None result for existing unreportable states.
                else:
                    # Native mass reporting swallows child exceptions; conflicts must reach
                    # this client's existing fail-closed disconnect before any report escapes.
                    for request_id, event in projection.terminals.items():
                        self._validate_cached_terminal(request_id, event)
                return await super().generate_mass_status(lookback_mins)
            except Mt5V1RequestTimeout as exc:
                self._mark_snapshot_unavailable(exc)
                self._mark_transient_request_timeout(exc)
                raise
            except Mt5V1RemoteError as exc:
                if exc.code != "SNAPSHOT_UNAVAILABLE" and self._identity is not None:
                    await self._fail_closed_disconnect()
                raise
            except BaseException:
                if self._identity is not None:
                    await self._fail_closed_disconnect()
                raise
            finally:
                self.reconciliation_active = False

    async def generate_order_status_report(
        self, command: GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        _, projection = self._require_reportable_state()
        if command.client_order_id is None and command.venue_order_id is None:
            raise ValueError("client_order_id and venue_order_id cannot both be None")
        if command.instrument_id not in {None, self._mt5_config.instrument_id}:
            return None
        for request_id, event in sorted(
            projection.terminals.items(),
            key=lambda item: int(cast(str, item[1]["event_seq"])),
        ):
            report = self._order_report(request_id, event)
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
            return report
        return None

    async def generate_order_status_reports(
        self, command: GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        _, projection = self._require_reportable_state()
        if command.instrument_id not in {None, self._mt5_config.instrument_id} or command.open_only:
            return []
        return [
            self._order_report(request_id, event)
            for request_id, event in sorted(
                projection.terminals.items(),
                key=lambda item: int(cast(str, item[1]["event_seq"])),
            )
            if self._event_in_window(event, command.start, command.end)
        ]

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        _, projection = self._require_reportable_state()
        if command.instrument_id not in {None, self._mt5_config.instrument_id}:
            return []
        reports = [
            self._fill_report(request_id, event)
            for request_id, event in sorted(
                projection.terminals.items(),
                key=lambda item: int(cast(str, item[1]["event_seq"])),
            )
            if event["event_type"] == "order_filled"
            and self._event_in_window(event, command.start, command.end)
        ]
        if command.venue_order_id is not None:
            reports = [
                report for report in reports if report.venue_order_id == command.venue_order_id
            ]
        return reports

    async def generate_position_status_reports(
        self, command: GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        snapshot, _ = self._require_reportable_state()
        if command.instrument_id not in {None, self._mt5_config.instrument_id}:
            return []
        instrument = self._report_instrument()
        contract_size = Decimal(str(instrument.lot_size))
        ts_init = self._clock.timestamp_ns()
        reports: list[PositionStatusReport] = []
        snapshot_position_ids: set[PositionId] = set()
        for position in sorted(
            cast(list[JsonObject], snapshot["positions"]),
            key=lambda value: cast(str, value["identifier"]),
        ):
            position_id = PositionId(cast(str, position["identifier"]))
            snapshot_position_ids.add(position_id)
            side_text = position["side"]
            if side_text == "buy":
                side = PositionSide.LONG
            elif side_text == "sell":
                side = PositionSide.SHORT
            else:
                raise Mt5V1ExecutionError(f"unsupported MT5 position side {side_text!r}")
            reports.append(
                PositionStatusReport(
                    account_id=self.account_id,
                    instrument_id=self._mt5_config.instrument_id,
                    position_side=side,
                    quantity=instrument.make_qty(
                        Decimal(cast(str, position["volume_lots"])) * contract_size
                    ),
                    report_id=UUID4(),
                    ts_last=int(cast(str, position["time_msc"])) * 1_000_000,
                    ts_init=ts_init,
                    venue_position_id=position_id,
                    avg_px_open=Decimal(cast(str, position["price_open"])),
                )
            )
        missing_positions = sorted(
            (
                position
                for position in self._cache.positions_open(
                    instrument_id=self._mt5_config.instrument_id,
                    account_id=self.account_id,
                )
                if position.id not in snapshot_position_ids
            ),
            key=lambda position: position.id.value,
        )
        reports.extend(
            PositionStatusReport(
                account_id=self.account_id,
                instrument_id=self._mt5_config.instrument_id,
                position_side=PositionSide.FLAT,
                quantity=instrument.make_qty(0),
                report_id=UUID4(),
                ts_last=position.ts_last,
                ts_init=ts_init,
                venue_position_id=position.id,
            )
            for position in missing_positions
        )
        return reports


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
    if (
        not config.expected_stream_id
        or config.expected_stream_id != config.expected_stream_id.strip()
    ):
        raise ValueError("expected_stream_id must be a non-empty trimmed token")
    expected_max_order_lots = config.expected_max_order_lots
    if (
        type(expected_max_order_lots) is not Decimal
        or not expected_max_order_lots.is_finite()
        or expected_max_order_lots <= 0
    ):
        raise ValueError(
            "expected_max_order_lots must be a positive finite Decimal with at most 8 places"
        )
    exponent = expected_max_order_lots.normalize().as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -8:
        raise ValueError(
            "expected_max_order_lots must be a positive finite Decimal with at most 8 places"
        )
    if not 50 <= config.request_timeout_ms <= 60_000:
        raise ValueError("request_timeout_ms is outside the supported range")
    if not config.request_timeout_ms <= config.mutation_timeout_ms <= 60_000:
        raise ValueError("mutation_timeout_ms must cover request_timeout_ms and be at most 60000")
    if not 50 <= config.event_poll_interval_ms <= 60_000:
        raise ValueError("event_poll_interval_ms is outside the supported range")
    if not 1_000 <= config.snapshot_refresh_interval_ms <= 60_000:
        raise ValueError("snapshot_refresh_interval_ms is outside the supported range")
    if config.snapshot_refresh_interval_ms < config.event_poll_interval_ms:
        raise ValueError("snapshot refresh cannot be faster than the event poll interval")
    if not 1 <= config.event_page_limit <= 500:
        raise ValueError("event_page_limit is outside the v1 wire range")
    if (
        type(config.event_pagination_max_pages) is not int
        or not 1 <= config.event_pagination_max_pages <= 10_000
    ):
        raise ValueError("event_pagination_max_pages is outside the supported range")
    if (
        type(config.event_pagination_timeout_ms) is not int
        or not 50 <= config.event_pagination_timeout_ms <= 60_000
    ):
        raise ValueError("event_pagination_timeout_ms is outside the supported range")
