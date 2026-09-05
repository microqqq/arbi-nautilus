from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from nautilus_trader.common.component import LiveClock, TestClock
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    SubmitOrder,
)
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.enums import (
    AccountType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.events import AccountState, OrderDenied, OrderEvent, OrderFilled
from nautilus_trader.model.identifiers import (
    ClientId,
    ClientOrderId,
    ExecAlgorithmId,
    InstrumentId,
    PositionId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.orders import Order
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

import py000_nautilus.mt5_v1_execution as mt5_execution
from py000_nautilus.mt5_v1_data import instrument_from_snapshot
from py000_nautilus.mt5_v1_execution import (
    Mt5V1ExecClientConfig,
    Mt5V1ExecutionClient,
    Mt5V1ExecutionError,
    Mt5V1LiveExecClientFactory,
    quantity_to_lots,
)
from py000_nautilus.mt5_v1_protocol import Binding, Identity, JsonObject, RecoveryState
from py000_nautilus.mt5_v1_transport import (
    Mt5V1RemoteError,
    Mt5V1RequestTimeout,
    Mt5V1TransportError,
)

ROOT = Path(__file__).parents[1]
FIXTURE = cast(
    dict[str, object],
    json.loads((ROOT / "tests" / "fixtures" / "mt5_ea_v1_readonly.json").read_text()),
)
INSTRUMENT_ID = InstrumentId.from_str("XAUUSD.MT5")


def _identity(*, execution_enabled: bool = True) -> Identity:
    value = deepcopy(cast(dict[str, object], FIXTURE["identity"]))
    value["execution_enabled"] = execution_enabled
    return Identity.from_wire(value)


def _snapshot(
    identity: Identity,
    *,
    execution_enabled: bool | None = None,
    recovery_state: RecoveryState = "ready",
) -> JsonObject:
    value = deepcopy(cast(JsonObject, FIXTURE["snapshot"]))
    value["identity"] = identity.to_wire()
    value["execution_enabled"] = (
        identity.execution_enabled if execution_enabled is None else execution_enabled
    )
    value["recovery_state"] = recovery_state
    return value


def _config(
    identity: Identity,
    *,
    expected_max_order_lots: Decimal = Decimal("100"),
    request_timeout_ms: int = 1_000,
    mutation_timeout_ms: int = 15_000,
    snapshot_refresh_interval_ms: int = 1_000,
    event_pagination_max_pages: int = 1_000,
    event_pagination_timeout_ms: int = 30_000,
) -> Mt5V1ExecClientConfig:
    return Mt5V1ExecClientConfig(
        pub_url="tcp://127.0.0.1:6001",
        rep_url="tcp://127.0.0.1:6002",
        instrument_id=INSTRUMENT_ID,
        expected_account_id=identity.account_id,
        expected_symbol=identity.symbol,
        expected_magic=identity.magic,
        expected_ea_build_id=identity.ea_build_id,
        expected_source_sha256=identity.declared_source_sha256,
        expected_max_order_lots=expected_max_order_lots,
        expected_stream_id=identity.stream_id,
        expected_server_timezone=identity.server_timezone,
        request_timeout_ms=request_timeout_ms,
        mutation_timeout_ms=mutation_timeout_ms,
        event_poll_interval_ms=50,
        snapshot_refresh_interval_ms=snapshot_refresh_interval_ms,
        event_pagination_max_pages=event_pagination_max_pages,
        event_pagination_timeout_ms=event_pagination_timeout_ms,
    )


def _event(
    identity: Identity,
    sequence: int,
    event_type: str,
    payload: JsonObject,
    *,
    boot_id: str | None = None,
) -> JsonObject:
    return {
        "boot_id": boot_id or identity.boot_id,
        "event_seq": str(sequence),
        "event_time_ms": "1788271200000",
        "event_type": event_type,
        "payload": payload,
        "stream_id": identity.stream_id,
    }


def _stream_started(
    identity: Identity,
    sequence: int = 1,
    *,
    boot_id: str | None = None,
) -> JsonObject:
    return _event(
        identity,
        sequence,
        "stream_started",
        {"ea_build_id": identity.ea_build_id, "execution_enabled": identity.execution_enabled},
        boot_id=boot_id,
    )


def _submission_payload(
    order: Order,
    quantity_lots: str = "1",
    *,
    position_ticket: str | None = None,
    position_identifier: str | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "client_request_id": str(order.client_order_id),
        "quantity_lots": quantity_lots,
        "side": "buy" if order.side == OrderSide.BUY else "sell",
    }
    if position_ticket is not None or position_identifier is not None:
        assert position_ticket is not None
        assert position_identifier is not None
        payload.update(
            {
                "position_identifier": position_identifier,
                "position_ticket": position_ticket,
            }
        )
    return payload


def _outcome(
    identity: Identity,
    order: Order,
    event_type: str,
    *,
    sequence: int = 3,
    quantity_lots: str = "1",
    position_ticket: str | None = None,
    position_identifier: str | None = None,
    venue_position_id: str = "900000002",
) -> JsonObject:
    payload = _submission_payload(
        order,
        quantity_lots,
        position_ticket=position_ticket,
        position_identifier=position_identifier,
    )
    if event_type in {"order_rejected", "order_unknown"}:
        payload.update({"broker_retcode": "10013", "reason": "broker_rejected"})
    elif event_type == "order_filled":
        payload.update(
            {
                "broker_retcode": "10009",
                "commission": "-1.25",
                "fill_price": "2401.25",
                "filled_quantity_lots": quantity_lots,
                "venue_deal_id": "800000002",
                "venue_order_id": "700000002",
                "venue_position_id": venue_position_id,
            }
        )
    return _event(identity, sequence, event_type, payload)


def _page(
    identity: Identity,
    *,
    after_cursor: str,
    events: list[JsonObject],
    last_cursor: str | None = None,
) -> JsonObject:
    next_cursor = cast(str, events[-1]["event_seq"]) if events else after_cursor
    last_cursor = last_cursor or next_cursor
    return {
        "events": events,
        "first_retained_cursor": "0",
        "has_more": int(next_cursor) < int(last_cursor),
        "identity": identity.to_wire(),
        "last_cursor": last_cursor,
        "next_cursor": next_cursor,
        "stream_id": identity.stream_id,
    }


class _FakeTransport:
    def __init__(
        self,
        identity: Identity,
        snapshot: JsonObject,
        *,
        outcome: JsonObject | BaseException | None = None,
        event_sink: list[OrderEvent] | None = None,
        recovery_state: RecoveryState = "ready",
    ) -> None:
        self.identity = identity
        self.current_snapshot = snapshot
        self.outcome = outcome
        self.event_sink = event_sink
        self.recovery_state = recovery_state
        self.pages: list[JsonObject | BaseException] = []
        self.submit_calls: list[tuple[str, str, str]] = []
        self.close_calls: list[tuple[str, str, str, str, str]] = []
        self.event_calls: list[tuple[str, int]] = []
        self.snapshot_calls: list[Binding] = []
        self.snapshot_results: list[JsonObject | BaseException] = []
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def hello(self) -> tuple[Identity, RecoveryState]:
        return self.identity, self.recovery_state

    async def snapshot(self, binding: Binding) -> JsonObject:
        assert binding == self.identity.binding()
        self.snapshot_calls.append(binding)
        if self.snapshot_results:
            result = self.snapshot_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return self.current_snapshot

    async def submit_market_delta(
        self,
        binding: Binding,
        *,
        client_request_id: str,
        side: str,
        quantity_lots: str,
    ) -> JsonObject:
        assert binding == self.identity.binding()
        assert self.event_sink is None or [type(event).__name__ for event in self.event_sink] == [
            "OrderSubmitted"
        ]
        self.submit_calls.append((client_request_id, side, quantity_lots))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome is None:
            raise AssertionError("unexpected submit")
        outcome_sequence = int(cast(str, self.outcome["event_seq"]))
        if outcome_sequence > 1:
            outcome_payload = cast(JsonObject, self.outcome["payload"])
            reservation_payload = {
                "client_request_id": outcome_payload["client_request_id"],
                "quantity_lots": outcome_payload["quantity_lots"],
                "side": outcome_payload["side"],
            }
            self.pages.append(
                _page(
                    self.identity,
                    after_cursor=str(outcome_sequence - 2),
                    events=[
                        _event(
                            self.identity,
                            outcome_sequence - 1,
                            "submission_reserved",
                            reservation_payload,
                            boot_id=cast(str, self.outcome["boot_id"]),
                        ),
                        self.outcome,
                    ],
                )
            )
        return {"identity": self.identity.to_wire(), "outcome": self.outcome}

    async def close_position(
        self,
        binding: Binding,
        *,
        client_request_id: str,
        side: str,
        quantity_lots: str,
        position_ticket: str,
        position_identifier: str,
    ) -> JsonObject:
        assert binding == self.identity.binding()
        assert self.event_sink is None or [type(event).__name__ for event in self.event_sink] == [
            "OrderSubmitted"
        ]
        self.close_calls.append(
            (
                client_request_id,
                side,
                quantity_lots,
                position_ticket,
                position_identifier,
            )
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome is None:
            raise AssertionError("unexpected close")
        outcome_sequence = int(cast(str, self.outcome["event_seq"]))
        if outcome_sequence > 1:
            self.pages.append(
                _page(
                    self.identity,
                    after_cursor=str(outcome_sequence - 2),
                    events=[
                        _event(
                            self.identity,
                            outcome_sequence - 1,
                            "submission_reserved",
                            {
                                "client_request_id": client_request_id,
                                "quantity_lots": quantity_lots,
                                "side": side,
                                "position_identifier": position_identifier,
                                "position_ticket": position_ticket,
                            },
                            boot_id=cast(str, self.outcome["boot_id"]),
                        ),
                        self.outcome,
                    ],
                )
            )
        return {"identity": self.identity.to_wire(), "outcome": self.outcome}

    async def execution_events(
        self,
        binding: Binding,
        *,
        after_cursor: str,
        limit: int,
    ) -> JsonObject:
        assert binding == self.identity.binding()
        assert 1 <= limit <= 500
        self.event_calls.append((after_cursor, limit))
        if self.pages:
            result = self.pages.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        if after_cursor == "0":
            return _page(
                self.identity,
                after_cursor=after_cursor,
                events=[_stream_started(self.identity)],
            )
        return _page(self.identity, after_cursor=after_cursor, events=[])


class _Harness:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        identity: Identity | None = None,
        snapshot: JsonObject | None = None,
        outcome: JsonObject | BaseException | None = None,
        recovery_state: RecoveryState = "ready",
        expected_max_order_lots: Decimal | None = None,
        request_timeout_ms: int = 1_000,
        mutation_timeout_ms: int = 15_000,
        snapshot_refresh_interval_ms: int = 1_000,
        event_pagination_max_pages: int = 1_000,
        event_pagination_timeout_ms: int = 30_000,
        capture_events: bool = True,
        clock: LiveClock | None = None,
    ) -> None:
        self.identity = identity or _identity()
        self.snapshot = snapshot or _snapshot(self.identity, recovery_state=recovery_state)
        self.clock = clock or TestComponentStubs.clock()
        self.msgbus = TestComponentStubs.msgbus()
        self.cache = TestComponentStubs.cache()
        self.events: list[OrderEvent] = []
        self.account_states: list[AccountState] = []
        self.pending_during_fills: list[tuple[str, ...]] = []
        self.client: Mt5V1ExecutionClient
        if capture_events:

            def capture_event(event: OrderEvent) -> None:
                if isinstance(event, OrderFilled):
                    self.pending_during_fills.append(self.client.pending_client_order_ids)
                self.events.append(event)

            self.msgbus.register("ExecEngine.process", capture_event)
            self.capture_event = capture_event
            self.msgbus.register("Portfolio.update_account", self.account_states.append)
        self.instrument = instrument_from_snapshot(self.snapshot, INSTRUMENT_ID, ts_init=0)
        provider = InstrumentProvider()
        provider.add(self.instrument)
        self.fake = _FakeTransport(
            self.identity,
            self.snapshot,
            outcome=outcome,
            event_sink=self.events if capture_events else None,
            recovery_state=recovery_state,
        )
        snapshot_limit = Decimal(
            cast(
                str,
                cast(JsonObject, self.snapshot["execution_limits"])["max_order_lots"],
            )
        )
        self.client = Mt5V1ExecutionClient(
            loop=loop,
            name="MT5",
            config=_config(
                self.identity,
                expected_max_order_lots=(
                    snapshot_limit if expected_max_order_lots is None else expected_max_order_lots
                ),
                request_timeout_ms=request_timeout_ms,
                mutation_timeout_ms=mutation_timeout_ms,
                snapshot_refresh_interval_ms=snapshot_refresh_interval_ms,
                event_pagination_max_pages=event_pagination_max_pages,
                event_pagination_timeout_ms=event_pagination_timeout_ms,
            ),
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            instrument_provider=provider,
            transport=self.fake,
        )

    async def connect(self) -> None:
        await self.client._connect()

    def market(
        self,
        quantity: str = "100",
        *,
        order_side: OrderSide = OrderSide.BUY,
        time_in_force: TimeInForce = TimeInForce.FOK,
        client_order_id: ClientOrderId | None = None,
        reduce_only: bool = False,
        quote_quantity: bool = False,
        exec_algorithm_id: ExecAlgorithmId | None = None,
    ) -> Order:
        return TestComponentStubs.order_factory().market(
            instrument_id=INSTRUMENT_ID,
            order_side=order_side,
            quantity=self.instrument.make_qty(Decimal(quantity)),
            time_in_force=time_in_force,
            client_order_id=client_order_id,
            reduce_only=reduce_only,
            quote_quantity=quote_quantity,
            exec_algorithm_id=exec_algorithm_id,
        )

    def limit(self, quantity: str = "100") -> Order:
        return TestComponentStubs.order_factory().limit(
            instrument_id=INSTRUMENT_ID,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(Decimal(quantity)),
            price=self.instrument.make_price(Decimal("2400")),
        )

    async def submit(
        self, order: Order, *, position_id: PositionId | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        await self.client._submit_order(
            SubmitOrder(
                trader_id=order.trader_id,
                strategy_id=order.strategy_id,
                order=order,
                command_id=UUID4(),
                ts_init=self.clock.timestamp_ns(),
                position_id=position_id,
                params=params,
            )
        )


def _order_reports_command(
    harness: _Harness,
    *,
    instrument_id: InstrumentId | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    open_only: bool = False,
) -> GenerateOrderStatusReports:
    return GenerateOrderStatusReports(
        instrument_id=instrument_id,
        start=start,
        end=end,
        open_only=open_only,
        command_id=UUID4(),
        ts_init=harness.clock.timestamp_ns(),
    )


def _fill_reports_command(
    harness: _Harness,
    *,
    instrument_id: InstrumentId | None = None,
    venue_order_id: VenueOrderId | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> GenerateFillReports:
    return GenerateFillReports(
        instrument_id=instrument_id,
        venue_order_id=venue_order_id,
        start=start,
        end=end,
        command_id=UUID4(),
        ts_init=harness.clock.timestamp_ns(),
    )


def _position_reports_command(
    harness: _Harness,
    *,
    instrument_id: InstrumentId | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> GeneratePositionStatusReports:
    return GeneratePositionStatusReports(
        instrument_id=instrument_id,
        start=start,
        end=end,
        command_id=UUID4(),
        ts_init=harness.clock.timestamp_ns(),
    )


def _live_engine(
    harness: _Harness,
    *,
    generate_missing_orders: bool,
) -> LiveExecutionEngine:
    harness.cache.add_instrument(harness.instrument)
    harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
    engine = LiveExecutionEngine(
        loop=asyncio.get_running_loop(),
        msgbus=harness.msgbus,
        cache=harness.cache,
        clock=harness.clock,
        config=LiveExecEngineConfig(
            load_cache=False,
            generate_missing_orders=generate_missing_orders,
            inflight_check_interval_ms=0,
            open_check_interval_secs=None,
            position_check_interval_secs=None,
        ),
    )
    engine.register_client(harness.client)
    return engine


def _cache_submitted_order(harness: _Harness, order: Order) -> None:
    order.apply(
        TestEventStubs.order_submitted(
            order,
            account_id=harness.client.account_id,
            ts_event=harness.clock.timestamp_ns(),
        )
    )
    harness.cache.add_order(order)


async def _connected_report_harness(
    loop: asyncio.AbstractEventLoop,
    *,
    capture_events: bool = True,
) -> tuple[_Harness, Order, Order]:
    harness = _Harness(loop, capture_events=capture_events)
    filled_order = harness.market(client_order_id=ClientOrderId("REPORT-FILLED-1"))
    rejected_order = harness.market(
        order_side=OrderSide.SELL,
        client_order_id=ClientOrderId("REPORT-REJECTED-1"),
    )
    harness.fake.pages.append(
        _page(
            harness.identity,
            after_cursor="0",
            events=[
                _stream_started(harness.identity),
                _event(
                    harness.identity,
                    2,
                    "submission_reserved",
                    _submission_payload(filled_order),
                ),
                _outcome(harness.identity, filled_order, "order_filled", sequence=3),
                _event(
                    harness.identity,
                    4,
                    "submission_reserved",
                    _submission_payload(rejected_order),
                ),
                _outcome(harness.identity, rejected_order, "order_rejected", sequence=5),
            ],
        )
    )
    await harness.connect()
    return harness, filled_order, rejected_order


@pytest.mark.parametrize(
    ("ounces", "expected_lots"),
    [("1", "0.01"), ("101", "1.01"), ("10000", "100")],
)
def test_quantity_to_lots_accepts_exact_boundaries(ounces: str, expected_lots: str) -> None:
    lots = quantity_to_lots(
        Decimal(ounces),
        lot_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
    )
    assert lots == Decimal(expected_lots)


@pytest.mark.parametrize("ounces", ["0.5", "1.5", "10001"])
def test_quantity_to_lots_rejects_non_exact_or_out_of_range(ounces: str) -> None:
    with pytest.raises(Mt5V1ExecutionError):
        quantity_to_lots(
            Decimal(ounces),
            lot_size=Decimal("100"),
            volume_min=Decimal("0.01"),
            volume_max=Decimal("100"),
            volume_step=Decimal("0.01"),
        )


def test_non_market_is_denied_without_transport_send() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        await harness.submit(harness.limit())
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


def test_connect_walks_every_retained_page_before_choosing_the_tail() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        harness.fake.pages.extend(
            [
                _page(
                    harness.identity,
                    after_cursor="0",
                    events=[
                        _stream_started(
                            harness.identity,
                            boot_id="boot-prior-001",
                        )
                    ],
                    last_cursor="2",
                ),
                _page(
                    harness.identity,
                    after_cursor="1",
                    events=[
                        _event(
                            harness.identity,
                            2,
                            "stream_started",
                            {"ea_build_id": "current", "execution_enabled": True},
                        )
                    ],
                ),
            ]
        )

        await harness.connect()

        assert harness.client._cursor == "2"
        assert harness.fake.event_calls == [("0", 100), ("1", 100), ("2", 100)]

    asyncio.run(scenario())


def test_complete_journal_default_budget_accepts_five_hundred_pages() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        harness.fake.pages.extend(
            _page(
                harness.identity,
                after_cursor=str(sequence - 1),
                events=[
                    _event(
                        harness.identity,
                        sequence,
                        "stream_started",
                        {"ea_build_id": "current", "execution_enabled": True},
                        boot_id=f"boot-history-{sequence}",
                    )
                ],
                last_cursor="500",
            )
            for sequence in range(1, 501)
        )

        events, cursor = await harness.client._read_complete_journal(harness.identity)

        assert len(events) == 500
        assert cursor == "500"
        assert len(harness.fake.event_calls) == 500

    asyncio.run(scenario())


def test_complete_journal_page_budget_fails_closed_without_an_extra_request() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop(), event_pagination_max_pages=2)
        harness.fake.pages.extend(
            [
                _page(
                    harness.identity,
                    after_cursor="0",
                    events=[_stream_started(harness.identity)],
                    last_cursor="3",
                ),
                _page(
                    harness.identity,
                    after_cursor="1",
                    events=[
                        _event(
                            harness.identity,
                            2,
                            "stream_started",
                            {"ea_build_id": "current", "execution_enabled": True},
                            boot_id="boot-history-002",
                        )
                    ],
                    last_cursor="3",
                ),
            ]
        )

        with pytest.raises(Mt5V1ExecutionError, match="page budget"):
            await harness.connect()

        assert harness.fake.event_calls == [("0", 100), ("1", 100)]
        assert harness.client.execution_admitted is False
        assert harness.fake.closed is True

    asyncio.run(scenario())


def test_runtime_page_walk_stops_at_the_first_declared_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        monkeypatch.setattr(harness.client, "_apply_event", lambda _event: None)
        harness.fake.pages.extend(
            [
                _page(
                    harness.identity,
                    after_cursor="1",
                    events=[_stream_started(harness.identity, sequence=2)],
                    last_cursor="3",
                ),
                _page(
                    harness.identity,
                    after_cursor="2",
                    events=[_stream_started(harness.identity, sequence=3)],
                    last_cursor="4",
                ),
                _page(
                    harness.identity,
                    after_cursor="3",
                    events=[_stream_started(harness.identity, sequence=4)],
                    last_cursor="4",
                ),
            ]
        )
        initial_calls = len(harness.fake.event_calls)

        await harness.client._consume_event_pages()

        assert harness.client._cursor == "3"
        assert harness.fake.event_calls[initial_calls:] == [("1", 100), ("2", 1)]
        assert len(harness.fake.pages) == 1

    asyncio.run(scenario())


def test_runtime_pagination_time_budget_disconnects_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(),
            event_pagination_timeout_ms=50,
        )
        await harness.connect()
        harness.client._set_connected(True)
        harness.client._running = True

        async def never_reply(
            _binding: Binding,
            *,
            after_cursor: str,
            limit: int,
        ) -> JsonObject:
            del after_cursor, limit
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(harness.fake, "execution_events", never_reply)

        await asyncio.wait_for(harness.client._poll_events(), timeout=0.5)

        assert harness.client.execution_admitted is False
        assert harness.client.is_connected is False
        assert harness.fake.closed is True
        assert harness.client.last_failure is not None
        assert "time budget" in harness.client.last_failure

    asyncio.run(scenario())


async def _capacity_harness(
    loop: asyncio.AbstractEventLoop,
    *,
    snapshot: JsonObject | None = None,
) -> tuple[_Harness, TestClock]:
    snapshot = snapshot or _snapshot(_identity())
    clock = TestClock()
    clock.set_time(
        int(cast(str, cast(JsonObject, snapshot["time"])["observed_utc_ms"])) * 1_000_000
    )
    harness = _Harness(loop, snapshot=snapshot, clock=cast(LiveClock, clock))
    assert not harness.client.account_capacity_ready(5_000_000_000)
    await harness.connect()
    harness.client._set_connected(True)
    return harness, clock


@pytest.mark.parametrize(
    ("sides", "expected_net"),
    [
        ([], "0"),
        ([("buy", "0.015"), ("sell", "0.004")], "1.1"),
        ([("buy", "0.004"), ("sell", "0.015")], "-1.1"),
        ([("buy", "0.015"), ("sell", "0.015")], "0"),
    ],
)
def test_capacity_metadata_uses_complete_exact_ticket_sample(
    sides: list[tuple[str, str]],
    expected_net: str,
) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(_identity())
        spec = cast(JsonObject, snapshot["symbol_spec"])
        spec.update({"volume_min": "0.001", "volume_step": "0.001"})
        template = cast(list[JsonObject], snapshot["positions"])[0]
        positions: list[JsonObject] = []
        for index, (side, lots) in enumerate(sides):
            position = deepcopy(template)
            position.update(
                {
                    "ticket": str(800000010 + index),
                    "identifier": str(800000010 + index),
                    "side": side,
                    "volume_lots": lots,
                }
            )
            positions.append(position)
        snapshot["positions"] = positions
        harness, clock = await _capacity_harness(asyncio.get_running_loop(), snapshot=snapshot)
        state = harness.account_states[-1]
        assert state.info["mt5_positions_complete"] is True
        assert Decimal(state.info["mt5_net_position_ounces"]) == Decimal(expected_net)
        assert state.info["mt5_position_count"] == len(sides)
        assert state.info["mt5_symbol"] == harness.identity.symbol
        assert state.info["mt5_stream_id"] == harness.identity.stream_id
        assert state.info["mt5_account_observed_ns"] == state.ts_event == clock.timestamp_ns()
        assert state.info["mt5_account_sample_valid"] is True
        assert harness.client.account_capacity_ready(5_000_000_000)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("elapsed_ns", "ready"), [(0, True), (5_000_000_000, True), (5_000_000_001, False), (-1, False)]
)
def test_capacity_age_is_checked_at_read_time(elapsed_ns: int, ready: bool) -> None:
    async def scenario() -> None:
        harness, clock = await _capacity_harness(asyncio.get_running_loop())
        event = harness.account_states[-1]
        clock.set_time(event.ts_event + elapsed_ns)
        assert harness.client.account_capacity_ready(5_000_000_000) is ready
        # Capacity expiry never changes the pre-existing execution gate or old event.
        assert harness.client.execution_admitted
        assert event.info["mt5_account_sample_valid"] is True
        assert len(harness.account_states) == 1
        assert not harness.fake.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_age", [-1, True, 1.5])
def test_capacity_age_parameter_cannot_create_an_implicit_allow(invalid_age: object) -> None:
    async def scenario() -> None:
        harness, _ = await _capacity_harness(asyncio.get_running_loop())
        assert not harness.client.account_capacity_ready(cast(int, invalid_age))

    asyncio.run(scenario())


@pytest.mark.parametrize("bad_kind", ["future", "backward"])
def test_capacity_bad_sample_time_does_not_poison_watermark_or_execution(bad_kind: str) -> None:
    async def scenario() -> None:
        harness, clock = await _capacity_harness(asyncio.get_running_loop())
        original = harness.account_states[-1]
        original_info = deepcopy(original.info)
        clock.set_time(original.ts_event + 10_000_000)
        invalid = deepcopy(harness.snapshot)
        bad_ns = (
            clock.timestamp_ns() + 1_000_000
            if bad_kind == "future"
            else original.ts_event - 1_000_000
        )
        cast(JsonObject, invalid["time"])["observed_utc_ms"] = str(bad_ns // 1_000_000)
        harness.fake.current_snapshot = invalid
        await harness.client._refresh_snapshot_if_due(force=True)
        invalid_event = harness.account_states[-1]
        assert invalid_event.info["mt5_account_sample_valid"] is False
        assert not harness.client.account_capacity_ready(5_000_000_000)
        assert harness.client.execution_admitted
        assert harness.client.is_connected and not harness.fake.closed
        assert original.info == original_info
        assert original.info is not invalid_event.info

        recovered = deepcopy(harness.snapshot)
        cast(JsonObject, recovered["time"])["observed_utc_ms"] = str(
            (clock.timestamp_ns() - 1_000_000) // 1_000_000
        )
        harness.fake.current_snapshot = recovered
        await harness.client._refresh_snapshot_if_due(force=True)
        assert harness.client.account_capacity_ready(5_000_000_000)
        assert harness.account_states[-1].info["mt5_account_sample_valid"] is True
        assert invalid_event.info["mt5_account_sample_valid"] is False
        assert original.info == original_info

    asyncio.run(scenario())


def test_capacity_same_second_refresh_is_complete_and_does_not_alias_old_info() -> None:
    async def scenario() -> None:
        harness, _ = await _capacity_harness(asyncio.get_running_loop())
        old = harness.account_states[-1]
        old_info = deepcopy(old.info)
        replacement = deepcopy(harness.snapshot)
        cast(JsonObject, replacement["account"]).update(
            {"equity": "10002.50", "margin_free": "9992.50"}
        )
        cast(list[JsonObject], replacement["positions"])[0]["volume_lots"] = "0.02"
        harness.fake.current_snapshot = replacement
        await harness.client._refresh_snapshot_if_due(force=True)
        new = harness.account_states[-1]
        assert new.ts_event == old.ts_event
        assert new.info["mt5_equity"] == "10002.50"
        assert Decimal(new.info["mt5_net_position_ounces"]) == Decimal("2")
        assert new.info["mt5_account_sample_valid"] is True
        assert harness.client.account_capacity_ready(5_000_000_000)
        assert old.info == old_info and old.info is not new.info
        assert new.info.keys() == old.info.keys()

    asyncio.run(scenario())


def test_capacity_unavailable_disconnect_and_foreign_magic_preserve_original_gates() -> None:
    async def scenario() -> None:
        harness, _ = await _capacity_harness(asyncio.get_running_loop())
        harness.fake.snapshot_results.append(Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "incomplete"))
        await harness.client._poll_once()  # Not yet due: old complete sample remains current.
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        assert not harness.client.account_capacity_ready(5_000_000_000)
        assert harness.client.is_connected and not harness.fake.closed
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        assert harness.client.account_capacity_ready(5_000_000_000)

        foreign = deepcopy(harness.snapshot)
        cast(list[JsonObject], foreign["positions"])[0]["magic"] = "0"
        harness.fake.current_snapshot = foreign
        await harness.client._refresh_snapshot_if_due(force=True)
        assert not harness.client.execution_admitted
        assert "foreign-magic" in cast(str, harness.client.execution_hold_reason)
        assert harness.account_states[-1].info["mt5_position_count"] == 1
        assert Decimal(harness.account_states[-1].info["mt5_net_position_ounces"]) == Decimal("1")
        await harness.client._disconnect()
        assert not harness.client.account_capacity_ready(5_000_000_000)
        harness.fake.current_snapshot = harness.snapshot
        await harness.connect()
        harness.client._set_connected(True)
        assert harness.client.account_capacity_ready(5_000_000_000)
        assert harness.client.execution_admitted

    asyncio.run(scenario())


@pytest.mark.parametrize("expired", [False, True])
def test_capacity_new_fill_invalidates_sample_without_blocking_existing_execution(
    expired: bool,
) -> None:
    async def scenario() -> None:
        harness, clock = await _capacity_harness(asyncio.get_running_loop())
        old = harness.account_states[-1]
        old_info = deepcopy(old.info)
        if expired:
            clock.set_time(old.ts_event + 6_000_000_000)
        ready_during_fill: list[bool] = []

        def capture_fill(event: OrderEvent) -> None:
            harness.capture_event(event)
            if isinstance(event, OrderFilled):
                ready_during_fill.append(harness.client.account_capacity_ready(5_000_000_000))

        harness.msgbus.deregister("ExecEngine.process", harness.capture_event)
        harness.msgbus.register("ExecEngine.process", capture_fill)
        order = harness.market()
        harness.fake.outcome = _outcome(harness.identity, order, "order_filled")
        await harness.submit(order)
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderAccepted",
            "OrderFilled",
        ]
        assert not harness.client.account_capacity_ready(5_000_000_000)
        assert ready_during_fill == [False]
        assert harness.client.pending_client_order_ids == ()
        assert harness.client.execution_admitted
        assert len(harness.fake.submit_calls) == 1 and not harness.fake.closed
        assert old.info == old_info
        # No new journal fact on a repeated poll restores or reapplies the fill.
        await harness.client._poll_once()
        assert not harness.client.account_capacity_ready(5_000_000_000)
        assert len(harness.events) == 3

        refreshed = deepcopy(harness.snapshot)
        positions = cast(list[JsonObject], refreshed["positions"])
        newly_filled = deepcopy(positions[0])
        newly_filled.update(
            {
                "identifier": "900000002",
                "ticket": "700000002",
                "volume_lots": "1",
                "price_open": "2401.25",
            }
        )
        positions.append(newly_filled)
        cast(JsonObject, refreshed["time"])["observed_utc_ms"] = str(
            clock.timestamp_ns() // 1_000_000
        )
        harness.fake.current_snapshot = refreshed
        await harness.client._refresh_snapshot_if_due(force=True)
        assert harness.client.account_capacity_ready(5_000_000_000)
        assert Decimal(harness.account_states[-1].info["mt5_net_position_ounces"]) == Decimal("101")
        assert old.info == old_info

    asyncio.run(scenario())


def test_capacity_account_callback_observes_recovered_sample_before_publication() -> None:
    async def scenario() -> None:
        harness, _ = await _capacity_harness(asyncio.get_running_loop())
        seen: list[tuple[bool, str]] = []

        def capture_account(event: AccountState) -> None:
            harness.account_states.append(event)
            seen.append(
                (
                    harness.client.account_capacity_ready(5_000_000_000),
                    event.info["mt5_net_position_ounces"],
                )
            )

        harness.msgbus.deregister("Portfolio.update_account", harness.account_states.append)
        harness.msgbus.register("Portfolio.update_account", capture_account)
        harness.fake.snapshot_results.append(Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "incomplete"))
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        assert not harness.client.account_capacity_ready(5_000_000_000)
        assert seen == []
        await harness.client._refresh_snapshot_if_due(force=True)
        assert seen == [(True, "1.00")]

    asyncio.run(scenario())


