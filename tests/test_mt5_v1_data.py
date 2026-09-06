from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest
import zmq
import zmq.asyncio
from nautilus_trader.common.component import TestClock
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.data.messages import (
    SubscribeInstrument,
    SubscribeInstrumentStatus,
    SubscribeQuoteTicks,
    UnsubscribeInstrument,
)
from nautilus_trader.model.data import InstrumentStatus, QuoteTick
from nautilus_trader.model.enums import MarketStatusAction
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Venue
from nautilus_trader.model.instruments import Cfd
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from py000_nautilus.mt5_v1_data import (
    Mt5V1DataClient,
    Mt5V1DataClientConfig,
    Mt5V1DataError,
    Mt5V1LiveDataClientFactory,
    _snapshot_instrument_signature,
    instrument_from_snapshot,
    quote_from_pub,
    server_wall_ms_to_utc_ns,
    status_from_snapshot,
)
from py000_nautilus.mt5_v1_protocol import (
    CAPABILITIES,
    PROTOCOL,
    VERSION,
    Binding,
    Identity,
    JsonObject,
    RecoveryState,
    decode_json_object,
    encode_json,
)
from py000_nautilus.mt5_v1_transport import (
    Mt5V1RemoteError,
    Mt5V1RequestTimeout,
    Mt5V1Transport,
    Mt5V1TransportError,
)

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "mt5_ea_v1_readonly.json").read_text())
INSTRUMENT_ID = InstrumentId.from_str("XAUUSD.MT5")


def _identity(**changes: object) -> Identity:
    value = {**FIXTURE["identity"], **changes}
    return Identity.from_wire(value)


def _snapshot(identity: Identity | None = None) -> JsonObject:
    current = identity or _identity()
    return cast(
        JsonObject,
        {
            **deepcopy(FIXTURE["snapshot"]),
            "execution_enabled": current.execution_enabled,
            "identity": current.to_wire(),
            "recovery_state": "ready",
        },
    )


def _pub(index: int, identity: Identity | None = None) -> JsonObject:
    return cast(
        JsonObject,
        {
            **deepcopy(FIXTURE["pub"][index]),
            "identity": (identity or _identity()).to_wire(),
            "protocol": PROTOCOL,
            "version": VERSION,
        },
    )


def _config(**changes: object) -> Mt5V1DataClientConfig:
    values: dict[str, object] = {
        "pub_url": "tcp://127.0.0.1:6101",
        "rep_url": "tcp://127.0.0.1:6102",
        "instrument_id": INSTRUMENT_ID,
        "expected_account_id": _identity().account_id,
        "expected_symbol": "XAUUSD",
        "expected_magic": "900000001",
        "expected_ea_build_id": "py000-mt5-ea-v1-readonly",
        "expected_source_sha256": "a" * 64,
        "expected_execution_enabled": False,
        "expected_server_timezone": "Europe/Athens",
        "snapshot_interval_ms": 60_000,
        "max_snapshot_age_ms": 60_000,
        "max_tick_age_ms": 60_000,
    }
    values.update(changes)
    return Mt5V1DataClientConfig(**values)  # type: ignore[arg-type]


def test_snapshot_builds_canonical_ounce_instrument() -> None:
    instrument = instrument_from_snapshot(_snapshot(), INSTRUMENT_ID, ts_init=7)

    assert isinstance(instrument, Cfd)
    assert str(instrument.size_increment) == "1"
    assert str(instrument.lot_size) == "100"
    assert str(instrument.min_quantity) == "1"
    assert str(instrument.max_quantity) == "10000"
    assert instrument.info["canonical_quantity"] == "ounce"
    assert instrument.info["mt5_contract_size_ounces"] == "100"
    assert instrument.info["point"] == "0.01"
    assert instrument.info["swap_long"] == "-1.25"
    assert instrument.info["swap_short"] == "0.5"
    assert instrument.info["swap_mode"] == 1
    assert instrument.info["swap_rates"] == ("0", "1", "1", "3", "1", "1", "0")
    assert isinstance(instrument.info["swap_rates"], tuple)
    assert instrument.info["server_timezone"] == "Europe/Athens"
    assert instrument.info["margin_and_fees_authoritative"] is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("point", "0.001"),
        ("contract_size", "10"),
        ("volume_step", "0.1"),
        ("tick_size", "0.1"),
    ],
)
def test_instrument_signature_covers_structure(field: str, replacement: object) -> None:
    original = _snapshot()
    changed = _snapshot()
    cast(JsonObject, changed["symbol_spec"])[field] = replacement

    assert _snapshot_instrument_signature(changed) != _snapshot_instrument_signature(original)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("swap_long", "-2.5"),
        ("swap_short", "1.5"),
        ("swap_mode", 2),
        ("swap_rates", ["0", "1", "1", "1", "3", "1", "0"]),
    ],
)
def test_instrument_signature_excludes_dynamic_swap(field: str, replacement: object) -> None:
    original = _snapshot()
    changed = _snapshot()
    cast(JsonObject, changed["symbol_spec"])[field] = replacement

    assert _snapshot_instrument_signature(changed) == _snapshot_instrument_signature(original)


