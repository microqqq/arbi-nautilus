from __future__ import annotations

import asyncio
from typing import Any, cast, override

import pytest

from py000_nautilus.mt5_v1_protocol import (
    ClosePositionRequest,
    ExecutionEventsRequest,
    Identity,
    JsonObject,
    Request,
    SubmitMarketDeltaRequest,
    decode_json_object,
    encode_json,
    normalized_account_id,
)
from py000_nautilus.mt5_v1_transport import (
    Mt5V1RemoteError,
    Mt5V1RequestTimeout,
    Mt5V1Transport,
)


def identity() -> Identity:
    server = "Synthetic Broker"
    login = "10000001"
    return Identity(
        account_id=normalized_account_id(server, login),
        broker_server=server,
        broker_login=login,
        server_timezone="Europe/Athens",
        server_timezone_source="configured_input",
        symbol="XAUUSD",
        terminal_build=5000,
        ea_build_id="py000-mt5-ea-v1-execution",
        declared_source_sha256="a" * 64,
        stream_id="stream-synthetic-001",
        boot_id="boot-synthetic-001",
        magic="900000001",
        execution_enabled=True,
    )


class RecordingTransport(Mt5V1Transport):
    def __init__(self) -> None:
        super().__init__(
            pub_url="inproc://unused-pub",
            rep_url="inproc://unused-rep",
            topic="py000.mt5.v1",
            request_timeout_ms=10,
        )
        self.requests: list[Request] = []

    @override
    async def _round_trip(self, request: Request) -> JsonObject:
        self.requests.append(request)
        return {
            "data": {"captured": True},
            "ok": True,
            "op": request.op,
            "protocol": "py000.mt5",
            "request_id": request.request_id,
            "version": 1,
        }


class TimeoutReq:
    def __init__(self) -> None:
        self.closed = False
        self.send_count = 0

    async def send(self, _payload: bytes) -> None:
        self.send_count += 1

    async def recv_multipart(self) -> list[bytes]:
        await asyncio.get_running_loop().create_future()
        raise AssertionError("unreachable")

    def close(self, *, linger: int) -> None:
        assert linger == 0
        self.closed = True


class _LaneProbe:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.ops: list[str] = []

    def enter(self, op: str) -> None:
        assert self.active == 0, "two REQ round trips entered the single REP lane"
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.ops.append(op)

    def leave(self) -> None:
        assert self.active == 1
        self.active -= 1


class _ControlledReq:
    def __init__(self, probe: _LaneProbe, release: asyncio.Event) -> None:
        self._probe = probe
        self._release = release
        self._reply: bytes | None = None
        self.sent = asyncio.Event()
        self.send_count = 0
        self.closed = False

    async def send(self, payload: bytes) -> None:
        request = decode_json_object(payload)
        self.send_count += 1
        self._probe.enter(cast(str, request["op"]))
        self._reply = encode_json(
            {
                "error": {"code": "SCHEMA_MISMATCH", "message": "synthetic reply"},
                "ok": False,
                "op": request["op"],
                "protocol": "py000.mt5",
                "request_id": request["request_id"],
                "version": 1,
            }
        ).encode()
        self.sent.set()

    async def recv_multipart(self) -> list[bytes]:
        try:
            await self._release.wait()
            assert self._reply is not None
            return [self._reply]
        finally:
            self._probe.leave()

    def close(self, *, linger: int) -> None:
        assert linger == 0
        self.closed = True


def test_public_execution_methods_construct_full_requests() -> None:
    async def scenario() -> None:
        current = identity()
        transport = RecordingTransport()

        submit = await transport.submit_market_delta(
            current.binding(),
            client_request_id="delta-1",
            side="sell",
            quantity_lots="0.05",
        )
        close = await transport.close_position(
            current.binding(),
            client_request_id="close-1",
            side="buy",
            quantity_lots="0.02",
            position_ticket="700000001",
            position_identifier="800000001",
        )
        events = await transport.execution_events(
            current.binding(),
            after_cursor="7",
            limit=25,
        )

        assert submit == {"captured": True}
        assert close == {"captured": True}
        assert events == {"captured": True}
        assert len(transport.requests) == 3
        submit_request = transport.requests[0]
        assert isinstance(submit_request, SubmitMarketDeltaRequest)
        assert submit_request.binding == current.binding()
        assert submit_request.client_request_id == "delta-1"
        assert submit_request.side == "sell"
        assert submit_request.quantity_lots == "0.05"
        close_request = transport.requests[1]
        assert isinstance(close_request, ClosePositionRequest)
        assert close_request.binding == current.binding()
        assert close_request.client_request_id == "close-1"
        assert close_request.side == "buy"
        assert close_request.quantity_lots == "0.02"
        assert close_request.position_ticket == "700000001"
        assert close_request.position_identifier == "800000001"
        event_request = transport.requests[2]
        assert isinstance(event_request, ExecutionEventsRequest)
        assert event_request.binding == current.binding()
        assert event_request.after_cursor == "7"
        assert event_request.limit == 25

    asyncio.run(scenario())