def test_capacity_journal_verification_does_not_renew_old_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness, clock = await _capacity_harness(asyncio.get_running_loop())
        observed_ns = harness.account_states[-1].ts_event
        original_query = harness.fake.execution_events

        async def delayed_query(binding: Binding, *, after_cursor: str, limit: int) -> JsonObject:
            page = await original_query(binding, after_cursor=after_cursor, limit=limit)
            clock.set_time(observed_ns + 5_000_000_001)
            return page

        monkeypatch.setattr(harness.fake, "execution_events", delayed_query)
        await harness.client._refresh_snapshot_if_due(force=True)
        assert harness.account_states[-1].info["mt5_account_observed_ns"] == observed_ns
        assert not harness.client.account_capacity_ready(5_000_000_000)
        assert harness.client.execution_admitted and not harness.fake.closed

    asyncio.run(scenario())


def test_connect_publishes_snapshot_backed_margin_account_state() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())

        await harness.connect()

        assert len(harness.account_states) == 1
        state = harness.account_states[0]
        assert state.account_id == harness.client.account_id
        assert state.account_type == AccountType.MARGIN
        assert state.is_reported is True
        assert state.ts_event == 1_788_271_200_000_000_000
        assert len(state.balances) == 1
        balance = state.balances[0]
        assert balance.total.as_decimal() == Decimal("10001.25")
        assert balance.locked.as_decimal() == Decimal("10.00")
        assert balance.free.as_decimal() == Decimal("9991.25")
        assert state.info == {
            "mt5_balance": "10000.00",
            "mt5_equity": "10001.25",
            "mt5_margin": "10.00",
            "mt5_margin_free": "9991.25",
            "mt5_margin_level": "100012.5",
            "mt5_leverage": 100,
            "mt5_positions_complete": True,
            "mt5_net_position_ounces": "1.00",
            "mt5_position_count": 1,
            "mt5_symbol": harness.identity.symbol,
            "mt5_stream_id": harness.identity.stream_id,
            "mt5_account_observed_ns": 1_788_271_200_000_000_000,
            "mt5_account_sample_valid": True,
        }

    asyncio.run(scenario())


