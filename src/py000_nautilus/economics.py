"""Taker formulas translated directly from the authenticated active script."""

from decimal import Decimal

from py000_nautilus.config import CarryConfig, FxConfig, TakerEconomicsConfig
from py000_nautilus.models import BookTop, HedgeAccount, Opportunity, SourceAccount, SourceDirection


def select_source_account(
    accounts: tuple[SourceAccount, ...],
    direction: SourceDirection,
) -> SourceAccount:
    """Match legacy sorting: lowest position for long, highest for short."""
    if not accounts:
        raise ValueError("at least one source account is required")
    ordered = sorted(accounts, key=lambda account: account.position_ounces)
    return ordered[0] if direction is SourceDirection.LONG else ordered[-1]


def long_net_return(
    source_ask_usdt: Decimal,
    hedge_bid_usd: Decimal,
    config: TakerEconomicsConfig,
    *,
    carry: CarryConfig | None = None,
    fx: FxConfig | None = None,
) -> Decimal:
    """Buy XAUT/USDT, sell XAU/USD, then apply funding, swap, and fees."""
    _require_positive("source ask", source_ask_usdt)
    _require_positive("hedge bid", hedge_bid_usd)
    current_carry = carry or config.carry
    current_fx = fx or config.fx
    hedge_bid_usdt = hedge_bid_usd * current_fx.usd_usdt_bid
    gross = hedge_bid_usdt / source_ask_usdt - Decimal(1)
    return (
        gross
        - current_carry.bitfinex_long
        + current_carry.mt5_short_swap
        - current_carry.total_trade_fee
    )


def short_net_return(
    source_bid_usdt: Decimal,
    hedge_ask_usd: Decimal,
    config: TakerEconomicsConfig,
    *,
    carry: CarryConfig | None = None,
    fx: FxConfig | None = None,
) -> Decimal:
    """Sell XAUT/USDT, buy XAU/USD, then apply funding, swap, and fees."""
    _require_positive("source bid", source_bid_usdt)
    _require_positive("hedge ask", hedge_ask_usd)
    current_carry = carry or config.carry
    current_fx = fx or config.fx
    hedge_ask_usdt = hedge_ask_usd * current_fx.usd_usdt_ask
    gross = source_bid_usdt / hedge_ask_usdt - Decimal(1)
    return (
        gross
        - current_carry.bitfinex_short
        + current_carry.mt5_long_swap
        - current_carry.total_trade_fee
    )


