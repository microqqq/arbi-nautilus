"""Authenticated normalized carry vectors; no local ZIP or legacy imports in CI.

Funding tests use a real parser, DataEngine and strategy callbacks with an inert
transport. They do not authenticate the legacy wire, accounts or live networking.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from msgspec.structs import replace
from nautilus_trader.model.data import FundingRateUpdate
from test_bitfinex_v1_data import _client, _FakeTransport, _funding_payload
from test_taker_events import _event_engine, _live_inputs_ready, _swap_instrument

from py000_nautilus.app import (
    _book_snapshot,
    _hedge_instrument,
    _maker_strategy_config,
    _quote,
    _source_instrument,
    _strategy_config,
)
from py000_nautilus.bitfinex_v1_data import BitfinexV1DataClient, BitfinexV1DataError
from py000_nautilus.config import CarryConfig, FxConfig
from py000_nautilus.economics import normalize_mt5_points_swap
from py000_nautilus.strategies._mt5_costs import mt5_swap_spec
from py000_nautilus.strategies.maker import MakerStrategy
from py000_nautilus.strategies.taker import TakerStrategy

D = Decimal
_PAYLOAD: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures/legacy_carry_vectors.json").read_text(),
)
_DEFAULTS = _PAYLOAD["defaults"]
_POINTS = {row["id"]: row for row in _PAYLOAD["points"]}
_FUNDING = {row["id"]: row for row in _PAYLOAD["funding"]}
_TOLERANCE = D("1e-15")
_FEE = D("0.00065")


def _values(case: dict[str, Any]) -> dict[str, Any]:
    return {**_DEFAULTS, **case}


def _ns(timestamp: str) -> int:
    delta = datetime.fromisoformat(timestamp) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _assert_original(actual: tuple[Decimal, Decimal], expected: list[str]) -> None:
    for value, original in zip(actual, map(D, expected), strict=True):
        assert value.is_finite() and abs(value - original) <= _TOLERANCE
        # Direction and explicit zero are discrete contracts, not tolerance windows.
        assert (value > 0) == (original > 0)
        assert (value < 0) == (original < 0)
        if not original:
            assert value == 0


def _normalize(case: dict[str, Any], *, raw_mode: bool = False) -> tuple[Decimal, Decimal]:
    values = _values(case)
    rates = values["rates"]
    if rates is None or "legacy_rates" in values:
        rates = []  # An incomplete dictionary has no valid current seven-item representation.
    return normalize_mt5_points_swap(
        swap_long=D(values["swap_long"]), swap_short=D(values["swap_short"]),
        point=D(values["point"]), ask=D(values["ask"]),
        native_swap_mode=case.get("raw_mode", 1) if raw_mode else 1,
        swap_rates=tuple(map(D, rates)), now_ns=_ns(values["utc"]),
        server_timezone=values["server_timezone"],
    )


def test_carry_fixture_records_authenticated_scope_and_canonical_vector_digest() -> None:
    metadata = _PAYLOAD["metadata"]
    assert metadata["schema_version"] == 1
    assert metadata["archive"]["sha256"] == (
        "3e50dcc625b1f78048622f3bb5e5ee2f94eb8e4c4635f39108fcb09f6e3d1892"
    )
    assert metadata["legacy_points_mode"] == 0 and metadata["native_points_mode"] == 1
    assert metadata["absolute_tolerance"] == str(_TOLERANCE).lower()
    assert "margin_funding" in metadata["funding_boundary"]
    assert "NEXT_FUNDING_ACCRUED" in metadata["funding_boundary"]
    assert "unverified" in metadata["wire_boundary"]
    assert "pytz.timezone" in metadata["injected_dependencies"]
    assert len(metadata["methods"]) == 7
    assert metadata["methods"][-1]["method_segment_sha256"] == (
        "e31fcb41db329e7792fc445b4c1c01fadb615e65b89c7d2d55c08408d47d4fe1"
    )
    vectors = {key: _PAYLOAD[key] for key in ("points", "fallbacks", "funding")}
    digest = hashlib.sha256(json.dumps(vectors, sort_keys=True, separators=(",", ":"))
                            .encode()).hexdigest()
    assert digest == metadata["vectors_sha256"] == (
        "f026ef800791e3bff4d615de5216354d96bb93752c18aa2c31ae033b69cf3419"
    )
    assert [len(rows) for rows in vectors.values()] == [17, 11, 4]
    assert all(len({row["id"] for row in rows}) == len(rows) for rows in vectors.values())


@pytest.mark.parametrize("case", _PAYLOAD["points"], ids=lambda case: case["id"])
def test_authenticated_normalized_points_calendar_and_signed_returns(case: dict[str, Any]) -> None:
    values = _values(case)
    local = datetime.fromisoformat(values["utc"]).astimezone(ZoneInfo(values["server_timezone"]))
    assert local.isoformat() == case["local"]
    assert (local.weekday() + 1) % 7 == case["mql_weekday"]
    assert D(values["rates"][case["mql_weekday"]]) == D(case["multiplier"])
    _assert_original(_normalize(case), case["expected"])


@pytest.mark.parametrize("case", _PAYLOAD["fallbacks"], ids=lambda case: case["id"])
def test_original_raw_modes_and_fallbacks_are_explicit_migration_differences(
    case: dict[str, Any],
) -> None:
    if case["current"] == "disabled_zero":
        assert _normalize(case, raw_mode=True) == (D(0), D(0))
        assert any(D(value) != 0 for value in case["expected"])
    elif case["current"] == "points":
        actual = _normalize(case, raw_mode=True)
        _assert_original(actual, _POINTS[case["current_reference"]]["expected"])
        assert all(a != D(old) for a, old in zip(actual, case["expected"], strict=True))
    elif case.get("absent") in {"spec", "point"}:
        info = None if case["absent"] == "spec" else {
            "swap_long": _DEFAULTS["swap_long"], "swap_short": _DEFAULTS["swap_short"],
            "swap_mode": 1, "swap_rates": _DEFAULTS["rates"],
            "server_timezone": _DEFAULTS["server_timezone"],
        }
        with pytest.raises((KeyError, TypeError)):
            mt5_swap_spec(cast(Any, SimpleNamespace(info=info)))
        assert tuple(map(D, case["expected"])) == (D(0), D(0))
    else:
        # Missing quote has no usable positive ask; it cannot become a zero cost.
        current = {**case, "ask": "0"} if case.get("absent") == "price" else case
        with pytest.raises(ValueError):
            _normalize(current, raw_mode=True)


def _strategy(kind: str, path: Path, timestamp: int, *, no_source_sizes: bool = False) -> Any:
    config: Any = _maker_strategy_config(path) if kind == "maker" else _strategy_config(path)
    economics = replace(config.economics, carry=CarryConfig(total_trade_fee=_FEE),
                        fx=FxConfig(usd_usdt_bid=D("0.9999"), usd_usdt_ask=D("1.0001")))
    if kind == "maker" and no_source_sizes:
        economics = replace(
            economics, bid=replace(economics.bid, open_quantity_ounces=D(0)),
            ask=replace(economics.ask, open_quantity_ounces=D(0)),
        )
    # Keep normal quote/cost/session TTLs. Only the synthetic epoch and fee/FX change.
    config = replace(config, economics=economics, initial_cost_ts_ns=0,
                     initial_session_ts_ns=timestamp)
    strategy_type = MakerStrategy if kind == "maker" else TakerStrategy
    return strategy_type(config, live_costs_from_adapters=True)


def _instrument(case: dict[str, Any], timestamp: int) -> Any:
    values = _values(case)
    return _swap_instrument(
        timestamp, **{key: values[key] for key in ("swap_long", "swap_short", "point")},
        swap_rates=tuple(values["rates"]), server_timezone=values["server_timezone"],
    )


def _funding_event(
    client: BitfinexV1DataClient, rate: str | None, now_ns: int,
) -> FundingRateUpdate:
    client._publish_funding = True
    frame = [1, _funding_payload(timestamp_ms=now_ns // 1_000_000,
                                 rate=None if rate is None else D(rate))]
    event, = client._consume_funding_frame(frame)
    assert isinstance(event, FundingRateUpdate)
    return event


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("rate_id", ["positive", "negative", "zero"])
def test_parser_dataengine_funding_and_swap_callbacks_preserve_oracle_direction_and_age(
    tmp_path: Path, kind: str, rate_id: str,
) -> None:
    async def run() -> None:
        case, funding = _POINTS["positive_short"], _FUNDING[rate_id]
        now_ns = _ns(_values(case)["utc"])
        strategy = _strategy(kind, tmp_path / kind, now_ns)
        with _event_engine(strategy) as engine:
            engine.kernel.clock.set_time(now_ns)
            strategy.clock.set_time(now_ns)
            engine.kernel.data_engine.process(_instrument({}, now_ns))
            engine.trader.start()
            assert strategy.is_running
            fake = _FakeTransport()
            client = _client(fake, clock=engine.kernel.clock)
            event = _funding_event(client, funding["rate"], now_ns)
            engine.kernel.data_engine.process(event)
            assert strategy._cost_snapshot_valid
            assert (strategy._carry.bitfinex_long, strategy._carry.bitfinex_short) == tuple(
                map(D, funding["expected"]),
            )
            assert strategy._carry.total_trade_fee == _FEE
            assert strategy._fx == strategy._config.economics.fx
            assert strategy._cost_ts_ns == event.ts_event

            strategy.clock.set_time(now_ns + 1)
            updated = _instrument(case, now_ns + 1)
            engine.kernel.data_engine.process(updated)
            assert engine.cache.instrument(updated.id) is strategy._hedge_instrument is updated
            assert strategy._cost_ts_ns == event.ts_event
            tick = _quote(_hedge_instrument(), "3999", "4000", "5", now_ns + 1)
            carry = strategy._carry_for_hedge_tick(tick, now_ns + 1)
            _assert_original((carry.mt5_long_swap, carry.mt5_short_swap), case["expected"])
            assert (carry.bitfinex_long, carry.bitfinex_short) == tuple(map(D, funding["expected"]))
            assert carry.total_trade_fee == _FEE

            expired = event.ts_event + strategy._config.max_cost_age_ns + 1
            strategy.clock.set_time(expired)
            engine.kernel.clock.set_time(expired)
            strategy.update_hedge_session(True, expired)
            engine.kernel.data_engine.process(_instrument(case, expired))
            assert strategy._hedge_instrument_valid and strategy._cost_ts_ns == event.ts_event
            assert not _live_inputs_ready(strategy, expired)
            fresh = _funding_event(client, funding["rate"], expired)
            engine.kernel.data_engine.process(fresh)
            assert strategy._cost_ts_ns == fresh.ts_event and _live_inputs_ready(strategy, expired)
            assert fake.sent == [] and not fake.opened
            engine.trader.stop()
    asyncio.run(run())


def test_missing_original_funding_is_not_a_current_zero_observation() -> None:
    async def run() -> None:
        case = _FUNDING["missing"]
        assert case["rate"] is None and tuple(map(D, case["expected"])) == (D(0), D(0))
        fake = _FakeTransport()
        with pytest.raises(BitfinexV1DataError):
            _funding_event(_client(fake), case["rate"], 1_000_000_000)
        assert fake.sent == [] and not fake.opened
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("season", ["summer", "winter"])
def test_native_quote_callback_uses_decision_day_with_authenticated_cross_day_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, season: str,
) -> None:
    async def run() -> None:
        before, after = _POINTS[f"{season}_before"], _POINTS[f"{season}_after"]
        quote_ns, decision_ns = _ns(before["utc"]), _ns(after["utc"])
        strategy = _strategy(kind, tmp_path / kind, quote_ns, no_source_sizes=True)
        observed: list[tuple[int, int, CarryConfig]] = []
        calculate = strategy._carry_for_hedge_tick

        def record(tick: Any, now_ns: int) -> Any:
            result = calculate(tick, now_ns)
            observed.append((tick.ts_event, now_ns, result))
            return result

        monkeypatch.setattr(strategy, "_carry_for_hedge_tick", record)
        with _event_engine(strategy) as engine:
            engine.kernel.clock.set_time(quote_ns)
            strategy.clock.set_time(quote_ns)
            engine.kernel.data_engine.process(_instrument(before, quote_ns))
            engine.trader.start()
            assert strategy.is_running
            fake = _FakeTransport()
            client = _client(fake, clock=engine.kernel.clock)
            event = _funding_event(client, _FUNDING["negative"]["rate"], quote_ns)
            engine.kernel.data_engine.process(event)
            source = _source_instrument()
            engine.kernel.data_engine.process(_quote(source, "3999", "4000", "5", quote_ns))
            engine.kernel.data_engine.process(_book_snapshot(source, "3999", "4000", "5", quote_ns))
            assert observed == []
            engine.kernel.clock.set_time(decision_ns)
            strategy.clock.set_time(decision_ns)
            engine.kernel.data_engine.process(_quote(
                _hedge_instrument(), "3999", "4000", "5", quote_ns,
            ))
            assert len(observed) == 1
            actual_quote_ns, actual_now_ns, carry = observed[0]
            assert (actual_quote_ns, actual_now_ns) == (quote_ns, decision_ns)
            _assert_original((carry.mt5_long_swap, carry.mt5_short_swap), after["expected"])
            assert carry.mt5_long_swap != D(before["expected"][0])
            assert (carry.bitfinex_long, carry.bitfinex_short) == tuple(
                map(D, _FUNDING["negative"]["expected"]),
            )
            assert carry.total_trade_fee == _FEE and strategy._cost_ts_ns == event.ts_event
            assert engine.cache.orders() == [] and fake.sent == [] and not fake.opened
            engine.trader.stop()
    asyncio.run(run())
