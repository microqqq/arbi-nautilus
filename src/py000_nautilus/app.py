"""Credential-free two-venue backtest for the first Taker slice."""

import argparse
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType
from nautilus_trader.model.identifiers import AccountId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Cfd, CryptoPerpetual
from nautilus_trader.model.objects import Currency, Money, Price, Quantity

from py000_nautilus.config import (
    CarryConfig,
    FxConfig,
    RiskConfig,
    SourceAccountRoute,
    TakerEconomicsConfig,
    TakerStrategyConfig,
)
from py000_nautilus.models import ObligationStatus
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.taker import TakerStrategy

BITFINEX = Venue("BITFINEX")
MT5 = Venue("MT5")
SOURCE_ID = InstrumentId.from_str("XAUTUSDT-PERP.BITFINEX")
HEDGE_ID = InstrumentId.from_str("XAUUSD.MT5")


@dataclass(frozen=True, slots=True)
class SimulationResult:
    orders: int
    hedge_intents: int
    completed_hedges: int
    source_side: str
    hedge_side: str
    source_time_in_force: str
    hedge_time_in_force: str
    source_quantity_ounces: Decimal
    hedge_quantity_ounces: Decimal
    source_filled_ounces: Decimal
    hedge_filled_ounces: Decimal
    source_limit_price: Decimal
    hedge_limit_price: Decimal
    source_average_fill_price: Decimal
    hedge_average_fill_price: Decimal
    source_position_ounces: Decimal
    hedge_position_ounces: Decimal
    source_notional_usdt: Decimal
    hedge_notional_usd: Decimal
    state_path: Path


def run_simulated_example(state_path: Path) -> SimulationResult:
    """Run one deterministic opportunity through both simulated execution clients."""
    engine = BacktestEngine(
        BacktestEngineConfig(
            logging=LoggingConfig(bypass_logging=True),
            run_analysis=False,
        )
    )
    engine.add_venue(
        venue=BITFINEX,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(1_000_000, USDT)],
        base_currency=USDT,
        default_leverage=Decimal(16),
    )
    engine.add_venue(
        venue=MT5,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(1_000_000, USD)],
        base_currency=USD,
        default_leverage=Decimal(10),
    )
    source = _source_instrument()
    hedge = _hedge_instrument()
    engine.add_instrument(source)
    engine.add_instrument(hedge)
    engine.add_strategy(TakerStrategy(_strategy_config(state_path)))
    engine.add_data(
        [
            _quote(hedge, "2404.00", "2405.00", "10.00", 1_000_000_000),
            _quote(source, "2398.00", "2400.00", "5", 2_000_000_000),
        ]
    )
    engine.run()
    store = JsonStateStore(state_path)
    intents = store.intents()
    orders = engine.cache.orders()
    source_order = next(order for order in orders if order.instrument_id == SOURCE_ID)
    hedge_order = next(order for order in orders if order.instrument_id == HEDGE_ID)
    source_quantity = Decimal(str(source_order.quantity))
    hedge_quantity = Decimal(str(hedge_order.quantity))
    source_filled = Decimal(str(source_order.filled_qty))
    hedge_filled = Decimal(str(hedge_order.filled_qty))
    source_price = Decimal(str(source_order.price))
    hedge_price = Decimal(str(hedge_order.price))
    source_average_fill_price = Decimal(str(source_order.avg_px))
    hedge_average_fill_price = Decimal(str(hedge_order.avg_px))
    result = SimulationResult(
        orders=len(orders),
        hedge_intents=len(intents),
        completed_hedges=sum(
            intent.status is ObligationStatus.COMPLETED for intent in intents
        ),
        source_side=source_order.side.name,
        hedge_side=hedge_order.side.name,
        source_time_in_force=source_order.time_in_force.name,
        hedge_time_in_force=hedge_order.time_in_force.name,
        source_quantity_ounces=source_quantity,
        hedge_quantity_ounces=hedge_quantity,
        source_filled_ounces=source_filled,
        hedge_filled_ounces=hedge_filled,
        source_limit_price=source_price,
        hedge_limit_price=hedge_price,
        source_average_fill_price=source_average_fill_price,
        hedge_average_fill_price=hedge_average_fill_price,
        source_position_ounces=cast(Decimal, engine.portfolio.net_position(SOURCE_ID)),
        hedge_position_ounces=cast(Decimal, engine.portfolio.net_position(HEDGE_ID)),
        source_notional_usdt=source_filled * source_average_fill_price,
        hedge_notional_usd=hedge_filled * hedge_average_fill_price,
        state_path=state_path,
    )
    engine.dispose()
    return result


