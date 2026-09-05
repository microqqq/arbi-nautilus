"""Authenticated callee vectors and explicit normalized migration boundaries.

These tests do not authenticate accounts, infer flat state, check freshness,
or grant order admission. The old modules/ZIP are not loaded by this suite.
"""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from py000_nautilus.margin import bitfinex_margin_capacity, mt5_margin_capacity

D = Decimal
_FIXTURE = Path(__file__).parent / "fixtures" / "legacy_margin_vectors.json"
_COLUMNS = (
    "expect", "base_ml", "vol", "price", "dur", "equity", "free_margin", "collateral",
    "pl", "avail", "bfx_first", "bfx_second", "mt5_first", "mt5_second",
)
_BFX_INPUT = {
    "margin_target": D(400), "base_margin_level": D(100), "position_ounces": D(0),
    "reference_price": D(4000), "collateral": D(0), "unrealized_pnl": D(0),
    "available_balance": D(1000), "free_margin": D(1000),
}
_MT5_INPUT = {
    "margin_target": D(400), "base_margin_level": D(60), "position_ounces": D(0),
    "ask": D(4000), "equity": D(1000),
}


@pytest.fixture(scope="module")
def oracle_vectors() -> list[dict[str, Decimal]]:
    payload = json.loads(_FIXTURE.read_text())
    metadata = payload["metadata"]
    assert metadata["schema_version"] == 1
    assert metadata["archive"]["sha256"] == (
        "3e50dcc625b1f78048622f3bb5e5ee2f94eb8e4c4635f39108fcb09f6e3d1892"
    )
    assert metadata["original_vector"] == {
        "name": "margin_golden_vectors.json",
        "sha256": "b2b084b3a1ee25db3eb998a17428ee0d92d3af5e393ebe4a5649c3dd4c6caf3f",
        "case_count": 2000,
    }
    assert [method["method_segment_sha256"] for method in metadata["methods"]] == [
        "4e22fa431dd519dde5bea5b5fd75be9502920bd56721464ca90c5d1e3d24c12e",
        "b3acf7db4ed91b044e4b0888e9da37a77c85a1f923cf4136fac5ab4ebb5952d7",
    ]
    assert metadata["absolute_tolerance"] == "0.000000001"
    assert tuple(payload["columns"]) == _COLUMNS
    assert len(payload["rows"]) == 2000
    return [dict(zip(_COLUMNS, map(D, row), strict=True)) for row in payload["rows"]]


@pytest.mark.parametrize("venue", ["bfx", "mt5"])
def test_authenticated_normalized_callee_vectors(
    oracle_vectors: list[dict[str, Decimal]], venue: str,
) -> None:
    for index, row in enumerate(oracle_vectors):
        if venue == "bfx":
            actual = bitfinex_margin_capacity(
                margin_target=row["expect"], base_margin_level=row["base_ml"],
                position_ounces=row["vol"], reference_price=row["price"],
                collateral=row["collateral"], unrealized_pnl=row["pl"],
                available_balance=row["avail"], free_margin=row["free_margin"],
            )
        else:
            actual = mt5_margin_capacity(
                margin_target=row["expect"], base_margin_level=row["base_ml"],
                position_ounces=row["vol"], ask=row["price"], equity=row["equity"],
            )
        assert row["dur"] in (D(1), D(-1))
        original = (row[f"{venue}_first"], row[f"{venue}_second"])
        expected = original if row["dur"] == 1 else original[::-1]
        assert all(isinstance(value, D) for value in actual)
        assert all(abs(a - e) <= D("1e-9") for a, e in zip(actual, expected, strict=True)), (
            index, venue, actual, expected,
        )


def test_explicit_normalized_flat_is_not_account_authentication() -> None:
    # The original BFX flat account producer divides by zero. These fully
    # supplied values are the migration input, NOT evidence that an account is flat.
    assert bitfinex_margin_capacity(**_BFX_INPUT) == (D(5), D(5))
    assert mt5_margin_capacity(**_MT5_INPUT) == (D(5), D(5))