def test_instrument_signature_covers_server_timezone() -> None:
    original = _snapshot()
    changed = _snapshot()
    changed_identity = cast(JsonObject, changed["identity"])
    changed_identity["server_timezone"] = "UTC"

    assert _snapshot_instrument_signature(changed) != _snapshot_instrument_signature(original)


def test_pub_tick_uses_athens_wall_clock_and_zero_unknown_sizes() -> None:
    snapshot = _snapshot()
    instrument = instrument_from_snapshot(snapshot, INSTRUMENT_ID, ts_init=7)
    quote = quote_from_pub(
        _pub(0),
        instrument,
        reference_utc_ms=1_788_271_200_000,
        now_utc_ms=1_788_271_200_000,
        timezone_name="Europe/Athens",
        ts_init=9,
        max_tick_age_ms=15_000,
    )

    assert isinstance(quote, QuoteTick)
    assert quote.ts_event == 1_788_271_200_000_000_000
    assert str(quote.bid_price) == "2400.10"
    assert str(quote.ask_price) == "2400.20"
    assert str(quote.bid_size) == "0"
    assert str(quote.ask_size) == "0"
    assert (
        quote_from_pub(
            _pub(1),
            instrument,
            reference_utc_ms=1_788_271_200_000,
            now_utc_ms=1_788_271_200_000,
            timezone_name="Europe/Athens",
            ts_init=10,
            max_tick_age_ms=15_000,
        )
        is None
    )


def test_pub_tick_uses_asymmetric_stale_and_future_boundaries() -> None:
    snapshot = _snapshot()
    instrument = instrument_from_snapshot(snapshot, INSTRUMENT_ID, ts_init=7)
    reference_utc_ms = 1_788_271_200_000

    for delta_ms in (-15_000, 1_000):
        assert (
            quote_from_pub(
                _tick_at_utc_ms(reference_utc_ms + delta_ms),
                instrument,
                reference_utc_ms=reference_utc_ms,
                now_utc_ms=reference_utc_ms,
                timezone_name="Europe/Athens",
                ts_init=9,
                max_tick_age_ms=15_000,
            )
            is not None
        )

    with pytest.raises(Mt5V1DataError, match="stale"):
        quote_from_pub(
            _tick_at_utc_ms(reference_utc_ms - 15_001),
            instrument,
            reference_utc_ms=reference_utc_ms,
            now_utc_ms=reference_utc_ms,
            timezone_name="Europe/Athens",
            ts_init=9,
            max_tick_age_ms=15_000,
        )
    with pytest.raises(Mt5V1DataError, match="future-dated"):
        quote_from_pub(
            _tick_at_utc_ms(reference_utc_ms + 1_001),
            instrument,
            reference_utc_ms=reference_utc_ms,
            now_utc_ms=reference_utc_ms,
            timezone_name="Europe/Athens",
            ts_init=9,
            max_tick_age_ms=15_000,
        )


def test_pub_tick_freshness_uses_live_clock_not_last_snapshot_time() -> None:
    snapshot = _snapshot()
    instrument = instrument_from_snapshot(snapshot, INSTRUMENT_ID, ts_init=7)
    snapshot_utc_ms = 1_788_271_200_000
    now_utc_ms = snapshot_utc_ms + 10_000

    quote = quote_from_pub(
        _tick_at_utc_ms(now_utc_ms),
        instrument,
        reference_utc_ms=snapshot_utc_ms,
        now_utc_ms=now_utc_ms,
        timezone_name="Europe/Athens",
        ts_init=9,
        max_tick_age_ms=15_000,
    )

    assert quote is not None
    assert quote.ts_event == now_utc_ms * 1_000_000


def test_session_status_uses_rep_fact_not_pub_hint() -> None:
    snapshot = _snapshot()
    session = cast(JsonObject, snapshot["session"])
    session["session_open"] = False
    session["scheduled_open"] = False
    status = status_from_snapshot(snapshot, INSTRUMENT_ID, ts_init=11)

    assert status.action == MarketStatusAction.CLOSE
    assert status.is_trading is False
    assert status.ts_event == 1_788_271_200_000_000_000
    assert _pub(0)["market_open_hint"] is True


def test_session_status_rejects_inconsistent_or_disabled_symbol_mode() -> None:
    snapshot = _snapshot()
    spec = cast(JsonObject, snapshot["symbol_spec"])
    flags = cast(JsonObject, snapshot["authority_flags"])
    spec["trade_mode"] = 0

    with pytest.raises(Mt5V1DataError, match="inconsistent symbol trade modes"):
        status_from_snapshot(snapshot, INSTRUMENT_ID, ts_init=11)

    flags["symbol_trade_mode"] = 0
    status = status_from_snapshot(snapshot, INSTRUMENT_ID, ts_init=11)
    assert status.action == MarketStatusAction.NOT_AVAILABLE_FOR_TRADING
    assert status.is_trading is False
    assert status.reason == "mt5_rep_symbol_trade_disabled"


