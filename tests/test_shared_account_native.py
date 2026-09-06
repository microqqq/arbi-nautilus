"""Q4: native strategy NETTING views versus one Bitfinex account position.

Real Strategy/LiveExecutionEngine objects and the real adapter consume synthetic
events/REST rows. No strategy or trading transport starts. These probes are not
shared admission, lane, business recovery, process durability or W7 acceptance.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import test_bitfinex_v1_execution as bfx
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import LiquiditySide, OrderSide, OrderStatus, PositionSide
from nautilus_trader.model.identifiers import ClientOrderId, PositionId, TradeId, VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from test_restart_ownership import _source_engine

from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionError
from py000_nautilus.restart_recovery import _positions

D = Decimal


def _facts(h: bfx._Harness) -> object:
    """Immutable ownership, economics, event and index facts; no report UUIDs."""
    return (
        sorted((
            str(o.client_order_id), str(o.strategy_id), str(o.trader_id), str(o.account_id),
            str(o.venue_order_id), o.status.name, o.price.as_decimal(),
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
