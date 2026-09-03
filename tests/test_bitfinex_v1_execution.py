from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveExecClientConfig, RoutingConfig, TradingNodeConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.execution.messages import (
    CancelOrder,
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    ModifyOrder,
    QueryOrder,
    SubmitOrder,
)
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.events import OrderAccepted, OrderEvent, OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    TradeId,
    TraderId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

import py000_nautilus.bitfinex_v1_execution as execution_module
from py000_nautilus.bitfinex_v1_data import (
    INSTRUMENT_ID as SOURCE_ID,
)
from py000_nautilus.bitfinex_v1_data import (
    PAPER_RAW_SYMBOL,
    BitfinexV1DataClientConfig,
    BitfinexV1LiveDataClientFactory,
    instrument_from_config,
)
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
    BitfinexV1ExecutionError,
    BitfinexV1LiveExecClientFactory,
)
from py000_nautilus.bitfinex_v1_protocol import POST_ONLY_FLAG

RAW_SYMBOL = "tXAUTF0:USTF0"
VENUE_ORDER_ID = 219_492_782_587


class _FakeTransport:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, object] | list[object]] = asyncio.Queue()
        self.sent: list[dict[str, object] | list[object]] = []
        self.opened = False
        self.closed = False
        self.fail_next_send = False

    async def open(self) -> None:
        self.opened = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def send_json(self, payload: dict[str, object] | list[object]) -> None:
        if self.fail_next_send:
            self.fail_next_send = False
            raise ConnectionError("synthetic uncertain send")
        self.sent.append(payload)

    async def recv_json(self) -> dict[str, object] | list[object]:
        return await self.queue.get()


class _FakeRest:
    def __init__(self, expected_symbol: str = RAW_SYMBOL) -> None:
        self.expected_symbol = expected_symbol
        self.user_id = 269_312
        self.paper_enabled = int(expected_symbol == PAPER_RAW_SYMBOL)
        self.user_info_calls = 0
        self.active: list[object] = []
        self.history: list[object] = []
        self.trades: list[object] = []
        self.position_rows: list[object] = []

    async def user_info(self) -> object:
        self.user_info_calls += 1
        row: list[object] = [None] * 22
        row[0] = self.user_id
        row[21] = self.paper_enabled
        return row

    async def active_orders_by_symbol(self, symbol: str) -> object:
        assert symbol == self.expected_symbol
        return self.active

    async def order_history_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2_500,
    ) -> object:
        assert symbol == self.expected_symbol and start is not None and end is not None
        assert limit == 2_500
        return self.history

    async def trades_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2_500,
    ) -> object:
        assert symbol == self.expected_symbol and start is not None and end is not None
        assert limit == 2_500
        return self.trades

    async def positions(self) -> object:
        return self.position_rows


class _Harness:
    def __init__(
        self,
        *,
        cid_store_path: Path | None = None,
        mutation_ack_timeout_ms: int = 10_000,
        rest: _FakeRest | None = None,
        raw_symbol: str = RAW_SYMBOL,
        wallet_currency: str = "USTF0",
        instrument_raw_symbol: str | None = None,
        instrument_available: bool = True,
    ) -> None:
        self._temporary = TemporaryDirectory() if cid_store_path is None else None
        if cid_store_path is None:
            assert self._temporary is not None
            cid_store_path = Path(self._temporary.name) / "cids.json"
        self.clock = TestComponentStubs.clock()
        self.msgbus = TestComponentStubs.msgbus()
        self.cache = TestComponentStubs.cache()
        self.events: list[Any] = []
        self.msgbus.register("ExecEngine.process", self.events.append)
        self.msgbus.register("Portfolio.update_account", self.events.append)
        profile_symbol = instrument_raw_symbol or raw_symbol
        self.instrument = instrument_from_config(
            BitfinexV1DataClientConfig(
                url="wss://api-pub.bitfinex.com/ws/2",
                instrument_id=SOURCE_ID,
                raw_symbol=profile_symbol,
                price_precision=2,
                size_precision=8,
                price_increment=Decimal("0.01"),
                size_increment=Decimal("0.00000001"),
                min_quantity=(
                    Decimal("2")
                    if profile_symbol == PAPER_RAW_SYMBOL
                    else Decimal("0.002")
                ),
                max_quantity=(
                    Decimal("10000")
                    if profile_symbol == PAPER_RAW_SYMBOL
                    else Decimal("400")
                ),
                margin_init=(
                    Decimal("0.01")
                    if profile_symbol == PAPER_RAW_SYMBOL
                    else Decimal("0.1")
                ),
                margin_maint=(
                    Decimal("0.005")
                    if profile_symbol == PAPER_RAW_SYMBOL
                    else Decimal("0.05")
                ),
                maker_fee=Decimal(0),
                taker_fee=Decimal("0.0002"),
                routing=RoutingConfig(default=False, venues=frozenset({"BITFINEX"})),
            ),
            ts_init=0,
        )
        self.order_factory = TestComponentStubs.order_factory()
        provider = InstrumentProvider()
        if instrument_available:
            provider.add(self.instrument)
        self.fake = _FakeTransport()
        self.rest = rest or _FakeRest(raw_symbol)
        self.client = BitfinexV1ExecutionClient(
            loop=asyncio.get_running_loop(),
            name="BITFINEX",
            config=BitfinexV1ExecClientConfig(
                url="wss://api.bitfinex.com/ws/2",
                api_key="TEST-KEY",
                api_secret="TEST-SECRET",
                user_id=269_312,
                account_id=AccountId("BITFINEX-001"),
                instrument_id=SOURCE_ID,
                raw_symbol=raw_symbol,
                wallet_currency=wallet_currency,
                cid_store_path=str(cid_store_path),
                mutation_ack_timeout_ms=mutation_ack_timeout_ms,
            ),
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            instrument_provider=provider,
            transport=self.fake,
            rest=self.rest,
        )
        self.raw_symbol = raw_symbol
        self.wallet_currency = wallet_currency

    async def connect(self, *, available: Decimal | None = Decimal("800")) -> None:
        await self.fake.queue.put({"event": "info", "version": 2})
        await self.fake.queue.put(
            {"event": "auth", "status": "OK", "chanId": 0, "userId": 269_312}
        )
        await self.fake.queue.put(
            [
                0,
                "ws",
                [
                    [
                        "margin",
                        self.wallet_currency,
                        Decimal("1000"),
                        Decimal("0"),
                        available,
                    ]
                ],
            ]
        )
        await self.client._connect()

    async def close(self) -> None:
        await self.client._disconnect()
        if self._temporary is not None:
            self._temporary.cleanup()

    def order(
        self,
        *,
        side: OrderSide = OrderSide.BUY,
        tif: TimeInForce = TimeInForce.GTC,
        post_only: bool = True,
        quantity: str = "4",
        price: str = "3926.70",
        client_order_id: ClientOrderId | None = None,
    ) -> Order:
        return self.order_factory.limit(
            instrument_id=SOURCE_ID,
            order_side=side,
            quantity=self.instrument.make_qty(Decimal(quantity)),
            price=self.instrument.make_price(Decimal(price)),
            time_in_force=tif,
            post_only=post_only,
            client_order_id=client_order_id,
        )

    async def submit(self, order: Order, leverage: object = 10) -> int:
        await self.client._submit_order(
            SubmitOrder(
                trader_id=order.trader_id,
                strategy_id=order.strategy_id,
                order=order,
                command_id=UUID4(),
                ts_init=self.clock.timestamp_ns(),
                params={"leverage": leverage},
            )
        )
        payload = cast(dict[str, object], cast(list[object], self.fake.sent[-1])[3])
        return cast(int, payload["cid"])

    async def modify(self, order: Order, *, price: str, leverage: object = 10) -> None:
        await self.client._modify_order(
            ModifyOrder(
                trader_id=order.trader_id,
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                quantity=None,
                price=self.instrument.make_price(Decimal(price)),
                trigger_price=None,
                command_id=UUID4(),
                ts_init=self.clock.timestamp_ns(),
                params={"leverage": leverage},
            )
        )

    async def cancel(self, order: Order) -> None:
        await self.client._cancel_order(
            CancelOrder(
                trader_id=order.trader_id,
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                command_id=UUID4(),
                ts_init=self.clock.timestamp_ns(),
            )
        )

    def order_frame(
        self,
        operation: str,
        cid: int,
        order: Order,
        *,
        remaining: str | None = None,
        status: str = "ACTIVE",
        price: str | None = None,
    ) -> list[object]:
        signed = Decimal(str(order.quantity))
        if order.side == OrderSide.SELL:
            signed = -signed
        remaining_qty = signed if remaining is None else Decimal(remaining)
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
                remaining_qty,
                signed,
                "IOC" if order.time_in_force == TimeInForce.IOC else "LIMIT",
                None,
                None,
                None,
                POST_ONLY_FLAG if order.is_post_only else 0,
                status,
                None,
                None,
                Decimal(price) if price is not None else Decimal(str(order.price)),
                Decimal("0"),
            ],
        ]

    @staticmethod
    def trade_frame(
        cid: int,
        order: Order,
        *,
        trade_id: int = 1234,
        quantity: str = "1",
        price: str = "3926.75",
        order_price: str | None = None,
        fee: str = "-0.10",
        maker: int = 1,
    ) -> list[object]:
        signed = Decimal(quantity)
        if order.side == OrderSide.SELL:
            signed = -signed
        return [
            0,
            "tu",
            [
                trade_id,
                RAW_SYMBOL,
                1_700_000_000_123,
                VENUE_ORDER_ID,
                signed,
                Decimal(price),
                "IOC" if order.time_in_force == TimeInForce.IOC else "LIMIT",
                Decimal(order_price) if order_price is not None else Decimal(str(order.price)),
                maker,
                Decimal(fee),
                "USTF0",
                cid,
            ],
        ]