def test_server_wall_dst_fold_uses_nearest_authoritative_utc_fact() -> None:
    wall_ms = int(datetime(2026, 10, 25, 3, 30, tzinfo=UTC).timestamp() * 1_000)
    first = int(datetime(2026, 10, 25, 0, 30, tzinfo=UTC).timestamp() * 1_000)
    second = int(datetime(2026, 10, 25, 1, 30, tzinfo=UTC).timestamp() * 1_000)

    assert (
        server_wall_ms_to_utc_ns(
            wall_ms,
            timezone_name="Europe/Athens",
            reference_utc_ms=first + 1_000,
        )
        == first * 1_000_000
    )
    assert (
        server_wall_ms_to_utc_ns(
            wall_ms,
            timezone_name="Europe/Athens",
            reference_utc_ms=second + 1_000,
        )
        == second * 1_000_000
    )
    with pytest.raises(Mt5V1DataError, match="DST-ambiguous"):
        server_wall_ms_to_utc_ns(
            wall_ms,
            timezone_name="Europe/Athens",
            reference_utc_ms=(first + second) // 2,
        )


def test_server_wall_rejects_dst_gap_and_unknown_timezone() -> None:
    gap_wall_ms = int(datetime(2026, 3, 29, 3, 30, tzinfo=UTC).timestamp() * 1_000)
    reference_utc_ms = int(datetime(2026, 3, 29, 1, 30, tzinfo=UTC).timestamp() * 1_000)

    with pytest.raises(Mt5V1DataError, match="DST gap"):
        server_wall_ms_to_utc_ns(
            gap_wall_ms,
            timezone_name="Europe/Athens",
            reference_utc_ms=reference_utc_ms,
        )
    with pytest.raises(Mt5V1DataError, match="unknown MT5 server timezone"):
        server_wall_ms_to_utc_ns(
            gap_wall_ms,
            timezone_name="Mars/Olympus",
            reference_utc_ms=reference_utc_ms,
        )


def test_transport_resets_req_after_timeout_and_receives_two_frame_pub() -> None:
    async def scenario() -> None:
        context = zmq.asyncio.Context()
        rep = context.socket(zmq.REP)
        pub = context.socket(zmq.PUB)
        rep.setsockopt(zmq.LINGER, 0)
        pub.setsockopt(zmq.LINGER, 0)
        rep_url = f"tcp://127.0.0.1:{rep.bind_to_random_port('tcp://127.0.0.1')}"
        pub_url = f"tcp://127.0.0.1:{pub.bind_to_random_port('tcp://127.0.0.1')}"
        transport = Mt5V1Transport(
            pub_url=pub_url,
            rep_url=rep_url,
            topic="XAUUSD",
            request_timeout_ms=50,
        )

        async def serve_two_hellos() -> None:
            first = decode_json_object(await rep.recv())
            await asyncio.sleep(0.08)
            await rep.send(_hello_response(first).encode())
            second = decode_json_object(await rep.recv())
            await rep.send(_hello_response(second).encode())

        server = asyncio.create_task(serve_two_hellos())
        try:
            await transport.open()
            with pytest.raises(Mt5V1TransportError, match="socket was reset"):
                await transport.hello()
            await asyncio.sleep(0.05)
            identity, recovery = await transport.hello()
            assert identity == _identity()
            assert recovery == "ready"

            await asyncio.sleep(0.08)
            await pub.send_multipart([b"XAUUSD", encode_json(_pub(1)).encode()])
            topic, message = await asyncio.wait_for(transport.recv_pub(), timeout=1)
            assert topic == b"XAUUSD"
            assert message["message_type"] == "heartbeat"
            await server
        finally:
            await transport.close()
            rep.close(linger=0)
            pub.close(linger=0)
            context.destroy(linger=0)

    asyncio.run(scenario())


def test_transport_resets_req_after_cancellation_post_send() -> None:
    async def scenario() -> None:
        context = zmq.asyncio.Context()
        rep = context.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep_url = f"tcp://127.0.0.1:{rep.bind_to_random_port('tcp://127.0.0.1')}"
        transport = Mt5V1Transport(
            pub_url="tcp://127.0.0.1:65530",
            rep_url=rep_url,
            topic="XAUUSD",
            request_timeout_ms=1_000,
        )
        first_received = asyncio.Event()
        release_first_reply = asyncio.Event()

        async def serve() -> None:
            first = decode_json_object(await rep.recv())
            first_received.set()
            await release_first_reply.wait()
            await rep.send(_hello_response(first).encode())
            second = decode_json_object(await rep.recv())
            await rep.send(_hello_response(second).encode())

        server = asyncio.create_task(serve())
        try:
            await transport.open()
            cancelled = asyncio.create_task(transport.hello())
            await asyncio.wait_for(first_received.wait(), timeout=1)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            release_first_reply.set()

            identity, recovery = await transport.hello()
            assert identity == _identity()
            assert recovery == "ready"
            await server
        finally:
            await transport.close()
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)
            rep.close(linger=0)
            context.destroy(linger=0)

    asyncio.run(scenario())


