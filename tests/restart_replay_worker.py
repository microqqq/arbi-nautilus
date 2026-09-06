"""Q3 subprocess experiment, not a production event store or recovery entry point.

The producer and consumer are separate processes using native codecs and engines.
All accounts/events are synthetic; no transport connects. The test carrier also
records cache position/client indexes explicitly: OrderInitialized lacks those.
"""

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import msgspec
from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.events import AccountState, OrderFilled, OrderInitialized
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    PositionId,
    StrategyId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import AccountBalance, Money
from nautilus_trader.model.orders import LimitOrder, OrderUnpacker
from nautilus_trader.serialization.serializer import MsgSpecSerializer
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

from py000_nautilus.app import BITFINEX, MT5, _hedge_instrument, _source_instrument

D = Decimal


@contextmanager
def _engine() -> Iterator[BacktestEngine]:
    engine = BacktestEngine(BacktestEngineConfig(
        logging=LoggingConfig(bypass_logging=True), run_analysis=False,
    ))
    try:
        for venue, oms, currency in (
            (BITFINEX, OmsType.NETTING, USDT), (MT5, OmsType.HEDGING, USD),
        ):
            engine.add_venue(
                venue=venue, oms_type=oms, account_type=AccountType.MARGIN,
                starting_balances=[Money(1_000_000, currency)], base_currency=currency,
            )
            state = AccountState(
                account_id=AccountId(f"{venue}-001"), account_type=AccountType.MARGIN,
                base_currency=currency, reported=True, margins=[], info={},
                balances=[AccountBalance(
                    Money(1_000_000, currency), Money(0, currency), Money(1_000_000, currency),
                )], event_id=UUID4(), ts_event=0, ts_init=0,
            )
            engine.cache.add_account(AccountFactory.create(state))
        values = CryptoPerpetual.to_dict(_source_instrument())
        values.update(size_precision=1, size_increment="0.1", lot_size="0.1")
        engine.add_instrument(CryptoPerpetual.from_dict(values))
        engine.add_instrument(_hedge_instrument())
        yield engine
    finally:
        engine.dispose()


def _summary(engine: BacktestEngine) -> dict[str, Any]:
    orders = {}
    for order in engine.cache.orders():
        position_id = engine.cache.position_id(order.client_order_id)
        client_id = engine.cache.client_id(order.client_order_id)
        orders[order.client_order_id.value] = {
            "strategy": str(order.strategy_id), "status": order.status.name,
            "instrument": order.instrument_id.value, "account": order.account_id.value,
            "side": order.side.name, "reduce_only": order.is_reduce_only,
            "quantity": str(order.quantity.as_decimal()),
            "filled": str(order.filled_qty.as_decimal()),
            "position_id": None if position_id is None else position_id.value,
            "client_id": None if client_id is None else client_id.value,
            "event_ids": [str(event.id) for event in order.events],
            "trades": [trade_id.value for trade_id in order.trade_ids],
            "fill_info": [event.info for event in order.events if isinstance(event, OrderFilled)],
            "commissions": [str(event.commission) for event in order.events
                            if isinstance(event, OrderFilled)],
        }
    positions = {
        position.id.value: {
            "strategy": position.strategy_id.value, "account": position.account_id.value,
            "instrument": position.instrument_id.value,
            "signed_qty": str(position.signed_decimal_qty()),
            "trades": [trade_id.value for trade_id in position.trade_ids],
            "opening_order": position.opening_order_id.value,
        }
        for position in engine.cache.positions()
    }
    return {"orders": orders, "positions": positions}