def _strategy_config(state_path: Path) -> TakerStrategyConfig:
    return TakerStrategyConfig(
        source_instrument_id=SOURCE_ID,
        hedge_instrument_id=HEDGE_ID,
        source_accounts=(
            SourceAccountRoute(
                account_id=AccountId("BITFINEX-001"),
                max_long_ounces=Decimal(10),
                max_short_ounces=Decimal(10),
                base_margin_level=Decimal(100),
            ),
        ),
        hedge_account_id=AccountId("MT5-001"),
        hedge_max_long_ounces=Decimal(10),
        hedge_max_short_ounces=Decimal(10),
        economics=TakerEconomicsConfig(
            base_book_quantity=Decimal(1),
            open_quantity_long=Decimal(1),
            open_quantity_short=Decimal(1),
            threshold_long=Decimal("0.001"),
            threshold_short=Decimal("0.001"),
            margin_level=Decimal(500),
            carry=CarryConfig(total_trade_fee=Decimal("0.0002")),
            fx=FxConfig(usd_usdt_bid=Decimal(1), usd_usdt_ask=Decimal(1)),
            risk=RiskConfig(source_max_abs=Decimal(10), hedge_max_abs=Decimal(10)),
        ),
        store_path=str(state_path),
        initial_cost_ts_ns=1_000_000_000,
        initial_hedge_session_open=True,
        initial_session_ts_ns=1_000_000_000,
    )


def _source_instrument() -> CryptoPerpetual:
    return CryptoPerpetual(
        instrument_id=SOURCE_ID,
        raw_symbol=Symbol("tXAUTF0:USTF0"),
        base_currency=Currency.from_str("XAUT"),
        quote_currency=USDT,
        settlement_currency=USDT,
        is_inverse=False,
        price_precision=2,
        size_precision=0,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        multiplier=Quantity.from_int(1),
        lot_size=Quantity.from_int(1),
        margin_init=Decimal("0.1"),
        margin_maint=Decimal("0.05"),
        maker_fee=Decimal(0),
        taker_fee=Decimal("0.0002"),
    )


def _hedge_instrument() -> Cfd:
    return Cfd(
        instrument_id=HEDGE_ID,
        raw_symbol=Symbol("XAUUSD"),
        asset_class=AssetClass.COMMODITY,
        base_currency=Currency.from_str("XAU"),
        quote_currency=USD,
        price_precision=2,
        size_precision=0,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_int(1),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        margin_init=Decimal("0.1"),
        margin_maint=Decimal("0.05"),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        info={
            "canonical_quantity": "ounce",
            "live_adapter_contract": "convert 100 ounces per MT5 lot",
        },
    )


def _quote(
    instrument: CryptoPerpetual | Cfd,
    bid: str,
    ask: str,
    size: str,
    timestamp: int,
) -> QuoteTick:
    return QuoteTick(
        instrument_id=instrument.id,
        bid_price=instrument.make_price(Decimal(bid)),
        ask_price=instrument.make_price(Decimal(ask)),
        bid_size=instrument.make_qty(Decimal(size)),
        ask_size=instrument.make_qty(Decimal(size)),
        ts_event=timestamp,
        ts_init=timestamp,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, help="state file (defaults to a temporary path)")
    args = parser.parse_args()
    if args.state is not None:
        result = run_simulated_example(args.state)
    else:
        with tempfile.TemporaryDirectory(prefix="py000-nautilus-") as directory:
            result = run_simulated_example(Path(directory) / "taker.state.json")
    print(
        f"orders={result.orders} hedge_intents={result.hedge_intents} "
        f"completed_hedges={result.completed_hedges} "
        f"positions_oz={result.source_position_ounces}/{result.hedge_position_ounces}"
    )


if __name__ == "__main__":
    main()
