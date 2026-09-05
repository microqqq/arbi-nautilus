"""Legacy margin arithmetic and pure native-account capacity projections.

The normalized arithmetic needs resolved inputs. Account projections additionally
validate one event's facts and intersect configured limits; their required current
client qualification must be read by the caller alongside that event. Neither layer
reserves working orders or grants admission. Finite negative balances retain the
original formula, and net ounces remain exact Decimal values.
"""

from collections.abc import Callable
from decimal import ROUND_FLOOR, Decimal
from typing import cast

from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import AccountId, InstrumentId
from nautilus_trader.model.objects import Currency

from py000_nautilus.config import HedgeAccountRoute, SourceAccountRoute
from py000_nautilus.models import BookTop, HedgeAccount, MakerAccount, SourceAccount

# Source/hedge books keep their own timestamps; the last argument requests new
# source budget. Live composition supplies the IO, not this pure module.
# Results are source, hedge, earliest account expiry, and new-budget qualification.
type LiveAccountReader = Callable[
    [BookTop, int, BookTop, int, bool],
    tuple[SourceAccount, HedgeAccount, int, bool] | None,
]


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


def bitfinex_source_account(
    event: AccountState | None,
    *,
    route: SourceAccountRoute,
    instrument_id: InstrumentId,
    wallet_currency: str,
    margin_target: Decimal,
    max_abs_ounces: Decimal,
    now_ns: int,
    max_account_age_ns: int,
    client_ready: bool,
    ask: Decimal,
    ask_ts_ns: int,
    max_quote_age_ns: int,
    ask_actionable: bool,
) -> SourceAccount | None:
    """Project one complete source sample, without refreshing it or granting admission.

    Only explicit complete flat state uses the route base and current source ask.
    A held position uses its own entry price and collateral-derived base instead.
    """
    try:
        if type(route) is not SourceAccountRoute:
            return None
        info = _account_info(event, route.account_id, client_ready, USDT)
        facts = _object(info["bitfinex_margin"])
        currency = info["bitfinex_wallet_currency"]
        if (
            not isinstance(wallet_currency, str) or not wallet_currency
            or not isinstance(instrument_id, InstrumentId)
            or facts["instrument_id"] != instrument_id.value
            or not isinstance(currency, str) or currency.upper() != wallet_currency.upper()
        ):
            return None
        wallet, positions = _object(facts["wallet"]), _object(facts["positions"])
        if (
            wallet["current"] is not True or positions["current"] is not True
            or positions["complete"] is not True
            or not _fresh(wallet["observed_ns"], now_ns, max_account_age_ns)
            or not _fresh(positions["observed_ns"], now_ns, max_account_age_ns)
        ):
            return None
        _decimal(wallet["balance"])
        available = _decimal(wallet["available_balance"])
        position = positions["position"]  # Missing is unavailable, never flat.
        if position is None:
            base = route.base_margin_level
            if not isinstance(base, Decimal) or not base.is_finite() or base <= 0:
                return None
            price = _fresh_ask(ask, ask_ts_ns, now_ns, max_quote_age_ns, ask_actionable)
            quantity = collateral = pnl = Decimal(0)
        else:
            row = _object(position)
            if (
                type(row["position_id"]) is not int or row["position_id"] <= 0
                or row["status"] != "ACTIVE" or type(row["type"]) is not int or row["type"] != 1
            ):
                return None
            quantity = _decimal(row["quantity"])
            price, pnl = _decimal(row["base_price"]), _decimal(row["profit_loss"])
            collateral, leverage = _decimal(row["collateral"]), _decimal(row["leverage"])
            minimum = _decimal(row["collateral_min"])
            if quantity == 0 or collateral <= 0 or leverage <= 0 or minimum < 0:
                return None
            base = Decimal(10_000) * minimum / (collateral * leverage)
        dynamic = bitfinex_margin_capacity(
            margin_target=margin_target, base_margin_level=base, position_ounces=quantity,
            reference_price=price, collateral=collateral, unrealized_pnl=pnl,
            available_balance=available, free_margin=available,
        )
        buy, sell = _intersect_capacity(
            dynamic, quantity, route.max_long_ounces, route.max_short_ounces, max_abs_ounces,
        )
        return SourceAccount(route.account_id, route.client_id, quantity, buy, sell, base)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def mt5_hedge_account(
    event: AccountState | None,
    *,
    route: HedgeAccountRoute,
    symbol: str,
    stream_id: str,
    margin_target: Decimal,
    max_abs_ounces: Decimal,
    now_ns: int,
    max_account_age_ns: int,
    client_ready: bool,
    ask: Decimal,
    ask_ts_ns: int,
    max_quote_age_ns: int,
    ask_actionable: bool,
) -> HedgeAccount | None:
    """Use complete MT5 ticket-net facts, raw equity and the original base 60.

    ``client_ready`` must include the execution client's current sample eligibility;
    a historical event retains its valid flag even after a subsequent native fill.
    """
    try:
        if type(route) is not HedgeAccountRoute:
            return None
        info = _account_info(event, route.account_id, client_ready, USD)
        if (
            not isinstance(symbol, str) or not symbol
            or not isinstance(stream_id, str) or not stream_id
            or info["mt5_symbol"] != symbol or info["mt5_stream_id"] != stream_id
            or info["mt5_positions_complete"] is not True
            or info["mt5_account_sample_valid"] is not True
            or not _fresh(info["mt5_account_observed_ns"], now_ns, max_account_age_ns)
        ):
            return None
        quantity, equity = _decimal(info["mt5_net_position_ounces"]), _decimal(info["mt5_equity"])
        count = info["mt5_position_count"]
        if (
            type(count) is not int or count < 0
            or (count == 0 and quantity != 0) or (count == 1 and quantity == 0)
        ):
            return None
        price = _fresh_ask(ask, ask_ts_ns, now_ns, max_quote_age_ns, ask_actionable)
        dynamic = mt5_margin_capacity(
            margin_target=margin_target, base_margin_level=Decimal(60),
            position_ounces=quantity, ask=price, equity=equity,
        )
        buy, sell = _intersect_capacity(
            dynamic, quantity, route.max_long_ounces, route.max_short_ounces, max_abs_ounces,
        )
        return HedgeAccount(quantity, buy, sell)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def mt5_maker_account(
    event: AccountState | None,
    *,
    route: HedgeAccountRoute,
    symbol: str,
    stream_id: str,
    margin_target: Decimal,
    max_abs_ounces: Decimal,
    now_ns: int,
    max_account_age_ns: int,
    client_ready: bool,
    ask: Decimal,
    ask_ts_ns: int,
    max_quote_age_ns: int,
    ask_actionable: bool,
) -> MakerAccount | None:
    """Attach the route identity to the same MT5 projection used by Taker."""
    hedge = mt5_hedge_account(
        event, route=route, symbol=symbol, stream_id=stream_id,
        margin_target=margin_target, max_abs_ounces=max_abs_ounces, now_ns=now_ns,
        max_account_age_ns=max_account_age_ns, client_ready=client_ready,
        ask=ask, ask_ts_ns=ask_ts_ns, max_quote_age_ns=max_quote_age_ns,
        ask_actionable=ask_actionable,
    )
    if hedge is None:
        return None
    return MakerAccount(
        route.account_id, route.client_id, hedge.position_ounces,
        hedge.max_long_ounces, hedge.max_short_ounces,
    )