def produce() -> dict[str, Any]:
    codec = MsgSpecSerializer(msgspec.msgpack, timestamps_as_str=True)
    with _engine() as engine:
        source = engine.cache.instrument(_source_instrument().id)
        hedge = engine.cache.instrument(_hedge_instrument().id)
        # Deliberately not a balanced-strategy acceptance scenario: this isolates
        # two owners, fractional source fills and exact MT5 ticket identities.
        cases = (
            ("Q3-M-SOURCE", "Maker-001", source, OrderSide.BUY, "2", "0.5", None),
            ("Q3-M-HEDGE", "Maker-001", hedge, OrderSide.SELL, "1", "1", "900000101"),
            ("Q3-T-SOURCE", "Taker-002", source, OrderSide.SELL, "2", "1", None),
            ("Q3-T-HEDGE", "Taker-002", hedge, OrderSide.BUY, "1", "1", "900000102"),
            ("Q3-M-CLOSE", "Maker-001", hedge, OrderSide.BUY, "1", None, "900000101"),
        )
        for index, (cid, owner, instrument, side, quantity, filled, ticket) in enumerate(cases):
            assert instrument is not None
            close = cid == "Q3-M-CLOSE"
            order = LimitOrder(
                trader_id=TestIdStubs.trader_id(), instrument_id=instrument.id,
                strategy_id=StrategyId(owner), init_id=UUID4(), ts_init=index * 10,
                client_order_id=ClientOrderId(cid), order_side=side,
                quantity=instrument.make_qty(D(quantity)), price=instrument.make_price(D(2400)),
                time_in_force=TimeInForce.GTC, reduce_only=close,
            )
            account = AccountId(f"{instrument.id.venue}-001")
            venue_order = VenueOrderId(str(700000100 + index))
            engine.cache.add_order(
                order, position_id=PositionId(ticket) if close and ticket is not None else None,
                client_id=ClientId(str(instrument.id.venue)),
            )
            process = engine.kernel.exec_engine.process
            process(TestEventStubs.order_submitted(order, account, ts_event=index * 10 + 1))
            process(TestEventStubs.order_accepted(order, account, venue_order, index * 10 + 2))
            if filled is not None:
                event = TestEventStubs.order_filled(
                    order=order, instrument=instrument, account_id=account,
                    venue_order_id=venue_order, trade_id=TradeId(str(800000100 + index)),
                    position_id=None if ticket is None else PositionId(ticket),
                    last_qty=instrument.make_qty(D(filled)), last_px=instrument.make_price(D(2400)),
                    commission=Money(0, USD), ts_event=index * 10 + 3,
                )
                if cid == "Q3-M-SOURCE":
                    values = OrderFilled.to_dict(event)
                    values["info"] = {
                        "bitfinex_fill_source": "te_paper", "bitfinex_fee_status": "pending",
                    }
                    event = OrderFilled.from_dict(values)
                process(event)
        snapshot = _summary(engine)
        carrier = []
        for order in sorted(engine.cache.orders(), key=lambda item: item.ts_last):
            summary = snapshot["orders"][order.client_order_id.value]
            carrier.append({
                "position_id": summary["position_id"], "client_id": summary["client_id"],
                "events": [codec.serialize(event).hex() for event in order.events],
            })
        return {"pid": os.getpid(), "snapshot": snapshot, "carrier": carrier}


def restore(payload: dict[str, Any], mode: str) -> dict[str, Any]:
    codec = MsgSpecSerializer(msgspec.msgpack, timestamps_as_str=True)
    duplicates = []
    with _engine() as engine:
        assert not engine.cache.orders() and not engine.cache.positions()
        for record in payload["carrier"]:
            events = [codec.deserialize(bytes.fromhex(value)) for value in record["events"]]
            assert isinstance(events[0], OrderInitialized)
            order = OrderUnpacker.from_init(events[0])
            position_id, client_id = record["position_id"], record["client_id"]
            if mode == "orders-only":
                for event in events[1:]:
                    order.apply(event)
            engine.cache.add_order(
                order,
                position_id=None if mode == "no-indexes" or position_id is None
                else PositionId(position_id),
                client_id=(
                    None if mode == "no-indexes" or client_id is None else ClientId(client_id)
                ),
            )
            if mode != "orders-only":
                for event in events[1:]:
                    engine.kernel.exec_engine.process(event)
                    if isinstance(event, OrderFilled):
                        duplicates.append(codec.serialize(event))
        snapshot = _summary(engine)
        if mode != "orders-only":
            for raw in duplicates:
                engine.kernel.exec_engine.process(codec.deserialize(raw))
        return {"pid": os.getpid(), "snapshot": snapshot, "after_duplicate": _summary(engine)}


def main() -> None:
    mode = sys.argv[1]
    if mode == "produce":
        result = produce()
    elif mode in {"replay", "orders-only", "no-indexes"}:
        result = restore(json.load(sys.stdin), mode)
    else:
        raise ValueError("unknown Q3 worker mode")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
