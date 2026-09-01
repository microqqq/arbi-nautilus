"""The minimum executable example uses two real simulated execution clients."""

from decimal import Decimal
from pathlib import Path

from py000_nautilus.app import run_simulated_example


def test_two_venue_backtest_completes_source_and_hedge(tmp_path: Path) -> None:
    result = run_simulated_example(tmp_path / "backtest.state.json")
    assert result.orders == 2
    assert result.hedge_intents == 1
    assert result.completed_hedges == 1
    assert result.source_side == "BUY"
    assert result.hedge_side == "SELL"
    assert result.source_time_in_force == "GTC"
    assert result.hedge_time_in_force == "IOC"
    assert result.source_quantity_ounces == Decimal(1)
    assert result.hedge_quantity_ounces == Decimal(1)
    assert result.source_filled_ounces == Decimal(1)
    assert result.hedge_filled_ounces == Decimal(1)
    assert result.source_limit_price == Decimal("2400.00")
    assert result.hedge_limit_price == Decimal("2404.00")
    assert result.source_average_fill_price == Decimal("2400.00")
    assert result.hedge_average_fill_price == Decimal("2404.00")
    assert result.source_position_ounces == Decimal(1)
    assert result.hedge_position_ounces == Decimal(-1)
    assert result.source_notional_usdt == Decimal("2400.00")
    assert result.hedge_notional_usd == Decimal("2404.00")