def test_transport_open_failure_rolls_back_and_allows_reopen() -> None:
    async def scenario() -> None:
        transport = Mt5V1Transport(
            pub_url="tcp://127.0.0.1:65530",
            rep_url="tcp://",
            topic="XAUUSD",
            request_timeout_ms=1_000,
        )

        with pytest.raises(Mt5V1TransportError, match="transport open failed"):
            await transport.open()
        assert transport._context is None
        assert transport._req is None
        assert transport._sub is None

        transport._rep_url = "tcp://127.0.0.1:65531"
        await transport.open()
        await transport.close()

    asyncio.run(scenario())


class _FakeTransport:
    def __init__(self, identity: Identity, snapshot: JsonObject) -> None:
        self.identity = identity
        self.current_snapshot = snapshot
        self.snapshot_results: list[JsonObject | BaseException] = []
        self.snapshot_calls = 0
        self.queue: asyncio.Queue[tuple[bytes, JsonObject]] = asyncio.Queue()
        self.opened = False
        self.closed = False

    @property
    def topic(self) -> bytes:
        return b"XAUUSD"

    async def open(self) -> None:
        self.opened = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def hello(self) -> tuple[Identity, RecoveryState]:
        return self.identity, "ready"

    async def snapshot(self, binding: Binding) -> JsonObject:
        if binding != self.identity.binding():
            raise AssertionError("unexpected binding")
        self.snapshot_calls += 1
        if self.snapshot_results:
            result = self.snapshot_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return self.current_snapshot

    async def recv_pub(self) -> tuple[bytes, JsonObject]:
        return await self.queue.get()


class _FailingPubTransport(_FakeTransport):
    async def recv_pub(self) -> tuple[bytes, JsonObject]:
        raise Mt5V1TransportError("immediate PUB failure")


class _RehandshakeTransport(_FakeTransport):
    def __init__(
        self,
        identity: Identity,
        snapshot: JsonObject,
        replacement_identity: Identity,
        replacement_snapshot: JsonObject,
    ) -> None:
        super().__init__(identity, snapshot)
        self.replacement_identity = replacement_identity
        self.replacement_snapshot = replacement_snapshot
        self.rehandshake = False
        self.replacement_snapshot_requested = asyncio.Event()
        self.release_replacement_snapshot = asyncio.Event()

    async def hello(self) -> tuple[Identity, RecoveryState]:
        if self.rehandshake:
            return self.replacement_identity, "ready"
        return await super().hello()

    async def snapshot(self, binding: Binding) -> JsonObject:
        if not self.rehandshake:
            return await super().snapshot(binding)
        if binding == self.identity.binding():
            raise Mt5V1RemoteError("BINDING_MISMATCH", "EA restarted")
        if binding != self.replacement_identity.binding():
            raise AssertionError("unexpected replacement binding")
        self.replacement_snapshot_requested.set()
        await self.release_replacement_snapshot.wait()
        return self.replacement_snapshot


