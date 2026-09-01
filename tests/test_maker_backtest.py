"""Credential-free dual-sided Maker backtest."""

from decimal import Decimal
from pathlib import Path

from py000_nautilus.app import run_maker_simulated_example


def test_dual_quotes_requote_fill_cancel_and_actual_fill_hedge(tmp_path: Path) -> None:
    result = run_maker_simulated_example(tmp_path / "maker.state")

    assert result.orders == 3
    assert result.source_orders == 2
    assert result.hedge_orders == 1
    assert result.bid_updated
    assert result.bid_status == "FILLED"
    assert result.ask_status == "CANCELED"
    assert result.hedge_status == "FILLED"
    assert result.source_position_ounces == Decimal(1)
    assert result.hedge_position_ounces == Decimal(-1)
    assert result.hedge_intents == 1
    assert result.completed_hedges == 1
