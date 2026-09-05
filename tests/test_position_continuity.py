"""Continuous ordinary strategies using native NETTING/HEDGING matching.

This proves strategy/order/position semantics, not live adapter or EA execution.
Orders and fills come only from native matching, without replacing strategies,
stores or positions. Maker terminal reads report the simulated matcher's native
closed orders; this is synthetic readback, not live reconciliation evidence.
Separate adapter tests cover the submission-time snapshot boundary.
"""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from msgspec.structs import replace
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.enums import AccountType, BookType, OmsType, OrderSide
from nautilus_trader.model.identifiers import ClientOrderId, PositionId, VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order
from test_taker_events import _terminal_report

from py000_nautilus.app import (
    BITFINEX,
    HEDGE_ID,
    MT5,
    SOURCE_ID,
    _book_snapshot,
    _hedge_instrument,
    _maker_strategy_config,
    _quote,
    _source_instrument,
    _strategy_config,
)
from py000_nautilus.models import ObligationStatus
from py000_nautilus.strategies.maker import MakerStrategy
from py000_nautilus.strategies.taker import TakerStrategy

D = Decimal


def _creation_key(order: Any) -> tuple[int, int]:
    # Native factory sequence, not lexical "10" before "9" at equal timestamps.
    return order.ts_init, int(str(order.client_order_id).rsplit("-", 1)[1])


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("sign", [1, -1], ids=["long-first", "short-first"])
@pytest.mark.parametrize(
    ("deltas", "leg_counts"),
    [
        ((2, 2, -2), (1, 2, 3)),
        ((2, -1), (1, 2)),
        ((2, -3), (1, 3)),
        ((1, 2, -4), (1, 2, 5)),
    ],
    ids=["add-add-reduce", "partial-ticket", "cross-zero", "two-tickets-cross-zero"],
)
def test_ordinary_strategy_continuous_position_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, sign: int,
    deltas: tuple[int, ...], leg_counts: tuple[int, ...],
) -> None:
    deltas = tuple(sign * value for value in deltas)
    engine = BacktestEngine(BacktestEngineConfig(
        logging=LoggingConfig(bypass_logging=True), run_analysis=False,
    ))
    engine.add_venue(
        venue=BITFINEX, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
        book_type=BookType.L2_MBP,
        starting_balances=[Money(1_000_000, USDT)], base_currency=USDT,
        default_leverage=D(16),
    )
    engine.add_venue(
        venue=MT5, oms_type=OmsType.HEDGING, account_type=AccountType.MARGIN,
        starting_balances=[Money(1_000_000, USD)], base_currency=USD,
        default_leverage=D(10),
    )
    source, hedge = _source_instrument(), _hedge_instrument()
    engine.add_instrument(source)
    engine.add_instrument(hedge)
    long_quantity = D(max(deltas))
    short_quantity = D(-min(deltas))
    if maker:
        maker_config = _maker_strategy_config(tmp_path / "maker")
        maker_config = replace(maker_config, economics=replace(
            maker_config.economics,
            bid=replace(maker_config.economics.bid, open_quantity_ounces=long_quantity),
            ask=replace(maker_config.economics.ask, open_quantity_ounces=short_quantity),
        ))
        def terminal_query(
            client_order_id: ClientOrderId, venue_order_id: VenueOrderId,
            complete: Callable[[OrderStatusReport | None], bool],
        ) -> None:
            # BacktestExecutionClient has no live REST readback. Report only
            # actual closed native matcher orders via the existing callback.
            order = engine.cache.order(client_order_id)
            assert order is not None and order.is_closed
            assert order.venue_order_id == venue_order_id
            assert complete(_terminal_report(
                order, ts_accepted=order.ts_accepted,
                ts_last=order.ts_last, ts_init=order.ts_last,
            ))

        strategy = MakerStrategy(maker_config, source_terminal_query=terminal_query)
        stores = tuple(strategy._stores.values())
    else:
        taker_config = _strategy_config(tmp_path / "taker.json")
        taker_config = replace(taker_config, economics=replace(
            taker_config.economics,
            open_quantity_long=long_quantity, open_quantity_short=short_quantity,
        ))
        strategy = TakerStrategy(taker_config)
        stores = (strategy.state_store,)
    engine.add_strategy(strategy)
    submit_order = strategy.submit_order
    observed_hedge_submissions: list[Order] = []

    def observe_submission(order: Any, **kwargs: Any) -> None:
        if order.instrument_id == HEDGE_ID:
            # Read-only observation before delegating to the real native route:
            # batch-sending legs cannot masquerade as sequential completion.
            assert all(previous.is_closed and previous.filled_qty == previous.quantity
                       for previous in observed_hedge_submissions)
            observed_hedge_submissions.append(order)
        submit_order(order, **kwargs)

    monkeypatch.setattr(strategy, "submit_order", observe_submission)
    source_position = D(0)
    expected_tickets: dict[PositionId, Decimal] = {}  # Actual opening ID -> remaining ounces.
    prior_hedge_ids = set()
    try:
        for index, delta in enumerate(deltas):
            now = 1_000_000_000 + index * 100_000_000
            bid, ask = ("2397", "2398") if delta > 0 else ("2411", "2412")
            # Neutralize the previous opportunity before refreshing the hedge.
            # Config and economics stay unchanged throughout the running strategy.
            batch = [
                _book_snapshot(source, "2404", "2405", "10", now),
                _quote(hedge, "2404", "2405", "10", now + 1_000_000),
                _book_snapshot(source, bid, ask, str(abs(delta)), now + 2_000_000),
            ]
            if maker:
                batch.extend([
                    _quote(source, "2404", "2405", "10", now),
                    _quote(source, bid, ask, str(abs(delta)), now + 2_000_000),
                ])
            engine.add_data(batch)
            engine.run(streaming=True)
            engine.clear_data()

            source_position += D(delta)
            assert engine.portfolio.net_position(SOURCE_ID) == source_position
            assert engine.portfolio.net_position(HEDGE_ID) == -source_position
            source_orders = sorted(
                (order for order in engine.cache.orders(instrument_id=SOURCE_ID)
                 if order.filled_qty.as_decimal() > 0),
                key=_creation_key,
            )
            assert [order.filled_qty.as_decimal() * (1 if order.side == OrderSide.BUY else -1)
                    for order in source_orders] == [D(value) for value in deltas[:index + 1]]
            if not maker:
                before = source_position - delta
                assert source_orders[-1].is_reduce_only == (
                    before * delta < 0 and abs(delta) <= abs(before)
                )
            hedge_orders = sorted(
                engine.cache.orders(instrument_id=HEDGE_ID),
                key=_creation_key,
            )
            assert len(hedge_orders) == leg_counts[index]
            assert len(observed_hedge_submissions) == len(hedge_orders)
            new_legs = [order for order in hedge_orders
                        if order.client_order_id not in prior_hedge_ids]
            remaining = D(-delta)
            # Expected inventory is carried from actual prior opening order IDs.
            # Opposing tickets must be consumed in their original numeric order.
            targets = [(position_id, quantity) for position_id, quantity in expected_tickets.items()
                       if quantity * remaining < 0]
            for order in new_legs:
                assert order.is_closed and order.filled_qty == order.quantity
                signed_quantity = order.quantity.as_decimal() * (
                    1 if order.side == OrderSide.BUY else -1
                )
                assert signed_quantity * remaining > 0
                position_id = engine.cache.position_id(order.client_order_id)
                assert position_id is not None
                if targets:
                    target_id, target_quantity = targets.pop(0)
                    assert order.is_reduce_only and position_id == target_id
                    assert abs(signed_quantity) == min(abs(remaining), abs(target_quantity))
                    expected_tickets[target_id] += signed_quantity
                    if not expected_tickets[target_id]:
                        del expected_tickets[target_id]
                else:
                    assert not order.is_reduce_only
                    assert position_id not in expected_tickets
                    assert signed_quantity == remaining
                    expected_tickets[position_id] = signed_quantity
                remaining -= signed_quantity
            assert remaining == 0
            assert {position.id: position.signed_qty for position in
                    engine.cache.positions_open(instrument_id=HEDGE_ID)} == expected_tickets
            prior_hedge_ids = {order.client_order_id for order in hedge_orders}
            intents = [intent for store in stores for intent in store.intents()]
            assert len(intents) == index + 1
            assert all(intent.status is ObligationStatus.COMPLETED for intent in intents)
        assert source_position != 0  # None of these scenarios end via a test-only flatten.
    finally:
        engine.end()
        engine.dispose()