def test_client_feeds_nautilus_data_engine_without_execution_surface() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        msgbus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock)
        identity = _identity()
        snapshot = _fresh_snapshot(identity, clock.timestamp_ns() // 1_000_000)
        fake = _FakeTransport(identity, snapshot)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )
        quote_command = SubscribeQuoteTicks(
            instrument_id=INSTRUMENT_ID,
            client_id=ClientId("MT5"),
            venue=Venue("MT5"),
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        status_command = SubscribeInstrumentStatus(
            instrument_id=INSTRUMENT_ID,
            client_id=ClientId("MT5"),
            venue=Venue("MT5"),
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        try:
            client.connect()
            await _wait_until(lambda: client.is_connected)
            await client._subscribe_quote_ticks(quote_command)
            await client._subscribe_instrument_status(status_command)
            assert cache.instrument(INSTRUMENT_ID) is not None
            status = cache.instrument_status(INSTRUMENT_ID)
            assert isinstance(status, InstrumentStatus)
            assert status.action == MarketStatusAction.TRADING

            await fake.queue.put((b"XAUUSD", _fresh_pub(1, identity, snapshot)))
            await asyncio.sleep(0)
            assert cache.quote_tick(INSTRUMENT_ID) is None

            await fake.queue.put((b"XAUUSD.extra", _fresh_pub(0, identity, snapshot)))
            await asyncio.sleep(0)
            assert cache.quote_tick(INSTRUMENT_ID) is None

            await fake.queue.put((b"XAUUSD", _fresh_pub(0, identity, snapshot)))
            await _wait_until(lambda: cache.quote_tick(INSTRUMENT_ID) is not None)
            quote = cache.quote_tick(INSTRUMENT_ID)
            assert isinstance(quote, QuoteTick)
            assert str(quote.bid_size) == "0"
            assert str(quote.ask_size) == "0"
        finally:
            client.disconnect()
            await _wait_until(lambda: fake.closed and not client.is_connected)
            engine.dispose()
        assert fake.opened is True
        assert fake.closed is True

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
def test_snapshot_swap_updates_flow_through_native_instrument_subscription(
    field: str,
    replacement: object,
) -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        msgbus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock)
        identity = _identity()
        observed_ms = clock.timestamp_ns() // 1_000_000 - 1_000
        initial = _fresh_snapshot(identity, observed_ms)
        fake = _FakeTransport(identity, initial)
        provider = InstrumentProvider()
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            transport=fake,
        )
        engine.register_client(client)
        updates: list[Cfd] = []

        def observe(instrument: Cfd) -> None:
            # Native DataEngine has already replaced cache, and the adapter commit
            # is visible before synchronous downstream strategy callbacks run.
            assert cache.instrument(INSTRUMENT_ID) is instrument
            assert provider.find(INSTRUMENT_ID) is instrument
            assert client.snapshot_refresh_healthy
            assert client._snapshot is not None
            spec = cast(JsonObject, client._snapshot["symbol_spec"])
            assert instrument.info["swap_long"] == spec["swap_long"]
            updates.append(instrument)

        msgbus.subscribe("data.instrument.*", observe)
        try:
            client.connect()
            await _wait_until(lambda: client.is_connected)
            assert len(updates) == 1
            original = updates[0]
            engine.execute(
                SubscribeInstrument(
                    instrument_id=INSTRUMENT_ID,
                    client_id=ClientId("MT5"),
                    venue=Venue("MT5"),
                    command_id=UUID4(),
                    ts_init=clock.timestamp_ns(),
                )
            )
            await _wait_until(lambda: len(updates) == 2)
            assert updates[-1] is original

            formatted = deepcopy(initial)
            formatted_spec = cast(JsonObject, formatted["symbol_spec"])
            formatted_spec["swap_long"] = "-1.2500"
            formatted_spec["swap_short"] = "0.500"
            formatted_spec["swap_rates"] = ["0.0", "1.0", "1.0", "3.0", "1.0", "1.0", "0.0"]
            fake.current_snapshot = formatted
            await client._refresh_snapshot(allow_rehandshake=False)
            assert len(updates) == 2
            assert cache.instrument(INSTRUMENT_ID) is original

            changed = _fresh_snapshot(identity, observed_ms + 1)
            cast(JsonObject, changed["symbol_spec"])[field] = replacement
            fake.current_snapshot = changed
            await client._refresh_snapshot(allow_rehandshake=False)
            assert len(updates) == 3
            current = updates[-1]
            assert current is not original
            wanted = tuple(replacement) if isinstance(replacement, list) else replacement
            assert current.info[field] == wanted
            assert current.ts_event == (observed_ms + 1) * 1_000_000
            assert client.is_connected
            assert client.last_failure is None

            # Same observation is idempotent; a new observation of the same values
            # updates MT5 freshness without fabricating another feed or funding tick.
            await client._refresh_snapshot(allow_rehandshake=False)
            assert len(updates) == 3
            later = deepcopy(changed)
            cast(JsonObject, later["time"])["observed_utc_ms"] = str(observed_ms + 2)
            fake.current_snapshot = later
            client._mark_snapshot_unavailable(Mt5V1RequestTimeout("temporary outage"))
            await client._refresh_snapshot(allow_rehandshake=False)
            assert len(updates) == 4
            assert updates[-1].info == current.info
            assert updates[-1].ts_event > current.ts_event

            client._mark_snapshot_unavailable(Mt5V1RequestTimeout("temporary outage"))
            # A native re-subscription during an outage must not republish stale cost.
            engine.execute(
                UnsubscribeInstrument(
                    instrument_id=INSTRUMENT_ID,
                    client_id=ClientId("MT5"),
                    venue=Venue("MT5"),
                    command_id=UUID4(),
                    ts_init=clock.timestamp_ns(),
                )
            )
            await asyncio.sleep(0)
            await client._subscribe_instrument(
                SubscribeInstrument(
                    instrument_id=INSTRUMENT_ID,
                    client_id=ClientId("MT5"),
                    venue=Venue("MT5"),
                    command_id=UUID4(),
                    ts_init=clock.timestamp_ns(),
                )
            )
            assert len(updates) == 4
        finally:
            client.disconnect()
            await _wait_until(lambda: fake.closed and not client.is_connected)
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "anomaly",
    ["backward", "same_time_conflict", "future", "stale", "structure", "authority"],
)
def test_invalid_snapshot_never_replaces_last_good_instrument(anomaly: str) -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        msgbus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock)
        identity = _identity()
        now_ms = clock.timestamp_ns() // 1_000_000
        initial = _fresh_snapshot(identity, now_ms - 1_000)
        fake = _FakeTransport(identity, initial)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(snapshot_interval_ms=250),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )
        try:
            await client._connect()
            client._set_connected(True)
            client._running = True
            await client._subscribe_instrument_status(
                SubscribeInstrumentStatus(
                    instrument_id=INSTRUMENT_ID,
                    client_id=ClientId("MT5"),
                    venue=Venue("MT5"),
                    command_id=UUID4(),
                    ts_init=clock.timestamp_ns(),
                )
            )
            original = cache.instrument(INSTRUMENT_ID)
            bad_time = {
                "backward": now_ms - 1_001,
                "same_time_conflict": now_ms - 1_000,
                "future": now_ms + 2_000,
                "stale": now_ms - 60_001,
                "structure": now_ms,
                "authority": now_ms,
            }[anomaly]
            bad = _fresh_snapshot(identity, bad_time)
            spec = cast(JsonObject, bad["symbol_spec"])
            spec["swap_long"] = "-99"
            if anomaly == "structure":
                spec["contract_size"] = "10"
            elif anomaly == "authority":
                cast(JsonObject, bad["authority_flags"])["symbol_trade_mode"] = 0
            fake.current_snapshot = bad

            await asyncio.wait_for(client._run_snapshots(), timeout=1)

            assert cache.instrument(INSTRUMENT_ID) is original
            assert client._snapshot is initial
            assert client.committed_snapshot_count == 1
            assert not client.snapshot_refresh_healthy
            assert not client.is_connected
            assert fake.closed
            status = cache.instrument_status(INSTRUMENT_ID)
            assert status is not None and status.is_trading is False
        finally:
            await client._disconnect()
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(("ahead_ms", "accepted"), [(266, True), (1000, True), (1001, False)])
def test_snapshot_cross_host_clock_boundary(ahead_ms: int, accepted: bool) -> None:
    async def scenario() -> None:
        clock = TestClock()
        clock.set_time(1_780_000_000_000_000_000)
        msgbus, cache = TestComponentStubs.msgbus(), TestComponentStubs.cache()
        engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock)
        identity = _identity()
        now_ms = clock.timestamp_ns() // 1_000_000
        fake = _FakeTransport(identity, _fresh_snapshot(identity, now_ms))
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(), name="MT5", config=_config(),
            msgbus=msgbus, cache=cache, clock=clock,
            instrument_provider=InstrumentProvider(), transport=fake,
        )
        try:
            await client._connect()
            original = cache.instrument(INSTRUMENT_ID)
            fake.current_snapshot = _fresh_snapshot(identity, now_ms + ahead_ms)
            if accepted:
                await client._refresh_snapshot(allow_rehandshake=False)
                assert client.committed_snapshot_count == 2
                assert cache.instrument(INSTRUMENT_ID).ts_event == (now_ms + ahead_ms) * 1_000_000
            else:
                with pytest.raises(Mt5V1DataError, match="future-dated"):
                    await client._refresh_snapshot(allow_rehandshake=False)
                assert cache.instrument(INSTRUMENT_ID) is original
                assert client.committed_snapshot_count == 1
        finally:
            await client._disconnect()
            engine.dispose()

    asyncio.run(scenario())


