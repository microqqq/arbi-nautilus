"""Q4: shared NETTING views and cross-owner MT5 HEDGING ticket histories.

Real Strategy/LiveExecutionEngine objects and the real adapter consume synthetic
events/REST rows. No strategy starts; MT5 connects only its synthetic transport.
These probes are not
shared admission, lane, business recovery, process durability or W7 acceptance.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import test_bitfinex_v1_execution as bfx
import test_mt5_v1_execution as mt5
import test_restart_ownership as q3
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, PositionId, TradeId, VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import LimitOrder, MarketOrder
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from test_restart_ownership import _source_engine

from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionError
from py000_nautilus.hedge import plan_hedge_delta
from py000_nautilus.live_cache import validate_native_cache
from py000_nautilus.models import BusinessOrderSide
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionError
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.restart_recovery import _positions

D = Decimal


def _facts(h: bfx._Harness | mt5._Harness) -> object:
    """Immutable ownership, economics, event and index facts; no report UUIDs."""
    return (
        sorted((
            str(o.client_order_id), str(o.strategy_id), str(o.trader_id), str(o.account_id),
            str(o.venue_order_id), o.status.name,
            o.price.as_decimal() if isinstance(o, LimitOrder) else None,
            str(o.position_id), o.quantity.as_decimal(), o.filled_qty.as_decimal(),
            tuple(str(event.id) for event in o.events), tuple(map(str, o.trade_ids)),
            str(h.cache.client_id(o.client_order_id)), str(h.cache.position_id(o.client_order_id)),
        ) for o in h.cache.orders()),
        sorted((
            str(p.id), str(p.strategy_id), str(p.trader_id), str(p.account_id),
            p.signed_decimal_qty(), p.avg_px_open, tuple(str(event.id) for event in p.events),
        ) for p in h.cache.positions()),
    )


@asynccontextmanager
async def _shared(
    path: Path, quantities: tuple[int, int], prices: tuple[int, int] = (2400, 2400),
) -> AsyncIterator[tuple[bfx._Harness, LiveExecutionEngine]]:
    h = bfx._Harness(cid_store_path=path / "cids.json")
    engine = _source_engine(h)
    h.msgbus.deregister("Portfolio.update_account", h.events.append)
    portfolio = Portfolio(msgbus=h.msgbus, cache=h.cache, clock=h.clock)
    owners = [Strategy(StrategyConfig(order_id_tag=tag, oms_type="NETTING"))
              for tag in ("Q4-MAKER", "Q4-TAKER")]
    engine.start()  # Native queues only; connect() and Strategy.start() are never called.
    try:
        epoch_ms = h.clock.timestamp_ns() // 1_000_000 - 1000
        cases = zip(owners, quantities, prices, strict=True)
        for index, (owner, quantity, price) in enumerate(cases):
            owner.register(trader_id=h.msgbus.trader_id, portfolio=portfolio,
                           msgbus=h.msgbus, cache=h.cache, clock=h.clock)
            engine.register_oms_type(owner)
            assert owner.order_factory is not None
            order = owner.order_factory.limit(
                instrument_id=h.instrument.id, order_side=OrderSide.BUY if index == 0
                else OrderSide.SELL, quantity=h.instrument.make_qty(quantity),
                price=h.instrument.make_price(price), post_only=True,
                client_order_id=ClientOrderId(f"Q4-{index}"),
            )
            binding = h.client._cid_store.allocate(order.client_order_id.value, epoch_ms=epoch_ms)
            venue_id, trade_id = 700_001 + index, 800_001 + index
            accepted_ms, filled_ms = epoch_ms + index * 10, epoch_ms + index * 10 + 1
            h.cache.add_order(order, client_id=h.client.id)
            for event in (
                TestEventStubs.order_submitted(order, h.client.account_id,
                                              ts_event=accepted_ms * 1_000_000),
                TestEventStubs.order_accepted(order, h.client.account_id,
                                             VenueOrderId(str(venue_id)), accepted_ms * 1_000_000),
                TestEventStubs.order_filled(
                    order, h.instrument, account_id=h.client.account_id,
                    venue_order_id=VenueOrderId(str(venue_id)), trade_id=TradeId(str(trade_id)),
                    last_px=h.instrument.make_price(price), commission=Money(0, USD),
                    liquidity_side=LiquiditySide.MAKER, ts_event=filled_ms * 1_000_000,
                ),
            ):
                engine.process(event)
            async with asyncio.timeout(2):
                while order.status is not OrderStatus.FILLED:
                    await asyncio.sleep(0.001)
            row = cast(list[object], h.order_frame(
                "oc", binding.cid, order, remaining="0", status=f"EXECUTED @ {price}",
            )[2])
            row[0], row[4], row[5], row[17] = venue_id, accepted_ms, filled_ms, D(price)
            h.rest.history.append(row)
            trade = cast(list[object], h.trade_frame(
                binding.cid, order, trade_id=trade_id, quantity=str(quantity),
                price=str(price), fee="0",
            )[2])
            trade[2], trade[3] = filled_ms, venue_id
            h.rest.trades.append(trade)
            position = h.cache.position(PositionId(f"{h.instrument.id}-{owner.id}"))
            assert position is not None and position.strategy_id == owner.id
            assert position.signed_decimal_qty() == D(quantity * (1 if index == 0 else -1))
            assert position.avg_px_open == price
            assert order.trade_ids == [TradeId(str(trade_id))]
            assert order.position_id == h.cache.position_id(order.client_order_id) == position.id
            assert h.cache.client_id(order.client_order_id) == h.client.id
            assert len(order.events) == 4 and len(position.events) == 1
        assert owners[0].id != owners[1].id
        assert len(h.cache.positions_open()) == 2 and h.cache.check_integrity()
        net = D(quantities[0] - quantities[1])
        # A smaller second SELL reduces the account's BUY at its original basis;
        # it opens a separate short virtual view for the other native owner.
        h.rest.position_rows = [] if net == 0 else [bfx._position_row(net, avg_px=D(prices[0]))]
        yield h, engine
        assert not h.fake.opened and h.fake.sent == []
        assert all(not owner.is_running for owner in owners)
    finally:
        engine.stop()
        commands, events = engine.get_cmd_queue_task(), engine.get_evt_queue_task()
        assert commands is not None and events is not None
        await asyncio.gather(commands, events)
        await h.close()
        for owner in owners:
            owner.dispose()
        engine.dispose()


@pytest.mark.parametrize("quantities", [(2, 2), (3, 1)], ids=["flat", "net-long"])
def test_complete_single_venue_mass_preserves_both_native_owners(
    tmp_path: Path, quantities: tuple[int, int],
) -> None:
    async def scenario() -> None:
        async with _shared(tmp_path, quantities) as (h, engine):
            before = _facts(h)
            for _ in range(2):
                mass = await h.client.generate_mass_status()
                assert mass is not None and len(mass.order_reports) == 2
                assert sum(map(len, mass.fill_reports.values())) == 2
                reports = mass.position_reports[h.instrument.id]
                assert len(reports) == 1 and reports[0].venue_position_id is None
                assert reports[0].signed_decimal_qty == D(quantities[0] - quantities[1])
                assert reports[0].position_side is (
                    PositionSide.FLAT if quantities[0] == quantities[1] else PositionSide.LONG
                )
                assert await engine.reconcile_execution_state(timeout_secs=1)
                assert _facts(h) == before and h.cache.check_integrity()
    asyncio.run(scenario())


@pytest.mark.parametrize("quantities", [(2, 2), (3, 1)], ids=["flat", "net-long"])
def test_native_true_does_not_replace_adapter_net_quantity_guard(
    tmp_path: Path, quantities: tuple[int, int],
) -> None:
    async def scenario() -> None:
        async with _shared(tmp_path, quantities) as (h, engine):
            before = _facts(h)
            wrong = D(quantities[0] - quantities[1] + 1)
            h.rest.position_rows = [bfx._position_row(wrong, avg_px=D(2400))]
            with pytest.raises(BitfinexV1ExecutionError, match="position differs"):
                await h.client.generate_mass_status()
            assert not await engine.reconcile_execution_state(timeout_secs=1)
            report, = await h.client.generate_position_status_reports(bfx._position_command(h))
            # Deliberately isolate native's public report entry from the adapter mass guard.
            assert engine.reconcile_execution_report(report)
            assert sum((p.signed_decimal_qty() for p in h.cache.positions_open()), D(0)) != wrong
            assert _facts(h) == before and h.cache.check_integrity()
    asyncio.run(scenario())


def test_opposing_virtual_position_average_is_not_the_venue_remaining_basis(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _shared(tmp_path, (3, 1), (2400, 2500)) as (h, engine):
            before = _facts(h)
            mass = await h.client.generate_mass_status()
            assert mass is not None
            report, = mass.position_reports[h.instrument.id]
            assert report.signed_decimal_qty == 2 and report.avg_px_open == D(2400)
            virtual_average = sum((abs(p.signed_decimal_qty()) * D(str(p.avg_px_open))
                                   for p in h.cache.positions_open()), D(0)) / D(4)
            assert virtual_average == D(2425) != report.avg_px_open
            assert await engine.reconcile_execution_state(timeout_secs=1)
            assert _facts(h) == before
            # Existing single-strategy startup arithmetic cannot certify a shared account.
            with pytest.raises(ValueError, match="average price differs"):
                _positions(h.cache, mass, h.instrument.id, netting=True)
            assert _facts(h) == before
    asyncio.run(scenario())


@asynccontextmanager
async def _cross_owner_close(
    fault: str | None,
) -> AsyncIterator[tuple[mt5._Harness, LiveExecutionEngine, MarketOrder]]:
    h, originals = q3._mt5_history()
    engine = mt5._live_engine(h, generate_missing_orders=False)
    history = deepcopy(cast(list[JsonObject], cast(JsonObject, h.fake.pages[0])["events"]))
    for order, ticket in zip(originals, q3._TICKETS, strict=True):
        h.cache.add_order(OrderUnpacker.from_init(order.events[0]),
                          position_id=ticket, client_id=h.client.id)
    try:
        await h.connect()
        assert await engine.reconcile_execution_state(timeout_secs=1)
        assert all(h.cache.position(ticket).quantity.as_decimal() == 100 for ticket in q3._TICKETS)
        await h.client._disconnect()
        close = MarketOrder(
            trader_id=originals[0].trader_id,
            strategy_id=q3._OWNERS[0] if fault == "initial-owner" else q3._OWNERS[1],
            instrument_id=h.instrument.id, client_order_id=ClientOrderId("Q4-CROSS-OWNER-CLOSE"),
            order_side=OrderSide.SELL, quantity=h.instrument.make_qty(1),
            time_in_force=TimeInForce.FOK, reduce_only=True, init_id=UUID4(), ts_init=100,
        )
        close.apply(TestEventStubs.order_submitted(close, h.client.account_id))
        h.cache.add_order(close, client_id=h.client.id,
                          position_id=q3._TICKETS[1] if fault == "ticket-index" else q3._TICKETS[0])
        h.cache.update_order(close)
        lots = "0.02" if fault == "journal-quantity" else "0.01"
        target = {"position_ticket": "700000101", "position_identifier": q3._TICKETS[0].value}
        history.append(mt5._event(h.identity, 6, "submission_reserved",
                                 mt5._submission_payload(close, lots, **target)))
        terminal = mt5._outcome(h.identity, close, "order_filled", sequence=7,
                               quantity_lots=lots, venue_position_id=q3._TICKETS[0].value, **target)
        cast(JsonObject, terminal["payload"]).update({
            "venue_order_id": "700000103", "venue_deal_id": "800000103",
        })
        history.append(terminal)
        cast(list[JsonObject], h.fake.current_snapshot["positions"])[0]["volume_lots"] = (
            str(D(1) - D(lots))
        )
        h.fake.pages.append(mt5._page(h.identity, after_cursor="0", events=history))
        await h.connect()  # Synthetic EA I/O; no submit, close, or Strategy.start().
        yield h, engine, close
        assert not h.fake.submit_calls and not h.fake.close_calls
    finally:
        await h.client._disconnect()
        engine.dispose()


@pytest.mark.parametrize("fault", [None, "initial-owner"],
                         ids=["known-taker-owner", "misowned-init-is-not-repaired"])
def test_cross_owner_mt5_reports_do_not_infer_business_ownership(fault: str | None) -> None:
    async def scenario() -> None:
        async with _cross_owner_close(fault) as (h, engine, close):
            for iteration in range(2):
                mass = await h.client.generate_mass_status(None)
                assert mass is not None and len(mass.order_reports) == 3
                assert sum(map(len, mass.fill_reports.values())) == 3
                assert await engine.reconcile_execution_state(timeout_secs=1)
                assert _positions(h.cache, mass, h.instrument.id, netting=False) == 199
                position = h.cache.position(q3._TICKETS[0])
                assert position.strategy_id == q3._OWNERS[0]
                assert position.quantity.as_decimal() == 99
                assert h.cache.position(q3._TICKETS[1]).quantity.as_decimal() == 100
                assert close.status is OrderStatus.FILLED and close.filled_qty.as_decimal() == 1
                assert (close.position_id == h.cache.position_id(close.client_order_id)
                        == position.id)
                assert h.cache.client_id(close.client_order_id) == h.client.id
                fill, = [event for event in close.events if isinstance(event, OrderFilled)]
                assert fill.strategy_id == close.strategy_id
                assert close.trade_ids == [TradeId("800000103")]
                assert [event.strategy_id for event in position.events] == [q3._OWNERS[0],
                                                                           close.strategy_id]
                # Both IDs are legitimate owners. The journal has no StrategyId:
                # a wrongly retained init owner also gets native True. W7 needs the
                # explicit CID -> business-view binding, not merely an allowed set.
                assert (close.strategy_id == q3._OWNERS[1]) is (fault is None)
                assert h.cache.check_integrity()
                if iteration == 0:
                    completed = _facts(h)
                else:
                    assert _facts(h) == completed  # All order/Position UUIDs and indexes.
            plan = plan_hedge_delta(h.cache.positions_open(instrument_id=h.instrument.id,
                                   account_id=h.client.account_id), BusinessOrderSide.SELL, D(1))
            assert plan[0].position_id == q3._TICKETS[0].value
            # This Q4 probe must not relax either ordinary standalone owner's guard.
            with pytest.raises(ValueError, match="identity mismatch"):
                validate_native_cache(h.cache, trader_id=close.trader_id,
                                      strategy_id=q3._OWNERS[1], routes={
                                          h.instrument.id: (h.client.account_id, h.client.id),
                                      })
    asyncio.run(scenario())


@pytest.mark.parametrize("fault,message", [
    ("ticket-index", "position index conflict"), ("journal-quantity", "order facts conflict"),
])
def test_cross_owner_mt5_conflicts_are_rejected_before_native_fills(
    fault: str, message: str,
) -> None:
    async def scenario() -> None:
        async with _cross_owner_close(fault) as (h, engine, close):
            before = _facts(h)
            with pytest.raises(Mt5V1ExecutionError, match=message):
                await h.client.generate_mass_status(None)
            assert not await engine.reconcile_execution_state(timeout_secs=1)
            assert _facts(h) == before and close.filled_qty.as_decimal() == 0
            assert close.status is OrderStatus.SUBMITTED
            assert not h.client.execution_admitted and h.fake.closed
    asyncio.run(scenario())
