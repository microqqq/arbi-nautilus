from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
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
from nautilus_trader.model.enums import OrderSide, PositionSide, TimeInForce
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.identifiers import (
    ClientId,
    ClientOrderId,
    ExecAlgorithmId,
    InstrumentId,
    Venue,
)
from nautilus_trader.model.orders import Order
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from py000_nautilus.mt5_v1_data import instrument_from_snapshot
from py000_nautilus.mt5_v1_execution import (
    Mt5V1ExecClientConfig,
    Mt5V1ExecutionClient,
    Mt5V1ExecutionError,
    Mt5V1LiveExecClientFactory,
    quantity_to_lots,
)
from py000_nautilus.mt5_v1_protocol import Binding, Identity, JsonObject, RecoveryState
from py000_nautilus.mt5_v1_transport import Mt5V1RemoteError, Mt5V1TransportError

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


def _config(identity: Identity) -> Mt5V1ExecClientConfig:
    return Mt5V1ExecClientConfig(
        pub_url="tcp://127.0.0.1:6001",
        rep_url="tcp://127.0.0.1:6002",
        instrument_id=INSTRUMENT_ID,
        expected_account_id=identity.account_id,
        expected_symbol=identity.symbol,
        expected_magic=identity.magic,
        expected_ea_build_id=identity.ea_build_id,
        expected_source_sha256=identity.declared_source_sha256,
        expected_stream_id=identity.stream_id,
        expected_server_timezone=identity.server_timezone,
        event_poll_interval_ms=50,
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


def _submission_payload(order: Order, quantity_lots: str = "1") -> JsonObject:
    return {
        "client_request_id": str(order.client_order_id),
        "quantity_lots": quantity_lots,
        "side": "buy" if order.side == OrderSide.BUY else "sell",
    }


def _outcome(
    identity: Identity,
    order: Order,
    event_type: str,
    *,
    sequence: int = 3,
) -> JsonObject:
    payload = _submission_payload(order)
    if event_type in {"order_rejected", "order_unknown"}:
        payload.update({"broker_retcode": "10013", "reason": "broker_rejected"})
    elif event_type == "order_filled":
        payload.update(
            {
                "broker_retcode": "10009",
                "commission": "-1.25",
                "fill_price": "2401.25",
                "filled_quantity_lots": "1",
                "venue_deal_id": "800000002",
                "venue_order_id": "700000002",
                "venue_position_id": "900000002",
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
        self.event_calls: list[tuple[str, int]] = []
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
    ) -> None:
        self.identity = identity or _identity()
        self.snapshot = snapshot or _snapshot(self.identity, recovery_state=recovery_state)
        self.clock = TestComponentStubs.clock()
        self.msgbus = TestComponentStubs.msgbus()
        self.cache = TestComponentStubs.cache()
        self.events: list[OrderEvent] = []
        self.msgbus.register("ExecEngine.process", self.events.append)
        self.instrument = instrument_from_snapshot(self.snapshot, INSTRUMENT_ID, ts_init=0)
        provider = InstrumentProvider()
        provider.add(self.instrument)
        self.fake = _FakeTransport(
            self.identity,
            self.snapshot,
            outcome=outcome,
            event_sink=self.events,
            recovery_state=recovery_state,
        )
        self.client = Mt5V1ExecutionClient(
            loop=loop,
            name="MT5",
            config=_config(self.identity),
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
        time_in_force: TimeInForce = TimeInForce.FOK,
        client_order_id: ClientOrderId | None = None,
        reduce_only: bool = False,
        quote_quantity: bool = False,
        exec_algorithm_id: ExecAlgorithmId | None = None,
    ) -> Order:
        return TestComponentStubs.order_factory().market(
            instrument_id=INSTRUMENT_ID,
            order_side=OrderSide.BUY,
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

    async def submit(self, order: Order) -> None:
        await self.client._submit_order(
            SubmitOrder(
                trader_id=order.trader_id,
                strategy_id=order.strategy_id,
                order=order,
                command_id=UUID4(),
                ts_init=self.clock.timestamp_ns(),
            )
        )


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
        assert str(fill.commission) == "-1.25 USD"
        assert harness.client._cursor == "3"
        assert harness.fake.pages == []
        assert harness.client.pending_client_order_ids == ()

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

        assert harness.client.execution_admitted is True
        assert harness.client._cursor == "3"
        assert harness.fake.event_calls == [("0", 100), ("2", 100), ("3", 100)]
        assert harness.events == []
        await harness.submit(old_order)
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("unresolved", ["dangling", "unknown"])
def test_historical_unresolved_request_keeps_execution_on_hold(unresolved: str) -> None:
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
        harness.fake.pages.append(_page(harness.identity, after_cursor="0", events=events))

        await harness.connect()

        assert harness.client.execution_admitted is False
        assert unresolved.upper() in cast(str, harness.client.execution_hold_reason).upper()
        assert harness.events == []
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


def test_timeout_stays_pending_and_poll_can_recover_real_fill() -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(),
            outcome=Mt5V1TransportError("timeout after send"),
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


def test_blocked_recovery_is_readable_but_never_execution_admitted() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop(), recovery_state="blocked")

        await harness.connect()

        assert harness.client.execution_admitted is False
        assert "recovery state is blocked" in cast(
            str,
            harness.client.execution_hold_reason,
        )
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


def test_matching_magic_snapshot_maps_to_position_status_report() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        command = GeneratePositionStatusReports(
            instrument_id=None,
            start=None,
            end=None,
            command_id=UUID4(),
            ts_init=harness.clock.timestamp_ns(),
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

        unsupported = (
            harness.client.generate_order_status_report(cast(GenerateOrderStatusReport, None)),
            harness.client.generate_order_status_reports(cast(GenerateOrderStatusReports, None)),
            harness.client.generate_fill_reports(cast(GenerateFillReports, None)),
        )
        for call in unsupported:
            with pytest.raises(NotImplementedError):
                await call

    asyncio.run(scenario())


def test_foreign_magic_position_is_not_reported_and_blocks_execution_admission() -> None:
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

        reports = await harness.client.generate_position_status_reports(
            GeneratePositionStatusReports(
                instrument_id=INSTRUMENT_ID,
                start=None,
                end=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )

        assert [str(report.venue_position_id) for report in reports] == ["800000001"]
        assert harness.client.execution_admitted is False
        assert "foreign-magic" in cast(str, harness.client.execution_hold_reason)
        await harness.submit(harness.market())
        assert [type(event).__name__ for event in harness.events] == ["OrderDenied"]
        assert harness.fake.submit_calls == []

    asyncio.run(scenario())


def test_explicit_instrument_query_reports_flat_when_no_managed_position_exists() -> None:
    async def scenario() -> None:
        identity = _identity()
        snapshot = _snapshot(identity)
        snapshot["positions"] = []
        harness = _Harness(asyncio.get_running_loop(), identity=identity, snapshot=snapshot)
        await harness.connect()

        reports = await harness.client.generate_position_status_reports(
            GeneratePositionStatusReports(
                instrument_id=INSTRUMENT_ID,
                start=None,
                end=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )

        assert len(reports) == 1
        assert reports[0].position_side == PositionSide.FLAT
        assert str(reports[0].quantity) == "0"
        assert reports[0].venue_position_id is None

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