def test_public_connect_immediate_reader_failure_stays_disconnected() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        identity = _identity()
        snapshot = _fresh_snapshot(identity, clock.timestamp_ns() // 1_000_000)
        fake = _FailingPubTransport(identity, snapshot)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )

        client.connect()
        await _wait_until(lambda: fake.closed)
        assert client.is_connected is False
        assert client._running is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (Mt5V1RequestTimeout("synthetic periodic timeout"), "mt5_snapshot_request_timeout"),
        (
            Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "synthetic incomplete positions"),
            "mt5_snapshot_unavailable",
        ),
    ],
)
def test_periodic_snapshot_failure_marks_unavailable_then_recovers(
    failure: Mt5V1RequestTimeout | Mt5V1RemoteError,
    reason: str,
) -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        msgbus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock)
        identity = _identity()
        snapshot = _fresh_snapshot(identity, clock.timestamp_ns() // 1_000_000)
        fake = _FakeTransport(identity, snapshot)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(snapshot_interval_ms=250),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )
        status_command = SubscribeInstrumentStatus(
            instrument_id=INSTRUMENT_ID,
            client_id=ClientId("MT5"),
            venue=Venue("MT5"),
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        quote_command = SubscribeQuoteTicks(
            instrument_id=INSTRUMENT_ID,
            client_id=ClientId("MT5"),
            venue=Venue("MT5"),
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        try:
            await client._connect()
            client._finish_connect()
            await client._subscribe_instrument_status(status_command)
            await client._subscribe_quote_ticks(quote_command)
            initially_healthy = client.snapshot_refresh_healthy
            assert initially_healthy is True
            fake.snapshot_results.extend(
                [
                    failure,
                    snapshot,
                ]
            )

            await _wait_until(
                lambda: fake.snapshot_calls >= 2 and not client.snapshot_refresh_healthy,
                attempts=100,
            )

            status = cache.instrument_status(INSTRUMENT_ID)
            assert isinstance(status, InstrumentStatus)
            assert status.action == MarketStatusAction.NOT_AVAILABLE_FOR_TRADING
            assert status.reason == reason
            assert client._snapshot is snapshot
            assert client.committed_snapshot_count == 1
            # A subscription arriving during the outage must not revive old TRADING.
            await client._subscribe_instrument_status(status_command)
            status = cache.instrument_status(INSTRUMENT_ID)
            assert isinstance(status, InstrumentStatus)
            assert status.action == MarketStatusAction.NOT_AVAILABLE_FOR_TRADING
            assert client.is_connected is True
            assert client._running is True
            assert fake.opened is True
            assert fake.closed is False
            timeout_failure = client.last_failure
            assert timeout_failure is not None
            pub_task = client._pub_task
            assert pub_task is not None and not pub_task.done()
            await fake.queue.put((b"XAUUSD", _fresh_pub(0, identity, snapshot)))
            await _wait_until(lambda: cache.quote_tick(INSTRUMENT_ID) is not None)

            await _wait_until(
                lambda: fake.snapshot_calls >= 3 and client.snapshot_refresh_healthy,
                attempts=100,
            )
            status = cache.instrument_status(INSTRUMENT_ID)
            assert isinstance(status, InstrumentStatus)
            assert status.action == MarketStatusAction.TRADING
            assert client.committed_snapshot_count == 2
            recovery_failure = client.last_failure
            assert recovery_failure is None
            assert fake.closed is False
        finally:
            await client._disconnect()
            client._set_connected(False)
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        Mt5V1RequestTimeout("synthetic startup timeout"),
        Mt5V1RemoteError("SNAPSHOT_UNAVAILABLE", "synthetic startup unavailable"),
    ],
)
def test_startup_snapshot_failure_then_same_ea_connect_recovers(
    failure: Mt5V1RequestTimeout | Mt5V1RemoteError,
) -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        identity = _identity()
        snapshot = _fresh_snapshot(identity, clock.timestamp_ns() // 1_000_000)
        fake = _FakeTransport(identity, snapshot)
        fake.snapshot_results.append(failure)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )

        with pytest.raises(type(failure), match="synthetic startup"):
            await client._connect()

        startup_healthy = client.snapshot_refresh_healthy
        assert startup_healthy is False
        assert fake.opened is True
        assert fake.closed is True
        failed_identity = client._identity
        failed_snapshot = client._snapshot
        failed_instrument = client._instrument
        failed_commit_count = client.committed_snapshot_count
        assert failed_identity is None
        assert failed_snapshot is None
        assert failed_instrument is None
        assert failed_commit_count == 0

        await client._connect()
        assert client._identity == identity
        assert client._snapshot is snapshot
        assert client.committed_snapshot_count == 1
        assert client.snapshot_refresh_healthy
        await client._disconnect()

    asyncio.run(scenario())