def test_connect_rejects_internally_inconsistent_account_balance() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        cast(JsonObject, snapshot["account"])["margin_free"] = "9991.24"
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
        )

        with pytest.raises(Mt5V1ExecutionError, match="free margin are inconsistent"):
            await harness.connect()

        assert harness.account_states == []
        assert harness.client.execution_admitted is False
        assert harness.fake.closed is True

    asyncio.run(scenario())


def test_connect_rejects_negative_used_margin() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        account = cast(JsonObject, snapshot["account"])
        account["margin"] = "-1"
        account["margin_free"] = "10002.25"
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
        )

        with pytest.raises(Mt5V1ExecutionError, match="margin cannot be negative"):
            await harness.connect()

        assert harness.account_states == []
        assert harness.client.execution_admitted is False
        assert harness.fake.closed is True

    asyncio.run(scenario())


def test_connect_binds_the_expected_ea_order_limit() -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(),
            expected_max_order_lots=Decimal("0.02"),
        )

        with pytest.raises(Mt5V1ExecutionError, match="snapshot is inconsistent"):
            await harness.connect()

        assert harness.client.execution_admitted is False
        assert harness.fake.closed is True

    asyncio.run(scenario())


def test_config_rejects_an_ea_order_limit_beyond_wire_precision() -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="at most 8 places"):
            _Harness(
                asyncio.get_running_loop(),
                expected_max_order_lots=Decimal("0.000000001"),
            )

    asyncio.run(scenario())


def test_order_above_the_bound_ea_limit_is_denied_before_transport() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        cast(JsonObject, snapshot["execution_limits"])["max_order_lots"] = "0.01"
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
        )
        await harness.connect()

        await harness.submit(harness.market("2"))

        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


def test_quantity_capability_reuses_the_current_broker_and_ea_limits_without_io() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        cast(JsonObject, snapshot["execution_limits"])["max_order_lots"] = "0.015"
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
        )

        assert harness.client.can_execute_quantity(Decimal("1")) is False
        await harness.connect()
        snapshot_calls = list(harness.fake.snapshot_calls)
        event_calls = list(harness.fake.event_calls)

        assert harness.client.can_execute_quantity(Decimal("1")) is True
        assert harness.client.can_execute_quantity(Decimal("2")) is False
        assert harness.client.can_execute_quantity(Decimal("0.5")) is False
        assert harness.client.can_execute_quantity(Decimal("1.5")) is False
        assert harness.fake.snapshot_calls == snapshot_calls
        assert harness.fake.event_calls == event_calls
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["pagination", "snapshot"])
def test_connect_rejects_a_journal_tail_that_moves_during_startup(phase: str) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        if phase == "pagination":
            harness.fake.pages.extend(
                [
                    _page(
                        harness.identity,
                        after_cursor="0",
                        events=[_stream_started(harness.identity, boot_id="boot-prior-001")],
                        last_cursor="2",
                    ),
                    _page(
                        harness.identity,
                        after_cursor="1",
                        events=[_stream_started(harness.identity, sequence=2)],
                        last_cursor="3",
                    ),
                ]
            )
            message = "tail changed during pagination"
        else:
            order = harness.market()
            harness.fake.pages.extend(
                [
                    _page(
                        harness.identity,
                        after_cursor="0",
                        events=[_stream_started(harness.identity)],
                    ),
                    _page(
                        harness.identity,
                        after_cursor="1",
                        events=[
                            _event(
                                harness.identity,
                                2,
                                "submission_reserved",
                                _submission_payload(order),
                            )
                        ],
                    ),
                ]
            )
            message = "changed while snapshot was captured"

        with pytest.raises(Mt5V1ExecutionError, match=message):
            await harness.connect()

        assert harness.client.execution_admitted is False
        assert harness.fake.closed is True
        assert harness.events == []

    asyncio.run(scenario())