def _types(harness: _Harness) -> list[str]:
    return [type(event).__name__ for event in harness.events]


def _position_row(
    quantity: Decimal,
    *,
    avg_px: Decimal,
    raw_symbol: str = RAW_SYMBOL,
) -> list[object]:
    return [
        raw_symbol,
        "ACTIVE",
        quantity,
        avg_px,
        Decimal(0),
        0,
        Decimal(0),
        Decimal(0),
        Decimal("3000"),
        Decimal(10),
        None,
        44,
        None,
        None,
        None,
        1,
    ]


def _order_events(harness: _Harness) -> list[OrderEvent]:
    return [event for event in harness.events if isinstance(event, OrderEvent)]


def test_connect_authenticates_and_uses_zero_free_when_wallet_available_is_unknown() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect(available=None)
        try:
            assert harness.fake.opened
            auth = cast(dict[str, object], harness.fake.sent[0])
            assert auth["event"] == "auth"
            assert auth["apiKey"] == "TEST-KEY"
            assert auth["filter"] == [f"trading-{RAW_SYMBOL}", "wallet", "notify"]
            assert "TEST-SECRET" not in repr(auth)
            account = next(
                event for event in harness.events if type(event).__name__ == "AccountState"
            )
            assert account.balances[0].total.as_decimal() == Decimal("1000")
            assert account.balances[0].locked.as_decimal() == Decimal("1000")
            assert account.balances[0].free.as_decimal() == Decimal("0")
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_connect_waits_for_data_instrument_before_private_io() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        harness = _Harness(rest=rest, instrument_available=False)

        async def publish_instrument() -> None:
            await asyncio.sleep(0)
            assert rest.user_info_calls == 0
            assert not harness.fake.opened
            assert harness.fake.sent == []
            harness.cache.add_instrument(harness.instrument)

        publish_task = asyncio.create_task(publish_instrument())
        try:
            await harness.connect()
            await publish_task
            assert rest.user_info_calls == 1
            assert harness.fake.opened
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_auth_nonce_remains_monotonic_when_the_clock_would_move_back() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        first = int(cast(str, cast(dict[str, object], harness.fake.sent[0])["authNonce"]))
        await harness.client._disconnect()
        harness.client._last_auth_nonce = first + 1_000_000_000_000
        await harness.connect()
        try:
            second = int(cast(str, cast(dict[str, object], harness.fake.sent[1])["authNonce"]))
            assert second == first + 1_000_000_000_001
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("channel_id", [1, False, None])
def test_authentication_requires_exact_account_channel_zero(channel_id: object) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.fake.queue.put(
            {
                "event": "auth",
                "status": "OK",
                "chanId": channel_id,
                "userId": 269_312,
            }
        )
        with pytest.raises(BitfinexV1ExecutionError, match="authentication failed"):
            await harness.client._connect()
        assert harness.fake.closed

    asyncio.run(scenario())


def test_authentication_binds_the_expected_bitfinex_user() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.fake.queue.put(
            {"event": "auth", "status": "OK", "chanId": 0, "userId": 269_313}
        )
        with pytest.raises(BitfinexV1ExecutionError, match="authentication failed"):
            await harness.client._connect()
        assert harness.fake.closed

    asyncio.run(scenario())


