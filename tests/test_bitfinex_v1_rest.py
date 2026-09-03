from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.core.nautilus_pyo3 import HttpError, HttpMethod, HttpTimeoutError

import py000_nautilus.bitfinex_v1_rest as rest_module
from py000_nautilus.bitfinex_v1_rest import (
    BitfinexV1RestClient,
    BitfinexV1RestError,
)

SYMBOL = "tXAUTF0:USTF0"
PAPER_SYMBOL = "tTESTXAUTF0:TESTUSDTF0"


@dataclass(frozen=True, slots=True)
class FakeResponse:
    status: int = 200
    headers: dict[str, str] = field(
        default_factory=lambda: {"content-type": "application/json; charset=utf-8"},
    )
    body: bytes = b"[]"


@dataclass(frozen=True, slots=True)
class Call:
    method: HttpMethod
    url: str
    headers: dict[str, str]
    body: bytes


class FakeHttpClient:
    def __init__(
        self,
        responses: list[FakeResponse] | None = None,
        *,
        hang: bool = False,
    ) -> None:
        self.calls: list[Call] = []
        self._responses = list(responses or [FakeResponse()])
        self._hang = hang
        self.active = 0
        self.max_active = 0

    async def request(
        self,
        method: HttpMethod,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        keys: list[str] | None = None,
        timeout_secs: int | None = None,
    ) -> FakeResponse:
        del params, keys, timeout_secs
        assert headers is not None
        assert body is not None
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self._hang:
                await asyncio.Event().wait()
            await asyncio.sleep(0)
            self.calls.append(Call(method, url, dict(headers), body))
            return self._responses.pop(0)
        finally:
            self.active -= 1


class FailingHttpClient(FakeHttpClient):
    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self._failure = failure

    async def request(
        self,
        method: HttpMethod,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        keys: list[str] | None = None,
        timeout_secs: int | None = None,
    ) -> FakeResponse:
        del method, url, params, headers, body, keys, timeout_secs
        raise self._failure


def client(
    http: FakeHttpClient,
    *,
    clock_ns: int = 1_700_000_000_000_123_000,
    timeout_secs: int = 10,
) -> BitfinexV1RestClient:
    return BitfinexV1RestClient(
        api_key="KEY",
        api_secret="SECRET",
        base_url="https://example.test/",
        http_client=http,
        clock_ns=lambda: clock_ns,
        timeout_secs=timeout_secs,
    )


@pytest.mark.parametrize("symbol", [SYMBOL, PAPER_SYMBOL])
def test_history_signature_uses_exact_path_nonce_and_compact_body(symbol: str) -> None:
    async def scenario() -> None:
        http = FakeHttpClient()
        rest = client(http)

        assert await rest.order_history_by_symbol(symbol, start=10, end=20) == []

        [call] = http.calls
        nonce = "1700000000000123"
        body = b'{"start":10,"end":20,"limit":2500}'
        expected = hmac.new(
            b"SECRET",
            f"/api/v2/auth/r/orders/{symbol}/hist".encode() + nonce.encode() + body,
            hashlib.sha384,
        ).hexdigest()
        assert call == Call(
            method=HttpMethod.POST,
            url=f"https://example.test/v2/auth/r/orders/{symbol}/hist",
            headers={
                "Content-Type": "application/json",
                "bfx-apikey": "KEY",
                "bfx-nonce": nonce,
                "bfx-signature": expected,
            },
            body=body,
        )

    asyncio.run(scenario())


def test_nonce_is_microsecond_and_strictly_monotonic_per_client() -> None:
    async def scenario() -> None:
        http = FakeHttpClient([FakeResponse(), FakeResponse(), FakeResponse()])
        rest = client(http, clock_ns=1_700_000_000_000_000_000)

        await rest.user_info()
        await rest.permissions()
        await rest.wallets()

        assert [call.headers["bfx-nonce"] for call in http.calls] == [
            "1700000000000000",
            "1700000000000001",
            "1700000000000002",
        ]

    asyncio.run(scenario())


