from __future__ import annotations

import asyncio
from typing import Any, cast, override

import pytest

from py000_nautilus.mt5_v1_protocol import (
    ExecutionEventsRequest,
    Identity,
    JsonObject,
    Request,
    SubmitMarketDeltaRequest,
    normalized_account_id,
)
from py000_nautilus.mt5_v1_transport import Mt5V1Transport, Mt5V1TransportError


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
        events = await transport.execution_events(
            current.binding(),
            after_cursor="7",
            limit=25,
        )

        assert submit == {"captured": True}
        assert events == {"captured": True}
        assert len(transport.requests) == 2
        submit_request = transport.requests[0]
        assert isinstance(submit_request, SubmitMarketDeltaRequest)
        assert submit_request.binding == current.binding()
        assert submit_request.client_request_id == "delta-1"
        assert submit_request.side == "sell"
        assert submit_request.quantity_lots == "0.05"
        event_request = transport.requests[1]
        assert isinstance(event_request, ExecutionEventsRequest)
        assert event_request.binding == current.binding()
        assert event_request.after_cursor == "7"
        assert event_request.limit == 25

    asyncio.run(scenario())


def test_submit_timeout_drops_socket_without_retry() -> None:
    async def scenario() -> None:
        current = identity()
        transport = Mt5V1Transport(
            pub_url="inproc://unused-pub",
            rep_url="inproc://unused-rep",
            topic="py000.mt5.v1",
            request_timeout_ms=1,
        )
        req = TimeoutReq()
        transport._req = cast(Any, req)

        with pytest.raises(Mt5V1TransportError, match="socket was reset"):
            await transport.submit_market_delta(
                current.binding(),
                client_request_id="delta-timeout",
                side="buy",
                quantity_lots="0.01",
            )

        assert req.send_count == 1
        assert req.closed is True
        assert transport._req is None

    asyncio.run(scenario())
