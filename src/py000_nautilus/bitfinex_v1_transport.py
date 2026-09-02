"""Small JSON WebSocket transport shared by the Bitfinex v1 clients."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import cast

from websockets.asyncio.client import ClientConnection, connect


class BitfinexV1TransportError(RuntimeError):
    """The Bitfinex WebSocket cannot supply a valid JSON frame."""


class BitfinexV1Transport:
    """One WebSocket connection with no authentication or reconnect supervisor."""

    def __init__(self, url: str, *, open_timeout_ms: int = 10_000) -> None:
        self._url = url
        self._open_timeout_ms = open_timeout_ms
        self._socket: ClientConnection | None = None

    async def open(self) -> None:
        if self._socket is not None:
            raise BitfinexV1TransportError("Bitfinex transport is already open")
        self._socket = await connect(
            self._url,
            open_timeout=self._open_timeout_ms / 1_000,
            max_size=64 * 1024,
            max_queue=64,
        )

    async def close(self) -> None:
        socket = self._socket
        self._socket = None
        if socket is not None:
            await socket.close()

    async def send_json(self, payload: dict[str, object] | list[object]) -> None:
        socket = self._require_socket()
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        await socket.send(encoded)

    async def recv_json(self) -> dict[str, object] | list[object]:
        socket = self._require_socket()
        raw = await socket.recv()
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise BitfinexV1TransportError("Bitfinex frame is not UTF-8") from exc
        else:
            text = raw
        if len(text.encode("utf-8")) > 64 * 1024:
            raise BitfinexV1TransportError("Bitfinex frame exceeds 64 KiB")
        try:
            value = json.loads(
                text,
                parse_float=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except (ValueError, TypeError) as exc:
            raise BitfinexV1TransportError("Bitfinex frame is not strict JSON") from exc
        if not isinstance(value, dict | list):
            raise BitfinexV1TransportError("Bitfinex frame must be an object or array")
        return cast(dict[str, object] | list[object], value)

    def _require_socket(self) -> ClientConnection:
        if self._socket is None:
            raise BitfinexV1TransportError("Bitfinex transport is not open")
        return self._socket


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


__all__ = ["BitfinexV1Transport", "BitfinexV1TransportError"]