def test_concurrent_reads_are_serialized_to_preserve_nonce_arrival_order() -> None:
    async def scenario() -> None:
        http = FakeHttpClient([FakeResponse(), FakeResponse(), FakeResponse()])
        rest = client(http)

        await asyncio.gather(rest.user_info(), rest.permissions(), rest.positions())

        assert http.max_active == 1
        assert len(http.calls) == 3
        nonces = [int(call.headers["bfx-nonce"]) for call in http.calls]
        assert nonces == list(range(nonces[0], nonces[0] + 3))

    asyncio.run(scenario())


def test_exposes_only_the_required_read_paths_as_post() -> None:
    async def scenario() -> None:
        http = FakeHttpClient([FakeResponse() for _ in range(7)])
        rest = client(http)

        await rest.user_info()
        await rest.permissions()
        await rest.wallets()
        await rest.active_orders_by_symbol(SYMBOL)
        await rest.order_history_by_symbol(SYMBOL, limit=12)
        await rest.trades_by_symbol(SYMBOL, start=1, end=2, limit=7)
        await rest.positions()

        assert all(call.method == HttpMethod.POST for call in http.calls)
        assert [call.url.removeprefix("https://example.test/") for call in http.calls] == [
            "v2/auth/r/info/user",
            "v2/auth/r/permissions",
            "v2/auth/r/wallets",
            f"v2/auth/r/orders/{SYMBOL}",
            f"v2/auth/r/orders/{SYMBOL}/hist",
            f"v2/auth/r/trades/{SYMBOL}/hist",
            "v2/auth/r/positions",
        ]
        assert [call.body for call in http.calls] == [
            b"{}",
            b"{}",
            b"{}",
            b"{}",
            b'{"limit":12}',
            b'{"start":1,"end":2,"limit":7,"sort":1}',
            b"{}",
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start": True}, "start"),
        ({"start": -1}, "start"),
        ({"start": 2, "end": 1}, "exceed"),
        ({"limit": 0}, "limit"),
        ({"limit": 2_501}, "limit"),
    ],
)
def test_history_rejects_invalid_windows_without_a_request(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    async def scenario() -> None:
        http = FakeHttpClient()
        rest = client(http)

        with pytest.raises(ValueError, match=message):
            await rest.order_history_by_symbol(SYMBOL, **kwargs)
        assert http.calls == []

    asyncio.run(scenario())


def test_symbol_cannot_escape_the_read_only_path() -> None:
    async def scenario() -> None:
        http = FakeHttpClient()
        rest = client(http)

        with pytest.raises(ValueError, match="symbol"):
            await rest.active_orders_by_symbol("tBTCUSD/../../submit")
        assert http.calls == []

    asyncio.run(scenario())


def test_numbers_with_fractional_json_syntax_decode_as_decimal() -> None:
    async def scenario() -> None:
        response = FakeResponse(body=b'[123,1.250,1e-8,{"fee":-0.004}]')
        rest = client(FakeHttpClient([response]))

        assert await rest.trades_by_symbol(SYMBOL) == [
            123,
            Decimal("1.250"),
            Decimal("1E-8"),
            {"fee": Decimal("-0.004")},
        ]

    asyncio.run(scenario())


def test_headerless_nautilus_response_still_requires_strict_json() -> None:
    async def scenario() -> None:
        response = FakeResponse(headers={}, body=b"[]")
        assert await client(FakeHttpClient([response])).user_info() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(status=201),
        FakeResponse(headers={"content-type": "text/html"}),
        FakeResponse(body=b""),
        FakeResponse(body=b"not-json"),
        FakeResponse(body=b"NaN"),
        FakeResponse(body=b'{"same":1,"same":2}'),
        FakeResponse(body=b"1"),
    ],
)
def test_response_contract_fails_closed_without_echoing_body_or_credentials(
    response: FakeResponse,
) -> None:
    async def scenario() -> None:
        rest = client(FakeHttpClient([response]))

        with pytest.raises(BitfinexV1RestError) as error:
            await rest.user_info()
        rendered = str(error.value)
        assert "KEY" not in rendered
        assert "SECRET" not in rendered
        assert not error.value.retryable
        if response.body:
            assert response.body.decode(errors="ignore") not in rendered

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("connection leaked SECRET"),
        HttpError("HTTP client leaked SECRET"),
        HttpTimeoutError("timeout leaked SECRET"),
    ],
)
def test_transport_failure_is_redacted_and_retryable(failure: Exception) -> None:
    async def scenario() -> None:
        rest = client(FailingHttpClient(failure))

        with pytest.raises(BitfinexV1RestError) as error:
            await rest._post("v2/auth/r/positions", {})
        assert "KEY" not in str(error.value)
        assert "SECRET" not in str(error.value)
        assert error.value.retryable
        assert error.value.__suppress_context__ is True

    asyncio.run(scenario())


