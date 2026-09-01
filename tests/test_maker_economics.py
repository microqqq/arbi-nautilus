"""Named counterexamples for the active-oracle Maker economics."""

from decimal import Decimal

import pytest
from nautilus_trader.model.identifiers import AccountId

from py000_nautilus.config import (
    CarryConfig,
    FxConfig,
    MakerEconomicsConfig,
    MakerSideConfig,
    RiskConfig,
)
from py000_nautilus.economics import expected_leverage
from py000_nautilus.maker_economics import (
    maker_adjusted_spread,
    maker_quote,
    passive_maker_price,
)
from py000_nautilus.models import BookTop, MakerAccount, SourceAccount, SourceDirection
from py000_nautilus.strategies.maker import _requote_required

D = Decimal


def _config(*, only_long: bool = False) -> MakerEconomicsConfig:
    return MakerEconomicsConfig(
        bid=MakerSideConfig(
            open_quantity_ounces=D(1),
            open_spread=D("0.07"),
            delta=D("0.01"),
        ),
        ask=MakerSideConfig(
            open_quantity_ounces=D(1),
            open_spread=D("0.07"),
            delta=D("0.01"),
        ),
        margin_level=D(500),
        carry=CarryConfig(
            bitfinex_long=D("0.02"),
            bitfinex_short=D("0.03"),
            mt5_long_swap=D("0.02"),
            mt5_short_swap=D("0.01"),
            total_trade_fee=D("0.01"),
        ),
        fx=FxConfig(),
        risk=RiskConfig(
            source_max_abs=D(10),
            hedge_max_abs=D(10),
            only_long=only_long,
        ),
    )


def _source(name: str, position: str) -> SourceAccount:
    return SourceAccount(
        account_id=AccountId(name),
        client_id=None,
        position_ounces=D(position),
        max_long_ounces=D(5),
        max_short_ounces=D(5),
        base_margin_level=D(100),
    )


def _hedge(name: str, position: str) -> MakerAccount:
    return MakerAccount(
        account_id=AccountId(name),
        client_id=None,
        position_ounces=D(position),
        max_long_ounces=D(5),
        max_short_ounces=D(5),
    )


def test_bid_and_ask_use_owner_frozen_multiplication_not_bid_division() -> None:
    """The active caller proves inputs; the final multiplication is review-attested."""
    config = _config()
    book = BookTop(D(1000), D(1001), D(10), D(10))
    source = (_source("BITFINEX-001", "0"),)
    hedge = (_hedge("MT5-001", "0"),)

    bid = maker_quote(SourceDirection.LONG, book, source, hedge, config)
    ask = maker_quote(SourceDirection.SHORT, book, source, hedge, config)

    assert bid is not None and ask is not None
    assert bid.adjusted_spread == D("0.10")
    assert ask.adjusted_spread == D("0.10")
    assert bid.source_price_usdt == D("900.0")
    assert bid.source_price_usdt != D(1000) / D("1.10")
    assert ask.source_price_usdt == D("1101.10")
    assert bid.leverage == 16


def test_adjusted_spread_maps_funding_and_swap_by_side() -> None:
    config = _config()
    assert maker_adjusted_spread(SourceDirection.LONG, config) == D("0.10")
    assert maker_adjusted_spread(SourceDirection.SHORT, config) == D("0.10")


def test_account_selection_matches_mirrored_legacy_order() -> None:
    sources = (
        _source("BITFINEX-LOW", "-4"),
        _source("BITFINEX-MID", "2"),
        _source("BITFINEX-HIGH", "8"),
    )
    hedges = (
        _hedge("MT5-LOW", "-4"),
        _hedge("MT5-MID", "2"),
        _hedge("MT5-HIGH", "8"),
    )
    config = _config()
    book = BookTop(D(1000), D(1001), D(10), D(10))

    bid = maker_quote(SourceDirection.LONG, book, sources, hedges, config)
    ask = maker_quote(SourceDirection.SHORT, book, sources, hedges, config)
    assert bid is not None and ask is not None
    assert bid.source_account.account_id == AccountId("BITFINEX-LOW")
    assert bid.hedge_account.account_id == AccountId("MT5-HIGH")
    assert ask.source_account.account_id == AccountId("BITFINEX-HIGH")
    assert ask.hedge_account.account_id == AccountId("MT5-LOW")


def test_capacity_and_only_long_gates_reject_unsafe_pair() -> None:
    book = BookTop(D(1000), D(1001), D(10), D(10))
    assert maker_quote(
        SourceDirection.LONG,
        book,
        (_source("BITFINEX-001", "-0.5"),),
        (_hedge("MT5-001", "0.5"),),
        _config(only_long=True),
    ) is None


def test_crossing_quotes_clamp_two_ticks_inside_touch_and_round_passively() -> None:
    source_book = BookTop(D("99.0"), D("100.0"), D(10), D(10))
    # The legacy absolute 0.2 is review-attested; 0.1 XAUT ticks make it two ticks.
    assert passive_maker_price(
        SourceDirection.LONG,
        D("105"),
        source_book,
        D("0.1"),
        2,
    ) == D("99.8")
    assert passive_maker_price(
        SourceDirection.SHORT,
        D("95"),
        source_book,
        D("0.1"),
        2,
    ) == D("99.2")
    assert passive_maker_price(
        SourceDirection.LONG,
        D("98.87"),
        source_book,
        D("0.1"),
        2,
    ) == D("98.8")
    assert passive_maker_price(
        SourceDirection.SHORT,
        D("100.12"),
        source_book,
        D("0.1"),
        2,
    ) == D("100.2")
    with pytest.raises(ValueError, match="positive"):
        passive_maker_price(
            SourceDirection.LONG,
            D(99),
            source_book,
            D("0.1"),
            0,
        )


def test_requote_requires_strictly_greater_relative_delta() -> None:
    assert not _requote_required(D(100), D(101), D("0.01"))
    assert _requote_required(D(100), D("101.01"), D("0.01"))


def test_leverage_floor_has_an_intentional_minimum_one_migration_guard() -> None:
    assert expected_leverage(D(500), D(100)) == 16
    assert expected_leverage(D(20_000), D(0)) == 1