def test_market_with_non_fok_semantics_is_denied_without_transport_send() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        await harness.submit(harness.market(time_in_force=TimeInForce.IOC))
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("semantic", ["reduce_only", "quote_quantity", "exec_algorithm"])
def test_market_with_unrepresentable_semantics_is_denied_without_transport_send(
    semantic: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        if semantic == "reduce_only":
            order = harness.market(reduce_only=True)
        elif semantic == "quote_quantity":
            order = harness.market(quote_quantity=True)
        else:
            order = harness.market(exec_algorithm_id=ExecAlgorithmId("ALGO"))

        await harness.submit(order)

        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("reduce_only", "position_id"),
    [
        (True, None),
        (False, PositionId("800000001")),
    ],
)
def test_exact_close_requires_reduce_only_and_position_id_together(
    reduce_only: bool,
    position_id: PositionId | None,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        order = harness.market(
            "1",
            order_side=OrderSide.SELL,
            reduce_only=reduce_only,
        )

        await harness.submit(order, position_id=position_id)

        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert (
            "requires both reduce-only and an exact position ID"
            in cast(
                OrderDenied,
                harness.events[0],
            ).reason
        )
        assert harness.fake.submit_calls == []
        assert harness.fake.close_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("target_volume_lots", ["0.01", "0.02"])
@pytest.mark.parametrize("expected_ounces", [Decimal(1), Decimal(2)])
def test_planned_close_requires_exact_fresh_target_quantity(
    target_volume_lots: str, expected_ounces: Decimal,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop(), snapshot_refresh_interval_ms=60_000)
        cast(list[JsonObject], harness.snapshot["positions"])[0]["volume_lots"] = "0.01"
        await harness.connect()
        fresh = deepcopy(harness.snapshot)
        cast(list[JsonObject], fresh["positions"])[0]["volume_lots"] = target_volume_lots
        harness.fake.snapshot_results.append(fresh)
        order = harness.market("1", order_side=OrderSide.SELL, reduce_only=True)
        harness.fake.outcome = _outcome(
            harness.identity, order, "order_filled", quantity_lots="0.01",
            position_ticket="700000001", position_identifier="800000001",
            venue_position_id="800000001",
        )
        await harness.submit(order, position_id=PositionId("800000001"), params={
            "py000_hedge_plan": True, "py000_expected_position_ounces": expected_ounces,
        })
        assert len(harness.fake.snapshot_calls) == 2
        if expected_ounces == Decimal(target_volume_lots) * 100:
            assert len(harness.fake.close_calls) == 1
            assert isinstance(harness.events[-1], OrderFilled)
        else:
            assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
            assert "planned target quantity" in cast(OrderDenied, harness.events[0]).reason
            assert harness.fake.close_calls == []
            assert str(order.client_order_id) not in harness.client._seen_request_ids
        assert harness.fake.submit_calls == []
        assert harness.client.pending_client_order_ids == ()
    asyncio.run(scenario())


@pytest.mark.parametrize("fresh_side", [None, "buy", "sell"])
def test_planned_open_forces_snapshot_and_refuses_new_opposing_ticket(
    fresh_side: str | None,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop(), snapshot_refresh_interval_ms=60_000)
        fresh = deepcopy(harness.snapshot)
        target = cast(list[JsonObject], fresh["positions"])[0]
        target["volume_lots"] = "0.01"
        if fresh_side is None:
            fresh["positions"] = []
        else:
            target["side"] = fresh_side
        harness.snapshot["positions"] = []
        await harness.connect()
        harness.fake.snapshot_results.append(fresh)
        order = harness.market("1", order_side=OrderSide.SELL)
        harness.fake.outcome = _outcome(
            harness.identity, order, "order_filled", quantity_lots="0.01",
        )
        await harness.submit(order, params={"py000_hedge_plan": True})
        assert len(harness.fake.snapshot_calls) == 2
        if fresh_side == "buy":
            assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
            assert "opposing" in cast(OrderDenied, harness.events[0]).reason
            assert harness.fake.submit_calls == []
            assert str(order.client_order_id) not in harness.client._seen_request_ids
        else:
            assert len(harness.fake.submit_calls) == 1
            assert isinstance(harness.events[-1], OrderFilled)
        assert harness.fake.close_calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize("params,is_close", [
    ({"py000_hedge_plan": False}, False),
    ({"py000_hedge_plan": 1}, False),
    ({"py000_hedge_plan": "true"}, False),
    ({"py000_hedge_plan": None}, False),
    ({"py000_expected_position_ounces": Decimal(1)}, True),
    ({"py000_hedge_plan": True}, True),
    ({"py000_hedge_plan": True, "py000_expected_position_ounces": None}, False),
    *[
        ({"py000_hedge_plan": True, "py000_expected_position_ounces": value}, True)
        for value in (None, True, 1, "1", Decimal(0), Decimal(-1), Decimal("NaN"),
                      Decimal("Infinity"), Decimal("-Infinity"))
    ],
])
def test_invalid_planned_hedge_params_deny_without_mutation(
    params: dict[str, object], is_close: bool,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        order = harness.market("1", order_side=OrderSide.SELL, reduce_only=is_close)
        harness.fake.outcome = _outcome(
            harness.identity, order, "order_rejected", quantity_lots="0.01",
            position_ticket="700000001" if is_close else None,
            position_identifier="800000001" if is_close else None,
        )
        await harness.submit(
            order, position_id=PositionId("800000001") if is_close else None, params=params,
        )
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == [] and harness.fake.close_calls == []
        assert harness.client.pending_client_order_ids == ()
        assert str(order.client_order_id) not in harness.client._seen_request_ids
    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", [False, True])
def test_planned_open_cannot_fall_back_when_forced_snapshot_fails(unavailable: bool) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop(), snapshot_refresh_interval_ms=60_000)
        harness.snapshot["positions"] = []
        await harness.connect()
        harness.fake.snapshot_results.append(
            Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "incomplete") if unavailable
            else Mt5V1RequestTimeout("forced snapshot timeout"),
        )
        order = harness.market("1")
        harness.fake.outcome = _outcome(
            harness.identity, order, "order_filled", quantity_lots="0.01",
        )
        await harness.submit(order, params={"py000_hedge_plan": True})
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []
        assert harness.client.pending_client_order_ids == ()
        assert str(order.client_order_id) not in harness.client._seen_request_ids
    asyncio.run(scenario())


@pytest.mark.parametrize("target_volume_lots", ["0.01", "0.02"])
def test_exact_close_or_reduce_forces_snapshot_and_binds_identifier_to_ticket(
    target_volume_lots: str,
) -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        target = cast(list[JsonObject], snapshot["positions"])[0]
        target["volume_lots"] = target_volume_lots
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
            snapshot_refresh_interval_ms=60_000,
        )
        order = harness.market(
            "1",
            order_side=OrderSide.SELL,
            reduce_only=True,
            client_order_id=ClientOrderId("CLOSE-TARGET-1"),
        )
        harness.fake.outcome = _outcome(
            identity,
            order,
            "order_filled",
            quantity_lots="0.01",
            position_ticket="700000001",
            position_identifier="800000001",
            venue_position_id="800000001",
        )
        await harness.connect()
        assert len(harness.fake.snapshot_calls) == 1

        await harness.submit(order, position_id=PositionId("800000001"))

        assert len(harness.fake.snapshot_calls) == 2
        assert harness.fake.submit_calls == []
        assert harness.fake.close_calls == [
            ("CLOSE-TARGET-1", "sell", "0.01", "700000001", "800000001")
        ]
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderAccepted",
            "OrderFilled",
        ]
        fill = harness.events[-1]
        assert str(fill.position_id) == "800000001"
        assert fill.order_side == OrderSide.SELL
        assert str(fill.last_qty) == "1"
        assert harness.client.pending_client_order_ids == ()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("invalid_target", "expected_reason"),
    [
        ("wrong_side", "does not oppose the target position"),
        ("absent", "absent or duplicated"),
        ("foreign", "snapshot contains foreign-magic position"),
        ("overqty", "exceeds the live target position"),
        ("bad_remainder", "invalid MT5 lot remainder"),
    ],
)
@pytest.mark.parametrize("planned", [False, True])
def test_exact_close_rejects_invalid_live_target_without_transport_send(
    invalid_target: str,
    expected_reason: str,
    planned: bool,
) -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        target = cast(list[JsonObject], snapshot["positions"])[0]
        order_side = OrderSide.SELL
        position_id = PositionId("800000001")
        quantity = "1"
        if invalid_target == "wrong_side":
            order_side = OrderSide.BUY
        elif invalid_target == "absent":
            position_id = PositionId("800000099")
        elif invalid_target == "foreign":
            target["magic"] = "900000099"
        elif invalid_target == "overqty":
            quantity = "2"
        else:
            target["volume_lots"] = "0.015"
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
            snapshot_refresh_interval_ms=60_000,
        )
        await harness.connect()
        order = harness.market(
            quantity,
            order_side=order_side,
            reduce_only=True,
        )

        await harness.submit(order, position_id=position_id, params={
            "py000_hedge_plan": True,
            "py000_expected_position_ounces": Decimal(cast(str, target["volume_lots"])) * 100,
        } if planned else None)

        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert expected_reason in cast(OrderDenied, harness.events[0]).reason
        assert len(harness.fake.snapshot_calls) == 2
        assert harness.fake.submit_calls == []
        assert harness.fake.close_calls == []

    asyncio.run(scenario())


def test_exact_close_position_id_mismatch_stays_unknown_without_retry() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        order = harness.market(
            "1",
            order_side=OrderSide.SELL,
            reduce_only=True,
            client_order_id=ClientOrderId("CLOSE-MISMATCH-1"),
        )
        harness.fake.outcome = _outcome(
            harness.identity,
            order,
            "order_filled",
            quantity_lots="0.01",
            position_ticket="700000001",
            position_identifier="800000001",
            venue_position_id="800000099",
        )
        await harness.connect()

        with pytest.raises(Mt5V1ExecutionError, match="reserved target position"):
            await harness.submit(order, position_id=PositionId("800000001"))

        assert harness.fake.close_calls == [
            ("CLOSE-MISMATCH-1", "sell", "0.01", "700000001", "800000001")
        ]
        assert harness.fake.submit_calls == []
        assert [type(event).__name__ for event in harness.events] == ["OrderSubmitted"]
        assert harness.client.pending_client_order_ids == ("CLOSE-MISMATCH-1",)
        assert "UNKNOWN" in cast(str, harness.client.execution_hold_reason)

        await harness.submit(harness.market(client_order_id=ClientOrderId("AFTER-MISMATCH-1")))
        assert len(harness.fake.close_calls) == 1
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


def test_historical_exact_close_report_preserves_reduce_only_and_target_position() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop(), capture_events=False)
        order = harness.market(
            "1",
            order_side=OrderSide.SELL,
            reduce_only=True,
            client_order_id=ClientOrderId("REPORT-CLOSE-1"),
        )
        harness.fake.pages.append(
            _page(
                harness.identity,
                after_cursor="0",
                events=[
                    _stream_started(harness.identity),
                    _event(
                        harness.identity,
                        2,
                        "submission_reserved",
                        _submission_payload(
                            order,
                            "0.01",
                            position_ticket="700000001",
                            position_identifier="800000001",
                        ),
                    ),
                    _outcome(
                        harness.identity,
                        order,
                        "order_filled",
                        sequence=3,
                        quantity_lots="0.01",
                        position_ticket="700000001",
                        position_identifier="800000001",
                        venue_position_id="800000001",
                    ),
                ],
            )
        )
        await harness.connect()

        reports = await harness.client.generate_order_status_reports(
            _order_reports_command(harness)
        )

        assert len(reports) == 1
        report = reports[0]
        assert report.client_order_id == order.client_order_id
        assert report.reduce_only is True
        assert str(report.venue_position_id) == "800000001"
        assert report.order_side == OrderSide.SELL
        assert str(report.quantity) == "1"
        assert str(report.filled_qty) == "1"

    asyncio.run(scenario())


