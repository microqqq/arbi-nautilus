from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import RoutingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.execution.messages import ModifyOrder, SubmitOrder
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, VenueOrderId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from py000_nautilus.bitfinex_v1_data import (
    INSTRUMENT_ID,
    BitfinexV1DataClientConfig,
    instrument_from_config,
)
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
)
from py000_nautilus.bitfinex_v1_protocol import POST_ONLY_FLAG

RAW_SYMBOL = "tXAUTF0:USTF0"
VENUE_ORDER_ID = 219_492_782_587


class _FakeTransport:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, object] | list[object]] = asyncio.Queue()
        self.sent: list[dict[str, object] | list[object]] = []

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send_json(self, payload: dict[str, object] | list[object]) -> None:
        self.sent.append(payload)

    async def recv_json(self) -> dict[str, object] | list[object]:
        return await self.queue.get()


class _FakeRest:
    async def user_info(self) -> object:
        row: list[object] = [None] * 22
        row[0] = 269_312
        row[21] = 0
        return row

    async def active_orders_by_symbol(self, symbol: str) -> object:
        del symbol
        return []

    async def order_history_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2_500,
    ) -> object:
        del symbol, start, end, limit
        return []

    async def trades_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2_500,
    ) -> object:
        del symbol, start, end, limit
        return []

    async def positions(self) -> object:
        return []


def _instrument() -> Instrument:
    return instrument_from_config(
        BitfinexV1DataClientConfig(
            url="wss://offline.invalid/ws/2",
            instrument_id=INSTRUMENT_ID,
            raw_symbol=RAW_SYMBOL,
            price_precision=2,
            size_precision=8,
            price_increment=Decimal("0.01"),
            size_increment=Decimal("0.00000001"),
            min_quantity=Decimal("0.00000001"),
            max_quantity=Decimal("100000"),
            margin_init=Decimal("0.1"),
            margin_maint=Decimal("0.05"),
            maker_fee=Decimal(0),
            taker_fee=Decimal("0.0002"),
            routing=RoutingConfig(default=False, venues=frozenset({"BITFINEX"})),
        ),
        ts_init=0,
    )


def _order_frame(
    operation: str,
    cid: int,
    *,
    remaining: str,
    status: str,
    price: str = "3926.70",
) -> list[object]:
    return [
        0,
        operation,
        [
            VENUE_ORDER_ID,
            None,
            cid,
            RAW_SYMBOL,
            1_700_000_000_000,
            1_700_000_000_100,
            Decimal(remaining),
            Decimal("4"),
            "LIMIT",
            None,
            None,
            None,
            POST_ONLY_FLAG,
            status,
            None,
            None,
            Decimal(price),
            Decimal("0"),
        ],
    ]


def _trade_frame(cid: int, *, trade_id: int, order_price: str) -> list[object]:
    return [
        0,
        "tu",
        [
            trade_id,
            RAW_SYMBOL,
            1_700_000_000_123 + trade_id,
            VENUE_ORDER_ID,
            Decimal("1"),
            Decimal("3926.75"),
            "LIMIT",
            Decimal(order_price),
            1,
            Decimal("-0.10"),
            "USTF0",
            cid,
        ],
    ]