def test_reverse_positions_return_physical_buy_sell_not_legacy_duration_order() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "position_ounces": D(-3), "collateral": D(600),
        "available_balance": D(400), "free_margin": D(400),
    }) == (D(8), D(2))
    # Original MT5 duration=-1 returns (8,2); physical BUY can add only 2 oz.
    assert mt5_margin_capacity(**{**_MT5_INPUT, "position_ounces": D(3)}) == (D(2), D(8))


def test_near_margin_target_preserves_reducing_direction() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "position_ounces": D(4), "collateral": D(800),
        "available_balance": D(30), "free_margin": D(30),
    }) == (D(0), D(8))
    assert mt5_margin_capacity(**{
        **_MT5_INPUT, "position_ounces": D(-4), "equity": D(830),
    }) == (D(8), D(0))


def test_bitfinex_free_margin_bound_is_not_replaced_by_total_equity() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "position_ounces": D(3), "collateral": D(1000),
        "available_balance": D(0), "free_margin": D(0),
    }) == (D(0), D(6))


def test_mt5_keeps_fractional_leverage_while_bitfinex_floors_it() -> None:
    assert mt5_margin_capacity(**{
        **_MT5_INPUT, "margin_target": D(500), "base_margin_level": D(100),
        "equity": D(240),
    }) == (D(1), D(1))
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "margin_target": D(500),
        "available_balance": D(240), "free_margin": D(240),
    }) == (D(0), D(0))


def test_mt5_exact_one_ounce_does_not_round_repeating_leverage_first() -> None:
    assert mt5_margin_capacity(**{**_MT5_INPUT, "equity": D(184)}) == (D(1), D(1))


@pytest.mark.parametrize("target", [100, 150, 200, 300, 400, 500, 1000])
@pytest.mark.parametrize("ask", [2000, 4000, 4100])
def test_mt5_realistic_integer_holding_boundaries(target: int, ask: int) -> None:
    for holding_limit in range(1, 13):
        equity = D(holding_limit) * (D(target) + D(60)) * D(ask) / D(10000)
        for position in (D("-0.4"), D(0), D("0.4"), D(3)):
            actual = mt5_margin_capacity(**{
                **_MT5_INPUT, "margin_target": D(target), "ask": D(ask),
                "equity": equity, "position_ounces": position,
            })
            expected = (
                max(D(holding_limit) - position, D(0)),
                max(D(holding_limit) + position, D(0)),
            )
            assert actual == expected, (target, ask, holding_limit, position, actual)


@pytest.mark.parametrize(
    ("equity", "holding_limit"),
    [("183.999999999999", 0), ("184.000000000001", 1)],
)
def test_mt5_realistic_integer_boundary_both_sides(equity: str, holding_limit: int) -> None:
    for position in (D("-0.4"), D(0), D("0.4"), D(3)):
        assert mt5_margin_capacity(**{
            **_MT5_INPUT, "equity": D(equity), "position_ounces": position,
        }) == (
            max(D(holding_limit) - position, D(0)),
            max(D(holding_limit) + position, D(0)),
        )


def test_bitfinex_zero_leverage_is_not_raised_to_one() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "margin_target": D(10000), "position_ounces": D("2.25"),
        "available_balance": D(100000), "free_margin": D(100000),
    }) == (D(0), D("2.25"))


@pytest.mark.parametrize("position", ["0.4", "-0.4"])
def test_fractional_net_position_is_not_rounded_by_account_producer(position: str) -> None:
    q = D(position)
    expected = (D(5) - q, D(5) + q)
    assert bitfinex_margin_capacity(**{**_BFX_INPUT, "position_ounces": q}) == expected
    assert mt5_margin_capacity(**{**_MT5_INPUT, "position_ounces": q}) == expected


