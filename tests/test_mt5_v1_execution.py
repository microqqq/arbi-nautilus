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
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Venue
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


def _snapshot(identity: Identity, *, execution_enabled: bool | None = None) -> JsonObject:
    value = deepcopy(cast(JsonObject, FIXTURE["snapshot"]))
    value["identity"] = identity.to_wire()
    value["execution_enabled"] = (
        identity.execution_enabled if execution_enabled is None else execution_enabled
    )
    value["recovery_state"] = "ready"
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
    sequence: int = 2,
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
    ) -> None:
        self.identity = identity
        self.current_snapshot = snapshot
        self.outcome = outcome
        self.event_sink = event_sink
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
        return self.identity, "ready"

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
        return _page(self.identity, after_cursor=after_cursor, events=[])


class _Harness:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        identity: Identity | None = None,
        snapshot: JsonObject | None = None,
        outcome: JsonObject | BaseException | None = None,
    ) -> None:
        self.identity = identity or _identity()
        self.snapshot = snapshot or _snapshot(self.identity)
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
    ) -> Order:
        return TestComponentStubs.order_factory().market(
            instrument_id=INSTRUMENT_ID,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(Decimal(quantity)),
            time_in_force=time_in_force,
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
                        _event(
                            harness.identity,
                            1,
                            "stream_started",
                            {"ea_build_id": "old", "execution_enabled": True},
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
        assert harness.fake.event_calls == [("0", 100), ("1", 100)]

    asyncio.run(scenario())


def test_market_with_non_fok_semantics_is_denied_without_transport_send() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        await harness.submit(harness.market(time_in_force=TimeInForce.IOC))
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
        assert harness.client._cursor == "2"
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
        assert harness.client._cursor == "2"

    asyncio.run(scenario())


def test_idempotent_old_outcome_does_not_move_event_cursor_backwards() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        order = harness.market()
        harness.fake.outcome = _outcome(harness.identity, order, "order_rejected")
        await harness.connect()
        harness.client._cursor = "9"

        await harness.submit(order)

        assert harness.client._cursor == "9"
        assert len(harness.fake.submit_calls) == 1

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
                after_cursor="0",
                events=[
                    _event(
                        harness.identity,
                        1,
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
        assert harness.client._cursor == "2"
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

        with pytest.raises(Mt5V1ExecutionError, match="journal stream changed"):
            await harness.connect()

        assert harness.fake.closed is True

    asyncio.run(scenario())


def test_reconnect_rejects_an_unresolved_submission() -> None:
    async def scenario() -> None:
        harness = _Harness(
            asyncio.get_running_loop(),
            outcome=Mt5V1TransportError("timeout after send"),
        )
        await harness.connect()
        await harness.submit(harness.market())
        await harness.client._disconnect()

        with pytest.raises(Mt5V1ExecutionError, match="unresolved submission"):
            await harness.connect()

        assert harness.fake.closed is True

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


def test_reconciliation_reports_are_explicitly_unsupported() -> None:
    async def scenario() -> None:
        harness = _Harness(asyncio.get_running_loop())
        await harness.connect()
        calls = (
            harness.client.generate_order_status_report(
                cast(GenerateOrderStatusReport, None)
            ),
            harness.client.generate_order_status_reports(
                cast(GenerateOrderStatusReports, None)
            ),
            harness.client.generate_fill_reports(cast(GenerateFillReports, None)),
            harness.client.generate_position_status_reports(
                cast(GeneratePositionStatusReports, None)
            ),
        )
        for call in calls:
            with pytest.raises(NotImplementedError):
                await call

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