def test_periodic_non_timeout_transport_error_still_fails_closed() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        identity = _identity()
        snapshot = _fresh_snapshot(identity, clock.timestamp_ns() // 1_000_000)
        fake = _FakeTransport(identity, snapshot)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(snapshot_interval_ms=250),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )
        await client._connect()
        client._set_connected(True)
        client._running = True
        fake.snapshot_results.append(Mt5V1TransportError("synthetic hard failure"))

        await client._run_snapshots()

        assert client.snapshot_refresh_healthy is False
        assert client.is_connected is False
        assert client._running is False
        assert fake.closed is True

    asyncio.run(scenario())


def test_client_rejects_identity_drift_before_publishing() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        identity = _identity(declared_source_sha256="b" * 64)
        snapshot = _fresh_snapshot(identity, clock.timestamp_ns() // 1_000_000)
        fake = _FakeTransport(identity, snapshot)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )
        with pytest.raises(Mt5V1DataError, match="declared_source_sha256"):
            await client._connect()
        assert fake.closed is True

    asyncio.run(scenario())


def test_client_binds_the_expected_execution_mode() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        identity = _identity(execution_enabled=True)
        snapshot = _fresh_snapshot(identity, clock.timestamp_ns() // 1_000_000)
        fake = _FakeTransport(identity, snapshot)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )

        with pytest.raises(Mt5V1DataError, match="execution_enabled"):
            await client._connect()

        enabled = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(expected_execution_enabled=True),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=_FakeTransport(identity, snapshot),
        )
        try:
            await enabled._connect()
            assert enabled.observed_identity == identity
        finally:
            await enabled._disconnect()

    asyncio.run(scenario())


def test_client_rejects_snapshot_full_identity_drift() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        identity = _identity()
        snapshot_identity = _identity(magic="900000002")
        snapshot = _fresh_snapshot(snapshot_identity, clock.timestamp_ns() // 1_000_000)
        fake = _FakeTransport(identity, snapshot)
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )

        with pytest.raises(Mt5V1DataError, match="snapshot identity"):
            await client._connect()
        assert fake.closed is True

    asyncio.run(scenario())