def test_cold_start_rejects_close_fill_for_a_different_position_id() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop(), capture_events=False)
        order = harness.market(
            "1",
            order_side=OrderSide.SELL,
            reduce_only=True,
            client_order_id=ClientOrderId("REPORT-CLOSE-MISMATCH-1"),
        )
        harness.fake.pages.append(
            _page(
                harness.identity,
                after_cursor="0",
                events=[
                    _stream_started(harness.identity),
                    _event(
                        harness.identity,
                        2,
                        "submission_reserved",
                        _submission_payload(
                            order,
                            "0.01",
                            position_ticket="700000001",
                            position_identifier="800000001",
                        ),
                    ),
                    _outcome(
                        harness.identity,
                        order,
                        "order_filled",
                        sequence=3,
                        quantity_lots="0.01",
                        position_ticket="700000001",
                        position_identifier="800000001",
                        venue_position_id="800000099",
                    ),
                ],
            )
        )

        with pytest.raises(Mt5V1ExecutionError):
            await harness.connect()

        assert harness.client.execution_admitted is False
        assert harness.fake.closed is True

    asyncio.run(scenario())


def test_market_is_submitted_before_one_call_then_accepted_and_filled() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        order = harness.market()
        harness.fake.outcome = _outcome(harness.identity, order, "order_filled")
        await harness.connect()
        await harness.submit(order)

        assert harness.fake.submit_calls == [(str(order.client_order_id), "buy", "1")]
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderAccepted",
            "OrderFilled",
        ]
        fill = harness.events[-1]
        assert str(fill.venue_order_id) == "700000002"
        assert str(fill.trade_id) == "800000002"
        assert str(fill.position_id) == "900000002"
        assert str(fill.last_qty) == "100"
        assert str(fill.last_px) == "2401.25"
        assert str(fill.commission) == "1.25 USD"
        assert harness.client._cursor == "3"
        assert harness.fake.pages == []
        assert harness.client.pending_client_order_ids == ()
        assert harness.pending_during_fills == [()]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("native_fee", "booked_fee"),
    [
        ("-1.25", "1.25"),
        ("1.25", "-1.25"),
        ("0", "0"),
        ("-0.005", "0"),
        ("0.005", "0"),
        ("-0.015", "0.02"),
        ("0.015", "-0.02"),
        ("-0.061668", "0.06"),
    ],
)
def test_native_commission_is_booked_as_cost_in_live_fills_and_replayed_reports(
    native_fee: str,
    booked_fee: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        order = harness.market()
        outcome = _outcome(harness.identity, order, "order_filled")
        cast(JsonObject, outcome["payload"])["commission"] = native_fee
        harness.fake.outcome = outcome
        await harness.connect()
        await harness.submit(order)

        fill = cast(OrderFilled, harness.events[-1])
        expected_cost = Decimal(booked_fee)
        assert fill.commission.as_decimal() == expected_cost
        position = Position(harness.instrument, fill)
        assert position.realized_pnl.as_decimal() == -expected_cost
        assert harness.client.pending_client_order_ids == ()
        assert len(harness.fake.submit_calls) == 1

        replay = _Harness(asyncio.get_running_loop(), identity=harness.identity)
        replay.fake.pages.append(
            _page(
                harness.identity,
                after_cursor="0",
                events=[
                    _stream_started(harness.identity),
                    harness.client._reservations[str(order.client_order_id)],
                    outcome,
                ],
            )
        )
        await replay.connect()
        reports = await replay.client.generate_fill_reports(_fill_reports_command(replay))
        assert len(reports) == 1
        assert reports[0].commission == fill.commission
        assert replay.fake.submit_calls == []
        assert cast(JsonObject, outcome["payload"])["commission"] == native_fee

    asyncio.run(scenario())


@pytest.mark.parametrize(("native_fee", "expected_pnl"), [("-1.25", "-2.50"), ("1.25", "2.50")])
def test_same_price_exact_close_realized_pnl_includes_both_native_commissions(
    native_fee: str,
    expected_pnl: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        opening = harness.market(client_order_id=ClientOrderId("FEE-OPEN"))
        outcome = _outcome(harness.identity, opening, "order_filled")
        cast(JsonObject, outcome["payload"])["commission"] = native_fee
        harness.fake.outcome = outcome
        await harness.connect()
        await harness.submit(opening)
        position = Position(harness.instrument, cast(OrderFilled, harness.events[-1]))

        target = deepcopy(cast(list[JsonObject], harness.snapshot["positions"])[0])
        target.update(
            {"identifier": "900000002", "ticket": "700000002", "side": "buy", "volume_lots": "1"}
        )
        harness.snapshot["positions"] = [target]
        harness.events.clear()
        closing = harness.market(
            client_order_id=ClientOrderId("FEE-CLOSE"),
            order_side=OrderSide.SELL,
            reduce_only=True,
        )
        outcome = _outcome(
            harness.identity,
            closing,
            "order_filled",
            sequence=5,
            position_ticket="700000002",
            position_identifier="900000002",
        )
        cast(JsonObject, outcome["payload"]).update(
            {"commission": native_fee, "venue_deal_id": "800000003", "venue_order_id": "700000003"}
        )
        harness.fake.outcome = outcome
        await harness.submit(closing, position_id=PositionId("900000002"))
        position.apply(cast(OrderFilled, harness.events[-1]))

        assert position.is_closed
        assert position.realized_pnl.as_decimal() == Decimal(expected_pnl)
        assert len(harness.fake.submit_calls) == len(harness.fake.close_calls) == 1
        assert harness.client.pending_client_order_ids == ()

    asyncio.run(scenario())


def test_commission_conversion_failure_keeps_pending_before_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_conversion(_native_fee: str) -> None:
        raise Mt5V1ExecutionError("injected commission conversion failure")

    monkeypatch.setattr(mt5_execution, "_mt5_usd_commission", fail_conversion, raising=False)

    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        order = harness.market()
        harness.fake.outcome = _outcome(harness.identity, order, "order_filled")
        await harness.connect()
        with pytest.raises(Mt5V1ExecutionError, match="injected commission"):
            await harness.submit(order)

        assert [type(event).__name__ for event in harness.events] == ["OrderSubmitted"]
        assert harness.client.pending_client_order_ids == (str(order.client_order_id),)
        assert harness.client._pending[str(order.client_order_id)].unknown
        assert harness.client.execution_admitted is False
        assert len(harness.fake.submit_calls) == 1

    asyncio.run(scenario())


def test_rejected_outcome_is_terminal() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        order = harness.market()
        harness.fake.outcome = _outcome(harness.identity, order, "order_rejected")
        await harness.connect()
        await harness.submit(order)
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderRejected",
        ]
        assert harness.client.pending_client_order_ids == ()
        assert harness.client._cursor == "3"

    asyncio.run(scenario())


def test_cold_start_hydrates_old_id_without_replaying_or_sending() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        order = harness.market()
        harness.fake.pages.append(
            _page(
                harness.identity,
                after_cursor="0",
                events=[
                    _stream_started(harness.identity),
                    _event(
                        harness.identity,
                        2,
                        "submission_reserved",
                        _submission_payload(order),
                    ),
                    _outcome(harness.identity, order, "order_filled"),
                ],
            )
        )
        await harness.connect()

        assert harness.events == []
        assert harness.client._cursor == "3"

        await harness.submit(order)
        await harness.submit(
            harness.market(
                "200",
                client_order_id=order.client_order_id,
            )
        )

        assert [type(event).__name__ for event in harness.events] == [
            "OrderDenied",
            "OrderDenied",
        ]
        assert harness.fake.submit_calls == []
        assert harness.client._cursor == "3"

    asyncio.run(scenario())


def test_cold_start_projects_all_pages_before_admitting_execution() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        old_order = harness.market()
        harness.fake.pages.extend(
            [
                _page(
                    harness.identity,
                    after_cursor="0",
                    events=[
                        _stream_started(harness.identity),
                        _event(
                            harness.identity,
                            2,
                            "submission_reserved",
                            _submission_payload(old_order),
                        ),
                    ],
                    last_cursor="3",
                ),
                _page(
                    harness.identity,
                    after_cursor="2",
                    events=[_outcome(harness.identity, old_order, "order_rejected")],
                ),
            ]
        )

        await harness.connect()

        initially_admitted = harness.client.execution_admitted
        assert initially_admitted is True
        assert harness.client._cursor == "3"
        assert harness.fake.event_calls == [("0", 100), ("2", 100), ("3", 100)]
        assert harness.events == []
        await harness.submit(old_order)
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("unresolved", "hold_text"),
    [
        ("dangling", "dangling"),
        ("unknown", "unknown"),
        ("mismatched_fill", "mismatched FOK fill"),
    ],
)
def test_historical_unresolved_request_keeps_execution_and_reports_on_hold(
    unresolved: str,
    hold_text: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        old_order = harness.market()
        events = [
            _stream_started(harness.identity),
            _event(
                harness.identity,
                2,
                "submission_reserved",
                _submission_payload(old_order),
            ),
        ]
        if unresolved == "unknown":
            events.append(_outcome(harness.identity, old_order, "order_unknown"))
        elif unresolved == "mismatched_fill":
            outcome = _outcome(harness.identity, old_order, "order_filled")
            cast(JsonObject, outcome["payload"])["filled_quantity_lots"] = "0.5"
            events.append(outcome)
        harness.fake.pages.append(_page(harness.identity, after_cursor="0", events=events))

        await harness.connect()

        admitted_during_timeout = harness.client.execution_admitted
        assert admitted_during_timeout is False
        assert hold_text.lower() in cast(str, harness.client.execution_hold_reason).lower()
        assert harness.events == []
        with pytest.raises(Mt5V1ExecutionError, match="reports are unavailable"):
            await harness.client.generate_order_status_reports(_order_reports_command(harness))
        await harness.submit(harness.market("200"))
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("invalid_history", "message"),
    [
        ("terminal_without_reservation", "no reservation"),
        ("payload_mismatch", "differs from its reservation"),
        ("duplicate_reservation", "repeats a request reservation"),
    ],
)
def test_invalid_historical_pairing_fails_connect_closed(
    invalid_history: str,
    message: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        old_order = harness.market()
        reservation = _event(
            harness.identity,
            2,
            "submission_reserved",
            _submission_payload(old_order),
        )
        if invalid_history == "terminal_without_reservation":
            events = [
                _stream_started(harness.identity),
                _outcome(harness.identity, old_order, "order_rejected", sequence=2),
            ]
        elif invalid_history == "payload_mismatch":
            mismatch = _outcome(harness.identity, old_order, "order_rejected")
            cast(JsonObject, mismatch["payload"])["quantity_lots"] = "2"
            events = [_stream_started(harness.identity), reservation, mismatch]
        else:
            events = [
                _stream_started(harness.identity),
                reservation,
                _event(
                    harness.identity,
                    3,
                    "submission_reserved",
                    _submission_payload(old_order),
                ),
            ]
        harness.fake.pages.append(_page(harness.identity, after_cursor="0", events=events))

        with pytest.raises(Mt5V1ExecutionError, match=message):
            await harness.connect()

        assert harness.fake.closed is True
        assert harness.events == []

    asyncio.run(scenario())


def test_unknown_outcome_stays_pending_and_blocks_without_retry() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        first = harness.market()
        harness.fake.outcome = _outcome(harness.identity, first, "order_unknown")
        await harness.connect()
        await harness.submit(first)
        await harness.submit(harness.market("200"))
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderDenied",
        ]
        assert len(harness.fake.submit_calls) == 1
        assert harness.client.pending_client_order_ids == (str(first.client_order_id),)

    asyncio.run(scenario())


def test_submit_outcome_page_budget_stays_pending_unknown_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(),
            event_pagination_max_pages=1,
        )
        order = harness.market()
        outcome = _outcome(harness.identity, order, "order_filled")
        harness.fake.outcome = outcome
        await harness.connect()
        original_submit = harness.fake.submit_market_delta

        async def submit_with_paginated_outcome(
            binding: Binding,
            *,
            client_request_id: str,
            side: str,
            quantity_lots: str,
        ) -> JsonObject:
            result = await original_submit(
                binding,
                client_request_id=client_request_id,
                side=side,
                quantity_lots=quantity_lots,
            )
            harness.fake.pages[:] = [
                _page(
                    harness.identity,
                    after_cursor="1",
                    events=[
                        _event(
                            harness.identity,
                            2,
                            "submission_reserved",
                            _submission_payload(order),
                        )
                    ],
                    last_cursor="3",
                ),
                _page(
                    harness.identity,
                    after_cursor="2",
                    events=[outcome],
                    last_cursor="3",
                ),
            ]
            return result

        monkeypatch.setattr(harness.fake, "submit_market_delta", submit_with_paginated_outcome)

        with pytest.raises(Mt5V1ExecutionError, match="page budget"):
            await harness.submit(order)

        assert harness.fake.submit_calls == [(str(order.client_order_id), "buy", "1")]
        assert harness.client.pending_client_order_ids == (str(order.client_order_id),)
        assert "UNKNOWN" in cast(str, harness.client.execution_hold_reason)
        assert len(harness.fake.pages) == 1

        await harness.submit(harness.market("200"))

        assert harness.fake.submit_calls == [(str(order.client_order_id), "buy", "1")]
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderDenied",
        ]

    asyncio.run(scenario())


