from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from nautilus_trader.model.data import FundingRateUpdate

from py000_nautilus.app import HEDGE_ID, SOURCE_ID
from py000_nautilus.config import CarryConfig, FxConfig
from py000_nautilus.strategies.taker import TakerStrategy


class _Clock:
    def __init__(self, now_ns: int) -> None:
        self.now_ns = now_ns

    def timestamp_ns(self) -> int:
        return self.now_ns


class _Log:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)


class _FundingHarness:
    def __init__(self, *, now_ns: int = 100) -> None:
        static = CarryConfig(total_trade_fee=Decimal("0.00065"))
        self._config = SimpleNamespace(
            source_instrument_id=SOURCE_ID,
            hedge_instrument_id=HEDGE_ID,
            economics=SimpleNamespace(carry=static, fx=FxConfig()),
            max_quote_age_ns=10,
            max_cross_leg_skew_ns=10,
            max_cost_age_ns=10,
            max_session_age_ns=10,
        )
        self._live_costs_from_adapters = True
        self._hedge_instrument_valid = True
        self._hedge_instrument = SimpleNamespace(ts_event=now_ns - 1)
        self._live_submission_ready = None
        self._one_shot = False
        self._one_shot_armed = False
        self._one_shot_claimed = False
        self._carry = static
        self._fx = FxConfig()
        self._cost_ts_ns = 0
        self._cost_snapshot_valid = False
        self._cost_recovery_after_ns = 0
        self._hedge_session_open = True
        self._session_ts_ns = now_ns - 1
        self.clock = _Clock(now_ns)
        self._hedge_tick = SimpleNamespace(ts_event=now_ns - 1)
        self.cache = SimpleNamespace(quote_tick=lambda _instrument_id: self._hedge_tick)
        self.log = _Log()

    def _required_hedge_instrument(self) -> Any:
        return self._hedge_instrument

    def update_cost_snapshot(
        self,
        carry: CarryConfig,
        fx: FxConfig,
        ts_event_ns: int,
    ) -> bool:
        return TakerStrategy.update_cost_snapshot(cast(Any, self), carry, fx, ts_event_ns)

    def _invalidate_cost_snapshot(self) -> None:
        TakerStrategy._invalidate_cost_snapshot(cast(Any, self))

    def on_funding_rate(self, update: FundingRateUpdate) -> None:
        TakerStrategy.on_funding_rate(cast(Any, self), update)

    def inputs_are_fresh(self, source_ts_ns: int, hedge_ts_ns: int) -> bool:
        tick = cast(Any, SimpleNamespace(ts_event=hedge_ts_ns))
        return TakerStrategy._inputs_are_fresh(
            cast(Any, self),
            source_ts_ns,
            tick,
            self.clock.now_ns,
        )

    def source_independent_inputs_ready(self) -> bool:
        return TakerStrategy.source_independent_inputs_ready(cast(Any, self))


def _funding(rate: str, ts_event: int, *, instrument_id: Any = SOURCE_ID) -> FundingRateUpdate:
    return FundingRateUpdate(
        instrument_id=instrument_id,
        rate=Decimal(rate),
        ts_event=ts_event,
        ts_init=ts_event,
    )


def test_live_funding_maps_one_signed_rate_and_preserves_static_fee_and_fx() -> None:
    harness = _FundingHarness()

    harness.on_funding_rate(_funding("-0.0007366", 99))

    assert harness._carry == CarryConfig(
        bitfinex_long=Decimal("-0.0007366"),
        bitfinex_short=Decimal("0.0007366"),
        total_trade_fee=Decimal("0.00065"),
    )
    assert harness._fx == FxConfig()
    assert harness._cost_ts_ns == 99
    assert harness._cost_snapshot_valid
    assert harness.inputs_are_fresh(99, 99)


def test_wrong_instrument_future_or_invalid_funding_cannot_open_admission() -> None:
    harness = _FundingHarness()
    other = type(SOURCE_ID).from_str("OTHER.BITFINEX")

    harness.on_funding_rate(_funding("0.1", 99, instrument_id=other))
    harness.on_funding_rate(_funding("0.1", 101))
    harness.on_funding_rate(_funding("1.1", 99))

    assert harness._cost_ts_ns == 0
    assert not harness._cost_snapshot_valid
    assert not harness.inputs_are_fresh(99, 99)
    assert harness.log.errors == [
        "Bitfinex funding observation is future-dated",
        "Bitfinex funding observation is invalid",
    ]


def test_funding_freshness_ages_out_without_fabricating_refreshes() -> None:
    harness = _FundingHarness(now_ns=100)
    harness.on_funding_rate(_funding("0.0001", 99))
    assert harness.inputs_are_fresh(99, 99)

    harness.clock.now_ns = 110
    harness._session_ts_ns = 109
    harness._hedge_instrument.ts_event = 109
    assert not harness.inputs_are_fresh(109, 109)

    harness.on_funding_rate(_funding("0.0002", 109))
    assert harness.inputs_are_fresh(109, 109)


def test_source_independent_inputs_can_be_ready_before_source_subscription() -> None:
    harness = _FundingHarness()
    assert not harness.source_independent_inputs_ready()

    harness.on_funding_rate(_funding("0.0001", 99))
    assert harness.source_independent_inputs_ready()

    harness._hedge_session_open = False
    assert not harness.source_independent_inputs_ready()


