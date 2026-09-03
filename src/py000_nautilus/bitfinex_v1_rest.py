"""Minimal authenticated read-only Bitfinex REST v2 client."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Never, Protocol

from nautilus_trader.core.nautilus_pyo3 import HttpClient, HttpMethod

MAX_PAGE_SIZE = 2_500
_DEFAULT_BASE_URL = "https://api.bitfinex.com"
_SYMBOL = re.compile(r"t[A-Za-z0-9:_-]+\Z")

type JsonScalar = None | bool | int | Decimal | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class BitfinexV1RestError(RuntimeError):
    """The response cannot be safely used as Bitfinex reconciliation evidence."""


class _HttpResponse(Protocol):
    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> dict[str, str]: ...

    @property
    def body(self) -> bytes: ...


class _HttpTransport(Protocol):
    async def request(
        self,
        method: HttpMethod,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        keys: list[str] | None = None,
        timeout_secs: int | None = None,
    ) -> _HttpResponse: ...


class BitfinexV1RestClient:
    """Sign and issue the small read-only request set needed by PY000 v1."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_secs: int = 10,
        http_client: _HttpTransport | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if type(api_key) is not str or not api_key:
            raise ValueError("api_key must be a non-empty string")
        if type(api_secret) is not str or not api_secret:
            raise ValueError("api_secret must be a non-empty string")
        if type(base_url) is not str or not base_url.startswith("https://"):
            raise ValueError("base_url must be an HTTPS URL")
        if type(timeout_secs) is not int or timeout_secs <= 0:
            raise ValueError("timeout_secs must be a positive integer")

        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._base_url = base_url.rstrip("/")
        self._http = http_client or HttpClient(timeout_secs=timeout_secs)
        self._clock_ns = clock_ns
        self._last_nonce = 0

    async def user_info(self) -> JsonValue:
        return await self._post("v2/auth/r/info/user", {})

    async def permissions(self) -> JsonValue:
        return await self._post("v2/auth/r/permissions", {})

    async def wallets(self) -> JsonValue:
        return await self._post("v2/auth/r/wallets", {})

    async def active_orders_by_symbol(self, symbol: str) -> JsonValue:
        return await self._post(f"v2/auth/r/orders/{_validated_symbol(symbol)}", {})

    async def order_history_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> JsonValue:
        return await self._post(
            f"v2/auth/r/orders/{_validated_symbol(symbol)}/hist",
            _window(start=start, end=end, limit=limit),
        )

    async def trades_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> JsonValue:
        body = _window(start=start, end=end, limit=limit)
        body["sort"] = 1
        return await self._post(
            f"v2/auth/r/trades/{_validated_symbol(symbol)}/hist",
            body,
        )

    async def positions(self) -> JsonValue:
        return await self._post("v2/auth/r/positions", {})

    async def _post(self, api_path: str, payload: dict[str, int]) -> JsonValue:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nonce = self._nonce()
        signature_payload = f"/api/{api_path}{nonce}".encode("ascii") + body
        signature = hmac.new(self._api_secret, signature_payload, hashlib.sha384).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "bfx-apikey": self._api_key,
            "bfx-nonce": nonce,
            "bfx-signature": signature,
        }
        try:
            response = await self._http.request(
                HttpMethod.POST,
                self._base_url + "/" + api_path,
                headers=headers,
                body=body,
            )
        except Exception:
            raise BitfinexV1RestError(f"Bitfinex REST request failed: {api_path}") from None
        return _decode_response(response, api_path)

    def _nonce(self) -> str:
        now_ns = self._clock_ns()
        if type(now_ns) is not int or now_ns <= 0:
            raise BitfinexV1RestError("Bitfinex REST nonce clock is invalid")
        candidate = now_ns // 1_000
        nonce = max(candidate, self._last_nonce + 1)
        self._last_nonce = nonce
        return str(nonce)


def _validated_symbol(symbol: str) -> str:
    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        raise ValueError("symbol must be a Bitfinex trading symbol")
    return symbol


def _window(*, start: int | None, end: int | None, limit: int) -> dict[str, int]:
    if start is not None and (type(start) is not int or start < 0):
        raise ValueError("start must be a non-negative integer")
    if end is not None and (type(end) is not int or end < 0):
        raise ValueError("end must be a non-negative integer")
    if start is not None and end is not None and start > end:
        raise ValueError("start must not exceed end")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")

    payload: dict[str, int] = {}
    if start is not None:
        payload["start"] = start
    if end is not None:
        payload["end"] = end
    payload["limit"] = limit
    return payload


def _decode_response(response: _HttpResponse, api_path: str) -> JsonValue:
    try:
        status = response.status
        headers = response.headers
        body = response.body
    except Exception:
        raise BitfinexV1RestError(f"Invalid Bitfinex REST response: {api_path}") from None

    if type(status) is not int or status != 200:
        safe_status = status if type(status) is int else "invalid"
        raise BitfinexV1RestError(f"Bitfinex REST HTTP {safe_status}: {api_path}")
    if type(headers) is not dict or any(
        type(key) is not str or type(value) is not str for key, value in headers.items()
    ):
        raise BitfinexV1RestError(f"Invalid Bitfinex REST headers: {api_path}")
    content_types = [value for key, value in headers.items() if key.lower() == "content-type"]
    media_type = content_types[0].split(";", 1)[0].strip().lower() if content_types else None
    if len(content_types) > 1 or (media_type is not None and media_type != "application/json"):
        raise BitfinexV1RestError(f"Invalid Bitfinex REST content type: {api_path}")
    if type(body) is not bytes or not body:
        raise BitfinexV1RestError(f"Invalid Bitfinex REST body: {api_path}")

    try:
        text = body.decode("utf-8")
        decoded: object = json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
        if not isinstance(decoded, list | dict):
            raise ValueError
        return _json_value(decoded)
    except (UnicodeDecodeError, ValueError, TypeError, OverflowError):
        raise BitfinexV1RestError(f"Invalid Bitfinex REST JSON: {api_path}") from None


def _reject_json_constant(value: str) -> Never:
    raise ValueError(f"non-standard JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite JSON number")
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError("non-string JSON object key")
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("unsupported JSON value")