def test_submit_encodes_taker_and_maker_with_distinct_per_order_leverage() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            taker = harness.order(tif=TimeInForce.IOC, post_only=False)
            maker = harness.order(side=OrderSide.SELL)
            await harness.submit(taker, leverage=7)
            await harness.submit(maker, leverage=19)
            taker_payload = cast(dict[str, object], cast(list[object], harness.fake.sent[-2])[3])
            maker_payload = cast(dict[str, object], cast(list[object], harness.fake.sent[-1])[3])
            assert taker_payload["type"] == "IOC"
            assert taker_payload["lev"] == 7
            assert "flags" not in taker_payload
            assert maker_payload["type"] == "LIMIT"
            assert maker_payload["amount"] == "-4.00000000"
            assert maker_payload["lev"] == 19
            assert maker_payload["flags"] == POST_ONLY_FLAG
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_submit_send_failure_stays_unknown_and_is_never_retried() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            harness.fake.fail_next_send = True
            first = harness.order(tif=TimeInForce.IOC, post_only=False)
            with pytest.raises(ConnectionError, match="uncertain"):
                await harness.submit(first)
            assert _types(harness).count("OrderSubmitted") == 1
            assert "OrderRejected" not in _types(harness)
            assert harness.client.execution_hold_reason is not None
            sent = len(harness.fake.sent)
            await harness.client._submit_order(
                SubmitOrder(
                    trader_id=first.trader_id,
                    strategy_id=first.strategy_id,
                    order=harness.order(tif=TimeInForce.IOC, post_only=False),
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                    params={"leverage": 10},
                )
            )
            assert len(harness.fake.sent) == sent
            assert _types(harness)[-1] == "OrderDenied"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_submit_ack_deadlines_do_not_block_concurrent_maker_sides() -> None:
    async def scenario() -> None:
        harness = _Harness(mutation_ack_timeout_ms=100)
        await harness.connect()
        try:
            bid = harness.order(side=OrderSide.BUY)
            ask = harness.order(side=OrderSide.SELL)
            bid_cid = await harness.submit(bid)
            ask_cid = await harness.submit(ask)
            assert len(harness.fake.sent) == 3
            assert not bool(harness.client.execution_hold_reason)
            assert len(harness.client._ack_deadlines) == 2

            await asyncio.sleep(0.15)

            assert harness.client.execution_hold_reason is not None
            assert harness.client._by_cid[bid_cid].unknown_operations == {"submit"}
            assert harness.client._by_cid[ask_cid].unknown_operations == {"submit"}
            assert not harness.client._ack_deadlines
            sent = len(harness.fake.sent)
            await harness.client._submit_order(
                SubmitOrder(
                    trader_id=bid.trader_id,
                    strategy_id=bid.strategy_id,
                    order=harness.order(),
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                    params={"leverage": 10},
                )
            )
            assert len(harness.fake.sent) == sent
            assert _types(harness)[-1] == "OrderDenied"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_targeted_rest_acceptance_binds_pending_submit_for_cancel() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        harness = _Harness(rest=rest)
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            accepted = harness.order_frame("on", cid, order)
            rest.active = [cast(list[object], accepted[2])]

            report = await harness.client.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                )
            )

            assert report is not None
            assert report.order_status == OrderStatus.ACCEPTED
            live = harness.client._by_cid[cid]
            assert live.accepted
            assert live.venue_order_id == VENUE_ORDER_ID
            assert harness.client._cid_by_venue[VENUE_ORDER_ID] == cid
            assert (cid, "submit") not in harness.client._ack_deadlines
            assert harness.client.execution_hold_reason is None
            assert "OrderAccepted" not in _types(harness)

            harness.client._consume_private_frame(accepted)
            assert "OrderAccepted" not in _types(harness)

            await harness.cancel(order)
            assert harness.fake.sent[-1] == [
                0,
                "oc",
                None,
                {"id": VENUE_ORDER_ID},
            ]
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("row_updates", "mismatch"),
    [
        ({6: Decimal("5"), 7: Decimal("5")}, "quantity"),
        ({12: 0}, "post_only"),
        ({16: Decimal("3926.71")}, "price"),
    ],
)
def test_targeted_rest_acceptance_rejects_submit_semantic_mismatch(
    row_updates: dict[int, object],
    mismatch: str,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        harness = _Harness(rest=rest)
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            row = cast(list[object], harness.order_frame("on", cid, order)[2]).copy()
            for row_index, wrong_value in row_updates.items():
                row[row_index] = wrong_value
            rest.active = [row]

            with pytest.raises(BitfinexV1ExecutionError, match=mismatch):
                await harness.client.generate_order_status_report(
                    GenerateOrderStatusReport(
                        instrument_id=order.instrument_id,
                        client_order_id=order.client_order_id,
                        venue_order_id=None,
                        command_id=UUID4(),
                        ts_init=harness.clock.timestamp_ns(),
                    )
                )

            live = harness.client._by_cid[cid]
            assert not live.accepted
            assert live.venue_order_id is None
            assert VENUE_ORDER_ID not in harness.client._cid_by_venue
            assert (cid, "submit") in harness.client._ack_deadlines
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_terminal_history_cannot_create_its_own_zero_flag_post_only_witness() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        harness = _Harness(rest=rest)
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            terminal = cast(
                list[object],
                harness.order_frame("oc", cid, order, status="CANCELED")[2],
            ).copy()
            now_ms = harness.clock.timestamp_ns() // 1_000_000
            terminal[4], terminal[5] = now_ms - 100, now_ms
            terminal[12] = 0
            rest.history = [terminal]

            with pytest.raises(BitfinexV1ExecutionError, match="post_only"):
                await harness.client.generate_order_status_report(
                    GenerateOrderStatusReport(
                        instrument_id=order.instrument_id,
                        client_order_id=order.client_order_id,
                        venue_order_id=None,
                        command_id=UUID4(),
                        ts_init=harness.clock.timestamp_ns(),
                    )
                )

            assert not harness.client._by_cid[cid].accepted
            assert "OrderAccepted" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "acceptance_timing",
    ["rest_only", "before_query", "during_rest"],
)
def test_ws_and_targeted_rest_race_publishes_one_acceptance(
    acceptance_timing: str,
) -> None:
    class WsFirstRest(_FakeRest):
        def __init__(self) -> None:
            super().__init__()
            self.inject: Callable[[], None] | None = None

        async def order_history_by_symbol(
            self,
            symbol: str,
            *,
            start: int | None = None,
            end: int | None = None,
            limit: int = 2_500,
        ) -> object:
            assert symbol == self.expected_symbol
            assert start is not None and end is not None and limit == 2_500
            if self.inject is not None:
                inject, self.inject = self.inject, None
                inject()
            return []

    async def scenario() -> None:
        rest = WsFirstRest()
        harness = _Harness(rest=rest)
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = LiveExecutionEngine(
            loop=asyncio.get_running_loop(),
            msgbus=harness.msgbus,
            cache=harness.cache,
            clock=harness.clock,
            config=LiveExecEngineConfig(
                load_cache=False,
                reconciliation=False,
                inflight_check_interval_ms=0,
                open_check_interval_secs=None,
                position_check_interval_secs=None,
                graceful_shutdown_on_exception=True,
            ),
        )
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(
            TestExecStubs.margin_account(account_id=harness.client.account_id)
        )
        order = harness.order()
        harness.cache.add_order(order)
        published: list[OrderEvent] = []
        harness.msgbus.subscribe(
            topic=f"events.order.{order.strategy_id}",
            handler=published.append,
        )
        await harness.connect()
        engine.start()
        try:
            cid = await harness.submit(order)
            for _ in range(20):
                if order.status == OrderStatus.SUBMITTED:
                    break
                await asyncio.sleep(0)
            assert order.status == OrderStatus.SUBMITTED
            accepted = harness.order_frame("on", cid, order)
            rest.active = [cast(list[object], accepted[2])]
            if acceptance_timing == "before_query":
                harness.client._consume_private_frame(accepted)
            elif acceptance_timing == "during_rest":
                rest.inject = lambda: harness.client._consume_private_frame(accepted)

            await harness.client._query_order(
                QueryOrder(
                    trader_id=order.trader_id,
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                )
            )
            for _ in range(20):
                await asyncio.sleep(0)

            if acceptance_timing == "rest_only":
                harness.client._consume_private_frame(accepted)
                await asyncio.sleep(0)
            assert sum(isinstance(event, OrderAccepted) for event in published) == 1
            assert [type(event).__name__ for event in order.events].count("OrderAccepted") == 1
            assert engine.report_count == (1 if acceptance_timing == "rest_only" else 0)
            assert harness.client._by_cid[cid].venue_order_id == VENUE_ORDER_ID
            await harness.cancel(order)
            assert harness.fake.sent[-1] == [0, "oc", None, {"id": VENUE_ORDER_ID}]
        finally:
            engine.stop()
            await asyncio.sleep(0)
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_modify_ack_deadline_is_sticky_until_authoritative_tu() -> None:
    async def scenario() -> None:
        harness = _Harness(mutation_ack_timeout_ms=100)
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            await harness.modify(order, price="3925.10")
            sent = len(harness.fake.sent)

            await asyncio.sleep(0.15)

            assert harness.client._by_cid[cid].unknown_operations == {"modify"}
            await harness.modify(order, price="3925.20")
            assert len(harness.fake.sent) == sent
            assert _types(harness)[-1] == "OrderModifyRejected"

            harness.client._consume_private_frame(
                harness.trade_frame(cid, order, order_price="3925.10")
            )
            assert harness.client.execution_hold_reason is None
            assert not harness.client._by_cid[cid].unknown_operations
            assert _types(harness)[-2:] == ["OrderUpdated", "OrderFilled"]
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_cancel_ack_deadline_is_sticky_until_authoritative_full_tu() -> None:
    async def scenario() -> None:
        harness = _Harness(mutation_ack_timeout_ms=100)
        await harness.connect()
        try:
            order = harness.order(quantity="1")
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            await harness.cancel(order)
            sent = len(harness.fake.sent)

            await asyncio.sleep(0.15)

            assert harness.client._by_cid[cid].unknown_operations == {"cancel"}
            await harness.cancel(order)
            assert len(harness.fake.sent) == sent
            assert _types(harness)[-1] == "OrderCancelRejected"

            harness.client._consume_private_frame(harness.trade_frame(cid, order))
            assert harness.client.execution_hold_reason is None
            assert not harness.client._by_cid[cid].unknown_operations
            assert not harness.client._by_cid[cid].pending_cancel
            assert not harness.client._ack_deadlines
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_terminal_order_cancels_all_mutation_deadlines() -> None:
    async def scenario() -> None:
        harness = _Harness(mutation_ack_timeout_ms=100)
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            await harness.modify(order, price="3925.10")
            await harness.cancel(order)
            assert len(harness.client._ack_deadlines) == 2

            harness.client._consume_private_frame(
                harness.order_frame("oc", cid, order, status="CANCELED")
            )
            await asyncio.sleep(0)

            assert not harness.client._ack_deadlines
            assert not harness.client._by_cid[cid].unknown_operations
            assert harness.client.execution_hold_reason is None
            assert _types(harness)[-1] == "OrderCanceled"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_disconnect_cancels_deadlines_and_marks_sent_mutation_unknown() -> None:
    async def scenario() -> None:
        harness = _Harness(mutation_ack_timeout_ms=10_000)
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            deadline = next(iter(harness.client._ack_deadlines.values()))

            await harness.client._disconnect()

            assert not harness.client._ack_deadlines
            assert deadline.cancelled()
            assert harness.client._by_cid[cid].unknown_operations == {"submit"}
            assert harness.client.execution_hold_reason is not None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_cid_persistence_failure_denies_before_submitted_or_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            sent = len(harness.fake.sent)
            order = harness.order()

            def fail_allocate(_client_order_id: str, *, epoch_ms: int) -> None:
                raise OSError(f"synthetic persistence failure at {epoch_ms}")

            monkeypatch.setattr(harness.client._cid_store, "allocate", fail_allocate)
            await harness.client._submit_order(
                SubmitOrder(
                    trader_id=order.trader_id,
                    strategy_id=order.strategy_id,
                    order=order,
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                    params={"leverage": 10},
                )
            )
            assert len(harness.fake.sent) == sent
            assert _types(harness)[-1] == "OrderDenied"
            assert "OrderSubmitted" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_wrong_submit_ack_price_fails_before_acceptance() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            event_count = len(harness.events)
            with pytest.raises(BitfinexV1ExecutionError, match="differs"):
                harness.client._consume_private_frame(
                    harness.order_frame("on", cid, order, price="3926.71")
                )
            assert len(harness.events) == event_count
            assert "OrderAccepted" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("fee", "maker", "commission", "liquidity"),
    [
        ("-0.10", 1, Decimal("0.10"), LiquiditySide.MAKER),
        ("0.03", -1, Decimal("-0.03"), LiquiditySide.TAKER),
        ("0", -1, Decimal("0"), LiquiditySide.TAKER),
    ],
)
def test_tu_is_single_fill_authority_with_real_fee_liquidity_and_dedupe(
    fee: str,
    maker: int,
    commission: Decimal,
    liquidity: LiquiditySide,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(
                tif=TimeInForce.GTC if maker == 1 else TimeInForce.IOC,
                post_only=maker == 1,
            )
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            trade = harness.trade_frame(cid, order, fee=fee, maker=maker)
            interim = cast(list[object], trade[2]).copy()
            interim[9:11] = [None, None]
            harness.client._consume_private_frame([0, "te", interim])
            harness.client._consume_private_frame(trade)
            harness.client._consume_private_frame(trade)
            fills = [
                event
                for event in _order_events(harness)
                if type(event).__name__ == "OrderFilled"
            ]
            assert len(fills) == 1
            fill = fills[0]
            assert fill.trade_id.value == "1234"
            assert fill.commission.as_decimal() == commission
            assert fill.liquidity_side == liquidity
            assert fill.info["bitfinex_fee"] == fee
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_canceled_oc_waits_for_authoritative_late_tu_then_cancels_once() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(
                harness.order_frame(
                    "oc",
                    cid,
                    order,
                    remaining="3",
                    status="CANCELED was: PARTIALLY FILLED @ 3926.75(1)",
                )
            )
            assert _types(harness)[-1] == "OrderAccepted"
            assert harness.client.execution_hold_reason is not None
            harness.client._consume_private_frame(harness.trade_frame(cid, order))
            assert _types(harness)[-2:] == ["OrderFilled", "OrderCanceled"]
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_partial_update_is_not_terminal_and_executed_oc_is_not_a_cancel() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(quantity="1")
            cid = await harness.submit(order)
            harness.client._consume_private_frame(
                harness.order_frame(
                    "ou",
                    cid,
                    order,
                    remaining="1",
                    status="PARTIALLY FILLED @ 3926.75(0)",
                )
            )
            assert _types(harness)[-1] == "OrderAccepted"
            harness.client._consume_private_frame(
                harness.order_frame(
                    "oc",
                    cid,
                    order,
                    remaining="0",
                    status="EXECUTED @ 3926.75(1)",
                )
            )
            assert "OrderCanceled" not in _types(harness)
            harness.client._consume_private_frame(harness.trade_frame(cid, order))
            assert _types(harness)[-1] == "OrderFilled"
            assert "OrderCanceled" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_fully_filled_canceled_oc_never_emits_cancel_after_final_tu() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(quantity="1")
            cid = await harness.submit(order)
            harness.client._consume_private_frame(
                harness.order_frame(
                    "oc",
                    cid,
                    order,
                    remaining="0",
                    status="CANCELED was: PARTIALLY FILLED @ 3926.75(1)",
                )
            )
            assert "OrderCanceled" not in _types(harness)
            harness.client._consume_private_frame(harness.trade_frame(cid, order))
            assert _types(harness)[-1] == "OrderFilled"
            assert "OrderCanceled" not in _types(harness)
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_terminal_quantity_conflict_rejects_late_tu_before_emitting_a_fill() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(
                harness.order_frame("oc", cid, order, remaining="4", status="CANCELED")
            )
            assert _types(harness)[-1] == "OrderCanceled"
            event_count = len(harness.events)
            with pytest.raises(BitfinexV1ExecutionError, match="exceed"):
                harness.client._consume_private_frame(harness.trade_frame(cid, order))
            assert len(harness.events) == event_count
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_unrepresentable_fractional_tu_fails_before_any_order_event() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(quantity="0.00000001")
            cid = await harness.submit(order)
            event_count = len(harness.events)
            with pytest.raises(BitfinexV1ExecutionError, match="precision"):
                harness.client._consume_private_frame(
                    harness.trade_frame(cid, order, quantity="0.000000001")
                )
            assert len(harness.events) == event_count
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_price_only_modify_and_cancel_use_native_ids_and_authoritative_events() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            await harness.modify(order, price="3925.10", leverage=23)
            assert harness.fake.sent[-1] == [
                0,
                "ou",
                None,
                {"id": VENUE_ORDER_ID, "lev": 23, "price": "3925.10"},
            ]
            harness.client._consume_private_frame(
                harness.order_frame("ou", cid, order, price="3925.10")
            )
            assert _types(harness)[-1] == "OrderUpdated"
            await harness.cancel(order)
            assert harness.fake.sent[-1] == [0, "oc", None, {"id": VENUE_ORDER_ID}]
            harness.client._consume_private_frame(
                harness.order_frame(
                    "oc", cid, order, status="CANCELED", price="3925.10"
                )
            )
            assert _types(harness)[-1] == "OrderCanceled"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_modify_ack_accepts_only_current_or_requested_price() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            await harness.modify(order, price="3925.10", leverage=23)
            event_count = len(harness.events)
            with pytest.raises(BitfinexV1ExecutionError, match="differs"):
                harness.client._consume_private_frame(
                    harness.order_frame("ou", cid, order, price="3925.11")
                )
            assert len(harness.events) == event_count
            assert _types(harness).count("OrderUpdated") == 0
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_late_tu_keeps_the_price_version_used_before_modify_ack() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            trade = harness.trade_frame(cid, order)
            interim = cast(list[object], trade[2]).copy()
            interim[9:11] = [None, None]
            harness.client._consume_private_frame([0, "te", interim])
            await harness.modify(order, price="3925.10", leverage=23)
            harness.client._consume_private_frame(
                harness.order_frame("ou", cid, order, price="3925.10")
            )
            harness.client._consume_private_frame(trade)
            assert _types(harness)[-1] == "OrderFilled"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_tu_at_pending_price_confirms_modify_before_fill_and_late_ou_is_noop() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(quantity="1")
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError):
                await harness.modify(order, price="3925.10", leverage=23)
            harness.client._consume_private_frame(
                harness.trade_frame(cid, order, order_price="3925.10")
            )
            assert _types(harness)[-2:] == ["OrderUpdated", "OrderFilled"]
            assert harness.client.execution_hold_reason is None
            event_count = len(harness.events)
            harness.client._consume_private_frame(
                harness.order_frame(
                    "ou",
                    cid,
                    order,
                    remaining="0",
                    status="EXECUTED @ 3926.75(1)",
                    price="3925.10",
                )
            )
            assert len(harness.events) == event_count
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_cancel_of_other_accepted_order_is_allowed_while_submit_is_unknown() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            accepted = harness.order()
            cid = await harness.submit(accepted)
            harness.client._consume_private_frame(harness.order_frame("on", cid, accepted))
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError):
                await harness.submit(harness.order(tif=TimeInForce.IOC, post_only=False))
            await harness.cancel(accepted)
            assert harness.fake.sent[-1] == [0, "oc", None, {"id": VENUE_ORDER_ID}]
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status",
    ["POSTONLY CANCELED", "INSUFFICIENT MARGIN", "RSN_DUST", "RSN_PAUSE"],
)
def test_first_explicit_terminal_failure_is_rejected_without_accept(status: str) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(
                harness.order_frame("oc", cid, order, status=status)
            )
            assert _types(harness)[-1] == "OrderRejected"
            assert "OrderAccepted" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_initial_insufficient_margin_partial_fill_waits_for_tu_then_cancels() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(
                harness.order_frame(
                    "oc",
                    cid,
                    order,
                    remaining="3",
                    status="INSUFFICIENT MARGIN was: PARTIALLY FILLED @ 3926.75(1)",
                )
            )
            assert _types(harness)[-1] == "OrderAccepted"
            assert "OrderRejected" not in _types(harness)
            assert harness.client.execution_hold_reason is not None
            harness.client._consume_private_frame(harness.trade_frame(cid, order))
            assert _types(harness)[-2:] == ["OrderFilled", "OrderCanceled"]
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_executed_terminal_requires_full_order_quantity_before_acceptance() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            event_count = len(harness.events)
            with pytest.raises(BitfinexV1ExecutionError, match="full order quantity"):
                harness.client._consume_private_frame(
                    harness.order_frame(
                        "oc",
                        cid,
                        order,
                        remaining="3",
                        status="EXECUTED @ 3926.75(1)",
                    )
                )
            assert len(harness.events) == event_count
            assert "OrderAccepted" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_notification_errors_map_to_submit_modify_and_cancel_rejections() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            submit_order = harness.order()
            submit_cid = await harness.submit(submit_order)
            harness.client._consume_private_frame(
                _notification("on-req", cid=submit_cid, venue_order_id=None)
            )
            assert _types(harness)[-1] == "OrderRejected"

            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            await harness.modify(order, price="3925.10", leverage=15)
            harness.client._consume_private_frame(
                _notification("ou-req", cid=cid, venue_order_id=VENUE_ORDER_ID)
            )
            assert _types(harness)[-1] == "OrderModifyRejected"
            await harness.cancel(order)
            harness.client._consume_private_frame(
                _notification("oc-req", cid=cid, venue_order_id=VENUE_ORDER_ID)
            )
            assert _types(harness)[-1] == "OrderCancelRejected"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_unrelated_notification_cannot_clear_an_unknown_cancel() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError):
                await harness.cancel(order)
            assert harness.client.execution_hold_reason is not None
            with pytest.raises(BitfinexV1ExecutionError, match=r"unexpected.*modify"):
                harness.client._consume_private_frame(
                    _notification("ou-req", cid=cid, venue_order_id=VENUE_ORDER_ID)
                )
            assert harness.client.execution_hold_reason is not None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_cancel_rejection_cannot_clear_an_earlier_unknown_modify() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError):
                await harness.modify(order, price="3925.10")
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError):
                await harness.cancel(order)
            harness.client._consume_private_frame(
                _notification("oc-req", cid=cid, venue_order_id=VENUE_ORDER_ID)
            )
            assert harness.client.execution_hold_reason is not None
            harness.client._consume_private_frame(
                harness.order_frame("oc", cid, order, status="CANCELED")
            )
            assert _types(harness)[-1] == "OrderCanceled"
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_wrong_notification_venue_id_cannot_clear_unknown_cancel() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError):
                await harness.cancel(order)
            with pytest.raises(BitfinexV1ExecutionError, match="venue order ID"):
                harness.client._consume_private_frame(
                    _notification("oc-req", cid=cid, venue_order_id=VENUE_ORDER_ID + 1)
                )
            assert harness.client.execution_hold_reason is not None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_authoritative_terminal_clears_unknown_modify_and_rejects_late_update() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError):
                await harness.modify(order, price="3925.10")
            assert bool(harness.client.execution_hold_reason)
            terminal = harness.order_frame("oc", cid, order, status="CANCELED")
            harness.client._consume_private_frame(terminal)
            assert _types(harness)[-1] == "OrderCanceled"
            assert harness.client.execution_hold_reason is None
            event_count = len(harness.events)
            with pytest.raises(BitfinexV1ExecutionError, match="terminal"):
                harness.client._consume_private_frame(
                    harness.order_frame("ou", cid, order, price="3925.10")
                )
            assert len(harness.events) == event_count
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_submit_failure_is_irreversible_and_duplicate_failure_is_idempotent() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            failure = _notification(
                "on-req",
                cid=cid,
                venue_order_id=None,
                status="FAILURE",
            )
            harness.client._consume_private_frame(failure)
            event_count = len(harness.events)
            harness.client._consume_private_frame(failure)
            assert len(harness.events) == event_count
            with pytest.raises(BitfinexV1ExecutionError, match="definitive rejection"):
                harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            with pytest.raises(BitfinexV1ExecutionError, match="definitive rejection"):
                harness.client._consume_private_frame(harness.trade_frame(cid, order))
            assert len(harness.events) == event_count
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_success_notification_is_not_an_order_acknowledgment() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            event_count = len(harness.events)
            harness.client._consume_private_frame(
                _notification("on-req", cid=cid, venue_order_id=None, status="SUCCESS")
            )
            assert len(harness.events) == event_count
            assert "OrderAccepted" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "frame",
    [[0, "hb", None], [0, "te"], [False, "hb"]],
)
def test_malformed_heartbeat_and_interim_trade_fail_closed(frame: list[object]) -> None:
    async def scenario() -> None:
        harness = _Harness()
        with pytest.raises(ValueError):
            harness.client._consume_private_frame(frame)

    asyncio.run(scenario())


