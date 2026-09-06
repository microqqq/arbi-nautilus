"""Explicit W6a Redis integration worker; never connect trading transports.

Only the producer creates synthetic accounts/orders. Consumers use the ordinary
builder's native database load, not an event carrier or Order.apply replay.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import msgspec
import test_live_maker as maker
import test_live_taker as taker
from msgspec.structs import replace as struct_replace
from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.cache.database import CacheDatabaseAdapter
from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.events import AccountState, OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    PositionId,
    TradeId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.objects import AccountBalance, Money
from nautilus_trader.model.orders import LimitOrder
from nautilus_trader.serialization.serializer import MsgSpecSerializer
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from restart_replay_worker import _summary

from py000_nautilus.app import _hedge_instrument
from py000_nautilus.bitfinex_v1_data import instrument_from_config
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport
from py000_nautilus.live_cache import native_cache_config
from py000_nautilus.live_maker import build_live_maker_node
from py000_nautilus.live_taker import build_live_taker_node
from py000_nautilus.mt5_v1_transport import Mt5V1Transport


async def _process(engine: Any, events: list[Any]) -> None:
    before = engine.event_count
    for event in events:
        engine.process(event)
    async with asyncio.timeout(3):
        while engine.event_count < before + len(events):
            await asyncio.sleep(0.001)
    assert engine.evt_qsize() == 0


async def _produce(node: Any, strategy: Any, configs: Any) -> None:
    assert node.cache.accounts() == node.cache.orders() == node.cache.positions() == []
    source = instrument_from_config(configs.bitfinex_data, ts_init=1)
    hedge = _hedge_instrument()
    for instrument in (source, hedge):
        node.cache.add_instrument(instrument)
    for account_id, currency in (
        (configs.bitfinex_exec.account_id, USDT),
        (AccountId("MT5-12345678"), USD),
    ):
        state = AccountState(
            account_id=account_id, account_type=AccountType.MARGIN, base_currency=currency,
            balances=[AccountBalance(
                Money(1_000_000, currency), Money(0, currency), Money(1_000_000, currency),
            )], margins=[], reported=True, info={}, event_id=UUID4(), ts_event=1, ts_init=1,
        )
        node.cache.add_account(AccountFactory.create(state))

    engine = node.kernel.exec_engine
    engine.start()  # Native event queues only: the node/strategies/transports never start.
    cases = (
        ("SOURCE", source, OrderSide.BUY, 2, 0.5, None),
        ("HEDGE", hedge, OrderSide.SELL, 1, 1, "900000101"),
        ("CLOSE", hedge, OrderSide.BUY, 1, None, "900000101"),
    )
    for index, (label, instrument, side, quantity, filled, position_id) in enumerate(cases):
        order = LimitOrder(
            trader_id=strategy.trader_id, strategy_id=strategy.id,
            instrument_id=instrument.id, client_order_id=ClientOrderId(f"W6A-{label}"),
            order_side=side, quantity=instrument.make_qty(quantity),
            price=instrument.make_price(2400), time_in_force=TimeInForce.GTC,
            reduce_only=label == "CLOSE", init_id=UUID4(), ts_init=index * 10 + 2,
        )
        node.cache.add_order(
            order, client_id=ClientId(instrument.id.venue.value),
            position_id=PositionId(position_id) if label == "CLOSE" else None,
        )
        account_id = (configs.bitfinex_exec.account_id if label == "SOURCE"
                      else AccountId("MT5-12345678"))
        venue_order = VenueOrderId(str(700000101 + index))
        events = [
            TestEventStubs.order_submitted(order, account_id, ts_event=index * 10 + 3),
            TestEventStubs.order_accepted(order, account_id, venue_order, index * 10 + 4),
        ]
        if filled is not None:
            fill = TestEventStubs.order_filled(
                order, instrument, account_id=account_id, venue_order_id=venue_order,
                trade_id=TradeId(str(800000101 + index)),
                position_id=PositionId(position_id) if position_id is not None else None,
                last_qty=instrument.make_qty(filled), last_px=instrument.make_price(2400),
                commission=Money(0, USD), ts_event=index * 10 + 5,
            )
            values = OrderFilled.to_dict(fill)
            values["trader_id"] = strategy.trader_id.value  # TestEventStubs uses a fixed TESTER ID.
            if label == "SOURCE":
                values["info"] = {
                    "bitfinex_fill_source": "te_paper", "bitfinex_fee_status": "pending",
                }
            events.append(OrderFilled.from_dict(values))
        await _process(engine, events)


async def _exercise_loaded(node: Any, new_trade: bool) -> None:
    engine = node.kernel.exec_engine
    engine.start()
    order = node.cache.order(ClientOrderId("W6A-SOURCE"))
    fill = next(event for event in order.events if isinstance(event, OrderFilled))
    values = OrderFilled.to_dict(fill)
    values["event_id"] = str(UUID4())
    if new_trade:
        values["trade_id"] = "800000199"
    await _process(engine, [OrderFilled.from_dict(values)])


async def _run(node: Any, strategy: Any, configs: Any, mode: str) -> dict[str, Any]:
    engine = node.kernel.exec_engine
    try:
        clients = engine._clients
        assert clients[ClientId("BITFINEX")].oms_type is OmsType.NETTING
        assert clients[ClientId("MT5")].oms_type is OmsType.HEDGING
        if mode == "produce":
            await _produce(node, strategy, configs)
        elif mode in {"duplicate", "new-trade"}:
            await _exercise_loaded(node, new_trade=mode == "new-trade")
        elif mode in {"omit-client", "omit-position"}:
            missing_position = mode == "omit-position"
            instrument = node.cache.instrument(
                _hedge_instrument().id if missing_position
                else configs.strategy.source_instrument_id,
            )
            # Synthetic incomplete native write, not an actual crash/durability proof.
            incomplete_order = LimitOrder(
                trader_id=strategy.trader_id, strategy_id=strategy.id,
                instrument_id=instrument.id, client_order_id=ClientOrderId(
                    "W6A-NO-POSITION" if missing_position else "W6A-NO-CLIENT",
                ),
                order_side=OrderSide.BUY, quantity=instrument.make_qty(1),
                price=instrument.make_price(2400), time_in_force=TimeInForce.GTC,
                reduce_only=missing_position, init_id=UUID4(), ts_init=40,
            )
            node.cache.add_order(
                incomplete_order, client_id=ClientId("MT5") if missing_position else None,
            )
            account_id = (AccountId("MT5-12345678") if missing_position
                          else configs.bitfinex_exec.account_id)
            engine.start()
            await _process(engine, [
                TestEventStubs.order_submitted(incomplete_order, account_id, ts_event=41),
                TestEventStubs.order_accepted(
                    incomplete_order, account_id, VenueOrderId("700000199"), 42,
                ),
            ])
        elif mode != "load":
            raise ValueError("unknown native cache worker mode")
        assert not node.is_running() and not strategy.is_running
        assert all(not client.is_connected for client in clients.values())
        return {
            "pid": os.getpid(), "trader_id": strategy.trader_id.value,
            "strategy_id": strategy.id.value, "snapshot": _summary(node),
            "order_traders": {order.client_order_id.value: order.trader_id.value
                              for order in node.cache.orders()},
            "accounts": sorted(account.id.value for account in node.cache.accounts()),
            "account_balances": {account.id.value: str(account.balance_total().as_decimal())
                                 for account in node.cache.accounts()},
            "source_ready": strategy._live_submission_ready(),
            "integrity": node.cache.check_integrity(), "event_count": engine.event_count,
        }
    finally:
        if engine.is_running:
            engine.stop()
            await asyncio.wait_for(asyncio.gather(
                engine._cmd_queue_task, engine._evt_queue_task,
            ), timeout=3)


def scenario(kind: str, mode: str, port: int, state_dir: Path) -> dict[str, Any]:
    configs: Any = maker._configs(state_dir) if kind == "maker" else taker._configs(state_dir)
    builder: Any = build_live_maker_node if kind == "maker" else build_live_taker_node
    mt5_data, mt5_exec, strategy_config = configs.mt5_data, configs.mt5_exec, configs.strategy
    if mode == "wrong-account":
        mt5_data = struct_replace(mt5_data, expected_account_id="12345679")
        mt5_exec = struct_replace(mt5_exec, expected_account_id="12345679")
        if kind == "taker":
            strategy_config = struct_replace(
                strategy_config, hedge_account_id=AccountId("MT5-12345679"),
            )
        else:
            strategy_config = struct_replace(strategy_config, hedge_accounts=(struct_replace(
                strategy_config.hedge_accounts[0], account_id=AccountId("MT5-12345679"),
            ),))
    database = DatabaseConfig(
        host="127.0.0.1", port=port, connection_timeout=1, response_timeout=1,
        number_of_retries=0,
    )
    if mode == "restore-fixture-position":
        # Test-only control: restore this synthetic order's KNOWN target before
        # the independent missing-client scenario. Never infer a live target.
        backend = CacheDatabaseAdapter(
            trader_id=TraderId(f"PY000-{kind.upper()}-LIVE-001"), instance_id=UUID4(),
            serializer=MsgSpecSerializer(msgspec.msgpack, timestamps_as_str=True),
            config=native_cache_config(database),
        )
        try:
            backend.index_order_position(ClientOrderId("W6A-NO-POSITION"), PositionId("900000101"))
        finally:
            backend.close()
        return {"fixture_position_restored": True}
    loop = asyncio.new_event_loop()
    node = None
    opened = AsyncMock(side_effect=AssertionError("W6a test must not open a trading transport"))
    try:
        with patch.object(BitfinexV1Transport, "open", opened), patch.object(
            Mt5V1Transport, "open", opened,
        ):
            node, strategy = builder(
                bitfinex_data_config=configs.bitfinex_data,
                bitfinex_exec_config=configs.bitfinex_exec,
                mt5_data_config=mt5_data, mt5_exec_config=mt5_exec,
                strategy_config=strategy_config, loop=loop, cache_database=database,
            )
            result = loop.run_until_complete(_run(node, strategy, configs, mode))
            opened.assert_not_awaited()
            return result
    finally:
        if node is not None:
            node.dispose()  # Native database.close outside the event loop, without a sleep.
        if not loop.is_closed():
            loop.close()


if __name__ == "__main__":
    result = scenario(sys.argv[1], sys.argv[2], int(sys.argv[3]), Path(sys.argv[4]))
    print("W6A_JSON=" + json.dumps(result, sort_keys=True))
