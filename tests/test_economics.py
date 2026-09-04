"""Named counterexamples for active-oracle Taker economics."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nautilus_trader.model.identifiers import AccountId

from py000_nautilus.config import CarryConfig, FxConfig, RiskConfig, TakerEconomicsConfig
from py000_nautilus.economics import (
    evaluate_taker,
    long_net_return,
    market_inputs_are_fresh,
    normalize_mt5_points_swap,
    round_hedge_ounces,
    select_source_account,
    short_net_return,
)
from py000_nautilus.models import BookTop, HedgeAccount, SourceAccount, SourceDirection

D = Decimal


def _utc_ns(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp()) * 1_000_000_000


def _normalized_swap(
    ts_utc_ns: int,
    *,
    timezone_name: str = "Europe/Athens",
    mode: int = 1,
    swap_rates: tuple[Decimal, ...] = (D(0), D(1), D(1), D(3), D(1), D(1), D(0)),
) -> tuple[Decimal, Decimal]:
    return normalize_mt5_points_swap(
        swap_long=D("-12.6"),
        swap_short=D("-4.6"),
        point=D("0.01"),
        ask=D("4000"),
        native_swap_mode=mode,
        swap_rates=swap_rates,
        now_ns=ts_utc_ns,
        server_timezone=timezone_name,
    )


def _config(
    *,
    long_threshold: str = "0.01",
    short_threshold: str = "0.01",
    only_long: bool = False,
) -> TakerEconomicsConfig:
    return TakerEconomicsConfig(
        base_book_quantity=D(1),
        open_quantity_long=D(4),
        open_quantity_short=D(4),
        threshold_long=D(long_threshold),
        threshold_short=D(short_threshold),
        margin_level=D(500),
        carry=CarryConfig(),
        fx=FxConfig(),
        risk=RiskConfig(
            source_max_abs=D(10),
            hedge_max_abs=D(10),
            source_min_keep_abs=D(1),
            hedge_min_keep_abs=D(1),
            only_long=only_long,
        ),
    )


def _account(name: str, position: str) -> SourceAccount:
    return SourceAccount(
        account_id=AccountId(name),
        client_id=None,
        position_ounces=D(position),
        max_long_ounces=D(5),
        max_short_ounces=D(5),
        base_margin_level=D(100),
    )


def test_long_direction_price_and_quantity_match_oracle_lines_1414_and_1466() -> None:
    opportunity = evaluate_taker(
        source_book=BookTop(D(100), D(101), D(9), D(3)),
        hedge_book=BookTop(D(103), D(104), D(9), D(9)),
        accounts=(_account("BITFINEX-001", "2"),),
        hedge=HedgeAccount(D(-2), D(5), D(5)),
        config=_config(),
    )
    assert opportunity is not None
    assert opportunity.direction is SourceDirection.LONG
    assert opportunity.source_price_usdt == D(101)
    assert opportunity.hedge_reference_price_usd == D(103)
    assert opportunity.source_quantity_ounces == D(3)
    assert opportunity.leverage == 16


def test_short_direction_uses_source_bid_and_mt5_ask() -> None:
    opportunity = evaluate_taker(
        source_book=BookTop(D(103), D(104), D(2), D(9)),
        hedge_book=BookTop(D(100), D(101), D(9), D(9)),
        accounts=(_account("BITFINEX-001", "2"),),
        hedge=HedgeAccount(D(-2), D(5), D(5)),
        config=_config(),
    )
    assert opportunity is not None
    assert opportunity.direction is SourceDirection.SHORT
    assert opportunity.source_price_usdt == D(103)
    assert opportunity.hedge_reference_price_usd == D(101)
    assert opportunity.source_quantity_ounces == D(2)


def test_direction_filter_preserves_default_short_first_and_can_select_long() -> None:
    source_book = BookTop(D(100), D(101), D(9), D(9))
    hedge_book = BookTop(D(100), D(101), D(9), D(9))
    accounts = (_account("BITFINEX-001", "0"),)
    hedge = HedgeAccount(D(0), D(5), D(5))
    config = _config(long_threshold="-1", short_threshold="-1")

    default = evaluate_taker(source_book, hedge_book, accounts, hedge, config)
    long_only = evaluate_taker(
        source_book,
        hedge_book,
        accounts,
        hedge,
        config,
        allowed_direction=SourceDirection.LONG,
    )
    short_only = evaluate_taker(
        source_book,
        hedge_book,
        accounts,
        hedge,
        config,
        allowed_direction=SourceDirection.SHORT,
    )

    assert default is not None and default.direction is SourceDirection.SHORT
    assert long_only is not None and long_only.direction is SourceDirection.LONG
    assert short_only is not None and short_only.direction is SourceDirection.SHORT


def test_threshold_is_strictly_greater_not_equal() -> None:
    exact = D(103) / D(101) - D(1)
    opportunity = evaluate_taker(
        source_book=BookTop(D(103), D(104), D(2), D(9)),
        hedge_book=BookTop(D(100), D(101), D(9), D(9)),
        accounts=(_account("BITFINEX-001", "2"),),
        hedge=HedgeAccount(D(-2), D(5), D(5)),
        config=_config(short_threshold=str(exact)),
    )
    assert opportunity is None


def test_migration_fx_boundary_and_oracle_carry_terms_are_side_specific() -> None:
    # The active oracle supplies already-comparable prices. USD/USDT bid/ask conversion is an
    # explicit migration requirement, not a claim that the legacy script performed this step.
    config = TakerEconomicsConfig(
        base_book_quantity=D(1),
        open_quantity_long=D(1),
        open_quantity_short=D(1),
        threshold_long=D(0),
        threshold_short=D(0),
        margin_level=D(500),
        carry=CarryConfig(
            bitfinex_long=D("0.001"),
            bitfinex_short=D("0.002"),
            mt5_long_swap=D("0.0004"),
            mt5_short_swap=D("0.0003"),
            total_trade_fee=D("0.0005"),
        ),
        fx=FxConfig(usd_usdt_bid=D("0.999"), usd_usdt_ask=D("1.001")),
        risk=RiskConfig(source_max_abs=D(10), hedge_max_abs=D(10)),
    )
    assert long_net_return(D(100), D(102), config) == (
        D(102) * D("0.999") / D(100) - 1 - D("0.001") + D("0.0003") - D("0.0005")
    )
    assert short_net_return(D(102), D(100), config) == (
        D(102) / (D(100) * D("1.001"))
        - 1
        - D("0.002")
        + D("0.0004")
        - D("0.0005")
    )


def test_mt5_points_swap_keeps_both_signed_negative_rates() -> None:
    # Monday: one normal rollover charge.
    assert _normalized_swap(_utc_ns(2026, 8, 31)) == (
        D("-0.0000315"),
        D("-0.0000115"),
    )


def test_mt5_points_swap_uses_three_on_the_mql_rollover_weekday() -> None:
    # 2026-09-02 is Wednesday, which is MQL weekday 3.
    assert _normalized_swap(_utc_ns(2026, 9, 2)) == (
        D("-0.0000945"),
        D("-0.0000345"),
    )


def test_mt5_points_swap_uses_broker_native_weekend_multiplier_without_inference() -> None:
    # Saturday is MQL weekday 6. A deliberately unusual native 3x proves
    # that normalization indexes the native vector rather than inferring weekends.
    assert _normalized_swap(
        _utc_ns(2026, 9, 5),
        swap_rates=(D(0), D(1), D(1), D(1), D(1), D(1), D(3)),
    ) == (D("-0.0000945"), D("-0.0000345"))


def test_mt5_points_swap_uses_the_server_timezone_for_the_rollover_day() -> None:
    # Tuesday 22:00 UTC is already Wednesday in Europe/Athens during summer time.
    timestamp = _utc_ns(2026, 9, 1, 22)
    athens = _normalized_swap(timestamp, timezone_name="Europe/Athens")
    utc = _normalized_swap(timestamp, timezone_name="UTC")
    assert athens == (D("-0.0000945"), D("-0.0000345"))
    assert utc == (D("-0.0000315"), D("-0.0000115"))


def test_mt5_disabled_swap_is_explicit_zero_and_unknown_mode_fails_closed() -> None:
    timestamp = _utc_ns(2026, 9, 2)
    assert _normalized_swap(timestamp, mode=0) == (D(0), D(0))
    with pytest.raises(ValueError, match=r"DISABLED\(0\) or POINTS\(1\)"):
        _normalized_swap(timestamp, mode=2)


def test_mt5_points_swap_rejects_invalid_timezone_and_inputs() -> None:
    timestamp = _utc_ns(2026, 9, 2)
    with pytest.raises(ValueError, match="IANA timezone"):
        _normalized_swap(timestamp, timezone_name="Not/A_Real_Zone")
    with pytest.raises(ValueError, match="exactly seven"):
        _normalized_swap(timestamp, swap_rates=(D(0),) * 6)
    with pytest.raises(ValueError, match=r"swap_rates\[3\].*finite Decimal"):
        _normalized_swap(
            timestamp,
            swap_rates=(D(0), D(1), D(1), D("NaN"), D(1), D(1), D(0)),
        )
    with pytest.raises(ValueError, match="finite Decimal"):
        normalize_mt5_points_swap(
            swap_long=D("NaN"),
            swap_short=D("-4.6"),
            point=D("0.01"),
            ask=D("4000"),
            native_swap_mode=1,
            swap_rates=(D(0), D(1), D(1), D(3), D(1), D(1), D(0)),
            now_ns=timestamp,
            server_timezone="Europe/Athens",
        )


def test_account_selection_matches_legacy_sorted_first_and_last() -> None:
    accounts = (
        _account("BITFINEX-001", "3"),
        _account("BITFINEX-002", "-4"),
        _account("BITFINEX-003", "8"),
    )
    assert select_source_account(accounts, SourceDirection.LONG).account_id == AccountId(
        "BITFINEX-002"
    )
    assert select_source_account(accounts, SourceDirection.SHORT).account_id == AccountId(
        "BITFINEX-003"
    )


def test_l1_price_is_rejected_when_top_size_cannot_cover_base_depth_quantity() -> None:
    opportunity = evaluate_taker(
        source_book=BookTop(D(100), D(101), D("0.5"), D("0.5")),
        hedge_book=BookTop(D(103), D(104), D(9), D(9)),
        accounts=(_account("BITFINEX-001", "2"),),
        hedge=HedgeAccount(D(-2), D(5), D(5)),
        config=_config(),
    )
    assert opportunity is None


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("session_open", False),
        ("source_ts_ns", 1),
        ("hedge_ts_ns", 1),
        ("cost_ts_ns", 1),
        ("session_ts_ns", 1),
        ("hedge_ts_ns", 7),
        ("source_ts_ns", 11),
    ],
)
def test_stale_skewed_closed_or_future_inputs_fail_closed(change: str, value: int | bool) -> None:
    inputs: dict[str, int | bool] = {
        "now_ns": 10,
        "source_ts_ns": 9,
        "hedge_ts_ns": 9,
        "cost_ts_ns": 9,
        "session_ts_ns": 9,
        "session_open": True,
        "max_quote_age_ns": 2,
        "max_cross_leg_skew_ns": 1,
        "max_cost_age_ns": 2,
        "max_session_age_ns": 2,
    }
    inputs[change] = value
    assert not market_inputs_are_fresh(**inputs)  # type: ignore[arg-type]


def test_current_open_cross_leg_inputs_pass_freshness_gate() -> None:
    assert market_inputs_are_fresh(
        now_ns=10,
        source_ts_ns=9,
        hedge_ts_ns=9,
        cost_ts_ns=9,
        session_ts_ns=9,
        session_open=True,
        max_quote_age_ns=2,
        max_cross_leg_skew_ns=1,
        max_cost_age_ns=2,
        max_session_age_ns=2,
    )


def test_only_long_rejects_crossing_zero_and_max_limit_rejects_growing_risk() -> None:
    crossing = evaluate_taker(
        source_book=BookTop(D(100), D(101), D(9), D(4)),
        hedge_book=BookTop(D(103), D(104), D(9), D(9)),
        accounts=(_account("BITFINEX-001", "-2"),),
        hedge=HedgeAccount(D(2), D(5), D(5)),
        config=_config(only_long=True),
    )
    assert crossing is None

    over_limit_config = _config()
    account = SourceAccount(
        account_id=AccountId("BITFINEX-001"),
        client_id=None,
        position_ounces=D(9),
        max_long_ounces=D(5),
        max_short_ounces=D(5),
        base_margin_level=D(100),
    )
    over_limit = evaluate_taker(
        source_book=BookTop(D(100), D(101), D(9), D(4)),
        hedge_book=BookTop(D(103), D(104), D(9), D(9)),
        accounts=(account,),
        hedge=HedgeAccount(D(-2), D(5), D(5)),
        config=over_limit_config,
    )
    assert over_limit is None


@pytest.mark.parametrize(("ounces", "expected"), [("0.5", 0), ("1.5", 2), ("2.5", 2)])
def test_legacy_bankers_rounding_counterexamples(ounces: str, expected: int) -> None:
    assert round_hedge_ounces(D(ounces)) == expected
