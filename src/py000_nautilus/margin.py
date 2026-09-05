"""Normalized legacy margin arithmetic, returning physical BUY/SELL ounces.

Inputs must already be resolved by the caller. These functions do not establish
account freshness or flat state, apply configured risk limits, reserve working
orders, or grant admission. Finite negative balances retain the original formula;
only the resulting per-side capacity is clamped to zero. Net ounces stay exact
Decimal values rather than copying the old account producer's integer rounding.
"""

from decimal import ROUND_FLOOR, Decimal


def bitfinex_margin_capacity(
    *,
    margin_target: Decimal,
    base_margin_level: Decimal,
    position_ounces: Decimal,
    reference_price: Decimal,
    collateral: Decimal,
    unrealized_pnl: Decimal,
    available_balance: Decimal,
    free_margin: Decimal,
) -> tuple[Decimal, Decimal]:
    """Apply both original BFX holding bounds, without a minimum-one leverage.

    ``reference_price`` and ``base_margin_level`` are explicit normalized inputs;
    no missing position field is interpreted as flat or replaced with a quote.
    """
    denominator = _validate_inputs(
        margin_target=margin_target, base_margin_level=base_margin_level,
        price_name="reference_price", reference_price=reference_price,
        position_ounces=position_ounces, collateral=collateral,
        unrealized_pnl=unrealized_pnl, available_balance=available_balance,
        free_margin=free_margin,
    )
    leverage = (Decimal(10_000) / denominator).to_integral_value(rounding=ROUND_FLOOR)
    total_available = collateral + unrealized_pnl + available_balance
    equity_bound = (total_available * leverage / reference_price).to_integral_value(
        rounding=ROUND_FLOOR,
    )
    free_bound = (free_margin * leverage / reference_price).to_integral_value(
        rounding=ROUND_FLOOR,
    ) + abs(position_ounces)
    holding_limit = min(equity_bound, free_bound)
    return (
        max(holding_limit - position_ounces, Decimal(0)),
        max(holding_limit + position_ounces, Decimal(0)),
    )


def mt5_margin_capacity(
    *,
    margin_target: Decimal,
    base_margin_level: Decimal,
    position_ounces: Decimal,
    ask: Decimal,
    equity: Decimal,
) -> tuple[Decimal, Decimal]:
    """Keep fractional MT5 leverage and return physical BUY/SELL capacity.

    The legacy duration=-1 tuple had SELL first. That caller-specific ordering
    is not exposed here; base margin (originally 60) is also an explicit input.
    """
    denominator = _validate_inputs(
        margin_target=margin_target, base_margin_level=base_margin_level,
        price_name="ask", ask=ask, position_ounces=position_ounces, equity=equity,
    )
    # Divide once: rounding repeating leverage first can lose a whole ounce
    # at an exact holding boundary (184 * 10000 / (460 * 4000) == 1).
    holding_limit = (equity * Decimal(10_000) / (denominator * ask)).to_integral_value(
        rounding=ROUND_FLOOR,
    )
    return (
        max(holding_limit - position_ounces, Decimal(0)),
        max(holding_limit + position_ounces, Decimal(0)),
    )


def _validate_inputs(
    *,
    margin_target: Decimal,
    base_margin_level: Decimal,
    price_name: str,
    **values: Decimal,
) -> Decimal:
    for name, value in {
        "margin_target": margin_target, "base_margin_level": base_margin_level, **values,
    }.items():
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"{name} must be a finite Decimal")
    if values[price_name] <= 0:
        raise ValueError(f"{price_name} must be positive")
    denominator = margin_target + base_margin_level
    if denominator <= 0:
        raise ValueError("combined margin target and base margin must be positive")
    return denominator