class _SwapHarness:
    def __init__(self, *, live: bool) -> None:
        self._live_costs_from_adapters = live
        self._carry = CarryConfig(
            bitfinex_long=Decimal("-0.0007366"),
            bitfinex_short=Decimal("0.0007366"),
            total_trade_fee=Decimal("0.00065"),
        )
        self._config = SimpleNamespace(economics=SimpleNamespace(carry=self._carry))
        self.log = _Log()

    def _mt5_swap_spec(
        self,
    ) -> tuple[Decimal, Decimal, Decimal, int, tuple[Decimal, ...], str]:
        return (
            Decimal("-12.6"),
            Decimal("-4.6"),
            Decimal("0.01"),
            1,
            (
                Decimal(0),
                Decimal(1),
                Decimal(1),
                Decimal(3),
                Decimal(1),
                Decimal(1),
                Decimal(0),
            ),
            "Europe/Athens",
        )

    def carry_for(self, *, ask: str, quote_ts: int, now_ns: int) -> CarryConfig | None:
        tick = cast(Any, SimpleNamespace(ask_price=ask, ts_event=quote_ts))
        return TakerStrategy._carry_for_hedge_tick(cast(Any, self), tick, now_ns)


def test_live_decision_normalizes_mt5_swap_while_offline_keeps_configured_carry() -> None:
    # 2026-09-03 12:00 UTC is Thursday in Europe/Athens, so the multiplier is one.
    thursday_ns = 1_788_439_200_000_000_000
    live = _SwapHarness(live=True)
    carry = live.carry_for(ask="4000", quote_ts=thursday_ns, now_ns=thursday_ns)
    assert carry == CarryConfig(
        bitfinex_long=Decimal("-0.0007366"),
        bitfinex_short=Decimal("0.0007366"),
        mt5_long_swap=Decimal("-0.0000315"),
        mt5_short_swap=Decimal("-0.0000115"),
        total_trade_fee=Decimal("0.00065"),
    )

    offline = _SwapHarness(live=False)
    assert offline.carry_for(ask="0", quote_ts=0, now_ns=0) is offline._carry


def test_live_swap_day_uses_decision_now_not_the_previous_days_quote_timestamp() -> None:
    # At 21:00 UTC Athens advances from Tuesday to Wednesday. The quote was observed
    # one minute before the boundary, but this decision must use Wednesday's native 3x.
    quote_ts = int(datetime(2026, 9, 1, 20, 59, tzinfo=UTC).timestamp()) * 1_000_000_000
    now_ns = int(datetime(2026, 9, 1, 21, 0, tzinfo=UTC).timestamp()) * 1_000_000_000

    carry = _SwapHarness(live=True).carry_for(
        ask="4000",
        quote_ts=quote_ts,
        now_ns=now_ns,
    )

    assert carry is not None
    assert carry.mt5_long_swap == Decimal("-0.0000945")
    assert carry.mt5_short_swap == Decimal("-0.0000345")


def test_book_decision_captures_now_once_for_freshness_and_swap_day(
    monkeypatch: Any,
) -> None:
    class _CountingClock:
        def __init__(self) -> None:
            self.calls = 0

        def timestamp_ns(self) -> int:
            self.calls += 1
            return 123_456_789

    clock = _CountingClock()
    observed: list[tuple[str, int]] = []
    source_book = SimpleNamespace(ts_last=123_456_788)
    hedge_tick = SimpleNamespace(
        bid_price=Decimal(3999),
        ask_price=Decimal(4000),
        bid_size=Decimal(10),
        ask_size=Decimal(10),
        ts_event=123_456_788,
    )

    def inputs_are_fresh(source_ts: int, tick: Any, now_ns: int) -> bool:
        observed.append(("fresh", now_ns))
        return True

    def carry_for_hedge_tick(tick: Any, now_ns: int) -> CarryConfig:
        observed.append(("swap", now_ns))
        return CarryConfig()

    harness = SimpleNamespace(
        _config=SimpleNamespace(
            source_instrument_id=SOURCE_ID,
            hedge_instrument_id=HEDGE_ID,
            economics=SimpleNamespace(base_book_quantity=Decimal(1)),
        ),
        _allowed_source_direction=None,
        _live_account_reader=None,
        state_store=SimpleNamespace(can_submit_source=lambda: True),
        cache=SimpleNamespace(
            order_book=lambda instrument_id: source_book,
            quote_tick=lambda instrument_id: hedge_tick,
        ),
        clock=clock,
        _fx=FxConfig(),
        _inputs_are_fresh=inputs_are_fresh,
        _carry_for_hedge_tick=carry_for_hedge_tick,
        _source_accounts=lambda: (),
        _hedge_account=lambda: object(),
        _submit_source=lambda opportunity: None,
        _source_book_callback_count=0,
        _last_source_attempt_market_ts_ns=None,
        _last_decision_gate="not_started",
    )
    monkeypatch.setattr(
        "py000_nautilus.strategies.taker._reference_book",
        lambda book, quantity: object(),
    )
    monkeypatch.setattr(
        "py000_nautilus.strategies.taker.evaluate_taker",
        lambda **kwargs: None,
    )
    harness._evaluate_and_submit = lambda: TakerStrategy._evaluate_and_submit(cast(Any, harness))

    TakerStrategy.on_order_book_deltas(
        cast(Any, harness),
        cast(Any, SimpleNamespace(instrument_id=SOURCE_ID, ts_event=123_456_788)),
    )

    assert clock.calls == 1
    assert observed == [("fresh", 123_456_789), ("swap", 123_456_789)]
    assert harness._source_book_callback_count == 1
    assert harness._last_decision_gate == "not_qualifying"
