"""Active-oracle Maker spread, account selection, and quote construction."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from py000_nautilus.config import CarryConfig, FxConfig, MakerEconomicsConfig, RiskConfig
from py000_nautilus.economics import expected_leverage, round_hedge_ounces
from py000_nautilus.models import BookTop, MakerAccount, MakerQuote, SourceAccount, SourceDirection


def maker_adjusted_spread(
    direction: SourceDirection,
    config: MakerEconomicsConfig,
    *,
    carry: CarryConfig | None = None,
) -> Decimal:
    """Oracle lines 103-121: open spread plus fee, delta, funding, and swap."""
    current_carry = carry or config.carry
    side = config.bid if direction is SourceDirection.LONG else config.ask
    if direction is SourceDirection.LONG:
        bfx_rate = current_carry.bitfinex_long
        meta_rate = current_carry.mt5_short_swap
    else:
        bfx_rate = current_carry.bitfinex_short
        meta_rate = current_carry.mt5_long_swap
    return side.open_spread + current_carry.total_trade_fee + side.delta + bfx_rate - meta_rate


def maker_quote(
    direction: SourceDirection,
    hedge_book: BookTop,
    source_accounts: tuple[SourceAccount, ...],
    hedge_accounts: tuple[MakerAccount, ...],
    config: MakerEconomicsConfig,
    *,
    carry: CarryConfig | None = None,
    fx: FxConfig | None = None,
    hedge_quantity_ounces: Decimal | None = None,
) -> MakerQuote | None:
    """Build from active-caller inputs and the Owner-frozen multiplicative price law."""
    current_fx = fx or config.fx
    side_config = config.bid if direction is SourceDirection.LONG else config.ask
    quantity = side_config.open_quantity_ounces
    if quantity <= 0:
        return None
    source = _select_source(
        source_accounts,
        direction,
        quantity,
        config,
    )
    hedge = _select_hedge(
        hedge_accounts,
        direction,
        quantity if hedge_quantity_ounces is None else hedge_quantity_ounces,
        config,
    )
    if source is None or hedge is None:
        return None

    spread = maker_adjusted_spread(direction, config, carry=carry)
    if spread <= Decimal(-1):
        raise ValueError("maker adjusted spread must be greater than -1")
    if direction is SourceDirection.LONG:
        reference = hedge_book.bid
        base_usdt = reference * current_fx.usd_usdt_bid
        source_price = base_usdt * (Decimal(1) - spread)
    else:
        reference = hedge_book.ask
        base_usdt = reference * current_fx.usd_usdt_ask
        source_price = base_usdt * (Decimal(1) + spread)
    if source_price <= 0:
        raise ValueError("maker source price must be positive")
    return MakerQuote(
        direction=direction,
        source_account=source,
        hedge_account=hedge,
        source_price_usdt=source_price,
        hedge_reference_price_usd=reference,
        quantity_ounces=quantity,
        adjusted_spread=spread,
        leverage=expected_leverage(config.margin_level, source.base_margin_level),
    )


def passive_maker_price(
    direction: SourceDirection,
    calculated_price: Decimal,
    source_book: BookTop,
    price_increment: Decimal,
    cross_clamp_ticks: int,
) -> Decimal:
    """Apply the review-attested two-tick passive clamp in instrument ticks."""
    if price_increment <= 0:
        raise ValueError("price increment must be positive")
    if cross_clamp_ticks <= 0:
        raise ValueError("cross clamp ticks must be positive")
    rounding = ROUND_FLOOR if direction is SourceDirection.LONG else ROUND_CEILING
    price = _to_increment(calculated_price, price_increment, rounding)
    offset = price_increment * cross_clamp_ticks
    if direction is SourceDirection.LONG and price >= source_book.ask:
        price = _to_increment(source_book.ask - offset, price_increment, ROUND_FLOOR)
    elif direction is SourceDirection.SHORT and price <= source_book.bid:
        price = _to_increment(source_book.bid + offset, price_increment, ROUND_CEILING)
    if price <= 0:
        raise ValueError("passive maker price must be positive")
    return price


def _to_increment(value: Decimal, increment: Decimal, rounding: str) -> Decimal:
    steps = (value / increment).to_integral_value(rounding=rounding)
    return steps * increment


def _select_source(
    accounts: tuple[SourceAccount, ...],
    direction: SourceDirection,
    quantity: Decimal,
    config: MakerEconomicsConfig,
) -> SourceAccount | None:
    ordered = sorted(
        accounts,
        key=lambda account: account.position_ounces,
        reverse=direction is SourceDirection.SHORT,
    )
    return next(
        (
            account
            for account in ordered
            if _account_allows(
                before=account.position_ounces,
                signed_change=quantity
                if direction is SourceDirection.LONG
                else -quantity,
                capacity=account.max_long_ounces
                if direction is SourceDirection.LONG
                else account.max_short_ounces,
                maximum=config.risk.source_max_abs,
                minimum=config.risk.source_min_keep_abs,
                only_long=config.risk.only_long,
            )
        ),
        None,
    )


def _select_hedge(
    accounts: tuple[MakerAccount, ...],
    direction: SourceDirection,
    quantity: Decimal,
    config: MakerEconomicsConfig,
) -> MakerAccount | None:
    # Legacy selects MT5 descending/front for bid and descending/back for ask.
    ordered = sorted(
        accounts,
        key=lambda account: account.position_ounces,
        reverse=direction is SourceDirection.LONG,
    )
    return next(
        (
            account
            for account in ordered
            if _account_allows(
                before=account.position_ounces,
                signed_change=-quantity
                if direction is SourceDirection.LONG
                else quantity,
                capacity=account.max_short_ounces
                if direction is SourceDirection.LONG
                else account.max_long_ounces,
                maximum=config.risk.hedge_max_abs,
                minimum=config.risk.hedge_min_keep_abs,
                only_long=config.risk.only_long,
            )
        ),
        None,
    )


def _account_allows(
    *,
    before: Decimal,
    signed_change: Decimal,
    capacity: Decimal,
    maximum: Decimal,
    minimum: Decimal,
    only_long: bool,
) -> bool:
    quantity = abs(signed_change)
    if quantity > capacity:
        return False
    after = before + signed_change
    if abs(after) > maximum and abs(after) > abs(before):
        return False
    if not only_long:
        return True
    if before * after < 0:
        return False
    return not (abs(after) < minimum and abs(after) < abs(before))


def maker_carry_bounds(
    residual: Decimal, buy_leaves: Decimal, sell_leaves: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return max unhedged exposure and cumulative BUY/SELL hedge delta bounds."""
    if (not all(value.is_finite() for value in (residual, buy_leaves, sell_leaves))
            or abs(residual) > Decimal("0.5") or min(buy_leaves, sell_leaves) < 0):
        raise ValueError("invalid Maker carry budget inputs")
    half = Decimal("0.5")
    exposure = (half + max(buy_leaves, sell_leaves) if buy_leaves and sell_leaves
                else max(abs(residual), abs(residual + buy_leaves - sell_leaves)))
    buy = (max(Decimal(0), (-residual + sell_leaves + half).to_integral_value(ROUND_FLOOR))
           if sell_leaves else Decimal(0))
    sell = (max(Decimal(0), (residual + buy_leaves + half).to_integral_value(ROUND_FLOOR))
            if buy_leaves else Decimal(0))
    return exposure, buy, sell


def maker_hedge_quantity_bound(
    residual: Decimal, source_quantity: Decimal, direction: SourceDirection,
    opposite_working: bool,
) -> Decimal:
    """Maximum single intent, distinct from a partitioned order's cumulative hedge."""
    signed = residual if direction is SourceDirection.LONG else -residual
    initial = Decimal("0.5") if opposite_working else signed
    return Decimal(max(0, round_hedge_ounces(initial + source_quantity)))


def maker_hedge_bounds_allow(
    account: MakerAccount, risk: RiskConfig, buy: Decimal, sell: Decimal,
) -> bool:
    return all(_account_allows(
        before=account.position_ounces, signed_change=delta, capacity=capacity,
        maximum=risk.hedge_max_abs, minimum=risk.hedge_min_keep_abs, only_long=risk.only_long,
    ) for delta, capacity in ((buy, account.max_long_ounces), (-sell, account.max_short_ounces)))
