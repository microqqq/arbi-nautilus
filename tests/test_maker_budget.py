"""Bounded residual budgets are distinct from funding and from single hedge lots."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from msgspec.structs import replace

from py000_nautilus import maker_economics as economics
from py000_nautilus.app import _maker_strategy_config
from py000_nautilus.economics import round_hedge_ounces
from py000_nautilus.models import SourceDirection

D = Decimal


@pytest.mark.parametrize(("residual", "buy", "sell", "expected"), [
    ("0.5", "2", "0", ("2.5", "0", "3")),
    ("-0.5", "0", "2", ("2.5", "3", "0")),
    ("-0.5", "2", "0.1", ("2.5", "1", "2")),
    ("0.5", "0.1", "2", ("2.5", "2", "1")),
    ("-0.5", "1", "0", ("0.5", "0", "1")),
    ("-0.5", "0.5", "0", ("0.5", "0", "0")),
    ("0.4", "0", "0.2", ("0.4", "0", "0")),
    ("0", "2", "2", ("2.5", "2", "2")),
])
def test_carry_bounds_include_ordered_hedge_prefixes_not_only_initial_net(
    residual: str, buy: str, sell: str, expected: tuple[str, str, str],
) -> None:
    assert economics.maker_carry_bounds(D(residual), D(buy), D(sell)) == tuple(map(D, expected))


@pytest.mark.parametrize(("residual", "quantity", "direction", "opposite", "expected"), [
    ("0.5", "2", SourceDirection.LONG, False, "2"),
    ("0.5", "1", SourceDirection.LONG, False, "2"),
    ("-0.5", "1", SourceDirection.LONG, False, "0"),
    ("-0.5", "1", SourceDirection.LONG, True, "2"),
    ("0.5", "1", SourceDirection.SHORT, True, "2"),
    ("0.5", "2", SourceDirection.LONG, True, "2"),
])
def test_single_intent_bound_does_not_turn_cumulative_three_into_one_three_ounce_leg(
    residual: str, quantity: str, direction: SourceDirection, opposite: bool, expected: str,
) -> None:
    assert economics.maker_hedge_quantity_bound(
        D(residual), D(quantity), direction, opposite,
    ) == D(expected)


def test_original_rounding_demonstrates_split_fill_and_opposite_completed_hedge_bounds() -> None:
    residual, allocations = D("0.5"), []
    for fill in (D("0.2"), D("1.8")):
        allocated = round_hedge_ounces(residual + fill)
        residual += fill - D(allocated)
        allocations.append(allocated)
    assert allocations == [1, 2] and residual == D("-0.5")
    residual = D("-0.5")
    sell_allocated = D(round_hedge_ounces(residual - D("0.1")))
    residual -= D("0.1") + sell_allocated
    assert sell_allocated == -1 and residual == D("0.4")
    assert residual + D(2) == D("2.4")  # BUY1 hedge done, then late BUY2 source.


@pytest.mark.parametrize("options", [
    {"residual_mode": "other"},
    {"residual_limit_ounces": D("0.5")},
    {"residual_mode": "bounded-carry"},
    {"residual_mode": "bounded-carry", "residual_limit_ounces": D("0.5")},
    {"residual_mode": "bounded-carry", "residual_limit_ounces": D("0.51"),
     "max_unhedged_ounces": D(3)},
    {"residual_mode": "bounded-carry", "residual_limit_ounces": D("NaN"),
     "max_unhedged_ounces": D(3)},
    {"residual_mode": "bounded-carry", "residual_limit_ounces": D("0.5"),
     "max_unhedged_ounces": D(0)},
    {"residual_mode": "bounded-carry", "residual_limit_ounces": D("0.5"),
     "max_unhedged_ounces": D("Infinity")},
])
def test_bounded_carry_requires_explicit_valid_config(
    tmp_path: Path, options: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        replace(_maker_strategy_config(tmp_path / "config"), **options)


def test_bounded_carry_is_single_route_opt_in_and_funding_config_is_unchanged(
    tmp_path: Path,
) -> None:
    config = _maker_strategy_config(tmp_path / "config")
    assert config.residual_mode == "strict" and config.residual_limit_ounces == 0
    assert config.max_unhedged_ounces is None
    bounded = replace(config, residual_mode="bounded-carry", residual_limit_ounces=D("0.5"),
                      max_unhedged_ounces=D("2.5"))
    assert bounded.economics.carry == config.economics.carry
    for fields in ({"source_accounts": ()}, {"hedge_accounts": ()},
                   {"source_accounts": config.source_accounts * 2},
                   {"hedge_accounts": config.hedge_accounts * 2}):
        with pytest.raises(ValueError, match="route"):
            replace(bounded, **fields)
