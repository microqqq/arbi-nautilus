"""Named counterexamples for active-oracle Taker economics."""

from decimal import Decimal

import pytest
from nautilus_trader.model.identifiers import AccountId

from py000_nautilus.config import CarryConfig, FxConfig, RiskConfig, TakerEconomicsConfig
from py000_nautilus.economics import (
    evaluate_taker,
    long_net_return,
    market_inputs_are_fresh,
    round_hedge_ounces,
    select_source_account,
    short_net_return,
)
from py000_nautilus.models import BookTop, HedgeAccount, SourceAccount, SourceDirection

D = Decimal


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