def test_pre_ack_cancel_and_non_price_modify_fail_closed_without_wire_send() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            await harness.submit(order)
            sent = len(harness.fake.sent)
            await harness.cancel(order)
            assert len(harness.fake.sent) == sent
            assert _types(harness)[-1] == "OrderCancelRejected"
            await harness.client._modify_order(
                ModifyOrder(
                    trader_id=order.trader_id,
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=None,
                    quantity=harness.instrument.make_qty(5),
                    price=None,
                    trigger_price=None,
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                    params={"leverage": 10},
                )
            )
            assert len(harness.fake.sent) == sent
            assert _types(harness)[-1] == "OrderModifyRejected"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_owned_rest_rows_generate_reports_but_unanchored_mass_status_holds() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        harness = _Harness(rest=rest)
        now_ms = harness.clock.timestamp_ns() // 1_000_000
        binding = harness.client._cid_store.allocate("REPORT-OWNED-1", epoch_ms=now_ms - 2_000)
        rest.active = [
            [
                VENUE_ORDER_ID,
                None,
                binding.cid,
                RAW_SYMBOL,
                now_ms - 2_000,
                now_ms - 1_000,
                Decimal("2"),
                Decimal("4"),
                "LIMIT",
                None,
                None,
                None,
                POST_ONLY_FLAG,
                "ACTIVE",
                None,
                None,
                Decimal("3926.70"),
                Decimal("3926.75"),
            ]
        ]
        rest.trades = [
            [
                1234,
                RAW_SYMBOL,
                now_ms - 1_500,
                VENUE_ORDER_ID,
                Decimal("2"),
                Decimal("3926.75"),
                "LIMIT",
                Decimal("3926.70"),
                1,
                Decimal("-0.10"),
                "USTF0",
                binding.cid,
            ]
        ]
        rest.position_rows = [
            [
                RAW_SYMBOL,
                "ACTIVE",
                Decimal("2"),
                Decimal("3926.75"),
                Decimal(0),
                0,
                Decimal(0),
                Decimal(0),
                Decimal("3000"),
                Decimal(10),
                None,
                44,
                now_ms - 2_000,
                now_ms - 1_000,
                None,
                1,
            ]
        ]
        start = datetime.fromtimestamp((now_ms - 60_000) / 1_000, UTC)

        orders = await harness.client.generate_order_status_reports(
            GenerateOrderStatusReports(
                instrument_id=SOURCE_ID,
                start=start,
                end=None,
                open_only=False,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        fills = await harness.client.generate_fill_reports(
            GenerateFillReports(
                instrument_id=SOURCE_ID,
                venue_order_id=None,
                start=start,
                end=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )
        positions = await harness.client.generate_position_status_reports(
            GeneratePositionStatusReports(
                instrument_id=SOURCE_ID,
                start=None,
                end=None,
                command_id=UUID4(),
                ts_init=harness.clock.timestamp_ns(),
            )
        )

        assert len(orders) == len(fills) == len(positions) == 1
        assert orders[0].client_order_id == ClientOrderId("REPORT-OWNED-1")
        assert orders[0].order_status == OrderStatus.PARTIALLY_FILLED
        assert orders[0].filled_qty.as_decimal() == Decimal("2")
        assert fills[0].client_order_id == ClientOrderId("REPORT-OWNED-1")
        assert fills[0].commission.as_decimal() == Decimal("0.10")
        assert positions[0].position_side == PositionSide.LONG
        assert positions[0].venue_position_id is None

        with pytest.raises(BitfinexV1ExecutionError, match="position differs"):
            await harness.client.generate_mass_status(lookback_mins=1)

        terminal_row = cast(list[object], rest.active[0]).copy()
        terminal_row[13] = "CANCELED"
        rest.active = []
        rest.history = [terminal_row]
        with pytest.raises(BitfinexV1ExecutionError, match="position differs"):
            await harness.client.generate_mass_status(lookback_mins=1)

    asyncio.run(scenario())


def test_order_history_bisects_full_pages_and_fails_on_one_ms_saturation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HistoryRest(_FakeRest):
        def __init__(self, rows: list[object], *, timestamp_index: int) -> None:
            super().__init__()
            self.rows = rows
            self.timestamp_index = timestamp_index
            self.calls = 0

        async def order_history_by_symbol(
            self,
            symbol: str,
            *,
            start: int | None = None,
            end: int | None = None,
            limit: int = 2,
        ) -> object:
            assert symbol == RAW_SYMBOL and start is not None and end is not None
            self.calls += 1
            return [
                row
                for row in self.rows
                if start
                <= cast(int, cast(list[object], row)[self.timestamp_index])
                <= end
            ][:limit]

    async def scenario() -> None:
        monkeypatch.setattr(execution_module, "_REPORT_PAGE_LIMIT", 2)
        now_ms = TestComponentStubs.clock().timestamp_ns() // 1_000_000
        by_creation = HistoryRest(
            [
                [1, None, None, None, now_ms - 9, now_ms + 10],
                [2, None, None, None, now_ms - 1, now_ms + 10],
            ],
            timestamp_index=4,
        )
        harness = _Harness(rest=by_creation)
        rows = await harness.client._order_history_rows(now_ms - 10, now_ms)
        assert rows == by_creation.rows
        assert by_creation.calls > 1

        by_update = HistoryRest(
            [
                [1, None, None, None, now_ms - 100, now_ms - 9],
                [2, None, None, None, now_ms - 100, now_ms - 1],
            ],
            timestamp_index=5,
        )
        harness = _Harness(rest=by_update)
        rows = await harness.client._order_history_rows(now_ms - 10, now_ms)
        assert rows == by_update.rows
        assert by_update.calls > 1

        saturated = HistoryRest(
            [
                [1, None, None, None, now_ms, now_ms],
                [2, None, None, None, now_ms, now_ms],
            ],
            timestamp_index=5,
        )
        harness = _Harness(rest=saturated)
        with pytest.raises(BitfinexV1ExecutionError, match="one-millisecond"):
            await harness.client._order_history_rows(now_ms - 10, now_ms)

    asyncio.run(scenario())


def test_order_history_requires_one_consistent_timestamp_axis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ScriptedHistoryRest(_FakeRest):
        def __init__(self, pages: list[list[object]]) -> None:
            super().__init__()
            self.pages = pages

        async def order_history_by_symbol(
            self,
            symbol: str,
            *,
            start: int | None = None,
            end: int | None = None,
            limit: int = 2,
        ) -> object:
            assert symbol == RAW_SYMBOL and start is not None and end is not None
            assert limit == 2
            return self.pages.pop(0)

    async def scenario() -> None:
        monkeypatch.setattr(execution_module, "_REPORT_PAGE_LIMIT", 2)
        now_ms = TestComponentStubs.clock().timestamp_ns() // 1_000_000
        lower, upper = now_ms - 10, now_ms
        both_in_whole_window: list[object] = [
            [1, None, None, None, lower + 1, lower + 2],
            [2, None, None, None, upper - 2, upper - 1],
        ]
        creation_only_in_lower: list[object] = [
            [3, None, None, None, lower + 1, upper - 1]
        ]
        update_only_in_upper: list[object] = [
            [4, None, None, None, lower + 1, upper - 1]
        ]
        rest = ScriptedHistoryRest(
            [both_in_whole_window, creation_only_in_lower, update_only_in_upper]
        )
        harness = _Harness(rest=rest)

        with pytest.raises(BitfinexV1ExecutionError, match="no consistent"):
            await harness.client._order_history_rows(lower, upper)

        outside = ScriptedHistoryRest(
            [[[5, None, None, None, lower - 2, upper + 2]]]
        )
        harness = _Harness(rest=outside)
        with pytest.raises(BitfinexV1ExecutionError, match="no consistent"):
            await harness.client._order_history_rows(lower, upper)

    asyncio.run(scenario())


def test_trade_history_uses_overlap_dedup_and_rejects_stalled_full_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TradeRest(_FakeRest):
        def __init__(self, rows: list[object]) -> None:
            super().__init__()
            self.rows = rows

        async def trades_by_symbol(
            self,
            symbol: str,
            *,
            start: int | None = None,
            end: int | None = None,
            limit: int = 2,
        ) -> object:
            assert symbol == RAW_SYMBOL and start is not None and end is not None
            return [
                row
                for row in self.rows
                if start <= cast(int, cast(list[object], row)[2]) <= end
            ][:limit]

    async def scenario() -> None:
        monkeypatch.setattr(execution_module, "_REPORT_PAGE_LIMIT", 2)
        now_ms = TestComponentStubs.clock().timestamp_ns() // 1_000_000
        complete = TradeRest(
            [
                [1, RAW_SYMBOL, now_ms - 3],
                [2, RAW_SYMBOL, now_ms - 2],
                [3, RAW_SYMBOL, now_ms - 1],
            ]
        )
        harness = _Harness(rest=complete)
        rows = await harness.client._trade_history_rows(now_ms - 4, now_ms)
        assert [cast(list[object], row)[0] for row in rows] == [1, 2, 3]

        stalled = TradeRest(
            [[1, RAW_SYMBOL, now_ms], [2, RAW_SYMBOL, now_ms]]
        )
        harness = _Harness(rest=stalled)
        with pytest.raises(BitfinexV1ExecutionError, match="pagination is saturated"):
            await harness.client._trade_history_rows(now_ms, now_ms)

    asyncio.run(scenario())


def test_cid_store_blocks_duplicate_after_restart_and_next_id_is_monotonic(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "cids.json"
        first = _Harness(cid_store_path=path)
        await first.connect()
        first_order = first.order()
        first_cid = await first.submit(first_order)
        await first.close()

        restarted = _Harness(cid_store_path=path)
        await restarted.connect()
        try:
            duplicate = restarted.order()
            await restarted.client._submit_order(
                SubmitOrder(
                    trader_id=duplicate.trader_id,
                    strategy_id=duplicate.strategy_id,
                    order=duplicate,
                    command_id=UUID4(),
                    ts_init=restarted.clock.timestamp_ns(),
                    params={"leverage": 10},
                )
            )
            assert _types(restarted)[-1] == "OrderDenied"
            assert len(restarted.fake.sent) == 1
            next_order = restarted.order(client_order_id=ClientOrderId("O-NEXT"))
            next_cid = await restarted.submit(next_order)
            assert next_cid > first_cid
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_reconnect_with_unresolved_order_is_blocked_until_reconciliation() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            await harness.client._disconnect()
            with pytest.raises(BitfinexV1ExecutionError, match="requires reconciliation"):
                await harness.client._connect()
            assert harness.client.execution_hold_reason is not None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_open_order_rehydrates_from_reconciled_cache_and_deduplicates_old_trade() -> None:
    async def scenario() -> None:
        harness = _Harness()
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(
            msgbus=harness.msgbus,
            cache=harness.cache,
            clock=harness.clock,
        )
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
        order = harness.order()
        harness.cache.add_order(order)
        await harness.connect()
        try:
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            trade = harness.trade_frame(cid, order)
            harness.client._consume_private_frame(trade)
            assert order.status == OrderStatus.PARTIALLY_FILLED
            assert order.filled_qty.as_decimal() == Decimal("1")

            harness.client._by_cid.clear()
            harness.client._cid_by_client.clear()
            harness.client._cid_by_venue.clear()
            harness.client._seen_trades.clear()
            event_count = engine.event_count

            harness.client._consume_private_frame(trade)
            assert engine.event_count == event_count
            await harness.modify(order, price="3925.10")
            assert cast(list[object], harness.fake.sent[-1])[1] == "ou"
            assert harness.client._by_cid[cid].filled_qty == Decimal("1")
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_live_engine_reconciliation_requires_consistent_open_order_report() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        harness = _Harness(rest=rest)
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = LiveExecutionEngine(
            loop=asyncio.get_running_loop(),
            msgbus=harness.msgbus,
            cache=harness.cache,
            clock=harness.clock,
            config=LiveExecEngineConfig(
                load_cache=False,
                reconciliation=True,
                reconciliation_lookback_mins=None,
                generate_missing_orders=False,
                inflight_check_interval_ms=0,
                open_check_interval_secs=None,
                position_check_interval_secs=None,
            ),
        )
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(
            TestExecStubs.margin_account(account_id=harness.client.account_id)
        )
        order = harness.order(client_order_id=ClientOrderId("ENGINE-OPEN-1"))
        harness.cache.add_order(order)
        await harness.connect()
        try:
            cid = await harness.submit(order)
            frame = harness.order_frame("on", cid, order)
            harness.client._consume_private_frame(frame)
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            harness.cache.update_order(order)
            order.apply(
                TestEventStubs.order_accepted(
                    order,
                    account_id=harness.client.account_id,
                    venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            harness.cache.update_order(order)
            assert order.status == OrderStatus.ACCEPTED
            assert harness.cache.orders_open(instrument_id=SOURCE_ID) == [order]

            rest.active = []
            assert await engine.reconcile_execution_state(timeout_secs=1.0) is False
            assert order.status == OrderStatus.ACCEPTED

            wrong_identity = cast(list[object], frame[2]).copy()
            wrong_identity[0] = VENUE_ORDER_ID + 1
            rest.active = [wrong_identity]
            assert await engine.reconcile_execution_state(timeout_secs=1.0) is False
            assert order.status == OrderStatus.ACCEPTED

            wrong_price = cast(list[object], frame[2]).copy()
            wrong_price[16] = Decimal("3926.80")
            rest.active = [wrong_price]
            assert await engine.reconcile_execution_state(timeout_secs=1.0) is False
            assert order.price == harness.instrument.make_price(Decimal("3926.70"))

            rest.active = [cast(list[object], frame[2])]
            rest.position_rows = [
                _position_row(Decimal("1"), avg_px=Decimal("3926.75"))
            ]
            assert await engine.reconcile_execution_state(timeout_secs=1.0) is False

            rest.position_rows = []
            assert await engine.reconcile_execution_state(timeout_secs=1.0) is True
            assert order.status == OrderStatus.ACCEPTED

            trade_ts_ms = harness.clock.timestamp_ns() // 1_000_000
            first_filled_frame = harness.order_frame(
                "on",
                cid,
                order,
                remaining="3",
            )
            cast(list[object], first_filled_frame[2])[17] = Decimal("3926.75")
            first_trade_row = cast(list[object], harness.trade_frame(cid, order)[2]).copy()
            first_trade_row[2] = trade_ts_ms
            rest.active = [cast(list[object], first_filled_frame[2])]
            rest.trades = [first_trade_row]
            rest.position_rows = [
                _position_row(Decimal("1"), avg_px=Decimal("3926.75"))
            ]
            assert await engine.reconcile_execution_state(timeout_secs=1.0) is True
            assert order.status == OrderStatus.PARTIALLY_FILLED
            assert order.filled_qty == harness.instrument.make_qty(Decimal("1"))
            assert sum(
                (
                    position.signed_decimal_qty()
                    for position in harness.cache.positions_open(
                        instrument_id=SOURCE_ID,
                        account_id=harness.client.account_id,
                    )
                ),
                Decimal(),
            ) == Decimal("1")
            first_fill = next(
                event for event in order.events if isinstance(event, OrderFilled)
            )
            assert first_fill.position_id is not None
            second_fill = TestEventStubs.order_filled(
                order=order,
                instrument=harness.instrument,
                account_id=harness.client.account_id,
                venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                trade_id=TradeId("1235"),
                last_qty=harness.instrument.make_qty(Decimal("2")),
                last_px=harness.instrument.make_price(Decimal("3926.80")),
                liquidity_side=LiquiditySide.MAKER,
                commission=Money(Decimal("0.20"), harness.instrument.quote_currency),
                ts_event=trade_ts_ms * 1_000_000,
            )
            ExecutionEngine.process(engine, second_fill)
            assert second_fill.position_id == first_fill.position_id
            assert order.status == OrderStatus.PARTIALLY_FILLED
            filled_frame = harness.order_frame(
                "on",
                cid,
                order,
                remaining="1",
            )
            cast(list[object], filled_frame[2])[17] = Decimal("3926.78333333")
            second_trade_row = cast(
                list[object],
                harness.trade_frame(
                    cid,
                    order,
                    trade_id=1235,
                    quantity="2",
                    price="3926.80",
                    fee="-0.20",
                )[2],
            ).copy()
            second_trade_row[2] = trade_ts_ms
            rest.active = [cast(list[object], filled_frame[2])]
            rest.trades = [first_trade_row, second_trade_row]
            rest.position_rows = [
                _position_row(Decimal("3"), avg_px=Decimal("3926.78333333"))
            ]
            assert await harness.client.generate_mass_status(lookback_mins=None) is not None
            rest.trades = [second_trade_row]
            assert await harness.client.generate_mass_status(lookback_mins=None) is not None

            wrong_average = cast(list[object], filled_frame[2]).copy()
            wrong_average[17] = Decimal("3926.79333333")
            rest.active = [wrong_average]
            with pytest.raises(BitfinexV1ExecutionError, match="average price"):
                await harness.client.generate_mass_status(lookback_mins=None)

            rest.active = [cast(list[object], filled_frame[2])]
            unknown_fill = second_trade_row.copy()
            unknown_fill[0] = 1236
            rest.trades = [unknown_fill]
            with pytest.raises(BitfinexV1ExecutionError, match="unknown fill"):
                await harness.client.generate_mass_status(lookback_mins=None)

            conflicting_fill = second_trade_row.copy()
            conflicting_fill[5] = Decimal("3926.81")
            rest.trades = [conflicting_fill]
            with pytest.raises(BitfinexV1ExecutionError, match="last_px"):
                await harness.client.generate_mass_status(lookback_mins=None)

            regressed_frame = harness.order_frame("on", cid, order, remaining="2")
            cast(list[object], regressed_frame[2])[17] = Decimal("3926.78")
            rest.active = [cast(list[object], regressed_frame[2])]
            rest.trades = [first_trade_row, second_trade_row]
            with pytest.raises(BitfinexV1ExecutionError, match="regressed"):
                await harness.client.generate_mass_status(lookback_mins=None)
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_config_rejects_account_issuer_or_client_id_mismatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        base = {
            "url": "wss://api.bitfinex.com/ws/2",
            "api_key": "KEY",
            "api_secret": "SECRET",
            "user_id": 269_312,
            "instrument_id": SOURCE_ID,
            "raw_symbol": RAW_SYMBOL,
            "cid_store_path": str(tmp_path / "cids.json"),
        }
        with pytest.raises(ValueError, match="issuer"):
            BitfinexV1ExecutionClient(
                loop=asyncio.get_running_loop(),
                name="BITFINEX",
                config=BitfinexV1ExecClientConfig(
                    account_id=AccountId("OTHER-001"),
                    **base,
                ),
                msgbus=TestComponentStubs.msgbus(),
                cache=TestComponentStubs.cache(),
                clock=clock,
                instrument_provider=InstrumentProvider(),
                transport=_FakeTransport(),
            )
        with pytest.raises(ValueError, match="client ID"):
            BitfinexV1ExecutionClient(
                loop=asyncio.get_running_loop(),
                name="BITFINEX-ALT",
                config=BitfinexV1ExecClientConfig(
                    account_id=AccountId("BITFINEX-001"),
                    **base,
                ),
                msgbus=TestComponentStubs.msgbus(),
                cache=TestComponentStubs.cache(),
                clock=clock,
                instrument_provider=InstrumentProvider(),
                transport=_FakeTransport(),
            )

    asyncio.run(scenario())


def test_execution_factory_builds_typed_client_without_connecting(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BitfinexV1ExecClientConfig(
            url="wss://offline.invalid/ws/2",
            rest_url="https://offline.invalid",
            api_key="KEY",
            api_secret="SECRET",
            user_id=269_312,
            account_id=AccountId("BITFINEX-001"),
            instrument_id=SOURCE_ID,
            raw_symbol=RAW_SYMBOL,
            cid_store_path=str(tmp_path / "cids.json"),
        )
        client = BitfinexV1LiveExecClientFactory.create(
            loop=asyncio.get_running_loop(),
            name="BITFINEX",
            config=config,
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        assert isinstance(client, BitfinexV1ExecutionClient)
        assert client.id == ClientId("BITFINEX")
        assert client.venue == Venue("BITFINEX")
        assert not client.is_connected
        with pytest.raises(TypeError):
            BitfinexV1LiveExecClientFactory.create(
                loop=asyncio.get_running_loop(),
                name="BITFINEX",
                config=LiveExecClientConfig(),
                msgbus=TestComponentStubs.msgbus(),
                cache=TestComponentStubs.cache(),
                clock=TestComponentStubs.clock(),
            )

    asyncio.run(scenario())


def test_paper_profile_binds_test_symbol_wallet_and_canonical_instrument() -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
            rest=rest,
        )
        try:
            await harness.connect()
            auth = cast(dict[str, object], harness.fake.sent[0])
            assert auth["filter"] == [f"trading-{PAPER_RAW_SYMBOL}", "wallet", "notify"]
            assert harness.instrument.id == SOURCE_ID
            assert harness.instrument.quote_currency.code == "USDT"
            assert harness.client.execution_hold_reason is None
            assert await harness.client._order_reports(start=None, end=None, open_only=True) == []

            payload = harness.client._submission_payload(
                harness.order(quantity="2"),
                leverage=10,
                cid=1,
            )
            assert cast(dict[str, object], payload[3])["symbol"] == PAPER_RAW_SYMBOL
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("execution_symbol", "wallet_currency", "instrument_symbol"),
    [
        (RAW_SYMBOL, "USTF0", PAPER_RAW_SYMBOL),
        (PAPER_RAW_SYMBOL, "TESTUSDTF0", RAW_SYMBOL),
    ],
)
def test_execution_rejects_crossed_data_and_account_profiles(
    execution_symbol: str,
    wallet_currency: str,
    instrument_symbol: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=execution_symbol,
            wallet_currency=wallet_currency,
            instrument_raw_symbol=instrument_symbol,
        )
        try:
            with pytest.raises(BitfinexV1ExecutionError, match="instrument profile"):
                await harness.connect()
            assert not harness.fake.opened
            assert harness.fake.sent == []
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw_symbol", "wallet_currency", "paper_enabled", "user_id", "message"),
    [
        (RAW_SYMBOL, "USTF0", 1, 269_312, "environments differ"),
        (PAPER_RAW_SYMBOL, "TESTUSDTF0", 0, 269_312, "environments differ"),
        (RAW_SYMBOL, "USTF0", 0, 999_999, "identity does not match"),
    ],
)
def test_rest_identity_must_match_ws_user_and_symbol_environment(
    raw_symbol: str,
    wallet_currency: str,
    paper_enabled: int,
    user_id: int,
    message: str,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest(raw_symbol)
        rest.paper_enabled = paper_enabled
        rest.user_id = user_id
        harness = _Harness(
            raw_symbol=raw_symbol,
            wallet_currency=wallet_currency,
            rest=rest,
        )
        try:
            with pytest.raises(BitfinexV1ExecutionError, match=message):
                await harness.client._connect()
            assert not harness.fake.opened
            assert harness.fake.sent == []
            assert rest.user_info_calls == 1
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_trading_node_builds_data_and_execution_clients_offline(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    data_config = BitfinexV1DataClientConfig(
        url="wss://offline.invalid/ws/2",
        instrument_id=SOURCE_ID,
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
    )
    exec_config = BitfinexV1ExecClientConfig(
        url="wss://offline.invalid/ws/2",
        rest_url="https://offline.invalid",
        api_key="KEY",
        api_secret="SECRET",
        user_id=269_312,
        account_id=AccountId("BITFINEX-001"),
        instrument_id=SOURCE_ID,
        raw_symbol=RAW_SYMBOL,
        cid_store_path=str(tmp_path / "cids.json"),
    )
    node = TradingNode(
        config=TradingNodeConfig(
            trader_id=TraderId("PY000-BFX-EXEC-001"),
            data_clients={"BITFINEX": data_config},
            exec_clients={"BITFINEX": exec_config},
            exec_engine=LiveExecEngineConfig(reconciliation=True),
        ),
        loop=loop,
    )
    try:
        node.add_data_client_factory("BITFINEX", BitfinexV1LiveDataClientFactory)
        node.add_exec_client_factory("BITFINEX", BitfinexV1LiveExecClientFactory)
        node.build()
        assert node.kernel.exec_engine.registered_clients == [ClientId("BITFINEX")]
        assert node.kernel.exec_engine.check_disconnected()
    finally:
        node.dispose()

    assert loop.is_closed()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"instrument_id": InstrumentId.from_str("BTCUSDT.BITFINEX")}, "XAUT"),
        ({"raw_symbol": "tBTCUSD"}, "XAUT"),
        ({"wallet_currency": "USD"}, "USTF0"),
        ({"raw_symbol": PAPER_RAW_SYMBOL}, "TESTUSDTF0"),
        ({"wallet_currency": "TESTUSDTF0"}, "USTF0"),
        ({"user_id": 0}, "user ID"),
        ({"mutation_ack_timeout_ms": 99}, "acknowledgment timeout"),
    ],
)
def test_config_locks_exact_account_instrument_symbol_and_wallet(
    tmp_path: Path,
    override: dict[str, object],
    message: str,
) -> None:
    async def scenario() -> None:
        values: dict[str, object] = {
            "url": "wss://api.bitfinex.com/ws/2",
            "api_key": "KEY",
            "api_secret": "SECRET",
            "user_id": 269_312,
            "account_id": AccountId("BITFINEX-001"),
            "instrument_id": SOURCE_ID,
            "raw_symbol": RAW_SYMBOL,
            "cid_store_path": str(tmp_path / "cids.json"),
        }
        values.update(override)
        with pytest.raises(ValueError, match=message):
            BitfinexV1ExecutionClient(
                loop=asyncio.get_running_loop(),
                name="BITFINEX",
                config=BitfinexV1ExecClientConfig(**cast(Any, values)),
                msgbus=TestComponentStubs.msgbus(),
                cache=TestComponentStubs.cache(),
                clock=TestComponentStubs.clock(),
                instrument_provider=InstrumentProvider(),
                transport=_FakeTransport(),
            )

    asyncio.run(scenario())


def _notification(
    request_type: str,
    *,
    cid: int,
    venue_order_id: int | None,
    status: str = "ERROR",
) -> list[object]:
    return [
        0,
        "n",
        [
            1_700_000_000_200,
            request_type,
            77,
            None,
            {"id": venue_order_id, "cid": cid},
            10020,
            status,
            "request rejected",
        ],
    ]
