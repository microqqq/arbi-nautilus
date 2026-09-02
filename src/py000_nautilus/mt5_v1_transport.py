"""Small ZeroMQ transport for the PY000 MT5 v1 EA."""

from __future__ import annotations

import asyncio
from typing import Literal, cast
from uuid import uuid4

import zmq
import zmq.asyncio

from py000_nautilus.mt5_v1_protocol import (
    MAX_WIRE_BYTES,
    Binding,
    ExecutionEventsRequest,
    HelloRequest,
    Identity,
    JsonObject,
    RecoveryState,
    Request,
    SnapshotRequest,
    SubmitMarketDeltaRequest,
    decode_pub,
    decode_response_for,
    encode_json,
    request_to_wire,
)


class Mt5V1TransportError(RuntimeError):
    """The transport cannot establish a trustworthy reply."""


class Mt5V1RemoteError(Mt5V1TransportError):
    """The EA returned a typed v1 error response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Mt5V1Transport:
    """Own exactly one REQ and one SUB socket, both on the caller's event loop."""

    def __init__(
        self,
        *,
        pub_url: str,
        rep_url: str,
        topic: str,
        request_timeout_ms: int,
    ) -> None:
        self._pub_url = pub_url
        self._rep_url = rep_url
        self._topic = topic.encode("utf-8", errors="strict")
        self._request_timeout = request_timeout_ms / 1000
        self._context: zmq.asyncio.Context | None = None
        self._req: zmq.asyncio.Socket | None = None
        self._sub: zmq.asyncio.Socket | None = None
        self._request_lock = asyncio.Lock()

    @property
    def topic(self) -> bytes:
        return self._topic

    async def open(self) -> None:
        if self._context is not None:
            raise Mt5V1TransportError("transport is already open")
        context: zmq.asyncio.Context | None = None
        req: zmq.asyncio.Socket | None = None
        sub: zmq.asyncio.Socket | None = None
        try:
            context = zmq.asyncio.Context()
            req = context.socket(zmq.REQ)
            self._configure(req, high_water_mark=100)
            req.connect(self._rep_url)
            sub = context.socket(zmq.SUB)
            self._configure(sub, high_water_mark=1_000)
            sub.setsockopt(zmq.SUBSCRIBE, self._topic)
            sub.connect(self._pub_url)
        except Exception as exc:
            if req is not None:
                req.close(linger=0)
            if sub is not None:
                sub.close(linger=0)
            if context is not None:
                context.destroy(linger=0)
            raise Mt5V1TransportError("transport open failed") from exc
        self._context = context
        self._req = req
        self._sub = sub

    async def close(self) -> None:
        if self._req is not None:
            self._req.close(linger=0)
            self._req = None
        if self._sub is not None:
            self._sub.close(linger=0)
            self._sub = None
        if self._context is not None:
            self._context.destroy(linger=0)
            self._context = None

    async def hello(self) -> tuple[Identity, RecoveryState]:
        request = HelloRequest(request_id=f"hello-{uuid4().hex}")
        response = await self._round_trip(request)
        data = self._success_data(response)
        identity = Identity.from_wire(data["identity"])
        recovery = data["recovery_state"]
        if recovery not in {"ready", "blocked"}:
            raise Mt5V1TransportError("hello recovery state escaped the codec")
        return identity, cast(RecoveryState, recovery)

    async def snapshot(self, binding: Binding) -> JsonObject:
        request = SnapshotRequest(
            request_id=f"snapshot-{uuid4().hex}",
            binding=binding,
        )
        response = await self._round_trip(request)
        return self._success_data(response)

    async def submit_market_delta(
        self,
        binding: Binding,
        *,
        client_request_id: str,
        side: Literal["buy", "sell"],
        quantity_lots: str,
    ) -> JsonObject:
        request = SubmitMarketDeltaRequest(
            request_id=f"submit-{uuid4().hex}",
            binding=binding,
            client_request_id=client_request_id,
            side=side,
            quantity_lots=quantity_lots,
        )
        response = await self._round_trip(request)
        return self._success_data(response)

    async def execution_events(
        self,
        binding: Binding,
        *,
        after_cursor: str,
        limit: int,
    ) -> JsonObject:
        request = ExecutionEventsRequest(
            request_id=f"events-{uuid4().hex}",
            binding=binding,
            after_cursor=after_cursor,
            limit=limit,
        )
        response = await self._round_trip(request)
        return self._success_data(response)

    async def recv_pub(self) -> tuple[bytes, JsonObject]:
        if self._sub is None:
            raise Mt5V1TransportError("SUB socket is not open")
        try:
            frames = await self._sub.recv_multipart()
        except asyncio.CancelledError:
            raise
        except zmq.ZMQError as exc:
            raise Mt5V1TransportError("SUB receive failed") from exc
        if len(frames) != 2:
            raise Mt5V1TransportError("PUB message must contain exactly two frames")
        topic, payload = frames
        if len(topic) > 128 or len(payload) > MAX_WIRE_BYTES:
            raise Mt5V1TransportError("PUB frame exceeds the v1 wire budget")
        return topic, decode_pub(payload)

    async def _round_trip(self, request: Request) -> JsonObject:
        payload = encode_json(request_to_wire(request)).encode("utf-8")
        async with self._request_lock:
            if self._req is None:
                self._replace_req()
            req = self._req
            if req is None:
                raise Mt5V1TransportError("REQ socket is not open")
            try:
                await req.send(payload)
                frames = await asyncio.wait_for(
                    req.recv_multipart(),
                    timeout=self._request_timeout,
                )
            except asyncio.CancelledError:
                self._drop_req()
                raise
            except (TimeoutError, zmq.ZMQError) as exc:
                self._drop_req()
                raise Mt5V1TransportError("REQ round-trip failed; socket was reset") from exc
            if len(frames) != 1 or len(frames[0]) > MAX_WIRE_BYTES:
                self._drop_req()
                raise Mt5V1TransportError("REP reply violated the one-frame wire budget")
            return decode_response_for(request, frames[0])

    def _replace_req(self) -> None:
        if self._context is None:
            raise Mt5V1TransportError("transport is not open")
        self._drop_req()
        req = self._context.socket(zmq.REQ)
        try:
            self._configure(req, high_water_mark=100)
            req.connect(self._rep_url)
        except Exception:
            req.close(linger=0)
            raise
        self._req = req

    def _drop_req(self) -> None:
        if self._req is not None:
            self._req.close(linger=0)
            self._req = None

    @staticmethod
    def _configure(socket: zmq.asyncio.Socket, *, high_water_mark: int) -> None:
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.SNDHWM, high_water_mark)
        socket.setsockopt(zmq.RCVHWM, high_water_mark)

    @staticmethod
    def _success_data(response: JsonObject) -> JsonObject:
        if response["ok"] is True:
            return cast(JsonObject, response["data"])
        error = cast(JsonObject, response["error"])
        raise Mt5V1RemoteError(cast(str, error["code"]), cast(str, error["message"]))