def test_retryable_response_failure_still_advances_the_nonce() -> None:
    async def scenario() -> None:
        http = FakeHttpClient([FakeResponse(status=500), FakeResponse()])
        rest = client(http, clock_ns=1_700_000_000_000_000_000)

        assert await rest.user_info() == []
        assert [call.headers["bfx-nonce"] for call in http.calls] == [
            "1700000000000000",
            "1700000000000001",
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("responses", "calls", "message"),
    [
        ([FakeResponse(500), FakeResponse(500), FakeResponse(500), FakeResponse()], 3, "500"),
        ([FakeResponse(500), FakeResponse(401), FakeResponse()], 2, "401"),
    ],
)
def test_read_retry_stops_at_the_bounded_or_permanent_failure(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[FakeResponse],
    calls: int,
    message: str,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(rest_module, "_READ_RETRY_DELAYS_SECS", (0.0, 0.0))
        http = FakeHttpClient(responses)

        with pytest.raises(BitfinexV1RestError, match=message):
            await client(http).user_info()

        assert len(http.calls) == calls
        assert len(http._responses) == len(responses) - calls

    asyncio.run(scenario())


def test_read_retry_has_one_total_timeout_budget() -> None:
    async def scenario() -> None:
        http = FakeHttpClient(hang=True)
        rest = client(http, timeout_secs=1)
        started = asyncio.get_running_loop().time()

        with pytest.raises(BitfinexV1RestError, match="read timed out"):
            await rest.user_info()

        assert asyncio.get_running_loop().time() - started < 1.5
        http._hang = False
        assert await rest.positions() == []

    asyncio.run(scenario())


def test_read_timeout_budget_includes_waiting_for_the_single_flight_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        http = FakeHttpClient()
        rest = client(http)
        monkeypatch.setattr(rest, "_read_timeout_secs", 0.05)
        await rest._read_lock.acquire()
        queued = asyncio.create_task(rest.user_info())
        await asyncio.sleep(0.1)

        with pytest.raises(BitfinexV1RestError, match="read timed out"):
            await queued
        assert http.calls == []

        rest._read_lock.release()
        assert await rest.user_info() == []
        assert len(http.calls) == 1

    asyncio.run(scenario())


def test_unknown_transport_failure_is_redacted_and_not_retryable() -> None:
    async def scenario() -> None:
        rest = client(FailingHttpClient(RuntimeError("programming failure leaked KEY SECRET")))

        with pytest.raises(BitfinexV1RestError) as error:
            await rest._post("v2/auth/r/positions", {})
        assert "KEY" not in str(error.value)
        assert "SECRET" not in str(error.value)
        assert not error.value.retryable

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (408, True),
        (500, True),
        (502, True),
        (503, True),
        (504, True),
        (400, False),
        (401, False),
        (425, False),
        (429, False),
        (501, False),
        (505, False),
        (201, False),
    ],
)
def test_http_error_exposes_only_bounded_retry_classification(
    status: int,
    retryable: bool,
) -> None:
    async def scenario() -> None:
        rest = client(FakeHttpClient([FakeResponse(status=status)]))

        with pytest.raises(BitfinexV1RestError) as error:
            await rest._post("v2/auth/r/info/user", {})

        assert error.value.retryable is retryable
        assert str(status) in str(error.value)

    asyncio.run(scenario())
