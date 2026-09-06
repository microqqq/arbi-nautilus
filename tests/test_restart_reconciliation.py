"""W6b pre-engine facts: real adapter/native engine with synthetic EA history.

This is a cache/journal consistency test, not process durability or strategy
recovery. No strategy or trading transport starts and no request is submitted.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from typing import cast

import pytest
import test_mt5_v1_execution as mt5
import test_restart_ownership as q3
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.events import OrderInitialized
from nautilus_trader.model.identifiers import ClientOrderId, StrategyId, TradeId
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from restart_replay_worker import _summary

from py000_nautilus.live_cache import validate_native_cache
from py000_nautilus.mt5_v1_protocol import JsonObject


@pytest.mark.parametrize("swapped", [False, True], ids=["correct-targets", "swapped-targets"])
def test_complete_cache_close_targets_must_match_journal_before_native_reconciliation(
    swapped: bool,
) -> None:
    async def scenario() -> None:
        harness, originals = q3._mt5_history()
        engine = mt5._live_engine(harness, generate_missing_orders=False)
        owner = StrategyId("W6B-MT5")
        history = deepcopy(cast(
            list[JsonObject], cast(JsonObject, harness.fake.pages[0])["events"],
        ))
        for original, ticket in zip(originals, q3._TICKETS, strict=True):
            values = OrderInitialized.to_dict(original.events[0])
            values["strategy_id"] = owner.value
            order = OrderUnpacker.from_init(OrderInitialized.from_dict(values))
            order.apply(TestEventStubs.order_submitted(order, harness.client.account_id))
            harness.cache.add_order(order, position_id=ticket, client_id=harness.client.id)
            harness.cache.update_order(order)
        try:
            await harness.connect()
            assert await engine.reconcile_execution_state(timeout_secs=1)
            assert {position.quantity.as_decimal() for position in harness.cache.positions()} == {
                Decimal(100),
            }
            await harness.client._disconnect()

            closes = []
            for index, target in enumerate(q3._TICKETS):
                close = MarketOrder(
                    trader_id=originals[0].trader_id, strategy_id=owner,
                    instrument_id=harness.instrument.id,
                    client_order_id=ClientOrderId(f"W6B-CLOSE-{index}"),
                    order_side=OrderSide.SELL, quantity=harness.instrument.make_qty(1),
                    time_in_force=TimeInForce.FOK, reduce_only=True,
                    init_id=UUID4(), ts_init=100 + index,
                )
                close.apply(TestEventStubs.order_submitted(close, harness.client.account_id))
                cached_target = q3._TICKETS[1 - index] if swapped else target
                harness.cache.add_order(
                    close, client_id=harness.client.id, position_id=cached_target,
                )
                harness.cache.update_order(close)
                closes.append(close)
                sequence = 6 + index * 2
                position_ticket = f"700000{101 + index}"
                history.append(mt5._event(
                    harness.identity, sequence, "submission_reserved",
                    mt5._submission_payload(
                        close, "0.01", position_ticket=position_ticket,
                        position_identifier=target.value,
                    ),
                ))
                terminal = mt5._outcome(
                    harness.identity, close, "order_filled", sequence=sequence + 1,
                    quantity_lots="0.01", position_ticket=position_ticket,
                    position_identifier=target.value, venue_position_id=target.value,
                )
                cast(JsonObject, terminal["payload"]).update({
                    "venue_deal_id": f"800000{201 + index}",
                    "venue_order_id": f"700000{201 + index}",
                })
                history.append(terminal)

            # Both alternatives are complete under W6a: one owner, real positions,
            # correct clients and dependencies. Only external journal facts differ.
            assert validate_native_cache(
                harness.cache, trader_id=originals[0].trader_id, strategy_id=owner,
                routes={harness.instrument.id: (harness.client.account_id, harness.client.id)},
            ) is True
            before = _summary(harness)
            snapshot = deepcopy(harness.fake.current_snapshot)
            for position in cast(list[JsonObject], snapshot["positions"]):
                position["volume_lots"] = "0.99"
            harness.fake.current_snapshot = snapshot
            harness.fake.pages.append(mt5._page(
                harness.identity, after_cursor="0", events=history,
            ))
            await harness.connect()  # Read the complete journal; no local pending submission.
            assert harness.client.pending_client_order_ids == ()
            reconciled = await engine.reconcile_execution_state(timeout_secs=1)
            if swapped:
                assert reconciled is False
                assert _summary(harness) == before  # No misattributed fill reached native cache.
                assert not harness.client.execution_admitted and harness.fake.closed
            else:
                assert reconciled is True
                for index, (close, target) in enumerate(zip(closes, q3._TICKETS, strict=True)):
                    assert close.status is OrderStatus.FILLED
                    assert close.filled_qty.as_decimal() == 1
                    assert close.position_id == target
                    assert close.trade_ids == [TradeId(f"800000{201 + index}")]
                    assert harness.cache.position(target).quantity.as_decimal() == 99
                completed = _summary(harness)
                assert await engine.reconcile_execution_state(timeout_secs=1)
                assert _summary(harness) == completed
            assert not harness.fake.submit_calls and not harness.fake.close_calls
        finally:
            await harness.client._disconnect()
            engine.dispose()

    asyncio.run(scenario())