def test_private_frames_drive_real_execution_engine_order_fsm() -> None:
    async def scenario(cid_path: Path) -> None:
        loop = asyncio.get_running_loop()
        clock = TestComponentStubs.clock()
        msgbus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        instrument = _instrument()
        provider = InstrumentProvider()
        provider.add(instrument)
        transport = _FakeTransport()
        client = BitfinexV1ExecutionClient(
            loop=loop,
            name="BITFINEX",
            config=BitfinexV1ExecClientConfig(
                url="wss://offline.invalid/ws/2",
                api_key="OFFLINE-KEY",
                api_secret="OFFLINE-SECRET",
                user_id=269_312,
                account_id=AccountId("BITFINEX-001"),
                instrument_id=INSTRUMENT_ID,
                raw_symbol=RAW_SYMBOL,
                cid_store_path=str(cid_path),
            ),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            transport=transport,
            rest=_FakeRest(),
        )
        msgbus.register("Portfolio.update_account", lambda _: None)
        engine = ExecutionEngine(msgbus=msgbus, cache=cache, clock=clock)
        engine.register_client(client)
        cache.add_instrument(instrument)
        cache.add_account(TestExecStubs.margin_account(account_id=client.account_id))
        order = TestComponentStubs.order_factory().limit(
            instrument_id=INSTRUMENT_ID,
            order_side=OrderSide.BUY,
            quantity=instrument.make_qty(Decimal("4")),
            price=instrument.make_price(Decimal("3926.70")),
            time_in_force=TimeInForce.GTC,
            post_only=True,
            client_order_id=ClientOrderId("ENGINE-FSM-1"),
        )
        cache.add_order(order)

        await transport.queue.put({"event": "info", "version": 2})
        await transport.queue.put(
            {"event": "auth", "status": "OK", "chanId": 0, "userId": 269_312}
        )
        await transport.queue.put(
            [0, "ws", [["margin", "USTF0", Decimal("1000"), Decimal("0"), Decimal("800")]]]
        )
        await client._connect()
        try:
            await client._submit_order(
                SubmitOrder(
                    trader_id=order.trader_id,
                    strategy_id=order.strategy_id,
                    order=order,
                    command_id=UUID4(),
                    ts_init=clock.timestamp_ns(),
                    params={"leverage": 10},
                )
            )
            submission = cast(dict[str, object], cast(list[object], transport.sent[-1])[3])
            cid = cast(int, submission["cid"])
            cached = cache.order(order.client_order_id)
            assert cached is not None and cached.status == OrderStatus.SUBMITTED

            client._consume_private_frame(
                _order_frame("on", cid, remaining="4", status="ACTIVE")
            )
            assert cached.status == OrderStatus.ACCEPTED

            await client._modify_order(
                ModifyOrder(
                    trader_id=order.trader_id,
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                    quantity=None,
                    price=instrument.make_price(Decimal("3926.80")),
                    trigger_price=None,
                    command_id=UUID4(),
                    ts_init=clock.timestamp_ns(),
                    params={"leverage": 10},
                )
            )
            client._consume_private_frame(
                _order_frame(
                    "ou",
                    cid,
                    remaining="4",
                    status="ACTIVE",
                    price="3926.80",
                )
            )
            assert cached.status == OrderStatus.ACCEPTED
            assert cached.price == instrument.make_price(Decimal("3926.80"))

            client._consume_private_frame(
                _trade_frame(cid, trade_id=1234, order_price="3926.80")
            )
            assert cached.status == OrderStatus.PARTIALLY_FILLED
            assert cached.filled_qty.as_decimal() == Decimal("1")

            client._consume_private_frame(
                _order_frame(
                    "oc",
                    cid,
                    remaining="2",
                    status="CANCELED was: PARTIALLY FILLED @ 3926.75(2)",
                    price="3926.80",
                )
            )
            assert cached.status == OrderStatus.PARTIALLY_FILLED
            assert client.execution_hold_reason is not None

            client._consume_private_frame(
                _trade_frame(cid, trade_id=1235, order_price="3926.80")
            )
            assert cached.status == OrderStatus.CANCELED
            assert cached.filled_qty.as_decimal() == Decimal("2")
            assert [str(trade_id) for trade_id in cached.trade_ids] == ["1234", "1235"]
            assert [type(event).__name__ for event in cached.events] == [
                "OrderInitialized",
                "OrderSubmitted",
                "OrderAccepted",
                "OrderUpdated",
                "OrderFilled",
                "OrderFilled",
                "OrderCanceled",
            ]
            assert engine.event_count == 6
        finally:
            await client._disconnect()

    with TemporaryDirectory() as temporary:
        asyncio.run(scenario(Path(temporary) / "cids.json"))