def test_timeout_stays_pending_and_poll_can_recover_real_fill() -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(),
            outcome=Mt5V1RequestTimeout("timeout after send"),
        )
        first = harness.market()
        await harness.connect()
        await harness.submit(first)
        await harness.submit(harness.market("200"))
        assert len(harness.fake.submit_calls) == 1
        assert harness.client.pending_client_order_ids == (str(first.client_order_id),)
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderDenied",
        ]
        with pytest.raises(Mt5V1ExecutionError, match="while an order is pending"):
            await harness.client.generate_order_status_reports(_order_reports_command(harness))

        harness.fake.pages.append(
            _page(
                harness.identity,
                after_cursor="1",
                events=[
                    _event(
                        harness.identity,
                        2,
                        "submission_reserved",
                        _submission_payload(first),
                    ),
                    _outcome(harness.identity, first, "order_filled"),
                ],
            )
        )
        await harness.client._poll_once()
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderDenied",
            "OrderAccepted",
            "OrderFilled",
        ]
        assert harness.client.pending_client_order_ids == ()
        assert harness.client._cursor == "3"
        assert len(harness.fake.submit_calls) == 1

    asyncio.run(scenario())


def test_query_timeout_holds_then_next_successful_poll_recovers() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        harness.client._set_connected(True)
        initially_admitted = harness.client.execution_admitted
        assert initially_admitted is True
        harness.fake.pages.append(Mt5V1RequestTimeout("synthetic event query timeout"))

        await harness.client._poll_once()

        admitted_during_timeout = harness.client.execution_admitted
        hold_during_timeout = harness.client.execution_hold_reason
        failure_during_timeout = harness.client.last_failure
        assert admitted_during_timeout is False
        assert hold_during_timeout is not None
        assert failure_during_timeout is not None
        assert harness.client.is_connected is True
        assert harness.fake.closed is False
        assert harness.fake.submit_calls == []
        assert harness.fake.close_calls == []

        await harness.client._poll_once()

        admitted_after_recovery = harness.client.execution_admitted
        hold_after_recovery = harness.client.execution_hold_reason
        failure_after_recovery = harness.client.last_failure
        assert admitted_after_recovery is True
        assert hold_after_recovery is None
        assert failure_after_recovery is None
        assert harness.client.is_connected is True
        assert harness.fake.closed is False
        assert harness.fake.submit_calls == []
        assert harness.fake.close_calls == []

    asyncio.run(scenario())


def test_remote_poll_error_disconnects_instead_of_retrying_a_stale_binding() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        harness.client._set_connected(True)
        harness.client._running = True
        harness.fake.pages.append(Mt5V1RemoteError("BINDING_MISMATCH", "EA rebooted"))

        await asyncio.wait_for(harness.client._poll_events(), timeout=0.5)

        assert harness.client.is_connected is False
        assert harness.client._running is False
        assert harness.fake.closed is True
        assert harness.client.last_failure == "Mt5V1RemoteError: BINDING_MISMATCH: EA rebooted"
        assert harness.fake.event_calls[-1][0] == harness.client._cursor
        assert harness.client.execution_admitted is False
        await harness.submit(harness.market())
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


def test_reconnect_rejects_a_replaced_journal_stream() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        await harness.client._disconnect()
        replacement_wire = harness.identity.to_wire()
        replacement_wire.update(
            {"boot_id": "boot-replacement-001", "stream_id": "stream-replacement-001"}
        )
        replacement = Identity.from_wire(replacement_wire)
        harness.fake.identity = replacement
        harness.fake.current_snapshot = _snapshot(replacement)

        with pytest.raises(Mt5V1ExecutionError, match="expected_stream_id"):
            await harness.connect()

        assert harness.fake.closed is True

    asyncio.run(scenario())


def test_fresh_process_rejects_a_stream_not_bound_by_config() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        replacement_wire = harness.identity.to_wire()
        replacement_wire.update(
            {"boot_id": "boot-replacement-001", "stream_id": "stream-replacement-001"}
        )
        replacement = Identity.from_wire(replacement_wire)
        harness.fake.identity = replacement
        harness.fake.current_snapshot = _snapshot(replacement)

        with pytest.raises(Mt5V1ExecutionError, match="expected_stream_id"):
            await harness.connect()

        assert harness.fake.closed is True
        assert harness.fake.event_calls == []

    asyncio.run(scenario())


def test_reconnect_keeps_an_unresolved_submission_on_hold_without_retry() -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(),
            outcome=Mt5V1TransportError("timeout after send"),
        )
        await harness.connect()
        await harness.submit(harness.market())
        await harness.client._disconnect()

        await harness.connect()

        assert harness.client.execution_admitted is False
        assert "UNKNOWN" in cast(str, harness.client.execution_hold_reason)
        assert len(harness.fake.submit_calls) == 1
        await harness.submit(harness.market("200"))
        assert len(harness.fake.submit_calls) == 1

    asyncio.run(scenario())


def test_blocked_recovery_rejects_reports_and_execution_admission() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop(), recovery_state="blocked")

        await harness.connect()

        assert harness.client.execution_admitted is False
        assert "recovery state is blocked" in cast(
            str,
            harness.client.execution_hold_reason,
        )
        with pytest.raises(Mt5V1ExecutionError, match="EA recovery is blocked"):
            await harness.client.generate_fill_reports(_fill_reports_command(harness))
        await harness.submit(harness.market())
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


def test_reconnect_recovers_a_durable_fill_for_local_unknown_exactly_once() -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(),
            outcome=Mt5V1TransportError("timeout after send"),
        )
        order = harness.market()
        await harness.connect()
        await harness.submit(order)
        await harness.client._disconnect()
        harness.fake.pages.append(
            _page(
                harness.identity,
                after_cursor="0",
                events=[
                    _stream_started(harness.identity),
                    _event(
                        harness.identity,
                        2,
                        "submission_reserved",
                        _submission_payload(order),
                    ),
                    _outcome(harness.identity, order, "order_filled"),
                ],
            )
        )

        await harness.connect()

        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderAccepted",
            "OrderFilled",
        ]
        assert harness.client.pending_client_order_ids == ()
        assert harness.client.execution_admitted is True
        assert len(harness.fake.submit_calls) == 1

        await harness.client._disconnect()
        harness.fake.pages.append(
            _page(
                harness.identity,
                after_cursor="0",
                events=[
                    _stream_started(harness.identity),
                    _event(
                        harness.identity,
                        2,
                        "submission_reserved",
                        _submission_payload(order),
                    ),
                    _outcome(harness.identity, order, "order_filled"),
                ],
            )
        )
        await harness.connect()
        assert [type(event).__name__ for event in harness.events] == [
            "OrderSubmitted",
            "OrderAccepted",
            "OrderFilled",
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("disabled_at", ["identity", "snapshot"])
def test_disabled_identity_or_snapshot_fails_connect_closed(disabled_at: str) -> None:
    async def scenario() -> None:
        identity = _identity(execution_enabled=disabled_at != "identity")
        snapshot = _snapshot(identity, execution_enabled=disabled_at != "snapshot")
        harness = _Harness(asyncio.get_running_loop(), identity=identity, snapshot=snapshot)
        with pytest.raises(Mt5V1ExecutionError):
            await harness.client._connect()
        assert harness.fake.closed is True
        assert harness.client.is_connected is False

    asyncio.run(scenario())


def test_all_report_queries_fail_explicitly_while_disconnected() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        commands = (
            harness.client.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=None,
                    client_order_id=ClientOrderId("REPORT-MISSING-1"),
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                )
            ),
            harness.client.generate_order_status_reports(_order_reports_command(harness)),
            harness.client.generate_fill_reports(_fill_reports_command(harness)),
            harness.client.generate_position_status_reports(_position_reports_command(harness)),
        )
        for command in commands:
            with pytest.raises(Mt5V1ExecutionError, match="identity is unavailable"):
                await command

    asyncio.run(scenario())


def test_terminal_journal_maps_native_filled_and_stable_rejected_reports() -> None:
    async def scenario() -> None:
        harness, filled_order, rejected_order = await _connected_report_harness(
            asyncio.get_running_loop()
        )

        order_reports = await harness.client.generate_order_status_reports(
            _order_reports_command(harness)
        )
        assert [report.order_status for report in order_reports] == [
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
        ]
        filled, rejected = order_reports
        assert filled.client_order_id == filled_order.client_order_id
        assert str(filled.venue_order_id) == "700000002"
        assert str(filled.venue_position_id) == "900000002"
        assert filled.order_side == OrderSide.BUY
        assert filled.order_type == OrderType.MARKET
        assert filled.time_in_force == TimeInForce.FOK
        assert str(filled.quantity) == "100"
        assert str(filled.filled_qty) == "100"
        assert filled.avg_px == Decimal("2401.25")
        assert filled.ts_accepted == 1_788_271_200_000_000_000
        assert filled.ts_last == filled.ts_accepted

        assert rejected.client_order_id == rejected_order.client_order_id
        assert rejected.order_side == OrderSide.SELL
        assert str(rejected.filled_qty) == "0"
        assert rejected.cancel_reason == "broker_rejected (retcode=10013)"
        material = "\0".join(
            (
                "py000-nautilus:mt5-v1:rejected-order",
                harness.identity.stream_id,
                harness.identity.account_id,
                harness.identity.symbol,
                harness.identity.magic,
                str(rejected_order.client_order_id),
            )
        ).encode()
        expected_rejected_id = f"PY000_REJ_{hashlib.sha256(material).hexdigest()}"
        assert str(rejected.venue_order_id) == expected_rejected_id
        assert not str(rejected.venue_order_id).isdigit()

        fills = await harness.client.generate_fill_reports(_fill_reports_command(harness))
        assert len(fills) == 1
        fill = fills[0]
        assert fill.client_order_id == filled_order.client_order_id
        assert str(fill.venue_order_id) == "700000002"
        assert str(fill.trade_id) == "800000002"
        assert str(fill.venue_position_id) == "900000002"
        assert fill.order_side == OrderSide.BUY
        assert str(fill.last_qty) == "100"
        assert str(fill.last_px) == "2401.25"
        assert fill.avg_px == Decimal("2401.25")
        assert str(fill.commission) == "1.25 USD"
        assert fill.ts_event == 1_788_271_200_000_000_000

        by_client = await harness.client.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=None,
                client_order_id=filled_order.client_order_id,
                venue_order_id=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        by_venue = await harness.client.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=INSTRUMENT_ID,
                client_order_id=None,
                venue_order_id=rejected.venue_order_id,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        assert by_client is not None
        assert by_client.client_order_id == filled_order.client_order_id
        assert by_venue is not None
        assert by_venue.client_order_id == rejected_order.client_order_id
        assert by_venue.venue_order_id == rejected.venue_order_id

        mismatch = await harness.client.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=None,
                client_order_id=filled_order.client_order_id,
                venue_order_id=rejected.venue_order_id,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        missing = await harness.client.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=None,
                client_order_id=ClientOrderId("REPORT-MISSING-1"),
                venue_order_id=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        assert mismatch is None
        assert missing is None

        with pytest.raises(ValueError, match="cannot both be None"):
            await harness.client.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=None,
                    client_order_id=None,
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                )
            )

        repeated_rejected = await harness.client.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=None,
                client_order_id=rejected_order.client_order_id,
                venue_order_id=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        assert repeated_rejected is not None
        assert repeated_rejected.venue_order_id == rejected.venue_order_id

    asyncio.run(scenario())


def test_live_engine_reconciles_rejected_order_once_without_fill() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        snapshot["positions"] = []
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
            capture_events=False,
        )
        order = harness.market(
            order_side=OrderSide.SELL,
            client_order_id=ClientOrderId("ENGINE-REJECTED-1"),
        )
        harness.fake.pages.append(
            _page(
                identity,
                after_cursor="0",
                events=[
                    _stream_started(identity),
                    _event(identity, 2, "submission_reserved", _submission_payload(order)),
                    _outcome(identity, order, "order_rejected", sequence=3),
                ],
            )
        )
        engine = _live_engine(harness, generate_missing_orders=False)
        _cache_submitted_order(harness, order)
        await harness.connect()

        assert await engine.reconcile_execution_state(timeout_secs=1.0)
        cached = harness.cache.order(order.client_order_id)
        assert cached is not None
        assert cached.status == OrderStatus.REJECTED
        assert str(cached.filled_qty) == "0"
        assert cached.trade_ids == []
        event_count = len(cached.events)
        order_count = len(harness.cache.orders())
        report = await harness.client.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=INSTRUMENT_ID,
                client_order_id=order.client_order_id,
                venue_order_id=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        assert report is not None
        assert harness.cache.client_order_id(report.venue_order_id) == order.client_order_id

        assert await engine.reconcile_execution_state(timeout_secs=1.0)
        assert len(harness.cache.orders()) == order_count
        assert len(harness.cache.order(order.client_order_id).events) == event_count
        repeated = await harness.client.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=INSTRUMENT_ID,
                client_order_id=order.client_order_id,
                venue_order_id=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        assert repeated is not None
        assert repeated.venue_order_id == report.venue_order_id

    asyncio.run(scenario())


