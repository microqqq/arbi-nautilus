from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.core.nautilus_pyo3 import HttpMethod

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
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.calls: list[Call] = []
        self._responses = list(responses or [FakeResponse()])

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
        self.calls.append(Call(method, url, dict(headers), body))
        return self._responses.pop(0)


def client(
    http: FakeHttpClient,
    *,
    clock_ns: int = 1_700_000_000_000_123_000,
) -> BitfinexV1RestClient:
    return BitfinexV1RestClient(
        api_key="KEY",
        api_secret="SECRET",
        base_url="https://example.test/",
        http_client=http,
        clock_ns=lambda: clock_ns,
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
        if response.body:
            assert response.body.decode(errors="ignore") not in rendered

    asyncio.run(scenario())


def test_transport_failure_is_redacted() -> None:
    class FailingHttpClient(FakeHttpClient):
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
            del method, url, params, body, keys, timeout_secs
            raise RuntimeError(f"upstream leaked {headers} SECRET")

    async def scenario() -> None:
        rest = client(FailingHttpClient())

        with pytest.raises(BitfinexV1RestError) as error:
            await rest.positions()
        assert "KEY" not in str(error.value)
        assert "SECRET" not in str(error.value)
        assert error.value.__suppress_context__ is True

    asyncio.run(scenario())