@pytest.mark.parametrize(
    ("available", "expected"),
    [("0.999999999999999999", "0"), ("1", "1"), ("1.000000000000000001", "1")],
)
def test_decimal_hold_floor_does_not_round_through_float(available: str, expected: str) -> None:
    common = {"margin_target": D(9900), "base_margin_level": D(100)}
    result = (D(expected), D(expected))
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, **common, "reference_price": D(1),
        "available_balance": D(available), "free_margin": D(available),
    }) == result
    assert mt5_margin_capacity(**{
        **_MT5_INPUT, **common, "ask": D(1), "equity": D(available),
    }) == result


@pytest.mark.parametrize("field", ["collateral", "unrealized_pnl", "available_balance"])
def test_finite_negative_bitfinex_value_keeps_original_floor_and_final_clamp(field: str) -> None:
    values = {
        **_BFX_INPUT, "position_ounces": D(3), "collateral": D(0),
        "unrealized_pnl": D(0), "available_balance": D(0), "free_margin": D(0),
        field: D(-1),
    }
    assert bitfinex_margin_capacity(**values) == (D(0), D(2))


def test_finite_negative_free_margin_is_not_silently_clamped_to_zero() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "position_ounces": D(3), "free_margin": D(-1),
    }) == (D(0), D(5))


def test_finite_negative_mt5_equity_keeps_original_floor_and_final_clamp() -> None:
    assert mt5_margin_capacity(**{
        **_MT5_INPUT, "position_ounces": D(3), "equity": D(-1),
    }) == (D(0), D(2))


@pytest.mark.parametrize(
    ("target", "base"), [("-400", "900"), ("600", "-100")],
)
def test_finite_margin_components_require_positive_sum_not_individual_risk_policy(
    target: str, base: str,
) -> None:
    values = {"margin_target": D(target), "base_margin_level": D(base)}
    assert bitfinex_margin_capacity(**{**_BFX_INPUT, **values}) == (D(5), D(5))
    assert mt5_margin_capacity(**{**_MT5_INPUT, **values}) == (D(5), D(5))


_FUNCTIONS: tuple[
    tuple[Callable[..., tuple[Decimal, Decimal]], dict[str, Decimal]], ...
] = ((bitfinex_margin_capacity, _BFX_INPUT), (mt5_margin_capacity, _MT5_INPUT))


@pytest.mark.parametrize(("calculate", "values"), _FUNCTIONS, ids=["bitfinex", "mt5"])
@pytest.mark.parametrize(
    "invalid", [None, True, 1, 1.0, "1", D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity")],
)
def test_every_input_requires_a_finite_decimal(
    calculate: Callable[..., tuple[Decimal, Decimal]], values: dict[str, Decimal], invalid: object,
) -> None:
    for field in values:
        with pytest.raises(ValueError, match=field):
            calculate(**{**values, field: cast(Decimal, invalid)})


@pytest.mark.parametrize(("calculate", "values"), _FUNCTIONS, ids=["bitfinex", "mt5"])
@pytest.mark.parametrize("target", ["-100", "-101"])
def test_nonpositive_combined_margin_is_rejected(
    calculate: Callable[..., tuple[Decimal, Decimal]], values: dict[str, Decimal], target: str,
) -> None:
    with pytest.raises(ValueError, match="combined margin"):
        calculate(**{**values, "margin_target": D(target), "base_margin_level": D(100)})


@pytest.mark.parametrize("price", ["0", "-1"])
def test_nonpositive_price_is_rejected(price: str) -> None:
    with pytest.raises(ValueError, match="reference_price"):
        bitfinex_margin_capacity(**{**_BFX_INPUT, "reference_price": D(price)})
    with pytest.raises(ValueError, match="ask"):
        mt5_margin_capacity(**{**_MT5_INPUT, "ask": D(price)})


@pytest.mark.parametrize(("calculate", "values"), _FUNCTIONS, ids=["bitfinex", "mt5"])
def test_all_normalized_values_are_required_arguments(
    calculate: Callable[..., tuple[Decimal, Decimal]], values: dict[str, Decimal],
) -> None:
    for field in values:
        with pytest.raises(TypeError, match=field):
            calculate(**{name: value for name, value in values.items() if name != field})