def expected_leverage(margin_level: Decimal, base_margin_level: Decimal) -> int:
    """Legacy `10000 // (margin_level + baseMarginLevel)`, bounded to one."""
    denominator = margin_level + base_margin_level
    if denominator <= 0:
        raise ValueError("combined margin level must be positive")
    return max(1, int(Decimal(10_000) // denominator))


def evaluate_taker(
    source_book: BookTop,
    hedge_book: BookTop,
    accounts: tuple[SourceAccount, ...],
    hedge: HedgeAccount,
    config: TakerEconomicsConfig,
    *,
    carry: CarryConfig | None = None,
    fx: FxConfig | None = None,
) -> Opportunity | None:
    """Return the legacy-priority opportunity if it also passes legacy risk checks."""
    _validate_book(source_book)
    _validate_book(hedge_book)
    if config.base_book_quantity <= 0:
        raise ValueError("base book quantity must be positive")
    short_return = (
        short_net_return(source_book.bid, hedge_book.ask, config, carry=carry, fx=fx)
        if source_book.bid_size >= config.base_book_quantity
        else None
    )
    long_return = (
        long_net_return(source_book.ask, hedge_book.bid, config, carry=carry, fx=fx)
        if source_book.ask_size >= config.base_book_quantity
        else None
    )

    # The active script checks short first and uses `elif` for long.
    if short_return is not None and short_return > config.threshold_short:
        account = select_source_account(accounts, SourceDirection.SHORT)
        quantity = min(
            account.max_short_ounces,
            hedge.max_long_ounces,
            source_book.bid_size,
            config.open_quantity_short,
        )
        candidate = Opportunity(
            direction=SourceDirection.SHORT,
            source_account=account,
            source_price_usdt=source_book.bid,
            hedge_reference_price_usd=hedge_book.ask,
            source_quantity_ounces=quantity,
            net_return=short_return,
            leverage=expected_leverage(config.margin_level, account.base_margin_level),
        )
        return candidate if risk_allows(candidate, hedge, config) else None

    if long_return is not None and long_return > config.threshold_long:
        account = select_source_account(accounts, SourceDirection.LONG)
        quantity = min(
            account.max_long_ounces,
            hedge.max_short_ounces,
            source_book.ask_size,
            config.open_quantity_long,
        )
        candidate = Opportunity(
            direction=SourceDirection.LONG,
            source_account=account,
            source_price_usdt=source_book.ask,
            hedge_reference_price_usd=hedge_book.bid,
            source_quantity_ounces=quantity,
            net_return=long_return,
            leverage=expected_leverage(config.margin_level, account.base_margin_level),
        )
        return candidate if risk_allows(candidate, hedge, config) else None
    return None


def risk_allows(
    opportunity: Opportunity,
    hedge: HedgeAccount,
    config: TakerEconomicsConfig,
) -> bool:
    """Preserve the active script's max, crossing, minimum-keep, and zero checks."""
    quantity = opportunity.source_quantity_ounces
    if quantity <= 0:
        return False

    sign = Decimal(1) if opportunity.direction is SourceDirection.LONG else Decimal(-1)
    source_before = opportunity.source_account.position_ounces
    hedge_before = hedge.position_ounces
    source_after = source_before + sign * quantity
    hedge_after = hedge_before - sign * quantity
    risk = config.risk

    if abs(source_after) > risk.source_max_abs and abs(source_after) > abs(source_before):
        return False
    if abs(hedge_after) > risk.hedge_max_abs and abs(hedge_after) > abs(hedge_before):
        return False
    if not risk.only_long:
        return True
    if source_before * source_after < 0 or hedge_before * hedge_after < 0:
        return False
    if abs(source_after) < risk.source_min_keep_abs and abs(source_after) < abs(source_before):
        return False
    return not (
        abs(hedge_after) < risk.hedge_min_keep_abs
        and abs(hedge_after) < abs(hedge_before)
    )


def round_hedge_ounces(value: Decimal) -> int:
    """Match Python's legacy `round(float)` (nearest integer, ties to even)."""
    return round(float(value))


def market_inputs_are_fresh(
    *,
    now_ns: int,
    source_ts_ns: int,
    hedge_ts_ns: int,
    cost_ts_ns: int,
    session_ts_ns: int,
    session_open: bool,
    max_quote_age_ns: int,
    max_cross_leg_skew_ns: int,
    max_cost_age_ns: int,
    max_session_age_ns: int,
) -> bool:
    """Small fail-closed gate for the facts needed by one Taker decision."""
    timestamps = (source_ts_ns, hedge_ts_ns, cost_ts_ns, session_ts_ns)
    if not session_open or any(timestamp > now_ns for timestamp in timestamps):
        return False
    if now_ns - source_ts_ns > max_quote_age_ns:
        return False
    if now_ns - hedge_ts_ns > max_quote_age_ns:
        return False
    if abs(source_ts_ns - hedge_ts_ns) > max_cross_leg_skew_ns:
        return False
    if now_ns - cost_ts_ns > max_cost_age_ns:
        return False
    return now_ns - session_ts_ns <= max_session_age_ns


def _validate_book(book: BookTop) -> None:
    _require_positive("bid", book.bid)
    _require_positive("ask", book.ask)
    if book.ask < book.bid:
        raise ValueError("crossed book")
    if book.bid_size < 0 or book.ask_size < 0:
        raise ValueError("book sizes cannot be negative")


def _require_positive(name: str, value: Decimal) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