def test_bulk_report_filters_are_applied_without_hiding_invalid_state() -> None:
    async def scenario() -> None:
        harness, _, _ = await _connected_report_harness(asyncio.get_running_loop())
        event_time = datetime.fromtimestamp(1_788_271_200, tz=UTC)
        after_event = event_time + timedelta(milliseconds=1)
        before_event = event_time - timedelta(milliseconds=1)
        other_instrument = InstrumentId.from_str("OTHER.MT5")

        assert (
            len(
                await harness.client.generate_order_status_reports(
                    _order_reports_command(harness, start=event_time, end=event_time)
                )
            )
            == 2
        )
        assert (
            await harness.client.generate_order_status_reports(
                _order_reports_command(harness, start=after_event)
            )
            == []
        )
        assert (
            await harness.client.generate_order_status_reports(
                _order_reports_command(harness, end=before_event)
            )
            == []
        )
        assert (
            await harness.client.generate_order_status_reports(
                _order_reports_command(harness, open_only=True)
            )
            == []
        )
        assert (
            await harness.client.generate_order_status_reports(
                _order_reports_command(harness, instrument_id=other_instrument)
            )
            == []
        )

        native_id = VenueOrderId("700000002")
        fills = await harness.client.generate_fill_reports(
            _fill_reports_command(
                harness,
                venue_order_id=native_id,
                start=event_time,
                end=event_time,
            )
        )
        assert [report.venue_order_id for report in fills] == [native_id]
        assert (
            await harness.client.generate_fill_reports(
                _fill_reports_command(harness, venue_order_id=VenueOrderId("700000999"))
            )
            == []
        )
        assert (
            await harness.client.generate_fill_reports(
                _fill_reports_command(harness, instrument_id=other_instrument)
            )
            == []
        )
        assert (
            await harness.client.generate_fill_reports(
                _fill_reports_command(harness, start=after_event)
            )
            == []
        )

        assert (
            await harness.client.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=other_instrument,
                    client_order_id=ClientOrderId("REPORT-FILLED-1"),
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                )
            )
            is None
        )

    asyncio.run(scenario())


def test_matching_magic_snapshot_maps_to_position_status_report() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        command = _position_reports_command(
            harness,
            start=datetime(2100, 1, 1, tzinfo=UTC),
            end=datetime(2100, 1, 2, tzinfo=UTC),
        )

        reports = await harness.client.generate_position_status_reports(command)

        assert len(reports) == 1
        report = reports[0]
        assert report.account_id == harness.client.account_id
        assert report.instrument_id == INSTRUMENT_ID
        assert report.position_side == PositionSide.LONG
        assert str(report.quantity) == "1"
        assert str(report.venue_position_id) == "800000001"
        assert report.avg_px_open == Decimal("2400.10")
        assert report.ts_last == 1_788_281_900_000_000_000

    asyncio.run(scenario())


def test_foreign_magic_position_blocks_reports_and_execution_admission() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        positions = cast(list[JsonObject], snapshot["positions"])
        foreign = deepcopy(positions[0])
        foreign.update(
            {
                "identifier": "800000099",
                "magic": "0",
                "side": "sell",
                "ticket": "700000099",
            }
        )
        positions.append(foreign)
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
        )
        await harness.connect()

        with pytest.raises(Mt5V1ExecutionError, match="foreign-magic"):
            await harness.client.generate_position_status_reports(
                _position_reports_command(harness, instrument_id=INSTRUMENT_ID)
            )
        assert harness.client.execution_admitted is False
        assert "foreign-magic" in cast(str, harness.client.execution_hold_reason)
        await harness.submit(harness.market())
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


def test_position_query_returns_empty_when_snapshot_and_cache_are_empty() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        snapshot["positions"] = []
        harness = _Harness(asyncio.get_running_loop(), identity=identity, snapshot=snapshot)
        await harness.connect()

        reports = await harness.client.generate_position_status_reports(
            _position_reports_command(harness, instrument_id=INSTRUMENT_ID)
        )

        assert reports == []

    asyncio.run(scenario())


def test_live_engine_mass_status_recovers_fills_and_closes_only_missing_position_id() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        template = deepcopy(cast(list[JsonObject], snapshot["positions"])[0])
        position_a = deepcopy(template)
        position_a.update(
            {
                "identifier": "900000101",
                "ticket": "700000101",
                "volume_lots": "1",
                "price_open": "2401.25",
            }
        )
        position_b = deepcopy(template)
        position_b.update(
            {
                "identifier": "900000102",
                "ticket": "700000102",
                "volume_lots": "1",
                "price_open": "2402.25",
            }
        )
        snapshot["positions"] = [position_a, position_b]
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
            capture_events=False,
        )
        order_a = harness.market(client_order_id=ClientOrderId("ENGINE-FILLED-A"))
        order_b = harness.market(client_order_id=ClientOrderId("ENGINE-FILLED-B"))
        fill_a = _outcome(identity, order_a, "order_filled", sequence=3)
        cast(JsonObject, fill_a["payload"]).update(
            {
                "venue_deal_id": "800000101",
                "venue_order_id": "700000101",
                "venue_position_id": "900000101",
            }
        )
        fill_b = _outcome(identity, order_b, "order_filled", sequence=5)
        cast(JsonObject, fill_b["payload"]).update(
            {
                "fill_price": "2402.25",
                "venue_deal_id": "800000102",
                "venue_order_id": "700000102",
                "venue_position_id": "900000102",
            }
        )
        harness.fake.pages.append(
            _page(
                identity,
                after_cursor="0",
                events=[
                    _stream_started(identity),
                    _event(
                        identity,
                        2,
                        "submission_reserved",
                        _submission_payload(order_a),
                    ),
                    fill_a,
                    _event(
                        identity,
                        4,
                        "submission_reserved",
                        _submission_payload(order_b),
                    ),
                    fill_b,
                ],
            )
        )
        engine = _live_engine(harness, generate_missing_orders=True)
        await harness.connect()

        assert await engine.reconcile_execution_state(timeout_secs=1.0)
        assert harness.client.reconciliation_active is False
        assert len(harness.fake.snapshot_calls) == 2
        assert harness.cache.order(order_a.client_order_id).status == OrderStatus.FILLED
        assert harness.cache.order(order_b.client_order_id).status == OrderStatus.FILLED
        cached_a = harness.cache.position(PositionId("900000101"))
        cached_b = harness.cache.position(PositionId("900000102"))
        assert cached_a is not None and cached_a.is_open
        assert cached_b is not None and cached_b.is_open
        original_orders = {
            order.client_order_id: (len(order.events), tuple(order.trade_ids))
            for order in harness.cache.orders()
        }

        assert await engine.reconcile_execution_state(timeout_secs=1.0)
        assert {
            order.client_order_id: (len(order.events), tuple(order.trade_ids))
            for order in harness.cache.orders()
        } == original_orders

        refreshed = deepcopy(snapshot)
        refreshed["positions"] = [deepcopy(position_b)]
        harness.fake.current_snapshot = refreshed
        await harness.client._refresh_snapshot_if_due(force=True)
        reports = await harness.client.generate_position_status_reports(
            _position_reports_command(harness)
        )
        reports_by_id = {str(report.venue_position_id): report for report in reports}
        assert set(reports_by_id) == {"900000101", "900000102"}
        assert reports_by_id["900000101"].position_side == PositionSide.FLAT
        assert str(reports_by_id["900000101"].quantity) == "0"
        assert reports_by_id["900000101"].ts_last == cached_a.ts_last
        assert reports_by_id["900000102"].position_side == PositionSide.LONG

        assert await engine.reconcile_execution_state(timeout_secs=1.0)
        assert harness.cache.position(PositionId("900000101")).is_closed
        assert harness.cache.position(PositionId("900000102")).is_open
        order_count = len(harness.cache.orders())
        event_counts = {
            order.client_order_id: len(order.events) for order in harness.cache.orders()
        }

        assert await engine.reconcile_execution_state(timeout_secs=1.0)
        assert len(harness.cache.orders()) == order_count
        assert {
            order.client_order_id: len(order.events) for order in harness.cache.orders()
        } == event_counts
        assert harness.cache.position(PositionId("900000101")).is_closed
        assert harness.cache.position(PositionId("900000102")).is_open

    asyncio.run(scenario())


def test_live_engine_mass_status_fails_on_unknown_and_clears_active_flag() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        snapshot["positions"] = []
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
            capture_events=False,
        )
        order = harness.market(client_order_id=ClientOrderId("ENGINE-UNKNOWN-1"))
        harness.fake.pages.append(
            _page(
                identity,
                after_cursor="0",
                events=[
                    _stream_started(identity),
                    _event(
                        identity,
                        2,
                        "submission_reserved",
                        _submission_payload(order),
                    ),
                    _outcome(identity, order, "order_unknown", sequence=3),
                ],
            )
        )
        engine = _live_engine(harness, generate_missing_orders=False)
        _cache_submitted_order(harness, order)
        event_count = len(order.events)
        await harness.connect()

        assert await engine.reconcile_execution_state(timeout_secs=1.0) is False
        assert harness.client.reconciliation_active is False
        cached = harness.cache.order(order.client_order_id)
        assert cached is not None
        assert cached.status == OrderStatus.SUBMITTED
        assert len(cached.events) == event_count
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_at", ["recovery", "foreign_magic"])
def test_live_engine_mass_status_fails_on_blocked_recovery_or_foreign_magic(
    blocked_at: str,
) -> None:
    async def scenario() -> None:
        identity = _identity()
        recovery: RecoveryState = "blocked" if blocked_at == "recovery" else "ready"
        snapshot = _snapshot(identity, recovery_state=recovery)
        if blocked_at == "foreign_magic":
            foreign = deepcopy(cast(list[JsonObject], snapshot["positions"])[0])
            foreign.update({"identifier": "800000099", "magic": "0"})
            cast(list[JsonObject], snapshot["positions"]).append(foreign)
        harness = _Harness(
            asyncio.get_running_loop(),
            identity=identity,
            snapshot=snapshot,
            recovery_state=recovery,
            capture_events=False,
        )
        engine = _live_engine(harness, generate_missing_orders=False)
        await harness.connect()

        assert await engine.reconcile_execution_state(timeout_secs=1.0) is False
        assert harness.client.reconciliation_active is False
        assert harness.client.execution_admitted is False
        assert harness.cache.orders() == []

    asyncio.run(scenario())