def test_shared_rep_lane_serializes_mutation_without_charging_query_queue_time() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        rep_url = "tcp://127.0.0.1:62002"
        mutation = Mt5V1Transport(
            loop=loop,
            pub_url="inproc://unused-mutation-pub",
            rep_url=rep_url,
            topic="py000.mt5.v1",
            request_timeout_ms=20,
            mutation_timeout_ms=1_000,
        )
        query = Mt5V1Transport(
            loop=loop,
            pub_url="inproc://unused-query-pub",
            rep_url=rep_url,
            topic="py000.mt5.v1",
            request_timeout_ms=20,
            mutation_timeout_ms=1_000,
        )
        probe = _LaneProbe()
        release_mutation = asyncio.Event()
        release_query = asyncio.Event()
        release_query.set()
        mutation_req = _ControlledReq(probe, release_mutation)
        query_req = _ControlledReq(probe, release_query)
        mutation._req = cast(Any, mutation_req)
        query._req = cast(Any, query_req)

        assert mutation.rep_coordinator is query.rep_coordinator

        mutation_task = asyncio.create_task(
            mutation.submit_market_delta(
                identity().binding(),
                client_request_id="delta-lane-1",
                side="buy",
                quantity_lots="0.01",
            )
        )
        await asyncio.wait_for(mutation_req.sent.wait(), timeout=1)
        query_task = asyncio.create_task(query.hello())

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(query_task), timeout=0.075)
        assert query_task.done() is False
        assert query_req.send_count == 0
        assert probe.ops == ["submit_market_delta"]

        release_mutation.set()
        with pytest.raises(Mt5V1RemoteError, match="SCHEMA_MISMATCH"):
            await asyncio.wait_for(mutation_task, timeout=1)
        with pytest.raises(Mt5V1RemoteError, match="SCHEMA_MISMATCH"):
            await asyncio.wait_for(query_task, timeout=1)

        assert mutation_req.send_count == 1
        assert query_req.send_count == 1
        assert probe.ops == ["submit_market_delta", "hello"]
        assert probe.max_active == 1
        assert probe.active == 0

    asyncio.run(scenario())


def test_submit_timeout_drops_socket_without_retry() -> None:
    async def scenario() -> None:
        current = identity()
        transport = Mt5V1Transport(
            loop=asyncio.get_running_loop(),
            pub_url="inproc://unused-pub",
            rep_url="inproc://unused-rep",
            topic="py000.mt5.v1",
            request_timeout_ms=1_000,
            mutation_timeout_ms=1,
        )
        req = TimeoutReq()
        transport._req = cast(Any, req)

        with pytest.raises(Mt5V1RequestTimeout, match="socket was reset"):
            await asyncio.wait_for(
                transport.submit_market_delta(
                    current.binding(),
                    client_request_id="delta-timeout",
                    side="buy",
                    quantity_lots="0.01",
                ),
                timeout=0.1,
            )

        assert req.send_count == 1
        assert req.closed is True
        assert transport._req is None

    asyncio.run(scenario())


def test_close_position_timeout_drops_socket_without_retry() -> None:
    async def scenario() -> None:
        current = identity()
        transport = Mt5V1Transport(
            loop=asyncio.get_running_loop(),
            pub_url="inproc://unused-pub",
            rep_url="inproc://unused-rep",
            topic="py000.mt5.v1",
            request_timeout_ms=1_000,
            mutation_timeout_ms=1,
        )
        req = TimeoutReq()
        transport._req = cast(Any, req)

        with pytest.raises(Mt5V1RequestTimeout, match="socket was reset"):
            await asyncio.wait_for(
                transport.close_position(
                    current.binding(),
                    client_request_id="close-timeout",
                    side="sell",
                    quantity_lots="0.01",
                    position_ticket="700000001",
                    position_identifier="800000001",
                ),
                timeout=0.1,
            )

        assert req.send_count == 1
        assert req.closed is True
        assert transport._req is None

    asyncio.run(scenario())
