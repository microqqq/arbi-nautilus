from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import RoutingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder, ModifyOrder, SubmitOrder
from nautilus_trader.model.enums import LiquiditySide, OrderSide, TimeInForce
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    VenueOrderId,
)
from nautilus_trader.model.orders import Order
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from py000_nautilus.bitfinex_v1_data import (
    INSTRUMENT_ID as SOURCE_ID,
)
from py000_nautilus.bitfinex_v1_data import (
    BitfinexV1DataClientConfig,
    instrument_from_config,
)
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
    BitfinexV1ExecutionError,
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


class _Harness:
    def __init__(self, *, cid_store_path: Path | None = None) -> None:
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
        self.instrument = instrument_from_config(
            BitfinexV1DataClientConfig(
                url="wss://api-pub.bitfinex.com/ws/2",
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
            ),
            ts_init=0,
        )
        self.order_factory = TestComponentStubs.order_factory()
        provider = InstrumentProvider()
        provider.add(self.instrument)
        self.fake = _FakeTransport()
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
                raw_symbol=RAW_SYMBOL,
                cid_store_path=str(cid_store_path),
            ),
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            instrument_provider=provider,
            transport=self.fake,
        )

    async def connect(self, *, available: Decimal | None = Decimal("800")) -> None:
        await self.fake.queue.put({"event": "info", "version": 2})
        await self.fake.queue.put(
            {"event": "auth", "status": "OK", "chanId": 0, "userId": 269_312}
        )
        await self.fake.queue.put(
            [
                0,
                "ws",
                [["margin", "USTF0", Decimal("1000"), Decimal("0"), available]],
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


def test_reports_are_explicitly_unavailable() -> None:
    async def scenario() -> None:
        harness = _Harness()
        with pytest.raises(BitfinexV1ExecutionError, match="reconciliation"):
            await harness.client.generate_order_status_reports(None)
        with pytest.raises(BitfinexV1ExecutionError, match="reconciliation"):
            await harness.client.generate_fill_reports(None)

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


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"instrument_id": InstrumentId.from_str("BTCUSDT.BITFINEX")}, "XAUT"),
        ({"raw_symbol": "tBTCUSD"}, "XAUT"),
        ({"wallet_currency": "USD"}, "USTF0"),
        ({"user_id": 0}, "user ID"),
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