def test_mass_status_fails_closed_if_journal_moves_during_snapshot() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        order = harness.market(client_order_id=ClientOrderId("MASS-MOVING-TAIL-1"))
        harness.fake.pages.extend(
            [
                _page(harness.identity, after_cursor="1", events=[]),
                _page(
                    harness.identity,
                    after_cursor="1",
                    events=[
                        _event(
                            harness.identity,
                            2,
                            "submission_reserved",
                            _submission_payload(order),
                        )
                    ],
                ),
            ]
        )

        with pytest.raises(Mt5V1ExecutionError, match="changed while snapshot was captured"):
            await harness.client.generate_mass_status()
        assert harness.client.reconciliation_active is False
        assert harness.client.execution_admitted is False
        assert harness.fake.closed is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("swap_long", "-2.5"),
        ("swap_short", "1.5"),
        ("swap_mode", 2),
        ("swap_rates", ["0", "1", "1", "1", "3", "1", "0"]),
    ],
)
def test_runtime_swap_update_does_not_change_execution_units_or_admission(
    field: str,
    replacement: object,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        harness.client._set_connected(True)
        instrument = harness.client._report_instrument()
        refreshed = deepcopy(harness.snapshot)
        cast(JsonObject, refreshed["symbol_spec"])[field] = replacement
        harness.fake.current_snapshot = refreshed
        harness.client._next_snapshot_refresh_at = 0

        await harness.client._poll_once()

        assert harness.client._snapshot is refreshed
        assert harness.client._report_instrument() is instrument
        assert harness.client._snapshot_refresh_healthy
        assert harness.client.execution_admitted
        assert not harness.fake.closed
        assert not harness.fake.submit_calls
        await harness.client._disconnect()

    asyncio.run(scenario())


def test_runtime_snapshot_refresh_is_throttled_and_updates_position_reports() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        assert len(harness.fake.snapshot_calls) == 1

        await harness.client._poll_once()
        assert len(harness.fake.snapshot_calls) == 1

        refreshed = deepcopy(harness.snapshot)
        position = cast(list[JsonObject], refreshed["positions"])[0]
        position["identifier"] = "800000002"
        position["volume_lots"] = "0.02"
        harness.fake.current_snapshot = refreshed
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()

        reports = await harness.client.generate_position_status_reports(
            GeneratePositionStatusReports(
                instrument_id=None,
                start=None,
                end=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        assert len(harness.fake.snapshot_calls) == 2
        assert len(reports) == 1
        assert str(reports[0].venue_position_id) == "800000002"
        assert str(reports[0].quantity) == "2"

        await harness.client._poll_once()
        assert len(harness.fake.snapshot_calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_kind", ["unavailable", "timeout"])
def test_snapshot_failure_blocks_all_reports_until_complete_refresh(failure_kind: str) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        harness.client._set_connected(True)
        last_good = harness.client._snapshot
        account_events = len(harness.account_states)
        failure = (
            Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "positions changed during collection")
            if failure_kind == "unavailable"
            else Mt5V1RequestTimeout("snapshot timed out")
        )
        harness.fake.snapshot_results.append(failure)
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()

        assert harness.client._snapshot is last_good
        assert len(harness.account_states) == account_events
        admitted_after_failure = harness.client.execution_admitted
        assert admitted_after_failure is False
        assert not harness.fake.closed
        assert harness.client.is_connected
        commands = {
            "generate_order_status_report": GenerateOrderStatusReport(
                instrument_id=INSTRUMENT_ID,
                client_order_id=ClientOrderId("NOT-SENT"),
                venue_order_id=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            ),
            "generate_order_status_reports": _order_reports_command(harness),
            "generate_fill_reports": _fill_reports_command(harness),
            "generate_position_status_reports": _position_reports_command(harness),
        }
        for name, command in commands.items():
            with pytest.raises(Mt5V1ExecutionError, match="snapshot"):
                await getattr(harness.client, name)(command)

        # A successful journal poll cannot make an unrefreshed snapshot authoritative.
        snapshot_calls = len(harness.fake.snapshot_calls)
        harness.client._next_snapshot_refresh_at = harness.client._loop.time() + 60
        await harness.client._poll_once()
        assert len(harness.fake.snapshot_calls) == snapshot_calls
        admitted_after_journal = harness.client.execution_admitted
        failure_after_journal = harness.client.last_failure
        assert admitted_after_journal is False
        assert failure_after_journal is not None

        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        admitted_after_refresh = harness.client.execution_admitted
        assert admitted_after_refresh
        reports = await harness.client.generate_position_status_reports(
            _position_reports_command(harness)
        )
        assert len(reports) == 1
        assert reports[0].position_side != PositionSide.FLAT
        assert harness.client.last_failure is None
        assert not harness.fake.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_kind", ["unavailable", "timeout"])
def test_mass_snapshot_failure_is_unreportable_without_disconnect(failure_kind: str) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        harness.client._set_connected(True)
        last_good = harness.client._snapshot
        failure = (
            Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "positions could not be read")
            if failure_kind == "unavailable"
            else Mt5V1RequestTimeout("snapshot timed out")
        )
        harness.fake.snapshot_results.append(failure)
        with pytest.raises(type(failure)):
            await harness.client.generate_mass_status()
        assert not harness.fake.closed
        assert harness.client.is_connected
        assert not harness.client.reconciliation_active
        assert harness.client._snapshot is last_good
        assert not harness.client.execution_admitted

        report = await harness.client.generate_mass_status()
        assert report is not None
        assert not harness.client.reconciliation_active
        assert harness.client.execution_admitted

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout_phase", ["before_snapshot", "after_snapshot"])
def test_mass_journal_timeout_blocks_old_reports_until_complete_refresh(
    timeout_phase: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        harness.client._set_connected(True)
        last_good = harness.client._snapshot
        account_events = len(harness.account_states)
        if timeout_phase == "after_snapshot":
            harness.fake.pages.append(_page(harness.identity, after_cursor="1", events=[]))
        harness.fake.pages.append(Mt5V1RequestTimeout("journal query timed out"))

        with pytest.raises(Mt5V1RequestTimeout):
            await harness.client.generate_mass_status()
        assert harness.client._snapshot is last_good
        assert len(harness.account_states) == account_events
        assert harness.client.is_connected
        assert not harness.fake.closed
        with pytest.raises(Mt5V1ExecutionError, match="snapshot"):
            await harness.client.generate_position_status_reports(
                _position_reports_command(harness)
            )

        snapshot_calls = len(harness.fake.snapshot_calls)
        harness.client._next_snapshot_refresh_at = harness.client._loop.time() + 60
        await harness.client._poll_once()
        assert len(harness.fake.snapshot_calls) == snapshot_calls
        with pytest.raises(Mt5V1ExecutionError, match="snapshot"):
            await harness.client.generate_position_status_reports(
                _position_reports_command(harness)
            )

        assert await harness.client.generate_mass_status() is not None
        assert harness.client.execution_admitted
        assert harness.client.last_failure is None

    asyncio.run(scenario())


def test_snapshot_recovery_does_not_clear_unknown_submission() -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(), outcome=Mt5V1RequestTimeout("timeout after send")
        )
        await harness.connect()
        order = harness.market()
        await harness.submit(order)
        harness.fake.snapshot_results.append(
            Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "positions unavailable")
        )
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        healthy_after_failure = harness.client._snapshot_refresh_healthy
        assert not healthy_after_failure

        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        assert harness.client._snapshot_refresh_healthy
        assert not harness.client.execution_admitted
        assert "UNKNOWN" in cast(str, harness.client.execution_hold_reason)
        assert harness.client.pending_client_order_ids == (str(order.client_order_id),)
        assert len(harness.fake.submit_calls) == 1
        with pytest.raises(Mt5V1ExecutionError, match="pending"):
            await harness.client.generate_position_status_reports(
                _position_reports_command(harness)
            )

    asyncio.run(scenario())


def test_startup_unavailable_then_connect_recovers_without_partial_account() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        harness.fake.snapshot_results.append(
            Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "startup positions unavailable")
        )
        with pytest.raises(Mt5V1RemoteError, match="SNAPSHOT_UNAVAILABLE"):
            await harness.connect()
        assert harness.account_states == []
        failed_snapshot = harness.client._snapshot
        failed_identity = harness.client._identity
        failed_admission = harness.client.execution_admitted
        closed_after_failure = harness.fake.closed
        assert failed_snapshot is None
        assert failed_identity is None
        assert not failed_admission
        assert closed_after_failure

        await harness.connect()
        assert harness.client._identity == harness.identity
        assert len(harness.account_states) == 1
        assert harness.client.execution_admitted
        assert not harness.fake.closed

    asyncio.run(scenario())


def test_close_snapshot_unavailable_denies_without_consuming_request_id() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        harness.client._set_connected(True)
        known_ids = harness.client._seen_request_ids.copy()
        harness.fake.snapshot_results.append(
            Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "target enumeration changed")
        )
        order = harness.market("1", order_side=OrderSide.SELL, reduce_only=True)
        await harness.submit(order, position_id=PositionId("800000001"))
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.close_calls == []
        assert harness.fake.submit_calls == []
        assert harness.client.pending_client_order_ids == ()
        assert harness.client._seen_request_ids == known_ids
        assert harness.client.is_connected
        assert not harness.client.execution_admitted
        assert not harness.fake.closed

    asyncio.run(scenario())


def test_runtime_foreign_position_holds_then_clean_snapshot_restores_admission() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        clean = deepcopy(harness.snapshot)
        foreign = deepcopy(harness.snapshot)
        positions = cast(list[JsonObject], foreign["positions"])
        foreign_position = deepcopy(positions[0])
        foreign_position.update({"identifier": "800000099", "magic": "0"})
        positions.append(foreign_position)

        harness.fake.current_snapshot = foreign
        harness.client._next_snapshot_refresh_at = 0
        await harness.submit(harness.market())
        assert harness.client.execution_admitted is False
        assert "foreign-magic" in cast(str, harness.client.execution_hold_reason)
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

        harness.fake.snapshot_results.append(
            Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "positions unavailable")
        )
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        assert harness.client._snapshot_refresh_healthy
        assert not harness.client.execution_admitted
        assert "foreign-magic" in cast(str, harness.client.execution_hold_reason)
        with pytest.raises(Mt5V1ExecutionError, match="foreign-magic"):
            await harness.client.generate_position_status_reports(
                _position_reports_command(harness)
            )

        harness.fake.current_snapshot = clean
        harness.client._next_snapshot_refresh_at = 0
        await harness.client._poll_once()
        assert harness.client.execution_admitted is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "anomaly",
    ["identity", "recovery", "spec", "step", "tick", "limit", "account"],
)
def test_runtime_snapshot_boundary_anomaly_fails_closed(anomaly: str) -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        invalid = deepcopy(harness.snapshot)
        if anomaly == "identity":
            identity = harness.identity.to_wire()
            identity["boot_id"] = "boot-replacement-001"
            invalid["identity"] = identity
        elif anomaly == "recovery":
            invalid["recovery_state"] = "blocked"
        elif anomaly == "spec":
            cast(JsonObject, invalid["symbol_spec"])["contract_size"] = "200"
        elif anomaly == "step":
            cast(JsonObject, invalid["symbol_spec"])["volume_step"] = "0.1"
        elif anomaly == "tick":
            cast(JsonObject, invalid["symbol_spec"])["tick_size"] = "0.1"
        elif anomaly == "limit":
            cast(JsonObject, invalid["execution_limits"])["max_order_lots"] = "0.02"
        else:
            cast(JsonObject, invalid["account"])["margin_free"] = "9991.24"
        harness.fake.current_snapshot = invalid
        harness.client._next_snapshot_refresh_at = 0

        with pytest.raises(Mt5V1ExecutionError):
            await harness.client._poll_once()

        assert harness.fake.closed is True
        assert harness.client.execution_admitted is False
        assert harness.client.execution_hold_reason == "MT5 execution is not connected"
        if anomaly == "account":
            assert len(harness.account_states) == 1

    asyncio.run(scenario())


def test_due_refresh_failure_denies_order_without_transport_submit() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        invalid = deepcopy(harness.snapshot)
        invalid["recovery_state"] = "blocked"
        harness.fake.current_snapshot = invalid
        harness.client._next_snapshot_refresh_at = 0

        await harness.submit(harness.market())

        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []
        assert harness.fake.closed is True
        assert harness.client.execution_admitted is False

    asyncio.run(scenario())


def test_forced_snapshot_timeout_denies_without_mutation_or_disconnect() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        harness.client._set_connected(True)
        harness.fake.snapshot_results.append(
            Mt5V1RequestTimeout("synthetic forced snapshot timeout")
        )
        harness.client._next_snapshot_refresh_at = 0

        await harness.submit(harness.market())

        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.client.pending_client_order_ids == ()
        assert harness.client.execution_admitted is False
        assert harness.client.execution_hold_reason is not None
        assert harness.fake.submit_calls == []
        assert harness.fake.close_calls == []
        assert harness.client.is_connected is True
        assert harness.fake.closed is False

    asyncio.run(scenario())


def test_snapshot_refresh_interval_is_bounded() -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="snapshot_refresh_interval_ms"):
            _Harness(
                asyncio.get_running_loop(),
                snapshot_refresh_interval_ms=999,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation_timeout_ms", [999, 60_001])
def test_mutation_timeout_must_cover_queries_and_remain_bounded(
    mutation_timeout_ms: int,
) -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="mutation_timeout_ms"):
            _Harness(
                asyncio.get_running_loop(),
                request_timeout_ms=1_000,
                mutation_timeout_ms=mutation_timeout_ms,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_pagination_max_pages", True),
        ("event_pagination_max_pages", 0),
        ("event_pagination_max_pages", 10_001),
        ("event_pagination_timeout_ms", True),
        ("event_pagination_timeout_ms", 49),
        ("event_pagination_timeout_ms", 60_001),
    ],
)
def test_event_pagination_budgets_are_exact_and_bounded(field: str, value: object) -> None:
    async def scenario() -> None:
        arguments: dict[str, object] = {field: value}
        with pytest.raises(ValueError, match=field):
            _Harness(asyncio.get_running_loop(), **arguments)  # type: ignore[arg-type]

    asyncio.run(scenario())


def test_factory_requires_typed_config_and_builds_client() -> None:
    async def scenario() -> None:
        identity = _identity()
        client = Mt5V1LiveExecClientFactory.create(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(identity),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        assert isinstance(client, Mt5V1ExecutionClient)
        assert client.id == ClientId("MT5")
        assert client.venue == Venue("MT5")
        with pytest.raises(TypeError):
            Mt5V1LiveExecClientFactory.create(
                loop=asyncio.get_running_loop(),
                name="MT5",
                config=LiveExecClientConfig(),
                msgbus=TestComponentStubs.msgbus(),
                cache=TestComponentStubs.cache(),
                clock=TestComponentStubs.clock(),
            )

    asyncio.run(scenario())
