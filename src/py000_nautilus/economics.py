"""Taker formulas translated directly from the authenticated active script."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from py000_nautilus.config import CarryConfig, FxConfig, TakerEconomicsConfig
from py000_nautilus.models import BookTop, HedgeAccount, Opportunity, SourceAccount, SourceDirection
from py000_nautilus.mt5_v1_protocol import MAX_OBSERVATION_FUTURE_NS

_MT5_SWAP_DISABLED = 0
_MT5_SWAP_POINTS = 1


def normalize_mt5_points_swap(
    *,
    swap_long: Decimal,
    swap_short: Decimal,
    point: Decimal,
    ask: Decimal,
    native_swap_mode: int,
    swap_rates: tuple[Decimal, ...],
    now_ns: int,
    server_timezone: str,
) -> tuple[Decimal, Decimal]:
    """Return signed MT5 long/short daily swap as decimal notional returns.

    ``swap_rates`` is the broker-native Sunday-through-Saturday multiplier vector,
    using MQL's weekday numbering. Only native ``DISABLED`` and ``POINTS`` modes have
    defined semantics here.
    """
    _require_finite_decimal("swap_long", swap_long)
    _require_finite_decimal("swap_short", swap_short)
    _require_positive_decimal("point", point)
    _require_positive_decimal("ask", ask)
    if type(native_swap_mode) is not int or native_swap_mode not in {
        _MT5_SWAP_DISABLED,
        _MT5_SWAP_POINTS,
    }:
        raise ValueError("native_swap_mode must be MT5 DISABLED(0) or POINTS(1)")
    if not isinstance(swap_rates, tuple) or len(swap_rates) != 7:
        raise ValueError("swap_rates must contain exactly seven MQL weekday multipliers")
    for index, rate in enumerate(swap_rates):
        _require_finite_decimal(f"swap_rates[{index}]", rate)
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("now_ns must be a non-bool non-negative integer")
    if (
        not isinstance(server_timezone, str)
        or not server_timezone
        or server_timezone != server_timezone.strip()
    ):
        raise ValueError("server_timezone must be a non-empty trimmed IANA timezone")
    try:
        server_zone = ZoneInfo(server_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("server_timezone must name an available IANA timezone") from exc

    seconds, nanoseconds = divmod(now_ns, 1_000_000_000)
    try:
        observed_utc = datetime.fromtimestamp(seconds, UTC) + timedelta(
            microseconds=nanoseconds // 1_000,
        )
        observed_server = observed_utc.astimezone(server_zone)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("now_ns is outside the supported datetime range") from exc

    if native_swap_mode == _MT5_SWAP_DISABLED:
        return Decimal(0), Decimal(0)

    # Python Monday=0; MQL Sunday=0.
    mql_weekday = (observed_server.weekday() + 1) % 7
    multiplier = swap_rates[mql_weekday]
    return (
        swap_long * point / ask * multiplier,
        swap_short * point / ask * multiplier,
    )


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
    allowed_direction: SourceDirection | None = None,
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
    if (
        allowed_direction in {None, SourceDirection.SHORT}
        and short_return is not None
        and short_return > config.threshold_short
    ):
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

    if (
        allowed_direction in {None, SourceDirection.LONG}
        and long_return is not None
        and long_return > config.threshold_long
    ):
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
    timestamps = (source_ts_ns, hedge_ts_ns, cost_ts_ns)
    if (
        not session_open or any(timestamp > now_ns for timestamp in timestamps)
        or session_ts_ns > now_ns + MAX_OBSERVATION_FUTURE_NS
    ):
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


def _require_finite_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


def _require_positive_decimal(name: str, value: Decimal) -> None:
    _require_finite_decimal(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
