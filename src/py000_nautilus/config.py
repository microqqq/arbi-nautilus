"""Configuration for the bounded PY000 Taker and Maker vertical slices."""

from decimal import Decimal
from typing import Literal

from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId
from nautilus_trader.trading.config import StrategyConfig


class CarryConfig(NautilusConfig, frozen=True):
    """Rates for one opportunity evaluation, expressed as decimal returns."""

    bitfinex_long: Decimal = Decimal(0)
    bitfinex_short: Decimal = Decimal(0)
    mt5_long_swap: Decimal = Decimal(0)
    mt5_short_swap: Decimal = Decimal(0)
    total_trade_fee: Decimal = Decimal(0)


class FxConfig(NautilusConfig, frozen=True):
    """USD/USDT top of book, quoted as USDT per USD."""

    usd_usdt_bid: Decimal = Decimal(1)
    usd_usdt_ask: Decimal = Decimal(1)


class RiskConfig(NautilusConfig, frozen=True):
    """Legacy position limits, represented in gold ounces."""

    source_max_abs: Decimal
    hedge_max_abs: Decimal
    source_min_keep_abs: Decimal = Decimal(0)
    hedge_min_keep_abs: Decimal = Decimal(0)
    only_long: bool = False


class TakerEconomicsConfig(NautilusConfig, frozen=True):
    """Taker thresholds and sizes from the active script."""

    base_book_quantity: Decimal
    open_quantity_long: Decimal
    open_quantity_short: Decimal
    threshold_long: Decimal
    threshold_short: Decimal
    margin_level: Decimal
    carry: CarryConfig
    fx: FxConfig
    risk: RiskConfig


class SourceAccountRoute(NautilusConfig, frozen=True):
    """One Bitfinex account and its configured margin capacity."""

    account_id: AccountId
    max_long_ounces: Decimal
    max_short_ounces: Decimal
    client_id: ClientId | None = None
    base_margin_level: Decimal = Decimal(0)


class TakerStrategyConfig(StrategyConfig, frozen=True):
    """Runtime wiring kept deliberately specific to the two PY000 legs."""

    source_instrument_id: InstrumentId
    hedge_instrument_id: InstrumentId
    source_accounts: tuple[SourceAccountRoute, ...]
    hedge_account_id: AccountId
    hedge_max_long_ounces: Decimal
    hedge_max_short_ounces: Decimal
    economics: TakerEconomicsConfig
    store_path: str
    hedge_client_id: ClientId | None = None
    max_quote_age_ns: int = 5_000_000_000
    max_cross_leg_skew_ns: int = 2_000_000_000
    max_cost_age_ns: int = 5_000_000_000
    max_session_age_ns: int = 5_000_000_000
    initial_cost_ts_ns: int = 0
    initial_hedge_session_open: bool = False
    initial_session_ts_ns: int = 0


class HedgeAccountRoute(NautilusConfig, frozen=True):
    """One MT5 account route, with capacity expressed in canonical ounces."""

    account_id: AccountId
    max_long_ounces: Decimal
    max_short_ounces: Decimal
    client_id: ClientId | None = None


class MakerSideConfig(NautilusConfig, frozen=True):
    open_quantity_ounces: Decimal
    open_spread: Decimal
    delta: Decimal


class MakerEconomicsConfig(NautilusConfig, frozen=True):
    bid: MakerSideConfig
    ask: MakerSideConfig
    margin_level: Decimal
    carry: CarryConfig
    fx: FxConfig
    risk: RiskConfig


class MakerStrategyConfig(StrategyConfig, frozen=True):
    """Specific wiring for the two active-oracle Maker working orders."""

    source_instrument_id: InstrumentId
    hedge_instrument_id: InstrumentId
    source_accounts: tuple[SourceAccountRoute, ...]
    hedge_accounts: tuple[HedgeAccountRoute, ...]
    economics: MakerEconomicsConfig
    store_path_prefix: str
    residual_mode: Literal["strict", "bounded-carry"] = "strict"
    residual_limit_ounces: Decimal = Decimal(0)
    max_unhedged_ounces: Decimal | None = None
    fixed_amount: bool = False
    keep_last_accounts: bool = False
    cross_clamp_ticks: int = 2
    max_quote_age_ns: int = 5_000_000_000
    max_cross_leg_skew_ns: int = 2_000_000_000
    max_cost_age_ns: int = 5_000_000_000
    max_session_age_ns: int = 5_000_000_000
    initial_cost_ts_ns: int = 0
    initial_hedge_session_open: bool = False
    initial_session_ts_ns: int = 0

    def __post_init__(self) -> None:
        if self.keep_last_accounts:
            raise ValueError("keep_last_accounts is not restart-safe and is unsupported")
        if self.cross_clamp_ticks <= 0:
            raise ValueError("cross_clamp_ticks must be positive")
        limit, budget = self.residual_limit_ounces, self.max_unhedged_ounces
        if self.residual_mode not in {"strict", "bounded-carry"}:
            raise ValueError("unsupported Maker residual mode")
        if (not isinstance(limit, Decimal) or not limit.is_finite()
                or not 0 <= limit <= Decimal("0.5")):
            raise ValueError("Maker residual limit must be finite and between zero and 0.5")
        if self.residual_mode == "strict":
            if limit != 0 or budget is not None:
                raise ValueError("strict Maker requires zero residual limit and no carry budget")
        elif (limit == 0 or not isinstance(budget, Decimal) or not budget.is_finite()
              or budget <= 0):
            raise ValueError("bounded-carry requires positive residual and exposure budgets")
        elif len(self.source_accounts) != 1 or len(self.hedge_accounts) != 1:
            raise ValueError("bounded-carry requires one explicit source and hedge account route")