def _account_info(
    event: AccountState | None, account_id: AccountId, client_ready: bool,
    currency: Currency,
) -> dict[str, object]:
    if (
        client_ready is not True or not isinstance(event, AccountState)
        or event.account_id != account_id or event.account_type != AccountType.MARGIN
        or event.base_currency != currency
    ):
        raise ValueError("account sample has no current qualification")
    return _object(event.info)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("account facts must be an object")
    return cast(dict[str, object], value)


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str | Decimal):
        raise ValueError("account value must be an exact decimal")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("account value must be finite")
    return number


def _fresh(observed_ns: object, now_ns: int, max_age_ns: int) -> bool:
    return (
        type(observed_ns) is int and type(now_ns) is int and type(max_age_ns) is int
        and observed_ns >= 0 and now_ns >= 0 and max_age_ns >= 0
        and 0 <= now_ns - observed_ns <= max_age_ns
    )


def _fresh_ask(
    ask: Decimal, ts_ns: int, now_ns: int, max_age_ns: int, actionable: bool,
) -> Decimal:
    if (
        actionable is not True or not _fresh(ts_ns, now_ns, max_age_ns)
        or not isinstance(ask, Decimal) or not ask.is_finite() or ask <= 0
    ):
        raise ValueError("account capacity requires a fresh actionable ask")
    return ask


def _intersect_capacity(
    dynamic: tuple[Decimal, Decimal], quantity: Decimal,
    route_buy: Decimal, route_sell: Decimal, max_abs: Decimal,
) -> tuple[Decimal, Decimal]:
    for value in (*dynamic, route_buy, route_sell, max_abs):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("capacity limits must be finite and nonnegative")
    # Dynamic H +/- q already includes the position. Route caps are trade amounts.
    return (
        min(dynamic[0], route_buy, max(max_abs - quantity, Decimal(0))),
        min(dynamic[1], route_sell, max(max_abs + quantity, Decimal(0))),
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