def test_rehandshake_installs_new_identity_and_snapshot_atomically() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        msgbus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock)
        identity = _identity()
        replacement = _identity(boot_id="boot-synthetic-002")
        now_ms = clock.timestamp_ns() // 1_000_000
        snapshot = _fresh_snapshot(identity, now_ms)
        replacement_snapshot = _fresh_snapshot(replacement, now_ms)
        fake = _RehandshakeTransport(
            identity,
            snapshot,
            replacement,
            replacement_snapshot,
        )
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )
        quote_command = SubscribeQuoteTicks(
            instrument_id=INSTRUMENT_ID,
            client_id=ClientId("MT5"),
            venue=Venue("MT5"),
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        try:
            await client._connect()
            await client._subscribe_quote_ticks(quote_command)
            client._publish_pub(b"XAUUSD", _fresh_pub(0, identity, snapshot))
            old_quote = cache.quote_tick(INSTRUMENT_ID)
            assert old_quote is not None
            assert client.committed_snapshot_count == 1
            assert client.identity_matched_pub_count == 1
            fake.rehandshake = True
            refresh = asyncio.create_task(client._refresh_snapshot(allow_rehandshake=True))
            await asyncio.wait_for(fake.replacement_snapshot_requested.wait(), timeout=1)

            client._publish_pub(
                b"XAUUSD",
                _fresh_pub(0, replacement, replacement_snapshot),
            )
            assert cache.quote_tick(INSTRUMENT_ID) is old_quote
            assert client._identity == identity

            fake.release_replacement_snapshot.set()
            await refresh
            assert client.committed_snapshot_count == 1
            assert client.identity_matched_pub_count == 0
            assert client.identity_matched_pub_age_ms is None
            assert client.observed_identity == replacement
            client._publish_pub(
                b"XAUUSD",
                _fresh_pub(0, replacement, replacement_snapshot),
            )
            assert cache.quote_tick(INSTRUMENT_ID) is not None
            assert client._identity == replacement
            assert client.identity_matched_pub_count == 1
        finally:
            await client._disconnect()
            engine.dispose()

    asyncio.run(scenario())


def test_rehandshake_rejects_journal_stream_change_without_partial_install() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        identity = _identity()
        replacement = _identity(
            stream_id="stream-synthetic-002",
            boot_id="boot-synthetic-002",
        )
        now_ms = clock.timestamp_ns() // 1_000_000
        snapshot = _fresh_snapshot(identity, now_ms)
        fake = _RehandshakeTransport(
            identity,
            snapshot,
            replacement,
            _fresh_snapshot(replacement, now_ms),
        )
        client = Mt5V1DataClient(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )
        try:
            await client._connect()
            fake.rehandshake = True
            with pytest.raises(Mt5V1DataError, match="journal stream changed"):
                await client._refresh_snapshot(allow_rehandshake=True)
            assert client._identity == identity
            assert client._snapshot is snapshot
        finally:
            await client._disconnect()

    asyncio.run(scenario())


def test_factory_creates_the_read_only_live_data_client() -> None:
    async def scenario() -> None:
        client = Mt5V1LiveDataClientFactory.create(
            loop=asyncio.get_running_loop(),
            name="MT5",
            config=_config(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )

        assert isinstance(client, Mt5V1DataClient)
        assert client.id == ClientId("MT5")
        assert client.venue == Venue("MT5")

    asyncio.run(scenario())


def test_data_client_does_not_smuggle_execution_policy() -> None:
    source = (ROOT / "src" / "py000_nautilus" / "mt5_v1_data.py").read_text()
    assert "LiveExecutionClient" not in source
    assert "LiveExecClientFactory" not in source
    assert "SubmitMarketDeltaRequest" not in source
    assert "submit_market_delta" not in source


def _hello_response(request: JsonObject) -> str:
    return encode_json(
        {
            "protocol": PROTOCOL,
            "version": VERSION,
            "request_id": request["request_id"],
            "op": "hello",
            "ok": True,
            "data": {
                "capabilities": list(CAPABILITIES),
                "identity": _identity().to_wire(),
                "recovery_state": "ready",
            },
        }
    )


def _fresh_snapshot(identity: Identity, observed_utc_ms: int) -> JsonObject:
    snapshot = _snapshot(identity)
    timezone = ZoneInfo(identity.server_timezone)
    observed = datetime.fromtimestamp(observed_utc_ms / 1_000, tz=UTC)
    local = observed.astimezone(timezone).replace(tzinfo=UTC)
    server_wall_ms = int(local.timestamp() * 1_000)
    time_data = cast(JsonObject, snapshot["time"])
    time_data["observed_utc_ms"] = str(observed_utc_ms)
    time_data["server_quote_time_ms"] = str(server_wall_ms)
    session = cast(JsonObject, snapshot["session"])
    session["sample_server_time_ms"] = str(server_wall_ms)
    return snapshot


def _fresh_pub(index: int, identity: Identity, snapshot: JsonObject) -> JsonObject:
    message = _pub(index, identity)
    snapshot_time = cast(JsonObject, snapshot["time"])
    if index == 0:
        message["event_time_ms"] = snapshot_time["server_quote_time_ms"]
    else:
        message["event_time_ms"] = snapshot_time["observed_utc_ms"]
        message["last_tick_time_ms"] = snapshot_time["server_quote_time_ms"]
    return message


def _tick_at_utc_ms(utc_ms: int) -> JsonObject:
    identity = _identity()
    timezone = ZoneInfo(identity.server_timezone)
    instant = datetime.fromtimestamp(utc_ms / 1_000, tz=UTC)
    server_wall = instant.astimezone(timezone).replace(tzinfo=UTC)
    message = _pub(0, identity)
    message["event_time_ms"] = str(int(server_wall.timestamp() * 1_000))
    return message


async def _wait_until(predicate: Callable[[], bool], attempts: int = 20) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")
