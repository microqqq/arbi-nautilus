from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from decimal import Overflow as DecimalOverflow
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import msgspec
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
    QueryAccount,
    QueryOrder,
    SubmitOrder,
)
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.events import OrderAccepted, OrderCanceled, OrderEvent, OrderFilled
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
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.serialization.serializer import MsgSpecSerializer
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

import py000_nautilus.bitfinex_v1_execution as execution_module
from py000_nautilus.bitfinex_v1_cids import BitfinexV1CidStore
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
from py000_nautilus.bitfinex_v1_protocol import POST_ONLY_FLAG, REDUCE_ONLY_FLAG
from py000_nautilus.bitfinex_v1_reports import BitfinexV1ReportError
from py000_nautilus.hedge import HedgeCoordinator
from py000_nautilus.models import BusinessOrderSide
from py000_nautilus.store import JsonStateStore

RAW_SYMBOL = "tXAUTF0:USTF0"
VENUE_ORDER_ID = 219_492_782_587


class _FakeTransport:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, object] | list[object]] = asyncio.Queue()
        self.sent: list[dict[str, object] | list[object]] = []
        self.opened = False
        self.closed = False
        self.fail_next_send = False
        self.after_send: Callable[[dict[str, object] | list[object]], None] | None = None

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
        if self.after_send is not None:
            self.after_send(payload)

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
        self.wallet_rows: list[object] = []
        self.wallet_calls = 0
        self.position_calls = 0

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
        self.position_calls += 1
        return self.position_rows

    async def wallets(self) -> object:
        self.wallet_calls += 1
        return self.wallet_rows


class _Harness:
    def __init__(
        self,
        *,
        cid_store_path: Path | None = None,
        mutation_ack_timeout_ms: int = 10_000,
        rest: _FakeRest | None = None,
        raw_symbol: str = RAW_SYMBOL,
        wallet_currency: str = "USTF0",
        fee_currency: str = "USD",
        instrument_raw_symbol: str | None = None,
        instrument_available: bool = True,
        allow_cold_position_reconciliation: bool = False,
        rest_timeout_secs: int = 10,
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
                    Decimal("2") if profile_symbol == PAPER_RAW_SYMBOL else Decimal("0.002")
                ),
                max_quantity=(
                    Decimal("10000") if profile_symbol == PAPER_RAW_SYMBOL else Decimal("400")
                ),
                margin_init=(
                    Decimal("0.01") if profile_symbol == PAPER_RAW_SYMBOL else Decimal("0.1")
                ),
                margin_maint=(
                    Decimal("0.005") if profile_symbol == PAPER_RAW_SYMBOL else Decimal("0.05")
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
                fee_currency=fee_currency,
                cid_store_path=str(cid_store_path),
                mutation_ack_timeout_ms=mutation_ack_timeout_ms,
                allow_cold_position_reconciliation=allow_cold_position_reconciliation,
                rest_timeout_secs=rest_timeout_secs,
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
        self.fee_currency = fee_currency

    async def connect(self, *, available: Decimal | None = Decimal("800")) -> None:
        await self.fake.queue.put({"event": "info", "version": 2})
        await self.fake.queue.put({"event": "auth", "status": "OK", "chanId": 0, "userId": 269_312})
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
        reduce_only: bool = False,
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
            reduce_only=reduce_only,
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
                self.raw_symbol,
                1_700_000_000_000,
                1_700_000_000_100,
                remaining_qty,
                signed,
                "IOC" if order.time_in_force == TimeInForce.IOC else "LIMIT",
                None,
                None,
                None,
                (
                    POST_ONLY_FLAG
                    if order.is_post_only
                    else REDUCE_ONLY_FLAG
                    if order.is_reduce_only
                    else 0
                ),
                status,
                None,
                None,
                Decimal(price) if price is not None else Decimal(str(order.price)),
                Decimal("0"),
            ],
        ]

    def trade_frame(
        self,
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
                self.raw_symbol,
                1_700_000_000_123,
                VENUE_ORDER_ID,
                signed,
                Decimal(price),
                "IOC" if order.time_in_force == TimeInForce.IOC else "LIMIT",
                Decimal(order_price) if order_price is not None else Decimal(str(order.price)),
                maker,
                Decimal(fee),
                self.fee_currency,
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


def _margin_info(harness: _Harness) -> dict[str, Any]:
    account = next(event for event in reversed(harness.events)
                   if type(event).__name__ == "AccountState")
    return cast(dict[str, Any], account.info["bitfinex_margin"])


def _margin_position(*, quantity: str = "-0.75", updated: int = 100, pid: int = 44) -> list[object]:
    row = _position_row(Decimal(quantity), avg_px=Decimal("4050.1"))
    row[11:14] = [pid, 50, updated]
    return [*row, None, Decimal("150.125"), Decimal("15.0125")]


def _position_command(harness: _Harness) -> GeneratePositionStatusReports:
    return GeneratePositionStatusReports(
        instrument_id=SOURCE_ID, start=None, end=None,
        command_id=UUID4(), ts_init=harness.clock.timestamp_ns(),
    )


def _account_command(
    harness: _Harness, *, account: AccountId | None = None, client: ClientId | None = None,
) -> QueryAccount:
    return QueryAccount(
        trader_id=harness.msgbus.trader_id, account_id=account or harness.client.account_id,
        client_id=client, command_id=UUID4(), ts_init=harness.clock.timestamp_ns(),
    )


def _query_account(harness: _Harness) -> asyncio.Task[None]:
    harness.client.query_account(_account_command(harness))
    task = harness.client._account_refresh_task
    assert task is not None
    return task


@pytest.mark.parametrize("positioned", [False, True])
@pytest.mark.parametrize("available", [None, Decimal(600)])
def test_query_account_native_entry_publishes_one_joint_sample_without_pushes(
    positioned: bool, available: Decimal | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(msgbus=harness.msgbus, cache=harness.cache, clock=harness.clock)
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        await harness.connect()
        harness.client._set_connected(True)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            before = _margin_info(harness)
            before_events = len(harness.events)
            harness.rest.wallet_rows = [["margin", "USTF0", Decimal(900), Decimal(0), available]]
            position_read_start = 0

            async def positions() -> object:
                nonlocal position_read_start
                harness.rest.position_calls += 1
                position_read_start = harness.clock.timestamp_ns()
                entered.set()
                await release.wait()
                return [_margin_position()] if positioned else []

            monkeypatch.setattr(harness.rest, "positions", positions)
            for index in range(10):
                engine.execute(_account_command(
                    harness, client=harness.client.id if index % 2 else None,
                ))
            task = harness.client._account_refresh_task
            assert task is not None
            await entered.wait()
            assert harness.rest.wallet_calls == harness.rest.position_calls == 1
            assert len(harness.events) == before_events
            assert _margin_info(harness) == before
            assert not task.done()
            position_read_end = harness.clock.timestamp_ns()
            release.set()
            await task
            after = _margin_info(harness)
            assert len(harness.events) == before_events + 1
            assert after["wallet"]["balance"] == "900"
            assert after["wallet"]["available_balance"] == (
                None if available is None else str(available)
            )
            assert after["wallet"]["current"] is True
            assert after["wallet"]["observed_ns"] <= position_read_start
            assert after["positions"]["observed_ns"] >= position_read_end
            assert after["positions"]["complete"] is True
            assert after["positions"]["current"] is True
            assert (after["positions"]["position"] is not None) is positioned
            assert before["positions"]["complete"] is False
            account_event = harness.events[-1]
            assert account_event.balances[0].free.as_decimal() == (available or Decimal(0))
            assert harness.client._account_refresh_task is None
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["account", "client", "running", "connected", "authenticated"])
def test_query_account_wrong_identity_or_lifecycle_does_not_start_io(bad: str) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        harness.client._set_connected(True)
        try:
            command = _account_command(harness)
            if bad == "account":
                command = _account_command(harness, account=AccountId("BITFINEX-OTHER"))
            elif bad == "client":
                command = _account_command(harness, client=ClientId("OTHER"))
            elif bad == "running":
                harness.client._running = False
            elif bad == "connected":
                harness.client._set_connected(False)
            else:
                harness.client._account_ready = False
            before = len(harness.events)
            harness.client.query_account(command)
            await asyncio.sleep(0)
            assert harness.client._account_refresh_task is None
            assert harness.rest.wallet_calls == harness.rest.position_calls == 0
            assert len(harness.events) == before
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["same_wallet", "position", "roundtrip"])
def test_query_account_joint_sample_drops_any_observed_revision_change(
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        harness.client._set_connected(True)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            harness.client._consume_private_frame([0, "ps", []])
            harness.rest.wallet_rows = [["margin", "USTF0", Decimal(900), Decimal(0), Decimal(500)]]

            async def positions() -> object:
                entered.set()
                await release.wait()
                return []

            monkeypatch.setattr(harness.rest, "positions", positions)
            task = _query_account(harness)
            await entered.wait()
            if change == "same_wallet":
                harness.client._consume_private_frame(
                    [0, "wu", ["margin", "USTF0", Decimal(1000), Decimal(0), Decimal(800)]],
                )
            elif change == "position":
                harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            else:
                harness.client._consume_private_frame([0, "pn", _margin_position()])
                closed = _margin_position(quantity="0", updated=102)
                closed[1] = "CLOSED"
                harness.client._consume_private_frame([0, "pc", closed])
            fresh = _margin_info(harness)
            count = len(harness.events)
            release.set()
            await task
            assert _margin_info(harness) == fresh and len(harness.events) == count
            assert harness.client._account_refresh_task is None
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["missing_wallet", "duplicate_wallet", "bad_wallet", "position"],
)
def test_query_account_bad_candidate_has_no_half_install_and_explicit_retry_recovers(
    failure: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        harness.client._set_connected(True)
        try:
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            before = _margin_info(harness)
            wallet: list[object] = ["margin", "USTF0", Decimal(900), Decimal(0), Decimal(500)]
            harness.rest.wallet_rows = [wallet]
            if failure == "missing_wallet":
                harness.rest.wallet_rows = []
            elif failure == "duplicate_wallet":
                harness.rest.wallet_rows.append(wallet.copy())
            elif failure == "bad_wallet":
                wallet[4] = True
            else:
                harness.rest.position_rows = [_margin_position(), _margin_position(pid=99)]
            with pytest.raises((ValueError, BitfinexV1ExecutionError)):
                await _query_account(harness)
            after = _margin_info(harness)
            assert after["wallet"]["balance"] == before["wallet"]["balance"]
            assert after["wallet"]["observed_ns"] == before["wallet"]["observed_ns"]
            assert after["positions"]["position"] == before["positions"]["position"]
            assert after["positions"]["observed_ns"] == before["positions"]["observed_ns"]
            if failure == "position":
                assert after["positions"]["complete"] is False
            else:
                assert after["wallet"]["current"] is False
            assert harness.client.execution_hold_reason is None
            harness.rest.wallet_rows = [["margin", "USTF0", Decimal(900), Decimal(0), None]]
            harness.rest.position_rows = [_margin_position(updated=103)]
            await _query_account(harness)
            assert _margin_info(harness)["wallet"]["balance"] == "900"
            assert _margin_info(harness)["positions"]["current"] is True
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_query_account_decimal_wallet_arithmetic_overflow_revokes_only_wallet() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        harness.client._set_connected(True)
        try:
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            before = _margin_info(harness)
            # This value is finite but balance - available overflows Decimal's
            # exponent range, before any native Money range conversion occurs.
            available = Decimal("1e1000000")
            assert available.is_finite()
            harness.rest.wallet_rows = [["margin", "USTF0", Decimal(900), Decimal(0), available]]
            with pytest.raises(DecimalOverflow):
                await _query_account(harness)
            after = _margin_info(harness)
            assert after["wallet"]["current"] is False
            assert after["wallet"]["balance"] == before["wallet"]["balance"]
            assert after["wallet"]["available_balance"] == before["wallet"]["available_balance"]
            assert after["wallet"]["observed_ns"] == before["wallet"]["observed_ns"]
            assert after["positions"] == before["positions"]
            assert before["wallet"]["current"] is True
            assert harness.rest.position_calls == 0
            assert harness.client.execution_hold_reason is None
            assert harness.client._account_refresh_task is None
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_query_account_decimal_position_arithmetic_overflow_revokes_only_positions() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        harness.client._set_connected(True)
        try:
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            before = _margin_info(harness)
            harness.rest.wallet_rows = [["margin", "USTF0", Decimal(900), Decimal(0), Decimal(500)]]
            # The report mapper's Decimal abs(amount) overflows before native
            # quantity conversion; this is not a native Quantity range failure.
            harness.rest.position_rows = [_margin_position(quantity="1e1000000")]
            with pytest.raises(DecimalOverflow):
                await _query_account(harness)
            after = _margin_info(harness)
            assert after["positions"]["complete"] is False
            assert after["positions"]["current"] is False
            assert after["positions"]["position"] == before["positions"]["position"]
            assert after["positions"]["observed_ns"] == before["positions"]["observed_ns"]
            assert after["wallet"] == before["wallet"]
            assert before["positions"]["current"] is True
            assert harness.client.execution_hold_reason is None
            assert harness.client._account_refresh_task is None
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["queued", "wallet", "position", "fatal"])
def test_query_account_disconnect_owns_pending_task_and_cannot_cross_reconnect(
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        harness.client._set_connected(True)
        entered, canceled = asyncio.Event(), asyncio.Event()
        try:
            harness.rest.wallet_rows = [["margin", "USTF0", Decimal(900), Decimal(0), None]]

            async def blocked() -> object:
                entered.set()
                try:
                    await asyncio.Event().wait()
                    return []
                finally:
                    canceled.set()

            if stage in {"wallet", "fatal"}:
                monkeypatch.setattr(harness.rest, "wallets", blocked)
            elif stage == "position":
                monkeypatch.setattr(harness.rest, "positions", blocked)
            task = _query_account(harness)
            if stage != "queued":
                await entered.wait()
            if stage == "fatal":
                await harness.fake.queue.put([0, "invalid", []])
                reader = harness.client._reader_task
                assert reader is not None
                await asyncio.wait_for(asyncio.shield(reader), 1)
            else:
                await harness.client._disconnect()
            assert task.done() and task.cancelled()
            assert harness.client._account_refresh_task is None
            if stage == "queued":
                assert harness.rest.wallet_calls == harness.rest.position_calls == 0
            else:
                assert canceled.is_set()
            monkeypatch.undo()
            await harness.connect()
            harness.client._set_connected(True)
            await _query_account(harness)
            assert _margin_info(harness)["wallet"]["balance"] == "900"
            assert _margin_info(harness)["positions"]["current"] is True
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["wallet", "position", "connection"])
def test_query_account_watermarks_are_captured_before_task_start(changed: str) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        harness.client._set_connected(True)
        try:
            task = _query_account(harness)
            # No event-loop turn has run the queued query yet.
            if changed == "wallet":
                harness.client._consume_private_frame(
                    [0, "wu", ["margin", "USTF0", Decimal(1000), Decimal(0), Decimal(800)]],
                )
            elif changed == "position":
                harness.client._consume_private_frame([0, "ps", []])
            else:
                harness.client._invalidate_margin_facts(connection_changed=True)
            fresh = _margin_info(harness)
            await task
            assert harness.rest.wallet_calls == harness.rest.position_calls == 0
            assert _margin_info(harness) == fresh
            assert harness.client._account_refresh_task is None
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
def test_query_account_total_budget_and_cancel_do_not_install_partial_samples(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(rest_timeout_secs=1)
        await harness.connect()
        harness.client._set_connected(True)
        entered = asyncio.Event()
        try:
            harness.client._consume_private_frame([0, "ps", []])
            before = _margin_info(harness)
            count = len(harness.events)
            wallet = ["margin", "USTF0", Decimal(900), Decimal(0), Decimal(500)]

            async def wallets() -> object:
                await asyncio.sleep(0.55 if failure == "timeout" else 0)
                return [wallet]

            async def positions() -> object:
                entered.set()
                await asyncio.sleep(0.55 if failure == "timeout" else 10)
                return []

            monkeypatch.setattr(harness.rest, "wallets", wallets)
            monkeypatch.setattr(harness.rest, "positions", positions)
            started = asyncio.get_running_loop().time()
            task = _query_account(harness)
            await entered.wait()
            if failure == "timeout":
                with pytest.raises(TimeoutError):
                    await task
                assert asyncio.get_running_loop().time() - started < 1.5
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert _margin_info(harness) == before and len(harness.events) == count
            assert harness.client._account_refresh_task is None
            assert harness.client.execution_hold_reason is None
            monkeypatch.undo()
            harness.rest.wallet_rows = [wallet]
            await _query_account(harness)
            assert _margin_info(harness)["wallet"]["balance"] == "900"
            assert _margin_info(harness)["positions"]["current"] is True
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("roundtrip", [False, True])
def test_query_account_native_fills_invalidate_even_if_net_quantity_returns_to_zero(
    monkeypatch: pytest.MonkeyPatch, roundtrip: bool,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(msgbus=harness.msgbus, cache=harness.cache, clock=harness.clock)
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
        await harness.connect()
        harness.client._set_connected(True)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            harness.client._consume_private_frame([0, "ps", []])
            harness.rest.wallet_rows = [["margin", "USTF0", Decimal(900), Decimal(0), Decimal(500)]]

            async def positions() -> object:
                entered.set()
                await release.wait()
                return []

            monkeypatch.setattr(harness.rest, "positions", positions)
            task = _query_account(harness)
            await entered.wait()
            for index, side in enumerate(
                [OrderSide.BUY, OrderSide.SELL] if roundtrip else [OrderSide.BUY],
            ):
                order = harness.order(side=side, tif=TimeInForce.IOC, post_only=False, quantity="1")
                harness.cache.add_order(order)
                venue_id = VenueOrderId(str(VENUE_ORDER_ID + index))
                engine.process(TestEventStubs.order_submitted(
                    order, account_id=harness.client.account_id,
                ))
                engine.process(TestEventStubs.order_accepted(
                    order, account_id=harness.client.account_id, venue_order_id=venue_id,
                ))
                engine.process(TestEventStubs.order_filled(
                    order=order, instrument=harness.instrument,
                    account_id=harness.client.account_id, venue_order_id=venue_id,
                    trade_id=TradeId(str(1234 + index)), commission=Money(0, USD),
                    last_qty=harness.instrument.make_qty(Decimal(1)),
                    last_px=harness.instrument.make_price(Decimal("3926.70")),
                    ts_event=harness.clock.timestamp_ns(),
                ))
                assert order.filled_qty.as_decimal() == 1
            net = sum(
                (position.signed_decimal_qty() for position in harness.cache.positions_open()),
                Decimal(0),
            )
            assert net == (0 if roundtrip else 1)
            invalid = _margin_info(harness)
            assert invalid["wallet"]["current"] is False
            assert invalid["positions"]["current"] is False
            count = len(harness.events)
            release.set()
            await task
            assert _margin_info(harness) == invalid and len(harness.events) == count
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["wallet", "position"])
def test_query_account_late_bad_reply_cannot_revoke_a_newer_complete_sample(
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        harness.client._set_connected(True)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            harness.rest.wallet_rows = [["margin", "USTF0", Decimal(900), Decimal(0), None]]

            async def delayed_bad_reply() -> object:
                entered.set()
                await release.wait()
                return "not a snapshot"

            monkeypatch.setattr(
                harness.rest, "wallets" if stage == "wallet" else "positions", delayed_bad_reply,
            )
            task = _query_account(harness)
            await entered.wait()
            harness.client._consume_private_frame([0, "ps", [_margin_position(updated=103)]])
            harness.client._consume_private_frame(
                [0, "wu", ["margin", "USTF0", Decimal(1100), Decimal(0), Decimal(900)]],
            )
            fresh = _margin_info(harness)
            count = len(harness.events)
            release.set()
            await task
            assert _margin_info(harness) == fresh and len(harness.events) == count
            assert fresh["wallet"]["current"] is True
            assert fresh["positions"]["complete"] is True
            assert fresh["positions"]["current"] is True
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_margin_facts_distinguish_absence_flat_and_independent_observations() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect(available=None)
        try:
            first = _margin_info(harness)
            assert first["wallet"]["available_balance"] is None
            assert first["wallet"]["current"] is True
            assert first["positions"] == {
                "complete": False, "current": False, "observed_ns": None, "position": None,
            }
            before_snapshot = harness.clock.timestamp_ns()
            harness.client._consume_private_frame([0, "ps", []])
            flat = _margin_info(harness)
            assert flat["wallet"] == first["wallet"]
            assert flat["positions"]["complete"] is True
            assert flat["positions"]["current"] is True
            assert flat["positions"]["position"] is None
            assert before_snapshot <= flat["positions"]["observed_ns"]
            assert flat["positions"]["observed_ns"] <= harness.clock.timestamp_ns()
            harness.client._consume_private_frame([0, "pn", _margin_position()])
            positioned = _margin_info(harness)
            assert positioned["positions"]["position"]["quantity"] == "-0.75"
            assert positioned["positions"]["position"]["collateral"] == "150.125"
            assert positioned["positions"]["position"]["venue_update_ms"] == 100
            before_wallet = harness.clock.timestamp_ns()
            harness.client._consume_private_frame(
                [0, "wu", ["margin", "USTF0", Decimal(900), Decimal(0), Decimal(600)]],
            )
            assert _margin_info(harness)["positions"] == positioned["positions"]
            assert before_wallet <= _margin_info(harness)["wallet"]["observed_ns"]
            # AccountState.info is not copied by Nautilus: old nested values must survive.
            assert first["positions"]["complete"] is False
            assert flat["positions"]["position"] is None
            assert positioned["wallet"] == first["wallet"]
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("message_type", ["pn", "pu"])
def test_margin_increment_without_full_snapshot_never_certifies_positions(
    message_type: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            harness.client._consume_private_frame([0, message_type, _margin_position()])
            info = _margin_info(harness)["positions"]
            assert info["complete"] is False and info["current"] is False
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            assert _margin_info(harness)["positions"]["current"] is True
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bad", ["old_time", "same_time_conflict", "wrong_id", "wrong_close", "duplicate"],
)
def test_margin_ambiguous_position_preserves_last_good_without_claiming_current(bad: str) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            good = _margin_position()
            harness.client._consume_private_frame([0, "ps", [good]])
            before = _margin_info(harness)["positions"]
            row = _margin_position(quantity="-1", updated=101)
            message_type = "pu"
            if bad == "old_time":
                row[13] = 99
            elif bad == "same_time_conflict":
                row[13] = 100
            elif bad == "wrong_id":
                row[11] = 99
            elif bad == "wrong_close":
                message_type = "pc"
            frame: list[object] = [0, message_type, row]
            if bad == "duplicate":
                frame = [0, "ps", [good, row]]
            harness.client._consume_private_frame(frame)
            after = _margin_info(harness)["positions"]
            assert after["position"] == before["position"]
            assert after["observed_ns"] == before["observed_ns"]
            assert after["current"] is False
            assert harness.client.execution_hold_reason is None
            harness.client._consume_private_frame([0, "ps", [_margin_position(updated=102)]])
            assert _margin_info(harness)["positions"]["current"] is True
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_margin_exact_close_does_not_allow_late_old_position_to_reappear() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            closed = _margin_position(quantity="0", updated=101)
            closed[1] = "CLOSED"
            harness.client._consume_private_frame([0, "pc", closed])
            flat = _margin_info(harness)["positions"]
            assert flat["position"] is None and flat["current"] is True
            harness.client._consume_private_frame([0, "pn", _margin_position()])
            assert _margin_info(harness)["positions"]["position"] is None
            assert _margin_info(harness)["positions"]["current"] is False
            harness.client._consume_private_frame([0, "ps", []])
            assert _margin_info(harness)["positions"]["current"] is True
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("race", ["position", "wallet", "disconnect", "cancel", "timeout", "none"])
def test_margin_rest_observation_cannot_overwrite_newer_stream_or_connection(
    monkeypatch: pytest.MonkeyPatch, race: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            before = _margin_info(harness)["positions"]

            async def positions() -> object:
                entered.set()
                await release.wait()
                if race == "timeout":
                    raise TimeoutError("synthetic positions timeout")
                return []

            monkeypatch.setattr(harness.rest, "positions", positions)
            task = asyncio.create_task(harness.client.generate_position_status_reports(
                _position_command(harness),
            ))
            await entered.wait()
            before_release = harness.clock.timestamp_ns()
            if race == "position":
                harness.client._consume_private_frame([0, "pu", _margin_position(updated=102)])
            elif race == "wallet":
                harness.client._consume_private_frame(
                    [0, "wu", ["margin", "USTF0", Decimal(900), Decimal(0), Decimal(600)]],
                )
            elif race == "disconnect":
                await harness.client._disconnect()
            elif race == "cancel":
                task.cancel()
            latest = _margin_info(harness)
            release.set()
            if race == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif race == "timeout":
                with pytest.raises(TimeoutError, match="synthetic positions timeout"):
                    await task
            else:
                reports = await task
                assert len(reports) == 1 and reports[0].signed_decimal_qty == 0
            after = _margin_info(harness)
            if race in {"none", "wallet"}:
                assert after["positions"]["position"] is None
                assert after["positions"]["current"] is True
                assert before_release <= after["positions"]["observed_ns"]
                assert after["wallet"] == latest["wallet"]
            else:
                assert after == latest
                if race == "position":
                    assert after["positions"]["position"]["venue_update_ms"] == 102
                else:
                    assert after["positions"]["position"] == before["position"]
            if race == "disconnect":
                assert after["wallet"]["current"] is False
                assert after["positions"]["current"] is False
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("bound", [False, True], ids=["no-fee-binding", "owned"])
def test_margin_applied_native_fill_invalidates_once_even_during_rest_without_fee_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: bool,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(msgbus=harness.msgbus, cache=harness.cache, clock=harness.clock)
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
        store = JsonStateStore(tmp_path / "margin-hedges.json")
        hedges = HedgeCoordinator(SOURCE_ID, store)
        await harness.connect()
        try:
            order = harness.order(tif=TimeInForce.IOC, post_only=False, quantity="2")
            harness.cache.add_order(order)
            store.begin_source(order.client_order_id.value, BusinessOrderSide.BUY, Decimal(2))

            def on_order(event: OrderEvent) -> None:
                if isinstance(event, OrderFilled):
                    hedges.on_source_filled(event)

            harness.msgbus.subscribe(f"events.order.{order.strategy_id}", on_order)
            if bound:
                cid = await harness.submit(order)
                harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            else:
                engine.process(TestEventStubs.order_submitted(
                    order, account_id=harness.client.account_id,
                ))
                engine.process(TestEventStubs.order_accepted(
                    order, account_id=harness.client.account_id,
                    venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                ))
            assert (harness.client._cid_store.binding_for_client(
                order.client_order_id.value,
            ) is not None) is bound

            # Bad optional data is delivered by the real reader, not only a codec call.
            row = _margin_position()
            row[17:19] = ["bad collateral", Decimal("NaN")]
            await harness.fake.queue.put([0, "ps", [row]])
            await asyncio.sleep(0)
            before = _margin_info(harness)
            assert before["positions"]["position"]["collateral"] is None
            assert before["positions"]["position"]["collateral_min"] is None
            assert before["positions"]["current"] is True

            entered, release = asyncio.Event(), asyncio.Event()

            async def positions() -> object:
                entered.set()
                await release.wait()
                return []

            monkeypatch.setattr(harness.rest, "positions", positions)
            task = asyncio.create_task(harness.client.generate_position_status_reports(
                _position_command(harness),
            ))
            await entered.wait()
            fill = TestEventStubs.order_filled(
                order=order, instrument=harness.instrument,
                account_id=harness.client.account_id,
                venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)), trade_id=TradeId("1234"),
                last_qty=harness.instrument.make_qty(Decimal(2)),
                last_px=harness.instrument.make_price(Decimal("3926.70")),
                commission=Money(0, USD), ts_event=harness.clock.timestamp_ns(),
            )
            # Merely calling the observer without cache application is not a mutation.
            if not bound:
                harness.client._capture_native_fee_evidence(fill)
                assert _margin_info(harness) == before
            engine.process(fill)
            after = _margin_info(harness)
            assert after["wallet"]["current"] is False
            assert after["positions"]["current"] is False
            assert after["wallet"]["observed_ns"] == before["wallet"]["observed_ns"]
            assert after["positions"]["observed_ns"] == before["positions"]["observed_ns"]
            assert before["wallet"]["current"] is True
            assert len(store.intents()) == 1 and order.filled_qty.as_decimal() == 2
            release.set()
            assert (await task)[0].signed_decimal_qty == 0
            assert _margin_info(harness) == after

            harness.client._consume_private_frame([0, "ps", [_margin_position(updated=102)]])
            harness.client._consume_private_frame(
                [0, "wu", ["margin", "USTF0", Decimal(900), Decimal(0), Decimal(600)]],
            )
            refreshed = _margin_info(harness)
            # Fee reconciliation also scans pre-existing cache events; it is not
            # a new Engine publication, even without an in-process seen marker.
            harness.client._margin_seen_fills.clear()
            harness.client._capture_native_fee_evidence(fill)
            harness.client._capture_native_fee_evidence(fill)
            assert _margin_info(harness) == refreshed
            assert refreshed["wallet"]["current"] is True
            assert refreshed["positions"]["current"] is True
            assert len(store.intents()) == 1
            assert harness.client._reader_task is not None
            assert not harness.client._reader_task.done()
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_margin_rest_after_disconnect_cannot_restore_current_connection() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            await harness.client._disconnect()
            before = _margin_info(harness)
            reports = await harness.client.generate_position_status_reports(
                _position_command(harness),
            )
            assert reports[0].signed_decimal_qty == 0
            assert _margin_info(harness) == before
            await harness.connect()
            harness.client._consume_private_frame([0, "pu", _margin_position(updated=102)])
            assert _margin_info(harness)["positions"]["complete"] is False
            harness.client._consume_private_frame([0, "ps", []])
            assert _margin_info(harness)["positions"]["current"] is True
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_margin_rest_enrichment_failure_keeps_existing_report_result_and_last_good() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])
            before = _margin_info(harness)
            # The existing position report does not consume funding/PL metadata.
            row = _margin_position(updated=102)
            row[6] = "bad auxiliary PL"
            harness.rest.position_rows = [row]
            reports = await harness.client.generate_position_status_reports(
                _position_command(harness),
            )
            assert reports[0].signed_decimal_qty == Decimal("-0.75")
            after = _margin_info(harness)
            assert after["wallet"] == before["wallet"]
            assert after["positions"]["position"] == before["positions"]["position"]
            assert after["positions"]["observed_ns"] == before["positions"]["observed_ns"]
            assert after["positions"]["current"] is False
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", [
        "ps_ambiguous", "rest_parse", "rest_report", "wrong_id", "pn_wrong_id", "time_conflict",
    ],
)
@pytest.mark.parametrize("delta", ["pc", "pu", "pn"])
def test_margin_rejected_projection_needs_full_sample_before_any_delta_can_recover(
    failure: str, delta: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            good = _margin_position()
            harness.client._consume_private_frame([0, "ps", [good]])
            before = _margin_info(harness)
            if failure == "ps_ambiguous":
                harness.client._consume_private_frame([0, "ps", [
                    _margin_position(updated=101), _margin_position(pid=99, updated=101),
                ]])
            elif failure == "rest_parse":
                invalid = _margin_position(updated=101)
                invalid[6] = "bad auxiliary PL"
                harness.rest.position_rows = [invalid]
                reports = await harness.client.generate_position_status_reports(
                    _position_command(harness),
                )
                assert reports[0].signed_decimal_qty == Decimal("-0.75")
            elif failure == "rest_report":
                harness.rest.position_rows = [
                    _margin_position(updated=101), _margin_position(pid=99, updated=101),
                ]
                with pytest.raises(BitfinexV1ReportError, match="one NETTING position"):
                    await harness.client.generate_position_status_reports(
                        _position_command(harness),
                    )
            elif failure in {"wrong_id", "pn_wrong_id"}:
                message_type = "pn" if failure == "pn_wrong_id" else "pu"
                harness.client._consume_private_frame([0, message_type, _margin_position(
                    pid=99, updated=101,
                )])
            else:
                harness.client._consume_private_frame([0, "pu", _margin_position(quantity="-1")])
            assert _margin_info(harness)["positions"]["current"] is False

            # Each delta would be valid against the old single position, but none
            # can exclude the missing/conflicting facts from the rejected sample.
            update = _margin_position(updated=102)
            if delta == "pc":
                update[1:3] = ["CLOSED", Decimal(0)]
            elif delta == "pn":
                update = good
            harness.client._consume_private_frame([0, delta, update])
            after = _margin_info(harness)
            assert after["positions"]["complete"] is False
            assert after["positions"]["current"] is False
            assert after["positions"]["position"] == before["positions"]["position"]
            assert after["positions"]["observed_ns"] == before["positions"]["observed_ns"]
            assert after["wallet"] == before["wallet"]

            recovered = _margin_position(updated=103)
            if failure == "ps_ambiguous":
                harness.rest.position_rows = [recovered]
                await harness.client.generate_position_status_reports(_position_command(harness))
            else:
                harness.client._consume_private_frame([0, "ps", [recovered]])
            assert _margin_info(harness)["positions"]["complete"] is True
            assert _margin_info(harness)["positions"]["current"] is True
            closed = _margin_position(quantity="0", updated=104)
            closed[1] = "CLOSED"
            harness.client._consume_private_frame([0, "pc", closed])
            assert _margin_info(harness)["positions"]["current"] is True
            assert _margin_info(harness)["positions"]["position"] is None
            assert harness.client.execution_hold_reason is None
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_margin_late_rejected_rest_report_cannot_revoke_new_full_stream_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            harness.client._consume_private_frame([0, "ps", [_margin_position()]])

            async def positions() -> object:
                entered.set()
                await release.wait()
                return [_margin_position(updated=101), _margin_position(pid=99, updated=101)]

            monkeypatch.setattr(harness.rest, "positions", positions)
            task = asyncio.create_task(harness.client.generate_position_status_reports(
                _position_command(harness),
            ))
            await entered.wait()
            harness.client._consume_private_frame([0, "ps", [_margin_position(updated=103)]])
            fresh = _margin_info(harness)
            assert fresh["positions"]["complete"] is True
            assert fresh["positions"]["current"] is True
            release.set()
            with pytest.raises(BitfinexV1ReportError, match="one NETTING position"):
                await task
            assert _margin_info(harness) == fresh
        finally:
            await harness.close()
    asyncio.run(scenario())


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


def test_reader_fatal_reports_only_sanitized_private_frame_type() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            sensitive_values = ("998.123456", "3926.751234", "2.00000000")
            await harness.fake.queue.put([0, "te", list(sensitive_values)])
            reader = harness.client._reader_task
            assert reader is not None

            await asyncio.wait_for(asyncio.shield(reader), timeout=1.0)

            assert harness.client.last_failure is not None
            assert harness.client.last_failure.startswith("private reader frame_type=te:")
            assert all(value not in harness.client.last_failure for value in sensitive_values)
            assert harness.fake.closed
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_reader_fatal_does_not_echo_an_unknown_private_message_type() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            marker = "SENSITIVE-FRAME-MARKER-987654"
            await harness.fake.queue.put([0, marker, None])
            reader = harness.client._reader_task
            assert reader is not None

            await asyncio.wait_for(asyncio.shield(reader), timeout=1.0)

            assert harness.client.last_failure == (
                "private reader frame_type=other: BitfinexV1ProtocolError"
            )
            assert marker not in harness.client.last_failure
            assert harness.fake.closed
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


def test_submit_encodes_reduce_only_ioc_with_exact_venue_flag() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            await harness.submit(order, leverage=16)

            payload = cast(dict[str, object], cast(list[object], harness.fake.sent[-1])[3])
            assert payload == {
                "type": "IOC",
                "symbol": RAW_SYMBOL,
                "amount": "-2.00000000",
                "price": "3926.70",
                "cid": payload["cid"],
                "lev": 16,
                "flags": REDUCE_ONLY_FLAG,
            }
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tif", "post_only", "reason"),
    [
        (TimeInForce.GTC, False, "requires an IOC"),
        (TimeInForce.GTC, True, "mutually exclusive"),
    ],
)
def test_submit_denies_invalid_reduce_only_semantics_before_wire(
    tif: TimeInForce,
    post_only: bool,
    reason: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(tif=tif, post_only=post_only, reduce_only=True)
            sent = len(harness.fake.sent)
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
            denied = harness.events[-1]
            assert type(denied).__name__ == "OrderDenied"
            assert reason in denied.reason
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


@pytest.mark.parametrize(
    ("raw_symbol", "wallet_currency", "venue_flags", "reported_post_only"),
    [
        (RAW_SYMBOL, "USTF0", POST_ONLY_FLAG, True),
        (PAPER_RAW_SYMBOL, "TESTUSDTF0", 0, False),
    ],
)
def test_targeted_rest_acceptance_binds_pending_submit_for_cancel(
    raw_symbol: str,
    wallet_currency: str,
    venue_flags: int,
    reported_post_only: bool,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest(raw_symbol)
        harness = _Harness(
            rest=rest,
            raw_symbol=raw_symbol,
            wallet_currency=wallet_currency,
        )
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
            row = cast(list[object], accepted[2])
            row[12] = venue_flags
            rest.active = [row]

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
            assert report.post_only is reported_post_only
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


def test_targeted_rest_acceptance_verifies_reduce_only_semantics() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        harness = _Harness(rest=rest)
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            rest.active = [cast(list[object], harness.order_frame("on", cid, order)[2])]

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
            assert report.reduce_only
            assert not report.post_only
            live = harness.client._by_cid[cid]
            assert live.accepted
            assert live.venue_flags_verified
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_targeted_rest_rehydrates_and_proves_restart_reduce_only_order() -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        order = harness.order(
            side=OrderSide.SELL,
            tif=TimeInForce.IOC,
            post_only=False,
            reduce_only=True,
            quantity="2",
        )
        binding = harness.client._cid_store.allocate(
            order.client_order_id.value,
            epoch_ms=harness.clock.timestamp_ns() // 1_000_000,
        )
        harness.cache.add_order(order)
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
        rest.active = [cast(list[object], harness.order_frame("on", binding.cid, order)[2])]

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
        assert report.reduce_only
        live = harness.client._by_cid[binding.cid]
        assert live.accepted
        assert not live.submitted_in_process
        assert live.venue_order_id == VENUE_ORDER_ID
        assert live.venue_flags_verified

    asyncio.run(scenario())


def test_targeted_rest_binds_unknown_reduce_only_submit_without_venue_id() -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError, match="uncertain"):
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
            binding = harness.client._cid_store.binding_for_client(order.client_order_id.value)
            assert binding is not None
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            harness.cache.add_order(order)
            rest.active = [cast(list[object], harness.order_frame("on", binding.cid, order)[2])]

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
            live = harness.client._by_cid[binding.cid]
            assert live.accepted
            assert live.venue_order_id == VENUE_ORDER_ID
            assert live.venue_flags_verified
            assert not live.unknown_operations
            assert harness.client.execution_hold_reason is None
            assert "OrderAccepted" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("row_updates", "mismatch"),
    [
        ({0: VENUE_ORDER_ID + 1}, "venue_order_id"),
        ({12: 0}, "reduce_only"),
        ({16: Decimal("3926.71")}, "price"),
        ({6: Decimal("-3"), 7: Decimal("-3")}, "quantity"),
        ({8: "LIMIT", 12: 0}, "time_in_force"),
    ],
)
def test_targeted_rest_restart_reduce_only_binding_is_semantically_exact(
    row_updates: dict[int, object],
    mismatch: str,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        order = harness.order(
            side=OrderSide.SELL,
            tif=TimeInForce.IOC,
            post_only=False,
            reduce_only=True,
            quantity="2",
        )
        binding = harness.client._cid_store.allocate(
            order.client_order_id.value,
            epoch_ms=harness.clock.timestamp_ns() // 1_000_000,
        )
        order.apply(
            TestEventStubs.order_submitted(
                order,
                account_id=harness.client.account_id,
                ts_event=harness.clock.timestamp_ns(),
            )
        )
        order.apply(
            TestEventStubs.order_accepted(
                order,
                account_id=harness.client.account_id,
                venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                ts_event=harness.clock.timestamp_ns(),
            )
        )
        harness.cache.add_order(order)
        row = cast(list[object], harness.order_frame("on", binding.cid, order)[2])
        for index, value in row_updates.items():
            row[index] = value
        rest.active = [row]

        with pytest.raises((BitfinexV1ExecutionError, ValueError), match=mismatch):
            await harness.client.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=harness.clock.timestamp_ns(),
                )
            )

        live = harness.client._by_cid.get(binding.cid)
        assert live is None or not live.venue_flags_verified

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("row_updates", "mismatch"),
    [
        ({6: Decimal("5"), 7: Decimal("5")}, "quantity"),
        ({12: 0}, "post_only"),
        ({16: Decimal("3926.71")}, "price"),
    ],
)
@pytest.mark.parametrize("ws_first", [False, True])
def test_targeted_rest_acceptance_rejects_submit_semantic_mismatch(
    row_updates: dict[int, object],
    mismatch: str,
    ws_first: bool,
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
            accepted = harness.order_frame("on", cid, order)
            if ws_first:
                harness.client._consume_private_frame(accepted)
            row = cast(list[object], accepted[2]).copy()
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
            assert live.accepted is ws_first
            assert live.venue_order_id == (VENUE_ORDER_ID if ws_first else None)
            assert (VENUE_ORDER_ID in harness.client._cid_by_venue) is ws_first
            assert ((cid, "submit") in harness.client._ack_deadlines) is not ws_first
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw_symbol", "wallet_currency", "operation", "row_updates"),
    [
        (RAW_SYMBOL, "USTF0", "on", {12: 0}),
        (
            PAPER_RAW_SYMBOL,
            "TESTUSDTF0",
            "on",
            {10: 1_700_000_200_000, 12: 0},
        ),
        (PAPER_RAW_SYMBOL, "TESTUSDTF0", "ou", {12: 0}),
    ],
)
def test_private_zero_flag_tolerance_keeps_profile_tif_and_operation_boundaries(
    raw_symbol: str,
    wallet_currency: str,
    operation: str,
    row_updates: dict[int, object],
) -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=raw_symbol,
            wallet_currency=wallet_currency,
        )
        await harness.connect()
        try:
            order = harness.order()
            cid = await harness.submit(order)
            frame = harness.order_frame(operation, cid, order)
            row = cast(list[object], frame[2])
            for index, value in row_updates.items():
                row[index] = value

            with pytest.raises(BitfinexV1ExecutionError, match="differs from local submission"):
                harness.client._consume_private_frame(frame)

            assert not harness.client._by_cid[cid].accepted
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_private_order_event_verifies_exact_reduce_only_flag() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))

            live = harness.client._by_cid[cid]
            assert live.accepted
            assert live.venue_flags_verified
            assert _types(harness)[-1] == "OrderAccepted"
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("venue_flags", [0, POST_ONLY_FLAG, REDUCE_ONLY_FLAG | POST_ONLY_FLAG])
@pytest.mark.parametrize(
    ("raw_symbol", "wallet_currency"),
    [(RAW_SYMBOL, "USTF0"), (PAPER_RAW_SYMBOL, "TESTUSDTF0")],
)
def test_private_reduce_only_never_tolerates_wrong_venue_flags(
    venue_flags: int,
    raw_symbol: str,
    wallet_currency: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(raw_symbol=raw_symbol, wallet_currency=wallet_currency)
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)
            frame = harness.order_frame("on", cid, order)
            cast(list[object], frame[2])[12] = venue_flags

            with pytest.raises(BitfinexV1ExecutionError, match="differs from local submission"):
                harness.client._consume_private_frame(frame)

            live = harness.client._by_cid[cid]
            assert not live.accepted
            assert not live.venue_flags_verified
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_reduce_only_trade_before_flag_evidence_fails_closed_without_fill() -> None:
    async def scenario() -> None:
        harness = _Harness()
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)
            event_count = len(harness.events)

            with pytest.raises(BitfinexV1ExecutionError, match="before venue flags were verified"):
                harness.client._consume_private_frame(
                    harness.trade_frame(cid, order, quantity="2", maker=-1)
                )

            assert len(harness.events) == event_count
            live = harness.client._by_cid[cid]
            assert not live.accepted
            assert live.filled_qty == 0
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_paper_positive_cid_te_after_wallet_waits_for_exact_oc_before_fill() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)
            harness.client._consume_private_frame(
                [
                    0,
                    "wu",
                    [
                        "margin",
                        "TESTUSDTF0",
                        Decimal("998"),
                        Decimal(0),
                        Decimal("798"),
                    ],
                ]
            )
            interim = cast(list[object], harness.trade_frame(cid, order, quantity="2", maker=-1)[2])
            interim[9:11] = [None, None]
            order_event_count = len(_order_events(harness))

            harness.client._consume_private_frame([0, "te", interim])

            assert len(_order_events(harness)) == order_event_count
            assert not any(isinstance(event, OrderFilled) for event in _order_events(harness))
            assert 1234 in harness.client._unbound_paper_interim_fills
            live = harness.client._by_cid[cid]
            accepted_before_terminal = live.accepted
            assert not accepted_before_terminal
            flags_before_terminal = live.venue_flags_verified
            assert not flags_before_terminal
            assert live.filled_qty == 0

            terminal = harness.order_frame(
                "oc",
                cid,
                order,
                remaining="0",
                status="EXECUTED @ 3926.75(2)",
            )
            cast(list[object], terminal[2])[17] = Decimal("3926.75")
            harness.client._consume_private_frame(terminal)

            fills = [event for event in _order_events(harness) if isinstance(event, OrderFilled)]
            assert len(fills) == 1
            assert fills[0].last_qty.as_decimal() == Decimal("2")
            assert _types(harness)[-2:] == ["OrderAccepted", "OrderFilled"]
            assert 1234 not in harness.client._unbound_paper_interim_fills
            accepted_after_terminal = live.accepted
            assert accepted_after_terminal
            flags_after_terminal = live.venue_flags_verified
            assert flags_after_terminal
            assert live.filled_qty == Decimal("2")
            assert len(harness.fake.sent) == 2
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", ["flags", "venue", "cid"])
def test_buffered_paper_positive_cid_te_mismatch_never_emits_fill(mismatch: str) -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)
            interim = cast(list[object], harness.trade_frame(cid, order, quantity="2", maker=-1)[2])
            interim[9:11] = [None, None]
            harness.client._consume_private_frame([0, "te", interim])
            terminal = harness.order_frame(
                "oc",
                cid,
                order,
                remaining="0",
                status="EXECUTED @ 3926.75(2)",
            )
            terminal_row = cast(list[object], terminal[2])
            terminal_row[17] = Decimal("3926.75")
            if mismatch == "flags":
                terminal_row[12] = 0
            elif mismatch == "venue":
                terminal_row[0] = VENUE_ORDER_ID + 1
            else:
                terminal_row[2] = cid + 1

            if mismatch == "cid":
                harness.client._consume_private_frame(terminal)
            else:
                with pytest.raises(BitfinexV1ExecutionError):
                    harness.client._consume_private_frame(terminal)

            live = harness.client._by_cid[cid]
            assert not any(isinstance(event, OrderFilled) for event in _order_events(harness))
            assert 1234 in harness.client._unbound_paper_interim_fills
            assert not live.accepted
            assert not live.venue_flags_verified
            assert live.filled_qty == 0
            assert len(harness.fake.sent) == 2
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_paper_cid_zero_te_waits_for_exact_reduce_only_on_before_fill() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)
            interim = cast(list[object], harness.trade_frame(cid, order, quantity="2", maker=-1)[2])
            interim[9:11] = [None, None]
            interim[11] = 0
            event_count = len(harness.events)

            harness.client._consume_private_frame([0, "te", interim])

            assert len(harness.events) == event_count
            assert harness.client.execution_hold_reason is not None
            assert 1234 in harness.client._unbound_paper_interim_fills

            harness.client._consume_private_frame(harness.order_frame("on", cid, order))

            fills = [event for event in _order_events(harness) if isinstance(event, OrderFilled)]
            assert len(fills) == 1
            assert fills[0].last_qty.as_decimal() == Decimal("2")
            assert _types(harness)[-2:] == ["OrderAccepted", "OrderFilled"]
            assert 1234 not in harness.client._unbound_paper_interim_fills
            live = harness.client._by_cid[cid]
            assert live.venue_flags_verified
            assert live.filled_qty == Decimal("2")
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_buffered_paper_cid_zero_te_mismatch_fails_before_acceptance() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)
            interim = cast(
                list[object],
                harness.trade_frame(
                    cid,
                    order,
                    quantity="2",
                    order_price="3926.71",
                    maker=-1,
                )[2],
            )
            interim[9:11] = [None, None]
            interim[11] = 0
            harness.client._consume_private_frame([0, "te", interim])
            event_count = len(harness.events)

            with pytest.raises(BitfinexV1ExecutionError, match="trade differs"):
                harness.client._consume_private_frame(harness.order_frame("on", cid, order))

            assert len(harness.events) == event_count
            assert 1234 in harness.client._unbound_paper_interim_fills
            live = harness.client._by_cid[cid]
            assert not live.accepted
            assert not live.venue_flags_verified
            assert live.filled_qty == 0
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_unbound_paper_cid_zero_te_buffer_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(execution_module, "_MAX_UNBOUND_PAPER_INTERIM_FILLS", 1, raising=False)
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            first = cast(list[object], harness.trade_frame(1, order, maker=-1)[2])
            first[9:11] = [None, None]
            first[11] = 0
            harness.client._consume_private_frame([0, "te", first])

            second = first.copy()
            second[0] = 1235
            second[3] = VENUE_ORDER_ID + 1
            with pytest.raises(BitfinexV1ExecutionError, match="buffer is full"):
                harness.client._consume_private_frame([0, "te", second])

            assert list(harness.client._unbound_paper_interim_fills) == [1234]
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_terminal_history_requires_strict_flags_and_does_not_bind_pending_submit() -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
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

            terminal[12] = POST_ONLY_FLAG
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
            assert report.order_status == OrderStatus.CANCELED
            assert not harness.client._by_cid[cid].accepted
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_paper_partial_rest_zero_flags_cannot_bind_pending_submit() -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
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
            partial = harness.order_frame("on", cid, order, remaining="3")
            row = cast(list[object], partial[2])
            row[12] = 0
            row[17] = Decimal("3926.75")
            rest.active = [row]

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

            live = harness.client._by_cid[cid]
            assert not live.accepted
            assert live.venue_order_id is None
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "acceptance_timing",
    ["rest_without_ws", "rest_then_ws", "before_query", "during_rest"],
)
def test_ws_and_targeted_rest_race_publishes_one_acceptance(
    acceptance_timing: str,
) -> None:
    class WsFirstRest(_FakeRest):
        def __init__(self) -> None:
            super().__init__(PAPER_RAW_SYMBOL)
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
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
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
            row = cast(list[object], accepted[2])
            row[12] = 0
            rest.active = [row]
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

            if acceptance_timing == "rest_then_ws":
                harness.client._consume_private_frame(accepted)
                await asyncio.sleep(0)
            assert sum(isinstance(event, OrderAccepted) for event in published) == 1
            assert [type(event).__name__ for event in order.events].count("OrderAccepted") == 1
            rest_first = acceptance_timing in {"rest_without_ws", "rest_then_ws"}
            assert engine.report_count == (1 if rest_first else 0)
            assert harness.client._by_cid[cid].venue_order_id == VENUE_ORDER_ID
            await harness.cancel(order)
            assert harness.fake.sent[-1] == [0, "oc", None, {"id": VENUE_ORDER_ID}]
            canceled = harness.order_frame("oc", cid, order, status="CANCELED")
            cast(list[object], canceled[2])[12] = 0
            harness.client._consume_private_frame(canceled)
            for _ in range(20):
                await asyncio.sleep(0)
            assert sum(isinstance(event, OrderCanceled) for event in published) == 1
            assert order.status == OrderStatus.CANCELED
            assert harness.client.execution_hold_reason is None
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
        ("-0.061668", -1, Decimal("0.06"), LiquiditySide.TAKER),
        ("-0.001", -1, Decimal("0.00"), LiquiditySide.TAKER),
        ("-0.005", -1, Decimal("0.00"), LiquiditySide.TAKER),
        ("0.005", 1, Decimal("0.00"), LiquiditySide.MAKER),
        ("-0.015", -1, Decimal("0.02"), LiquiditySide.TAKER),
        ("0.015", 1, Decimal("-0.02"), LiquiditySide.MAKER),
    ],
)
def test_production_tu_is_single_fill_authority_with_real_fee_liquidity_and_dedupe(
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
                event for event in _order_events(harness) if type(event).__name__ == "OrderFilled"
            ]
            assert len(fills) == 1
            fill = fills[0]
            assert fill.trade_id.value == "1234"
            assert fill.commission.as_decimal() == commission
            assert fill.commission.currency.code == "USD"
            assert fill.liquidity_side == liquidity
            assert fill.info["bitfinex_fee"] == fee

            changed_trade = cast(list[object], trade[2]).copy()
            changed_trade[2] = cast(int, changed_trade[2]) + 1
            with pytest.raises(
                BitfinexV1ExecutionError,
                match="trade ID changed its execution facts",
            ):
                harness.client._consume_private_frame([0, "tu", changed_trade])
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw_symbol", "wallet_currency"),
    [
        (RAW_SYMBOL, "USTF0"),
        (PAPER_RAW_SYMBOL, "TESTUSDTF0"),
    ],
)
def test_tu_requires_usd_fee_currency_independently_of_wallet(
    raw_symbol: str,
    wallet_currency: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=raw_symbol,
            wallet_currency=wallet_currency,
        )
        await harness.connect()
        try:
            order = harness.order(
                tif=TimeInForce.IOC,
                post_only=False,
                quantity="2",
            )
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            wrong_currency = harness.trade_frame(cid, order, quantity="2", maker=-1)
            cast(list[object], wrong_currency[2])[10] = wallet_currency
            event_count = len(harness.events)

            with pytest.raises(BitfinexV1ExecutionError, match="differs from local submission"):
                harness.client._consume_private_frame(wrong_currency)
            assert len(harness.events) == event_count
            assert 1234 not in harness.client._seen_trades
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_first_conversion", [False, True])
def test_subcent_tu_reaches_real_engine_and_reserves_one_hedge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_first_conversion: bool,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(
            msgbus=harness.msgbus, cache=harness.cache, clock=harness.clock,
        )
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
        store = JsonStateStore(tmp_path / "hedges.json")
        hedges = HedgeCoordinator(SOURCE_ID, store)

        def on_order(event: OrderEvent) -> None:
            if isinstance(event, OrderFilled):
                hedges.on_source_filled(event)

        await harness.connect()
        try:
            order = harness.order(
                tif=TimeInForce.IOC,
                post_only=False,
                quantity="2",
            )
            harness.cache.add_order(order)
            store.begin_source(order.client_order_id.value, BusinessOrderSide.BUY, Decimal(2))
            harness.msgbus.subscribe(f"events.order.{order.strategy_id}", on_order)
            cid = await harness.submit(order)
            trade = harness.trade_frame(cid, order, quantity="2", fee="-0.061668", maker=-1)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            if fail_first_conversion:
                def fail_conversion(fee: Decimal) -> Money:
                    raise ValueError("synthetic fee conversion failure")

                with monkeypatch.context() as patch:
                    patch.setattr(execution_module, "usd_commission", fail_conversion)
                    with pytest.raises(ValueError, match="synthetic fee conversion failure"):
                        harness.client._consume_private_frame(trade)
                assert harness.client._by_cid[cid].filled_qty == Decimal(0)
                assert 1234 not in harness.client._seen_trades
                assert 1234 not in harness.client._paper_interim_fills
                assert order.status == OrderStatus.ACCEPTED
                assert order.filled_qty.as_decimal() == Decimal(0)
                assert not any(isinstance(event, OrderFilled) for event in order.events)
                assert not harness.cache.positions_open()
                assert not store.intents()

            await harness.fake.queue.put(trade)
            await harness.fake.queue.put(trade)
            await asyncio.sleep(0)

            assert harness.client.execution_hold_reason is None
            assert order.status == OrderStatus.FILLED
            assert order.filled_qty.as_decimal() == Decimal(2)
            fills = [event for event in order.events if isinstance(event, OrderFilled)]
            assert len(fills) == 1
            assert fills[0].commission.as_decimal() == Decimal("0.06")
            assert fills[0].info["bitfinex_fee"] == "-0.061668"
            assert len(harness.cache.positions_open()) == 1
            position = harness.cache.positions_open()[0]
            assert position.signed_decimal_qty() == Decimal(2)
            assert position.settlement_currency.code == "USDT"
            assert position.commissions() == [Money.from_decimal(Decimal("0.06"), USD)]
            # Nautilus keeps the USD cost; it does not invent a USD/USDT conversion.
            assert position.realized_pnl is not None
            assert position.realized_pnl.currency.code == "USDT"
            assert position.realized_pnl.as_decimal() == Decimal(0)
            assert len(store.intents()) == 1
            assert store.intents()[0].hedge_quantity_ounces == Decimal(2)
            assert store.intents()[0].source_trade_id == "1234"

            changed_fee = cast(list[object], trade[2]).copy()
            changed_fee[9] = Decimal("-0.061669")  # Same booked cents, different venue fact.
            with pytest.raises(BitfinexV1ExecutionError, match="trade ID changed"):
                harness.client._consume_private_frame([0, "tu", changed_fee])
            assert len(store.intents()) == 1
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_paper_tu_then_te_matches_full_execution_or_fails_closed() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                tif=TimeInForce.IOC,
                post_only=False,
                quantity="2",
            )
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            update = harness.trade_frame(
                cid,
                order,
                quantity="2",
                fee="-1.79",
                maker=-1,
            )
            interim = cast(list[object], update[2]).copy()
            interim[9:11] = [None, None]
            interim[11] = 0

            harness.client._consume_private_frame(update)
            harness.client._consume_private_frame([0, "te", interim])
            fills = [event for event in _order_events(harness) if isinstance(event, OrderFilled)]
            assert len(fills) == 1
            assert fills[0].commission.as_decimal() == Decimal("1.79")
            assert fills[0].commission.currency.code == "USD"

            changed_interim = interim.copy()
            changed_interim[2] = cast(int, changed_interim[2]) + 1
            with pytest.raises(
                BitfinexV1ExecutionError,
                match="interim trade differs from its final update",
            ):
                harness.client._consume_private_frame([0, "te", changed_interim])
            assert (
                len([event for event in _order_events(harness) if isinstance(event, OrderFilled)])
                == 1
            )
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fee_write_fails", [False, True])
@pytest.mark.parametrize("final_source", ["tu", "rest"])
def test_paper_fee_metadata_waits_for_native_cache_and_never_blocks_deferred_hedge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fee_write_fails: bool, final_source: str,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "cids.json"
        harness = _Harness(
            cid_store_path=path, raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
        )
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = LiveExecutionEngine(
            loop=asyncio.get_running_loop(), msgbus=harness.msgbus,
            cache=harness.cache, clock=harness.clock,
            config=LiveExecEngineConfig(
                load_cache=False, reconciliation=False, inflight_check_interval_ms=0,
                open_check_interval_secs=None, position_check_interval_secs=None,
            ),
        )
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
        obligations = JsonStateStore(tmp_path / "hedges.json")
        hedges = HedgeCoordinator(SOURCE_ID, obligations)

        def on_fill(event: OrderEvent) -> None:
            if isinstance(event, OrderFilled):
                hedges.on_source_filled(event)

        await harness.connect()
        engine.start()
        try:
            order = harness.order(tif=TimeInForce.IOC, post_only=False, quantity="4")
            harness.cache.add_order(order)
            obligations.begin_source(order.client_order_id.value, BusinessOrderSide.BUY, Decimal(4))
            harness.msgbus.subscribe(f"events.order.{order.strategy_id}", on_fill)
            cid = await harness.submit(order)
            final = harness.trade_frame(cid, order, quantity="2", fee="-0.061668", maker=-1)
            cast(list[object], final[2])[2] = harness.clock.timestamp_ns() // 1_000_000
            interim = cast(list[object], final[2]).copy()
            interim[9:11] = [None, None]
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))

            with monkeypatch.context() as patch:
                if fee_write_fails:
                    def fail_write(*args: object) -> None:
                        raise OSError("synthetic fee metadata failure")
                    patch.setattr(harness.client._cid_store, "_persist", fail_write)
                harness.client._consume_private_frame([0, "te", interim])
                metadata = harness.client._cid_store.fee_metadata_for_cid(cid)
                assert metadata is not None
                assert metadata.native_fills == ()  # Enqueued is not booked.
                assert metadata.venue_trades[0].fee_finality == "pending"
                await harness.fake.queue.put([0, "te", interim])
                for _ in range(20):
                    await asyncio.sleep(0)
                assert harness.client.execution_hold_reason is None
                assert order.status == OrderStatus.PARTIALLY_FILLED
                assert len([e for e in order.events if isinstance(e, OrderFilled)]) == 1
                assert len(obligations.intents()) == 1
                assert len(JsonStateStore(tmp_path / "hedges.json").intents()) == 1
                assert harness.client.accounting_incomplete == fee_write_fails

            if final_source == "tu":
                await harness.fake.queue.put(final)
                await harness.fake.queue.put(final)
            else:
                harness.rest.trades = [final[2], final[2]]
                await harness.client.generate_fill_reports(GenerateFillReports(
                    instrument_id=SOURCE_ID, venue_order_id=None, start=None, end=None,
                    command_id=UUID4(), ts_init=0,
                ))
            for _ in range(20):
                await asyncio.sleep(0)
            assert not harness.client.accounting_incomplete
            restored = BitfinexV1CidStore(path, account_id=harness.client.account_id.value)
            metadata = restored.fee_metadata_for_cid(cid)
            assert metadata is not None
            assert len(metadata.native_fills) == len(metadata.venue_trades) == 1
            assert metadata.native_fills[0].native_fill_origin == "te_paper"
            assert metadata.native_fills[0].commission == Decimal(0)
            assert metadata.venue_trades[0].raw_fee == Decimal("-0.061668")
            summary = harness.client.fee_summary(order.client_order_id)
            assert summary.complete
            assert summary.currencies["USD"].provisional_correction == Decimal("0.06")
            assert len(obligations.intents()) == 1
            changed_fee = cast(list[object], final[2]).copy()
            changed_fee[9] = Decimal("-0.061669")
            second_interim = interim.copy()
            second_interim[0] = 1235
            with monkeypatch.context() as patch:
                if fee_write_fails:
                    patch.setattr(harness.client._cid_store, "_persist", fail_write)
                await harness.fake.queue.put([0, "tu", changed_fee])
                await harness.fake.queue.put([0, "te", second_interim])
                for _ in range(20):
                    await asyncio.sleep(0)
                assert harness.client._cid_store.accounting_conflict
                assert harness.client._cid_store.fees_durable != fee_write_fails
                assert not harness.client.accounting_ready
                assert harness.client.execution_hold_reason is None
                assert order.status == OrderStatus.FILLED
                assert len([e for e in order.events if isinstance(e, OrderFilled)]) == 2
                assert len(JsonStateStore(tmp_path / "hedges.json").intents()) == 2
            await harness.fake.queue.put(final)  # Ordinary replay retries dirty persistence only.
            for _ in range(20):
                await asyncio.sleep(0)
            assert harness.client._cid_store.fees_durable
            assert not harness.client.accounting_ready
            assert BitfinexV1CidStore(
                path, account_id=harness.client.account_id.value,
            ).accounting_conflict
        finally:
            engine.stop()
            await asyncio.sleep(0)
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("conflict_source", ["final_fee", "native_publication"])
def test_accounting_conflict_survives_new_client_empty_history_and_old_value_replay(
    tmp_path: Path, conflict_source: str,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "cids.json"
        first = _Harness(
            cid_store_path=path, raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
        )
        first.msgbus.deregister("ExecEngine.process", first.events.append)
        engine = ExecutionEngine(msgbus=first.msgbus, cache=first.cache, clock=first.clock)
        engine.register_client(first.client)
        first.cache.add_instrument(first.instrument)
        first.cache.add_account(TestExecStubs.margin_account(account_id=first.client.account_id))
        await first.connect()
        try:
            order = first.order(tif=TimeInForce.IOC, post_only=False, quantity="2")
            first.cache.add_order(order)
            cid = await first.submit(order)
            first.client._consume_private_frame(first.order_frame("on", cid, order))
            final = first.trade_frame(cid, order, quantity="2", fee="-0.061668", maker=-1)
            cast(list[object], final[2])[2] = first.clock.timestamp_ns() // 1_000_000
            interim = cast(list[object], final[2]).copy()
            interim[9:11] = [None, None]
            first.client._consume_private_frame([0, "te", interim])
            first.client._consume_private_frame(final)
            assert order.status == OrderStatus.FILLED
            assert bool(first.client.accounting_ready)
            assert first.client.fee_summary().complete
            original_metadata = first.client._cid_store.fee_metadata
            if conflict_source == "final_fee":
                changed = cast(list[object], final[2]).copy()
                changed[9] = Decimal("-0.061669")
                with pytest.raises(BitfinexV1ExecutionError, match="trade ID changed"):
                    first.client._consume_private_frame([0, "tu", changed])
            else:
                applied = next(event for event in order.events if isinstance(event, OrderFilled))
                changed_fill = OrderFilled.to_dict(applied)
                changed_fill["commission"] = "1.00 USD"
                first.client._capture_native_fee_evidence(OrderFilled.from_dict(changed_fill))
            assert not first.client.accounting_ready
            assert not first.client.fee_summary().complete
            assert first.client._cid_store.fee_metadata == original_metadata
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
        finally:
            await first.close()
            engine.dispose()

        second = _Harness(
            cid_store_path=path, raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
        )
        await second.connect()
        try:
            assert second.rest.active == second.rest.history == second.rest.trades == []
            assert second.rest.position_rows == []
            assert await second.client.generate_mass_status(lookback_mins=None) is not None
            assert second.client.execution_hold_reason is None
            assert not second.client.accounting_ready
            assert not second.client.fee_summary().complete
            second.rest.trades = [final[2]]
            assert len(await second.client.generate_fill_reports(GenerateFillReports(
                instrument_id=SOURCE_ID, venue_order_id=None, start=None, end=None,
                command_id=UUID4(), ts_init=0,
            ))) == 1
            second.client._cid_store.allocate("O-AFTER-CONFLICT", epoch_ms=cid + 1)
            assert second.client._cid_store.fee_metadata == original_metadata
            assert second.client._cid_store.fees_durable
            assert not second.client.accounting_ready
            assert not second.client.fee_summary(order.client_order_id).complete
            assert not any(isinstance(event, OrderFilled) for event in second.events)
            assert BitfinexV1CidStore(
                path, account_id=second.client.account_id.value,
            ).accounting_conflict
        finally:
            await second.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("state_version, keep_marker", [(2, False), (1, True), (1, False)])
def test_restart_fee_provenance_is_required_for_rest_commission_supplement(
    tmp_path: Path, state_version: int, keep_marker: bool,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "cids.json"
        first = _Harness(
            cid_store_path=path, raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
        )
        first.msgbus.deregister("ExecEngine.process", first.events.append)
        engine = ExecutionEngine(msgbus=first.msgbus, cache=first.cache, clock=first.clock)
        engine.register_client(first.client)
        first.cache.add_instrument(first.instrument)
        first.cache.add_account(TestExecStubs.margin_account(account_id=first.client.account_id))
        await first.connect()
        try:
            order = first.order(tif=TimeInForce.IOC, post_only=False, quantity="2")
            first.cache.add_order(order)
            cid = await first.submit(order)
            first.client._consume_private_frame(first.order_frame("on", cid, order))
            final = first.trade_frame(cid, order, quantity="1", fee="-0.061668", maker=-1)
            cast(list[object], final[2])[2] = first.clock.timestamp_ns() // 1_000_000
            interim = cast(list[object], final[2]).copy()
            interim[9:11] = [None, None]
            first.client._consume_private_frame([0, "te", interim])
            assert order.filled_qty.as_decimal() == Decimal(1)
            codec = MsgSpecSerializer(msgspec.msgpack, timestamps_as_str=True)
            persisted = [codec.serialize(event) for event in order.events]
        finally:
            await first.close()
            engine.dispose()

        if state_version == 1:
            old = json.loads(path.read_text())
            old["schema_version"] = 1
            old.pop("fee_metadata")
            path.write_text(json.dumps(old))
        restored = OrderUnpacker.from_init(codec.deserialize(persisted[0]))
        for data in persisted[1:]:
            event = codec.deserialize(data)
            if isinstance(event, OrderFilled) and not keep_marker:
                values = OrderFilled.to_dict(event)
                values["info"] = {}
                event = OrderFilled.from_dict(values)
            restored.apply(event)
        second = _Harness(
            cid_store_path=path, raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
        )
        second.cache.add_order(restored)
        await second.connect()
        try:
            accepted = second.order_frame("on", cid, restored, remaining="1")
            row = cast(list[object], accepted[2])
            row[13] = "PARTIALLY FILLED @ 3926.75(1)"
            row[17] = Decimal("3926.75")
            second.rest.active = [row]
            second.rest.trades = [final[2]]
            reports = await second.client.generate_order_status_reports(GenerateOrderStatusReports(
                instrument_id=SOURCE_ID, start=None, end=None, open_only=True,
                command_id=UUID4(), ts_init=0,
            ))
            fills = await second.client.generate_fill_reports(GenerateFillReports(
                instrument_id=SOURCE_ID, venue_order_id=None, start=None, end=None,
                command_id=UUID4(), ts_init=0,
            ))
            if state_version == 1 and not keep_marker:
                unknown = second.client._cid_store.fee_metadata_for_cid(cid)
                assert unknown is not None
                assert unknown.native_fills[0].native_fill_origin == "unknown"
                with pytest.raises(BitfinexV1ExecutionError, match="commission"):
                    second.client._validate_cached_open_report(restored, reports[0], fills)
                assert not second.client.fee_summary(restored.client_order_id).complete
                return
            second.client._validate_cached_open_report(restored, reports[0], fills)
            assert second.client.fee_summary(restored.client_order_id).complete
            second.client._consume_private_frame([0, "te", interim])
            assert second.client._by_cid[cid].filled_qty == Decimal(1)
            assert not any(isinstance(event, OrderFilled) for event in second.events)
            changed = cast(list[object], final[2]).copy()
            changed[9] = Decimal("-0.061669")
            with pytest.raises(BitfinexV1ExecutionError, match="trade ID changed"):
                second.client._consume_private_frame([0, "tu", changed])
        finally:
            await second.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal_status", ["CANCELED", "INSUFFICIENT MARGIN"])
def test_fee_summary_excludes_only_cached_proven_zero_fill_terminals(
    terminal_status: str,
) -> None:
    async def scenario() -> None:
        harness = _Harness()
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(msgbus=harness.msgbus, cache=harness.cache, clock=harness.clock)
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
        await harness.connect()
        try:
            order = harness.order(tif=TimeInForce.IOC, post_only=False, quantity="2")
            harness.cache.add_order(order)
            cid = await harness.submit(order)
            assert not harness.client.fee_summary().complete
            if terminal_status == "CANCELED":
                harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            harness.client._consume_private_frame(
                harness.order_frame("oc", cid, order, status=terminal_status),
            )
            assert order.is_closed and order.filled_qty.as_decimal() == Decimal(0)
            assert harness.client._cid_store.fee_metadata_for_cid(cid) is None
            assert harness.client.fee_summary().complete
            harness.cache.reset()
            assert not harness.client.fee_summary().complete  # Lost execution proof is not zero.
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("message_type", ["te", "tu"])
def test_trade_observation_can_replay_after_fill_application_fails(
    message_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        is_interim = message_type == "te"
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL if is_interim else RAW_SYMBOL,
            wallet_currency="TESTUSDTF0" if is_interim else "USTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                tif=TimeInForce.IOC,
                post_only=False,
                quantity="2",
            )
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            update = harness.trade_frame(
                cid,
                order,
                quantity="2",
                fee="-1.79",
                maker=-1,
            )
            frame = update
            if is_interim:
                interim = cast(list[object], update[2]).copy()
                interim[9:11] = [None, None]
                frame = [0, "te", interim]

            original_apply = harness.client._apply_trade_fill
            attempts = 0

            def fail_once(
                live: Any,
                *,
                trade: Any,
                commission: Money,
                info: dict[str, str],
            ) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise BitfinexV1ExecutionError("synthetic fill application failure")
                original_apply(
                    live,
                    trade=trade,
                    commission=commission,
                    info=info,
                )

            monkeypatch.setattr(harness.client, "_apply_trade_fill", fail_once)
            with pytest.raises(
                BitfinexV1ExecutionError,
                match="synthetic fill application failure",
            ):
                harness.client._consume_private_frame(frame)
            assert 1234 not in harness.client._seen_trades
            assert 1234 not in harness.client._paper_interim_fills

            harness.client._consume_private_frame(frame)
            assert attempts == 2
            assert (
                len([event for event in _order_events(harness) if isinstance(event, OrderFilled)])
                == 1
            )
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_paper_te_is_prompt_fill_authority_when_tu_is_absent() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                tif=TimeInForce.IOC,
                post_only=False,
                quantity="2",
            )
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            update = harness.trade_frame(
                cid,
                order,
                quantity="2",
                fee="-1.79",
                maker=-1,
            )
            interim = cast(list[object], update[2]).copy()
            interim[9:11] = [None, None]

            harness.client._consume_private_frame([0, "te", interim])
            fills = [event for event in _order_events(harness) if isinstance(event, OrderFilled)]
            assert len(fills) == 1
            assert fills[0].last_qty.as_decimal() == Decimal("2")
            assert fills[0].commission.as_decimal() == Decimal(0)
            assert fills[0].commission.currency.code == "USD"
            assert fills[0].info == {
                "bitfinex_fill_source": "te_paper",
                "bitfinex_fee_status": "pending",
            }

            harness.client._consume_private_frame(
                harness.order_frame(
                    "oc",
                    cid,
                    order,
                    remaining="0",
                    status="EXECUTED @ 3926.75(2)",
                )
            )
            assert harness.client.execution_hold_reason is None

            harness.client._consume_private_frame(update)
            harness.client._consume_private_frame([0, "te", interim])
            fills = [event for event in _order_events(harness) if isinstance(event, OrderFilled)]
            assert len(fills) == 1
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_paper_full_te_then_zero_flag_executed_oc_survives_cancel_race() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(quantity="2")
            cid = await harness.submit(order)
            accepted = harness.order_frame("on", cid, order)
            cast(list[object], accepted[2])[12] = 0
            harness.client._consume_private_frame(accepted)
            await harness.cancel(order)

            assert harness.client._by_cid[cid].pending_cancel
            trade = harness.trade_frame(cid, order, quantity="2", maker=1)
            interim = cast(list[object], trade[2]).copy()
            interim[9:11] = [None, None]
            harness.client._consume_private_frame([0, "te", interim])
            live = harness.client._by_cid[cid]
            assert live.accepted
            assert live.filled_qty == Decimal("2")
            assert not live.pending_cancel

            terminal = harness.order_frame(
                "oc",
                cid,
                order,
                remaining="0",
                status="EXECUTED @ 3926.75(2)",
            )
            cast(list[object], terminal[2])[12] = 0
            harness.client._consume_private_frame(terminal)

            assert live.terminal is not None
            assert live.terminal_emitted
            assert harness.client.execution_hold_reason is None
            assert _types(harness).count("OrderFilled") == 1
            assert "OrderCanceled" not in _types(harness)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_paper_zero_flag_executed_oc_rejects_partial_trusted_fill() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(quantity="2")
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            trade = harness.trade_frame(cid, order, quantity="1", maker=1)
            interim = cast(list[object], trade[2]).copy()
            interim[9:11] = [None, None]
            harness.client._consume_private_frame([0, "te", interim])

            live = harness.client._by_cid[cid]
            assert live.accepted
            assert live.filled_qty == Decimal("1")
            assert not live.pending_cancel
            terminal = harness.order_frame(
                "oc",
                cid,
                order,
                remaining="0",
                status="EXECUTED @ 3926.75(2)",
            )
            cast(list[object], terminal[2])[12] = 0

            with pytest.raises(BitfinexV1ExecutionError, match="differs from local submission"):
                harness.client._consume_private_frame(terminal)

            assert live.terminal is None
            assert not live.terminal_emitted
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
                harness.order_frame("oc", cid, order, status="CANCELED", price="3925.10")
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


@pytest.mark.parametrize("paper", [False, True])
def test_cancel_rejection_cannot_clear_an_earlier_unknown_modify(paper: bool) -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL if paper else RAW_SYMBOL,
            wallet_currency="TESTUSDTF0" if paper else "USTF0",
        )
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
                "USD",
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
                if start <= cast(int, cast(list[object], row)[self.timestamp_index]) <= end
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
        creation_only_in_lower: list[object] = [[3, None, None, None, lower + 1, upper - 1]]
        update_only_in_upper: list[object] = [[4, None, None, None, lower + 1, upper - 1]]
        rest = ScriptedHistoryRest(
            [both_in_whole_window, creation_only_in_lower, update_only_in_upper]
        )
        harness = _Harness(rest=rest)

        with pytest.raises(BitfinexV1ExecutionError, match="no consistent"):
            await harness.client._order_history_rows(lower, upper)

        outside = ScriptedHistoryRest([[[5, None, None, None, lower - 2, upper + 2]]])
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
                row for row in self.rows if start <= cast(int, cast(list[object], row)[2]) <= end
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

        stalled = TradeRest([[1, RAW_SYMBOL, now_ms], [2, RAW_SYMBOL, now_ms]])
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
        harness.cache.add_account(
            TestExecStubs.margin_account(account_id=harness.client.account_id)
        )
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


def test_restart_order_snapshot_proves_reduce_only_flags_before_fill() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(
            msgbus=harness.msgbus,
            cache=harness.cache,
            clock=harness.clock,
        )
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(
            TestExecStubs.margin_account(account_id=harness.client.account_id)
        )
        order = harness.order(
            side=OrderSide.SELL,
            tif=TimeInForce.IOC,
            post_only=False,
            reduce_only=True,
            quantity="2",
        )
        harness.cache.add_order(order)
        await harness.connect()
        try:
            cid = await harness.submit(order)
            accepted = harness.order_frame("on", cid, order)
            harness.client._consume_private_frame(accepted)
            assert order.status == OrderStatus.ACCEPTED

            harness.client._by_cid.clear()
            harness.client._cid_by_client.clear()
            harness.client._cid_by_venue.clear()
            event_count = engine.event_count

            harness.client._consume_private_frame([0, "os", [accepted[2]]])

            live = harness.client._by_cid[cid]
            assert live.accepted
            assert not live.submitted_in_process
            assert live.venue_order_id == VENUE_ORDER_ID
            assert live.venue_flags_verified
            assert engine.event_count == event_count

            harness.client._consume_private_frame(
                harness.trade_frame(cid, order, quantity="2", maker=-1)
            )
            assert order.status == OrderStatus.FILLED
            assert order.filled_qty.as_decimal() == Decimal("2")
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_restart_order_snapshot_rejects_wrong_reduce_only_flags() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        order = harness.order(
            side=OrderSide.SELL,
            tif=TimeInForce.IOC,
            post_only=False,
            reduce_only=True,
            quantity="2",
        )
        binding = harness.client._cid_store.allocate(
            order.client_order_id.value,
            epoch_ms=harness.clock.timestamp_ns() // 1_000_000,
        )
        order.apply(
            TestEventStubs.order_submitted(
                order,
                account_id=harness.client.account_id,
                ts_event=harness.clock.timestamp_ns(),
            )
        )
        order.apply(
            TestEventStubs.order_accepted(
                order,
                account_id=harness.client.account_id,
                venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                ts_event=harness.clock.timestamp_ns(),
            )
        )
        harness.cache.add_order(order)
        snapshot = harness.order_frame("on", binding.cid, order)
        cast(list[object], snapshot[2])[12] = 0

        with pytest.raises(BitfinexV1ExecutionError, match="differs from local submission"):
            harness.client._consume_private_frame([0, "os", [snapshot[2]]])

        live = harness.client._by_cid[binding.cid]
        assert not live.venue_flags_verified
        assert live.filled_qty == 0
        assert harness.client.execution_hold_reason == (
            "Bitfinex reduce-only order is waiting for exact venue flags"
        )

    asyncio.run(scenario())


def test_rehydrated_paper_order_does_not_inherit_zero_flag_tolerance() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(
            msgbus=harness.msgbus,
            cache=harness.cache,
            clock=harness.clock,
        )
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(
            TestExecStubs.margin_account(account_id=harness.client.account_id)
        )
        order = harness.order()
        harness.cache.add_order(order)
        await harness.connect()
        try:
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            assert order.status == OrderStatus.ACCEPTED

            harness.client._by_cid.clear()
            harness.client._cid_by_client.clear()
            harness.client._cid_by_venue.clear()
            update = harness.order_frame("ou", cid, order)
            cast(list[object], update[2])[12] = 0

            with pytest.raises(BitfinexV1ExecutionError, match="differs from local submission"):
                harness.client._consume_private_frame(update)

            assert not harness.client._by_cid[cid].submitted_in_process
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_paper_mass_status_does_not_inherit_pending_submit_zero_flag_tolerance() -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        harness.msgbus.deregister("ExecEngine.process", harness.events.append)
        engine = ExecutionEngine(
            msgbus=harness.msgbus,
            cache=harness.cache,
            clock=harness.clock,
        )
        engine.register_client(harness.client)
        harness.cache.add_instrument(harness.instrument)
        harness.cache.add_account(
            TestExecStubs.margin_account(account_id=harness.client.account_id)
        )
        order = harness.order()
        harness.cache.add_order(order)
        await harness.connect()
        try:
            cid = await harness.submit(order)
            accepted = harness.order_frame("on", cid, order)
            harness.client._consume_private_frame(accepted)
            assert order.status == OrderStatus.ACCEPTED
            row = cast(list[object], accepted[2]).copy()
            row[12] = 0
            rest.active = [row]

            with pytest.raises(BitfinexV1ExecutionError, match="post_only"):
                await harness.client.generate_mass_status(lookback_mins=1)
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("wrong_price", [None, Decimal("3926.71")])
def test_mass_status_safely_binds_unknown_reduce_only_submit_without_venue_id(
    wrong_price: Decimal | None,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            harness.fake.fail_next_send = True
            with pytest.raises(ConnectionError, match="uncertain"):
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
            binding = harness.client._cid_store.binding_for_client(order.client_order_id.value)
            assert binding is not None
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            harness.cache.add_order(order)
            row = cast(list[object], harness.order_frame("on", binding.cid, order)[2])
            if wrong_price is not None:
                row[16] = wrong_price
            rest.active = [row]

            if wrong_price is not None:
                with pytest.raises(BitfinexV1ExecutionError, match="price"):
                    await harness.client.generate_mass_status(lookback_mins=1)
                live = harness.client._by_cid[binding.cid]
                assert not live.accepted
                assert live.venue_order_id is None
                assert not live.venue_flags_verified
                assert live.unknown_operations == {"submit"}
                return

            mass_status = await harness.client.generate_mass_status(lookback_mins=1)
            assert mass_status is not None
            live = harness.client._by_cid[binding.cid]
            assert live.accepted
            assert live.venue_order_id == VENUE_ORDER_ID
            assert live.venue_flags_verified
            assert not live.unknown_operations
            assert harness.client._cid_by_venue[VENUE_ORDER_ID] == binding.cid
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_mass_status_rehydrates_and_proves_restart_reduce_only_order() -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        order = harness.order(
            side=OrderSide.SELL,
            tif=TimeInForce.IOC,
            post_only=False,
            reduce_only=True,
            quantity="2",
        )
        binding = harness.client._cid_store.allocate(
            order.client_order_id.value,
            epoch_ms=harness.clock.timestamp_ns() // 1_000_000,
        )
        harness.cache.add_order(order)
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
        rest.active = [cast(list[object], harness.order_frame("on", binding.cid, order)[2])]

        mass_status = await harness.client.generate_mass_status(lookback_mins=1)

        assert mass_status is not None
        live = harness.client._by_cid[binding.cid]
        assert live.accepted
        assert not live.submitted_in_process
        assert live.venue_order_id == VENUE_ORDER_ID
        assert live.venue_flags_verified

    asyncio.run(scenario())


@pytest.mark.parametrize("allow_cold", [False, True])
def test_nonzero_position_only_mass_status_requires_explicit_cold_start_opt_in(
    allow_cold: bool,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        rest.position_rows = [_position_row(Decimal("2"), avg_px=Decimal("3926.75"))]
        harness = _Harness(
            rest=rest,
            allow_cold_position_reconciliation=allow_cold,
        )
        assert harness.cache.orders(instrument_id=SOURCE_ID) == []
        assert harness.cache.positions(instrument_id=SOURCE_ID) == []
        assert not harness.client._by_cid

        if not allow_cold:
            with pytest.raises(BitfinexV1ExecutionError, match="position differs"):
                await harness.client.generate_mass_status(lookback_mins=1)
            return

        mass_status = await harness.client.generate_mass_status(lookback_mins=1)
        assert mass_status is not None
        positions = mass_status.position_reports[SOURCE_ID]
        assert len(positions) == 1
        assert positions[0].signed_decimal_qty == Decimal("2")

    asyncio.run(scenario())


def test_cold_position_opt_in_still_rejects_live_pending_order() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        rest.position_rows = [_position_row(Decimal("2"), avg_px=Decimal("3926.75"))]
        harness = _Harness(
            rest=rest,
            allow_cold_position_reconciliation=True,
        )
        await harness.connect()
        try:
            order = harness.order()
            await harness.submit(order)
            assert harness.client._by_cid

            with pytest.raises(BitfinexV1ExecutionError, match="position differs"):
                await harness.client.generate_mass_status(lookback_mins=1)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_cold_position_opt_in_still_rejects_cached_and_venue_open_order() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        rest.position_rows = [_position_row(Decimal("2"), avg_px=Decimal("3926.75"))]
        harness = _Harness(
            rest=rest,
            allow_cold_position_reconciliation=True,
        )
        order = harness.order()
        binding = harness.client._cid_store.allocate(
            order.client_order_id.value,
            epoch_ms=harness.clock.timestamp_ns() // 1_000_000,
        )
        order.apply(
            TestEventStubs.order_submitted(
                order,
                account_id=harness.client.account_id,
                ts_event=harness.clock.timestamp_ns(),
            )
        )
        order.apply(
            TestEventStubs.order_accepted(
                order,
                account_id=harness.client.account_id,
                venue_order_id=VenueOrderId(str(VENUE_ORDER_ID)),
                ts_event=harness.clock.timestamp_ns(),
            )
        )
        harness.cache.add_order(order)
        rest.active = [cast(list[object], harness.order_frame("on", binding.cid, order)[2])]

        with pytest.raises(BitfinexV1ExecutionError, match="position differs"):
            await harness.client.generate_mass_status(lookback_mins=1)

    asyncio.run(scenario())


def test_cold_position_opt_in_still_rejects_owned_venue_open_order_without_cache() -> None:
    async def scenario() -> None:
        rest = _FakeRest()
        rest.position_rows = [_position_row(Decimal("2"), avg_px=Decimal("3926.75"))]
        harness = _Harness(
            rest=rest,
            allow_cold_position_reconciliation=True,
        )
        order = harness.order()
        binding = harness.client._cid_store.allocate(
            order.client_order_id.value,
            epoch_ms=harness.clock.timestamp_ns() // 1_000_000,
        )
        rest.active = [cast(list[object], harness.order_frame("on", binding.cid, order)[2])]
        assert harness.cache.orders(instrument_id=SOURCE_ID) == []
        assert not harness.client._by_cid

        with pytest.raises(BitfinexV1ExecutionError, match="position differs"):
            await harness.client.generate_mass_status(lookback_mins=1)

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
            rest.position_rows = [_position_row(Decimal("1"), avg_px=Decimal("3926.75"))]
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
            rest.position_rows = [_position_row(Decimal("1"), avg_px=Decimal("3926.75"))]
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
            first_fill = next(event for event in order.events if isinstance(event, OrderFilled))
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
                commission=Money(Decimal("0.20"), USD),
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
            rest.position_rows = [_position_row(Decimal("3"), avg_px=Decimal("3926.78333333"))]
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
            original_metadata = harness.client._cid_store.fee_metadata
            with pytest.raises(BitfinexV1ExecutionError, match="trade ID changed"):
                await harness.client.generate_fill_reports(GenerateFillReports(
                    instrument_id=SOURCE_ID, venue_order_id=None, start=None, end=None,
                    command_id=UUID4(), ts_init=0,
                ))
            # NT's parent mass-status boundary represents a failed component query
            # as None; this is not a successful empty reconciliation.
            assert await harness.client.generate_mass_status(lookback_mins=None) is None
            assert harness.client._cid_store.fee_metadata == original_metadata
            assert order.filled_qty.as_decimal() == Decimal(3)
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 2
            assert not harness.client.accounting_ready

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


@pytest.mark.parametrize("late_tu_has_cid", [False, True])
def test_live_engine_recovers_executed_order_when_trade_history_is_empty(
    late_tu_has_cid: bool,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
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
        order = harness.order(
            tif=TimeInForce.IOC,
            post_only=False,
            quantity="2",
            client_order_id=ClientOrderId("ENGINE-MISSING-TRADE-1"),
        )
        harness.cache.add_order(order)
        await harness.connect()
        try:
            cid = await harness.submit(order)
            accepted = harness.order_frame("on", cid, order)
            harness.client._consume_private_frame(accepted)
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

            terminal = harness.order_frame(
                "oc",
                cid,
                order,
                remaining="0",
                status="EXECUTED @ 3926.75(2)",
            )
            terminal_row = cast(list[object], terminal[2])
            now_ms = harness.clock.timestamp_ns() // 1_000_000
            terminal_row[4] = now_ms - 100
            terminal_row[5] = now_ms
            terminal_row[17] = Decimal("3926.75")
            harness.client._consume_private_frame(terminal)
            reconciliation_required_before = harness.client.terminal_reconciliation_required
            assert reconciliation_required_before
            assert order.status == OrderStatus.ACCEPTED

            rest.active = []
            rest.history = [terminal_row.copy()]
            rest.trades = []
            rest.position_rows = [
                _position_row(
                    Decimal("2"),
                    avg_px=Decimal("3926.75"),
                    raw_symbol=PAPER_RAW_SYMBOL,
                )
            ]
            assert await engine.reconcile_execution_state(timeout_secs=1.0) is True
            assert order.status == OrderStatus.FILLED
            assert order.filled_qty == harness.instrument.make_qty(Decimal("2"))

            harness.client.confirm_terminal_reconciliation()
            assert not bool(harness.client.terminal_reconciliation_required)
            assert harness.client.execution_hold_reason is None
            before = harness.client.fee_summary(order.client_order_id)
            assert not before.complete
            metadata = harness.client._cid_store.fee_metadata_for_cid(cid)
            assert metadata is not None
            assert metadata.native_fills[0].native_fill_origin == "inferred"
            native_id = metadata.native_fills[0].trade_id
            assert not native_id.isdecimal()
            for trade_id in (1234, 1235):
                final = harness.trade_frame(
                    cid, order, quantity="1", price="3926.75", fee="-0.0049", maker=-1,
                )
                cast(list[object], final[2])[0] = trade_id
                cast(list[object], final[2])[2] = now_ms
                if not late_tu_has_cid:
                    cast(list[object], final[2])[11] = None
                harness.client._consume_private_frame(final)
                harness.client._consume_private_frame(final)
                summary = harness.client.fee_summary(order.client_order_id)
                assert summary.complete == (trade_id == 1235)
            assert summary.currencies["USD"].raw_cost == Decimal("0.0098")
            assert summary.currencies["USD"].quantized_cost == Decimal(0)
            assert summary.currencies["USDT"].native_cost == Decimal(0)
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
            changed = cast(list[object], final[2]).copy()
            changed[9] = Decimal("-0.0048")
            with pytest.raises(BitfinexV1ExecutionError, match="trade ID changed"):
                harness.client._consume_private_frame([0, "tu", changed])
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_live_engine_recovers_silent_reduce_only_ioc_from_exact_rest_terminal() -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
            mutation_ack_timeout_ms=100,
        )
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
        order = harness.order(
            side=OrderSide.SELL,
            tif=TimeInForce.IOC,
            post_only=False,
            reduce_only=True,
            quantity="2",
            client_order_id=ClientOrderId("ENGINE-SILENT-REDUCE-1"),
        )
        harness.cache.add_order(order)
        await harness.connect()
        try:
            cid = await harness.submit(order)
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            harness.cache.update_order(order)
            await asyncio.sleep(0.11)

            reconciliation_required_before = harness.client.terminal_reconciliation_required
            assert reconciliation_required_before
            assert order.status == OrderStatus.SUBMITTED
            assert len(harness.fake.sent) == 2

            terminal = harness.order_frame(
                "oc",
                cid,
                order,
                remaining="0",
                status="EXECUTED @ 3926.75(2)",
            )
            terminal_row = cast(list[object], terminal[2])
            now_ms = harness.clock.timestamp_ns() // 1_000_000
            terminal_row[4] = now_ms - 100
            terminal_row[5] = now_ms
            terminal_row[17] = Decimal("3926.75")
            trade_row = cast(
                list[object],
                harness.trade_frame(
                    cid,
                    order,
                    quantity="2",
                    price="3926.75",
                    fee="-0.10",
                    maker=-1,
                )[2],
            )
            trade_row[2] = now_ms
            rest.history = [terminal_row]
            rest.trades = [trade_row]
            rest.position_rows = [
                _position_row(
                    Decimal("-2"),
                    avg_px=Decimal("3926.75"),
                    raw_symbol=PAPER_RAW_SYMBOL,
                )
            ]

            assert await engine.reconcile_execution_state(timeout_secs=1.0) is True
            assert order.status == OrderStatus.FILLED
            assert order.filled_qty == harness.instrument.make_qty(Decimal("2"))

            harness.client.confirm_terminal_reconciliation()
            reconciliation_required_after = harness.client.terminal_reconciliation_required
            assert not reconciliation_required_after
            assert harness.client.execution_hold_reason is None
            assert len(harness.fake.sent) == 2
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_post_send_stream_stop_marks_reduce_only_ioc_for_reconciliation() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
            mutation_ack_timeout_ms=100,
        )
        await harness.connect()
        try:

            def stop_after_order_send(payload: dict[str, object] | list[object]) -> None:
                if isinstance(payload, list):
                    harness.client._running = False

            harness.fake.after_send = stop_after_order_send
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            cid = await harness.submit(order)

            live = harness.client._by_cid[cid]
            assert live.unknown_operations == {"submit"}
            assert live.terminal_reconciliation_pending
            reconciliation_required_before = harness.client.terminal_reconciliation_required
            assert not reconciliation_required_before
            await asyncio.sleep(0.11)
            reconciliation_required_after = harness.client.terminal_reconciliation_required
            assert reconciliation_required_after
            assert not harness.client._ack_deadlines
            assert len(harness.fake.sent) == 2
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("ack_before_failure", [False, True])
def test_reader_failure_after_public_submit_still_recovers_exact_rest_terminal(
    ack_before_failure: bool,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
            mutation_ack_timeout_ms=100,
        )
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
        order = harness.order(
            side=OrderSide.SELL,
            tif=TimeInForce.IOC,
            post_only=False,
            reduce_only=True,
            quantity="2",
            client_order_id=ClientOrderId("READER-FAILED-SILENT-REDUCE-1"),
        )
        harness.cache.add_order(order)
        command = SubmitOrder(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            order=order,
            command_id=UUID4(),
            ts_init=harness.clock.timestamp_ns(),
            params={"leverage": 10},
        )
        await harness.connect()
        engine.start()
        try:
            # Exercise the same task scheduling API used by the live execution engine.
            harness.client.submit_order(command)

            async def wait_for_send() -> None:
                while len(harness.fake.sent) < 2 or order.status != OrderStatus.SUBMITTED:
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_send(), timeout=1.0)
            payload = cast(dict[str, object], cast(list[object], harness.fake.sent[-1])[3])
            cid = cast(int, payload["cid"])
            assert order.status == OrderStatus.SUBMITTED
            assert (cid, "submit") in harness.client._ack_deadlines

            if ack_before_failure:
                await harness.fake.queue.put(harness.order_frame("on", cid, order))

                async def wait_for_acceptance() -> None:
                    while order.status != OrderStatus.ACCEPTED:
                        await asyncio.sleep(0)

                await asyncio.wait_for(wait_for_acceptance(), timeout=1.0)
                assert (cid, "submit") not in harness.client._ack_deadlines

            # A malformed post-auth control frame terminates the real reader task.
            await harness.fake.queue.put({"event": "info", "version": 2})
            reader = harness.client._reader_task
            assert reader is not None
            await asyncio.wait_for(asyncio.shield(reader), timeout=1.0)

            live = harness.client._by_cid[cid]
            assert live.unknown_operations == {"submit"}
            assert live.terminal_reconciliation_pending
            reconciliation_required_before = harness.client.terminal_reconciliation_required
            assert not reconciliation_required_before
            await asyncio.sleep(0.11)
            reconciliation_required_after = harness.client.terminal_reconciliation_required
            assert reconciliation_required_after
            assert not harness.client._ack_deadlines

            terminal_row = cast(
                list[object],
                harness.order_frame(
                    "oc",
                    cid,
                    order,
                    remaining="0",
                    status="EXECUTED @ 3926.75(2)",
                )[2],
            )
            now_ms = harness.clock.timestamp_ns() // 1_000_000
            terminal_row[4:6] = [now_ms - 100, now_ms]
            terminal_row[17] = Decimal("3926.75")
            trade_row = cast(
                list[object],
                harness.trade_frame(
                    cid,
                    order,
                    quantity="2",
                    price="3926.75",
                    fee="-0.10",
                    maker=-1,
                )[2],
            )
            trade_row[2] = now_ms
            rest.history = [terminal_row]
            rest.trades = [trade_row]
            rest.position_rows = [
                _position_row(
                    Decimal("-2"),
                    avg_px=Decimal("3926.75"),
                    raw_symbol=PAPER_RAW_SYMBOL,
                )
            ]

            assert await engine.reconcile_execution_state(timeout_secs=1.0) is True
            assert order.status == OrderStatus.FILLED
            assert order.filled_qty == harness.instrument.make_qty(Decimal("2"))
            harness.client.confirm_terminal_reconciliation()
            assert not live.unknown_operations
            assert not harness.client.terminal_reconciliation_required
            assert cid not in harness.client._by_cid
            assert harness.client.execution_hold_reason is not None
            assert len(harness.fake.sent) == 2
        finally:
            engine.stop()
            await asyncio.sleep(0)
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing_trade", "lacks exact trade quantity evidence"),
        ("wrong_flags", "reduce_only"),
        ("wrong_quantity", "quantity"),
        ("wrong_trade_venue", "changes CID/order identity"),
    ],
)
def test_silent_reduce_only_terminal_recovery_requires_exact_rest_evidence(
    fault: str,
    message: str,
) -> None:
    async def scenario() -> None:
        rest = _FakeRest(PAPER_RAW_SYMBOL)
        harness = _Harness(
            rest=rest,
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
            mutation_ack_timeout_ms=100,
        )
        await harness.connect()
        try:
            order = harness.order(
                side=OrderSide.SELL,
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=True,
                quantity="2",
            )
            harness.cache.add_order(order)
            cid = await harness.submit(order)
            order.apply(
                TestEventStubs.order_submitted(
                    order,
                    account_id=harness.client.account_id,
                    ts_event=harness.clock.timestamp_ns(),
                )
            )
            harness.cache.update_order(order)
            await asyncio.sleep(0.11)

            terminal_row = cast(
                list[object],
                harness.order_frame(
                    "oc",
                    cid,
                    order,
                    remaining="0",
                    status="EXECUTED @ 3926.75(2)",
                )[2],
            )
            now_ms = harness.clock.timestamp_ns() // 1_000_000
            terminal_row[4:6] = [now_ms - 100, now_ms]
            terminal_row[17] = Decimal("3926.75")
            trade_row = cast(
                list[object],
                harness.trade_frame(
                    cid,
                    order,
                    quantity="2",
                    price="3926.75",
                    fee="-0.10",
                    maker=-1,
                )[2],
            )
            trade_row[2] = now_ms
            if fault == "missing_trade":
                trades: list[object] = []
            else:
                trades = [trade_row]
            if fault == "wrong_flags":
                terminal_row[12] = 0
            elif fault == "wrong_quantity":
                terminal_row[7] = Decimal("-1")
            elif fault == "wrong_trade_venue":
                trade_row[3] = VENUE_ORDER_ID + 1
            rest.history = [terminal_row]
            rest.trades = trades
            rest.position_rows = []

            with pytest.raises(BitfinexV1ExecutionError, match=message):
                await harness.client.generate_mass_status(lookback_mins=None)

            live = harness.client._by_cid[cid]
            assert live.reconciled_terminal is None
            assert live.unknown_operations == {"submit"}
            assert harness.client.terminal_reconciliation_required
            assert len(harness.fake.sent) == 2
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw_symbol", "wallet_currency", "reduce_only"),
    [
        (RAW_SYMBOL, "USTF0", False),
        (RAW_SYMBOL, "USTF0", True),
    ],
)
def test_silent_terminal_recovery_does_not_widen_production_orders(
    raw_symbol: str,
    wallet_currency: str,
    reduce_only: bool,
) -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=raw_symbol,
            wallet_currency=wallet_currency,
            mutation_ack_timeout_ms=100,
        )
        await harness.connect()
        try:
            order = harness.order(
                tif=TimeInForce.IOC,
                post_only=False,
                reduce_only=reduce_only,
                quantity="2",
            )
            cid = await harness.submit(order)
            await asyncio.sleep(0.11)

            assert harness.client._by_cid[cid].unknown_operations == {"submit"}
            assert not harness.client._by_cid[cid].terminal_reconciliation_pending
            assert not harness.client.terminal_reconciliation_required
            assert len(harness.fake.sent) == 2
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_terminal_confirmation_rejects_an_unreconciled_cache() -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL,
            wallet_currency="TESTUSDTF0",
        )
        await harness.connect()
        try:
            order = harness.order(tif=TimeInForce.IOC, post_only=False, quantity="2")
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            terminal = harness.order_frame(
                "oc",
                cid,
                order,
                remaining="0",
                status="EXECUTED @ 3926.75(2)",
            )
            cast(list[object], terminal[2])[17] = Decimal("3926.75")
            harness.client._consume_private_frame(terminal)

            with pytest.raises(
                BitfinexV1ExecutionError,
                match="cache does not prove",
            ):
                harness.client.confirm_terminal_reconciliation()
            assert harness.client.terminal_reconciliation_required
        finally:
            await harness.close()

    asyncio.run(scenario())


@asynccontextmanager
async def _terminal_recovery_case(
    tmp_path: Path, *, partial: bool, maker: bool = False, paper: bool = True,
    request_cancel: bool = False, terminal_flags: int | None = None, rest_flags: int | None = None,
    silent: bool = False, accepted: bool = True, zero: bool = False, reduce_only: bool = False,
    working: bool = False,
) -> AsyncIterator[tuple[_Harness, LiveExecutionEngine, JsonStateStore, Order, list[object]]]:
    raw_symbol = PAPER_RAW_SYMBOL if paper else RAW_SYMBOL
    harness = _Harness(
        cid_store_path=tmp_path / "cids.json", raw_symbol=raw_symbol,
        wallet_currency="TESTUSDTF0" if paper else "USTF0",
        mutation_ack_timeout_ms=100 if silent else 10_000,
    )
    harness.msgbus.deregister("ExecEngine.process", harness.events.append)
    engine = LiveExecutionEngine(
        loop=asyncio.get_running_loop(), msgbus=harness.msgbus,
        cache=harness.cache, clock=harness.clock,
        config=LiveExecEngineConfig(
            load_cache=False, reconciliation=True, generate_missing_orders=False,
            inflight_check_interval_ms=0, open_check_interval_secs=None,
            position_check_interval_secs=None,
        ),
    )
    engine.register_client(harness.client)
    harness.cache.add_instrument(harness.instrument)
    harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
    store = JsonStateStore(tmp_path / "hedges.json")
    hedges = HedgeCoordinator(SOURCE_ID, store)

    def observe(event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            hedges.on_source_filled(event)
        elif isinstance(event, OrderCanceled):
            store.update_source_status(event.client_order_id.value, "CANCELED")

    await harness.connect()
    engine.start()
    try:
        order = harness.order(
            tif=TimeInForce.GTC if maker else TimeInForce.IOC, post_only=maker,
            quantity="4" if partial else "2", reduce_only=reduce_only,
        )
        harness.cache.add_order(order)
        store.begin_source(
            order.client_order_id.value, BusinessOrderSide.BUY, Decimal(str(order.quantity)),
        )
        harness.msgbus.subscribe(f"events.order.{order.strategy_id}", observe)
        cid = await harness.submit(order)
        if accepted:
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
        for _ in range(20):
            await asyncio.sleep(0)
        assert order.status == (OrderStatus.ACCEPTED if accepted else OrderStatus.SUBMITTED)
        if request_cancel:
            await harness.cancel(order)
            for _ in range(20):
                await asyncio.sleep(0)
        now_ms = harness.clock.timestamp_ns() // 1_000_000
        terminal = harness.order_frame(
            "oc", cid, order, remaining=str(order.quantity) if zero else "2" if partial else "0",
            status="IOC CANCELED" if zero else "CANCELED" if partial else "EXECUTED @ 3926.75(2)",
        )
        terminal_row = cast(list[object], terminal[2])
        terminal_row[4:6] = [now_ms - 100, now_ms]
        terminal_row[17] = Decimal(0) if zero else Decimal("3926.75")
        if terminal_flags is not None:
            terminal_row[12] = terminal_flags
        final = harness.trade_frame(
            cid, order, quantity="2", fee="-0.061668", maker=1 if maker else -1,
        )
        cast(list[object], final[2])[2] = now_ms
        if not silent:
            harness.client._consume_private_frame(terminal)
        history = terminal_row.copy()
        if rest_flags is not None:
            history[12] = rest_flags
        harness.rest.history = [history]
        if working:
            history[13] = "ACTIVE" if zero else "PARTIALLY FILLED @ 3926.75(2)"
            harness.rest.active = [history]
            harness.rest.history = []
        harness.rest.trades = [] if zero else [final[2]]
        harness.rest.position_rows = [] if zero else [_position_row(
            Decimal(2), avg_px=Decimal("3926.75"), raw_symbol=raw_symbol,
        )]
        if silent:
            await asyncio.sleep(0.11)
        yield harness, engine, store, order, final
    finally:
        engine.stop()
        await asyncio.sleep(0)
        await harness.close()
        engine.dispose()


@pytest.mark.parametrize("state", ["unchanged", "opaque", "missing", "filled", "price"])
def test_working_maker_observation_only_detects_differences(tmp_path: Path, state: str) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=state == "filled", zero=state != "filled", maker=True,
            silent=True, working=True,
        ) as (harness, _, store, order, _):
            assert harness.client.has_working_orders
            if state == "missing":
                harness.rest.active = []
            elif state == "opaque":
                cast(list[object], harness.rest.active[0])[12] = 0
            elif state == "price":
                cast(list[object], harness.rest.active[0])[16] = Decimal("3900")
            assert await harness.client.check_working_orders() == (
                state not in {"unchanged", "opaque"}
            )
            assert order.status == OrderStatus.ACCEPTED and order.filled_qty.as_decimal() == 0
            assert not store.intents() and len(harness.fake.sent) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["zero", "partial", "full", "working"])
def test_working_maker_mass_registers_exact_applied_quantities(
    tmp_path: Path, terminal: str,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=terminal in {"partial", "working"}, zero=terminal == "zero",
            maker=True, silent=True, working=terminal == "working",
        ) as (harness, engine, store, order, final):
            assert await engine.reconcile_execution_state(timeout_secs=1)
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None
            assert live.filled_qty == order.filled_qty.as_decimal()
            harness.client.confirm_terminal_reconciliation()
            if terminal == "working":
                assert harness.client._live_for_client(order.client_order_id) is live
            else:
                assert harness.client._live_for_client(order.client_order_id) is None
            if terminal != "zero":
                harness.client._consume_private_frame(final)
                harness.client._consume_private_frame(final)
            await asyncio.sleep(0)
            assert len(store.intents()) == (terminal != "zero")
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["fill", "cancel", "modify", "unknown_modify"])
def test_working_maker_observation_rechecks_live_authority_after_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=True, zero=True, maker=True, silent=True, working=True,
        ) as (harness, _, store, order, final):
            entered, release = asyncio.Event(), asyncio.Event()
            snapshot = [cast(list[object], harness.rest.active[0]).copy()]

            async def delayed_active(_symbol: str) -> object:
                entered.set()
                await release.wait()
                return snapshot

            monkeypatch.setattr(harness.rest, "active_orders_by_symbol", delayed_active)
            observed = asyncio.create_task(harness.client.check_working_orders())
            await entered.wait()
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None
            if action == "fill":
                harness.client._consume_private_frame(final)
            elif action == "cancel":
                await harness.cancel(order)
            elif action == "unknown_modify":
                harness.fake.fail_next_send = True
                with pytest.raises(ConnectionError):
                    await harness.modify(order, price="3900")
            else:
                await harness.modify(order, price="3900")
                harness.client._consume_private_frame(harness.order_frame(
                    "ou", live.cid, order, price="3900",
                ))
            for _ in range(20):
                await asyncio.sleep(0)
            sent = len(harness.fake.sent)
            release.set()
            assert await observed == (action == "modify")
            assert len(harness.fake.sent) == sent
            assert live.reconciled_working is None and live.reconciled_terminal is None
            assert order.filled_qty.as_decimal() == (Decimal(2) if action == "fill" else 0)
            assert len(store.intents()) == (action == "fill")
            if action == "unknown_modify":
                assert "modify" in live.unknown_operations
                assert harness.client.execution_hold_reason is not None
            elif action == "modify":
                assert order.price.as_decimal() == live.current_price == Decimal(3900)
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["production", "unaccepted", "cold", "ioc"])
def test_working_maker_observation_excludes_unowned_or_out_of_scope_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=False, zero=True, maker=boundary != "ioc", silent=True,
            paper=boundary != "production", accepted=boundary != "unaccepted", working=True,
        ) as (harness, _, _, order, _):
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None
            if boundary == "cold":
                live.submitted_in_process = False  # Deliberately remove same-run authority.

            async def unexpected(_symbol: str) -> object:
                pytest.fail("out-of-scope order must not start an active-list observation")

            monkeypatch.setattr(harness.rest, "active_orders_by_symbol", unexpected)
            assert not harness.client.has_working_orders
            assert not await harness.client.check_working_orders()
    asyncio.run(scenario())


@pytest.mark.parametrize("working", [False, True])
@pytest.mark.parametrize("fault", ["missing", "type", "price", "time"])
def test_working_maker_mass_never_infers_missing_or_inconsistent_fills(
    tmp_path: Path, working: bool, fault: str,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=working, maker=True, silent=True, working=working,
        ) as (harness, engine, store, order, final):
            if fault == "missing":
                harness.rest.trades = []
            else:
                row = cast(list[object], final[2])
                if fault == "type":
                    row[6] = "IOC"
                elif fault == "price":
                    row[7] = Decimal("3900")
                else:
                    row[2] = cast(int, row[2]) - 1000
            assert not await engine.reconcile_execution_state(timeout_secs=1)
            assert order.filled_qty.as_decimal() == 0 and not store.intents()
    asyncio.run(scenario())


def test_working_maker_native_fill_then_new_tu_counts_actual_cumulative_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=True, maker=True, silent=True, working=True,
        ) as (harness, engine, store, order, final):
            assert await engine.reconcile_execution_state(timeout_secs=1)
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None and live.filled_qty == Decimal(2)
            next_trade = cast(list[object], final[2]).copy()
            next_trade[0] = 1235
            next_trade[4] = Decimal(1)
            harness.client._consume_private_frame([0, "tu", next_trade])
            harness.client._consume_private_frame([0, "tu", next_trade])
            for _ in range(20):
                await asyncio.sleep(0)
            assert live.filled_qty == order.filled_qty.as_decimal() == Decimal(3)
            assert len(store.intents()) == 2
            row = cast(list[object], harness.rest.active[0])
            row[6] = Decimal(1)
            harness.rest.trades.append(next_trade)
            harness.rest.position_rows = [_position_row(
                Decimal(3), avg_px=Decimal("3926.75"), raw_symbol=PAPER_RAW_SYMBOL,
            )]
            assert await engine.reconcile_execution_state(timeout_secs=1)
            harness.client.confirm_terminal_reconciliation()
            assert live.filled_qty == Decimal(3) and len(store.intents()) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("first_source", ["ws", "rest"])
@pytest.mark.parametrize("evidence", ["complete", "missing_old", "changed_old"])
def test_working_maker_complete_trade_set_covers_previously_applied_fills(
    tmp_path: Path, first_source: str, evidence: str,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=True, maker=True, silent=True, working=True,
        ) as (harness, engine, store, order, final):
            active = cast(list[object], harness.rest.active[0])
            first = cast(list[object], final[2])
            active[6], active[13] = Decimal(3), "PARTIALLY FILLED @ 3926.75(1)"
            first[4] = Decimal(1)
            harness.rest.position_rows = [_position_row(
                Decimal(1), avg_px=Decimal("3926.75"), raw_symbol=PAPER_RAW_SYMBOL,
            )]
            if first_source == "ws":
                harness.client._consume_private_frame(final)
                for _ in range(20):
                    await asyncio.sleep(0)
            else:
                assert await engine.reconcile_execution_state(timeout_secs=1)
                harness.client.confirm_terminal_reconciliation()
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None and live.filled_qty == order.filled_qty.as_decimal() == 1
            assert len(store.intents()) == 1
            # Active-only observation intentionally has no trades; this remains healthy.
            assert not await harness.client.check_working_orders()

            second = first.copy()
            second[0] = 1235
            active[6], active[13] = Decimal(2), "PARTIALLY FILLED @ 3926.75(2)"
            if evidence == "missing_old":
                second[4] = Decimal(2)
                harness.rest.trades = [second]
            elif evidence == "changed_old":
                altered = first.copy()
                altered[4], second[4] = Decimal("0.5"), Decimal("1.5")
                harness.rest.trades = [altered, second]
            else:
                harness.rest.trades = [first, second]
            harness.rest.position_rows = [_position_row(
                Decimal(2), avg_px=Decimal("3926.75"), raw_symbol=PAPER_RAW_SYMBOL,
            )]
            assert await engine.reconcile_execution_state(timeout_secs=1) == (
                evidence == "complete"
            )
            expected = Decimal(2) if evidence == "complete" else Decimal(1)
            assert live.filled_qty == order.filled_qty.as_decimal() == expected
            assert len(store.intents()) == int(expected)
            if evidence == "missing_old":
                with pytest.raises(BitfinexV1ExecutionError, match="previously applied"):
                    await harness.client.generate_mass_status(15)
            elif evidence == "complete":
                harness.client.confirm_terminal_reconciliation()
                for trade in (first, second, first, second):
                    harness.client._consume_private_frame([0, "tu", trade])
                assert live.filled_qty == Decimal(2) and len(store.intents()) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("repeat_mass", [False, True])
def test_working_maker_real_cancel_before_native_confirmation_retires_exactly(
    tmp_path: Path, repeat_mass: bool,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=True, maker=True, silent=True, working=True,
        ) as (harness, engine, store, order, final):
            assert await engine.reconcile_execution_state(timeout_secs=1)
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None and live.reconciled_working is not None
            await harness.cancel(order)
            row = cast(list[object], harness.rest.active[0]).copy()
            row[13] = "CANCELED"
            harness.rest.active, harness.rest.history = [], [row]
            harness.client._consume_private_frame([0, "oc", row])
            for _ in range(20):
                await asyncio.sleep(0)
            assert order.status == OrderStatus.CANCELED and live.terminal_emitted
            if repeat_mass:
                assert await engine.reconcile_execution_state(timeout_secs=1)
            harness.client.confirm_terminal_reconciliation()
            assert harness.client._live_for_client(order.client_order_id) is None
            harness.client._consume_private_frame(final)
            assert len(store.intents()) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["quantity", "trade", "missing_trade"])
def test_working_maker_closed_before_confirmation_rechecks_latest_real_facts(
    tmp_path: Path, fault: str,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=True, maker=True, silent=True, working=True,
        ) as (harness, engine, store, order, _):
            assert await engine.reconcile_execution_state(timeout_secs=1)
            await harness.cancel(order)
            row = cast(list[object], harness.rest.active[0]).copy()
            row[13] = "CANCELED"
            harness.rest.active, harness.rest.history = [], [row]
            harness.client._consume_private_frame([0, "oc", row.copy()])
            for _ in range(20):
                await asyncio.sleep(0)
            if fault == "quantity":
                row[6], row[7] = Decimal(3), Decimal(5)
            elif fault == "trade":
                cast(list[object], harness.rest.trades[0])[0] = 9999
            else:
                harness.rest.trades = []
            assert not await engine.reconcile_execution_state(timeout_secs=1)
            assert order.filled_qty.as_decimal() == 2 and len(store.intents()) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["unacked_ioc", "accepted_ioc", "cancel_maker"])
@pytest.mark.parametrize("terminal", ["zero", "partial", "full"])
def test_same_run_paper_silent_terminal_requires_native_confirmation(
    tmp_path: Path, kind: str, terminal: str,
) -> None:
    async def scenario() -> None:
        maker = kind == "cancel_maker"
        async with _terminal_recovery_case(
            tmp_path, partial=terminal == "partial", zero=terminal == "zero",
            maker=maker, request_cancel=maker, accepted=kind != "unacked_ioc", silent=True,
        ) as (harness, engine, store, order, final):
            assert bool(harness.client.terminal_reconciliation_required)
            assert bool(harness.client.execution_hold_reason)
            sent = len(harness.fake.sent)
            assert await engine.reconcile_execution_state(timeout_secs=1)
            for _ in range(20):
                await asyncio.sleep(0)
            assert order.status == (OrderStatus.FILLED if terminal == "full"
                                    else OrderStatus.CANCELED)
            expected = Decimal(0) if terminal == "zero" else Decimal(2)
            assert order.filled_qty.as_decimal() == expected
            assert bool(harness.client.terminal_reconciliation_required)
            if expected:
                # Native reconciliation won; a late real TU before adapter retirement is inert.
                harness.client._consume_private_frame(final)
            harness.client.confirm_terminal_reconciliation()
            assert not harness.client.terminal_reconciliation_required
            assert harness.client.execution_hold_reason is None
            if expected:
                harness.client._consume_private_frame(final)
            for _ in range(20):
                await asyncio.sleep(0)
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == bool(
                expected,
            )
            assert len(store.intents()) == bool(expected)
            assert len(harness.fake.sent) == sent  # Recovery never resends submit/cancel.
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["unacked_ioc", "reduce_ioc", "cancel_maker"])
@pytest.mark.parametrize("fault", ["type", "order_price", "before_order", "after_order", "missing"])
def test_silent_full_terminal_requires_complete_raw_trade_terms(
    tmp_path: Path, kind: str, fault: str,
) -> None:
    async def scenario() -> None:
        maker = kind == "cancel_maker"
        async with _terminal_recovery_case(
            tmp_path, partial=False, maker=maker, request_cancel=maker,
            accepted=maker, reduce_only=kind == "reduce_ioc", silent=True,
        ) as (harness, engine, store, order, final):
            trade = cast(list[object], final[2])
            row = cast(list[object], harness.rest.history[0])
            if fault == "type":
                trade[6] = "IOC" if maker else "LIMIT"
            elif fault == "order_price":
                trade[7] = Decimal("3800")
            elif fault == "before_order":
                trade[2] = cast(int, row[4]) - 1
            elif fault == "after_order":
                trade[2] = cast(int, row[5]) + 1
            else:
                harness.rest.trades = []
            with pytest.raises(BitfinexV1ExecutionError, match="trade|quantity evidence"):
                await harness.client.generate_mass_status(15)
            assert not await engine.reconcile_execution_state(timeout_secs=1)
            assert order.filled_qty.as_decimal() == 0 and not store.intents()
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None and live.reconciled_terminal is None
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("history", ["missing", "active"])
def test_silent_terminal_needs_a_terminal_not_empty_or_active_history(
    tmp_path: Path, maker: bool, history: str,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=False, zero=True, maker=maker, request_cancel=maker,
            silent=True,
        ) as (harness, engine, store, order, _):
            row = cast(list[object], harness.rest.history[0])
            harness.rest.history = []
            if history == "active":
                row[13] = "ACTIVE"
                harness.rest.active = [row]
            if history == "missing":
                assert not await engine.reconcile_execution_state(timeout_secs=1)
            else:
                assert await engine.reconcile_execution_state(timeout_secs=1)
            harness.client.confirm_terminal_reconciliation()
            assert harness.client.terminal_reconciliation_required
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None and live.reconciled_terminal is None
            assert not order.is_closed and not store.intents()
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["production", "cold", "working_maker", "unknown_modify"])
def test_silent_deadline_does_not_expand_outside_current_paper_actions(
    tmp_path: Path, boundary: str,
) -> None:
    async def scenario() -> None:
        maker = boundary in {"working_maker", "unknown_modify"}
        async with _terminal_recovery_case(
            tmp_path, partial=False, zero=True, maker=maker, paper=boundary != "production",
            request_cancel=boundary == "unknown_modify", silent=True,
        ) as (harness, _, _, order, _final):
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None
            if boundary == "cold":
                live.submitted_in_process = False
            elif boundary == "unknown_modify":
                live.unknown_operations.add("modify")
                live.pending_modify_price = Decimal("3900")
            assert not harness.client.terminal_reconciliation_required
            await harness.client.generate_mass_status(15)
            # W4d explicitly allows an exact full mass to recover a working Maker;
            # its order age still does not manufacture a silent mutation deadline.
            assert (live.reconciled_terminal is not None) == (boundary == "working_maker")
            if boundary == "unknown_modify":
                assert "modify" in live.unknown_operations
    asyncio.run(scenario())


@pytest.mark.parametrize("complete_trade", [False, True])
def test_paper_ioc_terminal_deadline_survives_ack_during_send(complete_trade: bool) -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
            mutation_ack_timeout_ms=100,
        )
        await harness.connect()
        try:
            order = harness.order(tif=TimeInForce.IOC, post_only=False, quantity="2")

            def accept_during_send(_payload: object) -> None:
                live = harness.client._live_for_client(order.client_order_id)
                assert live is not None
                harness.client._consume_private_frame(harness.order_frame("on", live.cid, order))

            harness.fake.after_send = accept_during_send
            cid = await harness.submit(order)
            live = harness.client._by_cid[cid]
            assert live.accepted and (cid, "submit") not in harness.client._ack_deadlines
            deadline = live.terminal_reconciliation_deadline
            assert deadline is not None and not harness.client.terminal_reconciliation_required
            if complete_trade:
                harness.client._consume_private_frame(
                    harness.trade_frame(cid, order, quantity="2", maker=-1),
                )
            await asyncio.sleep(0.11)
            assert harness.client.terminal_reconciliation_required == (not complete_trade)
            assert (harness.client.execution_hold_reason is None) == complete_trade
            assert live.terminal_reconciliation_deadline == deadline
            assert len(harness.fake.sent) == 2
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("partial", [False, True])
def test_silent_maker_terminal_does_not_inherit_stream_post_only_omission(
    tmp_path: Path, partial: bool,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=partial, maker=True, request_cancel=True, silent=True, rest_flags=0,
        ) as (harness, engine, store, order, _):
            with pytest.raises(BitfinexV1ExecutionError, match="post_only"):
                await harness.client.generate_mass_status(15)
            assert not await engine.reconcile_execution_state(timeout_secs=1)
            assert order.filled_qty.as_decimal() == 0 and not store.intents()
            assert harness.client.terminal_reconciliation_required
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True])
def test_definitive_cancel_rejection_ends_only_its_maker_terminal_wait(maker: bool) -> None:
    async def scenario() -> None:
        harness = _Harness(
            raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
            mutation_ack_timeout_ms=100,
        )
        await harness.connect()
        try:
            order = harness.order(
                tif=TimeInForce.GTC if maker else TimeInForce.IOC, post_only=maker,
            )
            cid = await harness.submit(order)
            harness.client._consume_private_frame(harness.order_frame("on", cid, order))
            await harness.cancel(order)
            live = harness.client._by_cid[cid]
            first_deadline = live.terminal_reconciliation_deadline
            assert first_deadline is not None
            harness.client._consume_private_frame(
                _notification("oc-req", cid=cid, venue_order_id=VENUE_ORDER_ID),
            )
            assert not live.pending_cancel and not live.unknown_operations
            await asyncio.sleep(0.11)
            if maker:
                assert not bool(harness.client.terminal_reconciliation_required)
                await harness.cancel(order)  # A new explicit command, never an automatic resend.
                assert not bool(harness.client.terminal_reconciliation_required)
                assert live.terminal_reconciliation_deadline is not None
                assert live.terminal_reconciliation_deadline > first_deadline
                assert (cid, "cancel") in harness.client._ack_deadlines
                await asyncio.sleep(0.11)
                assert harness.client.terminal_reconciliation_required
                assert len(harness.fake.sent) == 4
            else:
                assert live.terminal_reconciliation_deadline == first_deadline
                assert harness.client.terminal_reconciliation_required
                assert len(harness.fake.sent) == 3
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_zero_terminal_confirmation_does_not_ignore_nonzero_average(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=False, zero=True, silent=True,
        ) as (harness, engine, _, order, _):
            assert await engine.reconcile_execution_state(timeout_secs=1)
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None and live.reconciled_terminal is not None
            live.reconciled_terminal.avg_px = Decimal("3926.75")
            with pytest.raises(BitfinexV1ExecutionError, match="cache does not prove"):
                harness.client.confirm_terminal_reconciliation()
            assert harness.client.terminal_reconciliation_required
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", [None, "cold", "accepted", "venue_binding", "cid_binding"])
def test_silent_rejected_ioc_confirms_native_missing_venue_id_only_from_exact_owned_rest(
    tmp_path: Path, fault: str | None,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=False, zero=True, silent=True, accepted=False,
        ) as (harness, engine, store, order, _):
            cast(list[object], harness.rest.history[0])[13] = "INSUFFICIENT MARGIN"
            assert await engine.reconcile_execution_state(timeout_secs=1)
            assert order.status == OrderStatus.REJECTED and order.venue_order_id is None
            assert order.filled_qty.as_decimal() == 0 and not store.intents()
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None and live.reconciled_terminal is not None
            assert not live.accepted
            if fault == "cold":
                live.submitted_in_process = False
            elif fault == "accepted":
                live.accepted = True
            elif fault == "venue_binding":
                harness.client._cid_by_venue[VENUE_ORDER_ID] = live.cid + 1
            elif fault == "cid_binding":
                harness.client._cid_store._by_cid.pop(live.cid)
            if fault is None:
                harness.client.confirm_terminal_reconciliation()
                assert not harness.client.terminal_reconciliation_required
                assert harness.client.execution_hold_reason is None
                assert order.venue_order_id is None  # Do not invent a native acceptance/ID.
                assert not any(isinstance(event, OrderAccepted | OrderFilled)
                               for event in order.events)
                assert len(harness.fake.sent) == 2
            else:
                with pytest.raises(BitfinexV1ExecutionError, match="cache does not prove"):
                    harness.client.confirm_terminal_reconciliation()
                assert harness.client.terminal_reconciliation_required
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal, fault", [
    (terminal, fault)
    for terminal in ("rejected", "partial", "full")
    for fault in (None, "missing_report", "quantity", "flags", "missing_trade", "trade_id")
    if terminal != "rejected" or fault not in {"missing_trade", "trade_id"}
])
def test_silent_terminal_retry_revalidates_closed_native_order_before_adapter_retirement(
    tmp_path: Path, terminal: str, fault: str | None,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=terminal == "partial", zero=terminal == "rejected",
            silent=True, accepted=False,
        ) as (harness, engine, store, order, _):
            row = cast(list[object], harness.rest.history[0])
            if terminal == "rejected":
                row[13] = "INSUFFICIENT MARGIN"
            assert await engine.reconcile_execution_state(timeout_secs=1)
            assert order.is_closed
            live = harness.client._live_for_client(order.client_order_id)
            assert live is not None and live.reconciled_terminal is not None
            original_report = live.reconciled_terminal
            original_events = list(order.events)
            original_intents = store.intents()
            if fault == "missing_report":
                harness.rest.history = []
            elif fault == "quantity":
                row[6:8] = [cast(Decimal, row[6]) + 1, cast(Decimal, row[7]) + 1]
            elif fault == "flags":
                row[12] = REDUCE_ONLY_FLAG
            elif fault == "missing_trade":
                harness.rest.trades = []
            elif fault == "trade_id":
                trade = cast(list[object], harness.rest.trades[0])
                trade[0] = cast(int, trade[0]) + 1
            if fault is None:
                assert await engine.reconcile_execution_state(timeout_secs=1)
                harness.client.confirm_terminal_reconciliation()
                assert not harness.client.terminal_reconciliation_required
                assert harness.client.execution_hold_reason is None
            else:
                try:
                    result = await harness.client.generate_mass_status(15)
                except BitfinexV1ExecutionError:
                    result = None
                assert result is None  # The native parent returns None on report conversion errors.
                assert not await engine.reconcile_execution_state(timeout_secs=1)
                assert live.reconciled_terminal is original_report
                assert harness.client.terminal_reconciliation_required
                assert harness.client.execution_hold_reason is not None
            assert list(order.events) == original_events
            assert store.intents() == original_intents
    asyncio.run(scenario())


@pytest.mark.parametrize("stream_flags, rest_flags", [
    (0, POST_ONLY_FLAG), (0, 0), (POST_ONLY_FLAG, POST_ONLY_FLAG), (POST_ONLY_FLAG, 0),
])
def test_partial_maker_terminal_normalizes_only_its_authenticated_opaque_post_only_bit(
    tmp_path: Path, stream_flags: int, rest_flags: int,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=True, maker=True, request_cancel=True,
            terminal_flags=stream_flags, rest_flags=rest_flags,
        ) as case:
            harness, engine, store, order, _ = case
            allowed = stream_flags == 0 or rest_flags == POST_ONLY_FLAG
            assert await engine.reconcile_execution_state(timeout_secs=1.0) == allowed
            if allowed:
                assert order.status == OrderStatus.CANCELED
                assert order.filled_qty.as_decimal() == Decimal(2)
                assert len(store.intents()) == 1
                cid = harness.client._cid_by_client[order.client_order_id]
                terminal = harness.client._by_cid[cid].terminal
                assert terminal is not None and terminal.flags == stream_flags
                assert cast(list[object], harness.rest.history[0])[12] == rest_flags
                harness.client.confirm_terminal_reconciliation()
                assert harness.client.execution_hold_reason is None
            else:
                assert order.filled_qty.as_decimal() == 0
                assert not order.is_closed
                assert not store.intents()
                assert harness.client.terminal_reconciliation_required

    asyncio.run(scenario())


@pytest.mark.parametrize("paper, request_cancel", [(False, True), (True, False)])
def test_partial_maker_terminal_rejects_post_only_omission_without_prior_authority(
    tmp_path: Path, paper: bool, request_cancel: bool,
) -> None:
    async def scenario() -> None:
        with pytest.raises(BitfinexV1ExecutionError, match="differs from local submission"):
            async with _terminal_recovery_case(
                tmp_path, partial=True, maker=True, paper=paper, request_cancel=request_cancel,
                terminal_flags=0, rest_flags=POST_ONLY_FLAG,
            ):
                raise AssertionError("unauthorized omission reached reconciliation")

    asyncio.run(scenario())


@pytest.mark.parametrize("field, changed", [
    (12, REDUCE_ONLY_FLAG), (17, Decimal("3926.85")), (16, Decimal("3926.85")),
])
def test_authenticated_opaque_post_only_does_not_relax_other_terminal_facts(
    tmp_path: Path, field: int, changed: object,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(
            tmp_path, partial=True, maker=True, request_cancel=True,
            terminal_flags=0, rest_flags=POST_ONLY_FLAG,
        ) as case:
            harness, engine, store, order, _ = case
            cast(list[object], harness.rest.history[0])[field] = changed
            assert not await engine.reconcile_execution_state(timeout_secs=1.0)
            assert order.filled_qty.as_decimal() == 0
            assert not order.is_closed and not store.intents()
            assert harness.client.terminal_reconciliation_required

    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("fault", [
    "missing", "quantity", "side", "price", "order_price", "trade_venue", "trade_time",
    "report_side", "report_quantity", "report_average", "report_price",
])
def test_partial_cancel_rejects_incomplete_or_conflicting_trades_before_native_close(
    tmp_path: Path, maker: bool, fault: str,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(tmp_path, partial=True, maker=maker) as case:
            harness, engine, store, order, final = case
            row = cast(list[object], final[2])
            if fault == "missing":
                harness.rest.trades = []
            elif fault == "quantity":
                row[4] = Decimal(1)
            elif fault == "side":
                row[4] = Decimal(-2)
            elif fault == "price":
                row[5] = Decimal("3926.85")
            elif fault == "order_price":
                row[7] = Decimal("3926.85")
            elif fault == "trade_venue":
                row[3] = VENUE_ORDER_ID + 1
            elif fault == "trade_time":
                row[2] = cast(int, row[2]) + 1
            else:
                terminal = cast(list[object], harness.rest.history[0]).copy()
                if fault == "report_side":
                    terminal[6:8] = [Decimal(-2), Decimal(-4)]
                elif fault == "report_quantity":
                    terminal[6:8] = [Decimal(3), Decimal(5)]
                elif fault == "report_average":
                    terminal[17] = Decimal("3926.85")
                elif fault == "report_price":
                    terminal[16] = Decimal("3926.85")
                harness.rest.history = [terminal]
            assert not await engine.reconcile_execution_state(timeout_secs=1.0)
            assert order.status == OrderStatus.ACCEPTED
            assert order.filled_qty.as_decimal() == 0
            assert not store.intents()
            assert harness.client.terminal_reconciliation_required

    asyncio.run(scenario())


@pytest.mark.parametrize("partial, maker", [(False, False), (True, False), (True, True)])
def test_terminal_reconciliation_releases_exact_cache_without_republishing_late_trades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: bool, maker: bool,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(tmp_path, partial=partial, maker=maker) as case:
            harness, engine, store, order, final = case
            if not partial:
                harness.rest.trades = []  # Preserve the existing full inferred rule.
            assert await engine.reconcile_execution_state(timeout_secs=1.0)
            assert order.status == (OrderStatus.CANCELED if partial else OrderStatus.FILLED)
            assert order.filled_qty.as_decimal() == Decimal(2)
            assert len(store.intents()) == 1

            def no_republication(*args: object, **kwargs: object) -> None:
                raise AssertionError("already applied native fill must never be republished")

            monkeypatch.setattr(harness.client, "generate_order_filled", no_republication)
            # Hit the window before the runner gets to explicit adapter confirmation.
            harness.client._consume_private_frame(final)
            harness.client._consume_private_frame(final)
            interim = cast(list[object], final[2]).copy()
            interim[9:11] = [None, None]
            harness.client._consume_private_frame([0, "te", interim])
            harness.client.confirm_terminal_reconciliation()
            assert not harness.client.terminal_reconciliation_required
            assert harness.client.execution_hold_reason is None
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
            assert len(JsonStateStore(tmp_path / "hedges.json").intents()) == 1
            assert harness.client.fee_summary(order.client_order_id).complete
            assert not store.can_submit_source()  # Adapter readiness cannot erase the hedge.
            changed = cast(list[object], final[2]).copy()
            changed[5] = Decimal("3926.85")
            with pytest.raises(BitfinexV1ExecutionError, match="trade ID changed"):
                harness.client._consume_private_frame([0, "tu", changed])

    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("first_fill_streamed", [False, True])
def test_partial_cancel_retry_applies_complete_trade_set_once_before_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, first_fill_streamed: bool,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(tmp_path, partial=True, maker=maker) as case:
            harness, engine, store, order, final = case
            first = cast(list[object], final[2]).copy()
            second = first.copy()
            first[4:6] = [Decimal(1), Decimal("3926.70")]
            second[0] = 1235
            second[4:6] = [Decimal(1), Decimal("3926.80")]
            if first_fill_streamed:
                harness.client._consume_private_frame([0, "tu", first])
                for _ in range(20):
                    await asyncio.sleep(0)
            harness.rest.trades = []
            assert not await engine.reconcile_execution_state(timeout_secs=1.0)
            assert order.filled_qty.as_decimal() == int(first_fill_streamed)
            assert not order.is_closed
            harness.rest.trades = [first, first, second]  # Preserve requested REST sort order.
            assert await engine.reconcile_execution_state(timeout_secs=1.0)
            assert order.status == OrderStatus.CANCELED
            assert order.filled_qty.as_decimal() == Decimal(2)
            events = [event for event in order.events
                      if isinstance(event, OrderFilled | OrderCanceled)]
            assert [type(event) for event in events] == [OrderFilled, OrderFilled, OrderCanceled]
            assert len(store.intents()) == 2

            def no_republication(*args: object, **kwargs: object) -> None:
                raise AssertionError("late duplicate must not publish a native fill")

            monkeypatch.setattr(harness.client, "generate_order_filled", no_republication)
            for row in (first, second):
                harness.client._consume_private_frame([0, "tu", row])
            harness.client.confirm_terminal_reconciliation()
            assert harness.client.execution_hold_reason is None
            assert not store.can_submit_source()
            assert len(JsonStateStore(tmp_path / "hedges.json").intents()) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", [None, "excess_quantity", "wrong_price", "future_trade"])
def test_inferred_terminal_covers_late_real_trade_set_without_publishing_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    async def scenario() -> None:
        async with _terminal_recovery_case(tmp_path, partial=False) as case:
            harness, engine, store, order, final = case
            harness.rest.trades = []
            assert await engine.reconcile_execution_state(timeout_secs=1.0)

            def no_republication(*args: object, **kwargs: object) -> None:
                raise AssertionError("inferred fill must cover real trades without republishing")

            monkeypatch.setattr(harness.client, "generate_order_filled", no_republication)
            first = cast(list[object], final[2]).copy()
            first[4] = Decimal(1)
            harness.client._consume_private_frame([0, "tu", first])
            assert not harness.client.fee_summary(order.client_order_id).complete
            second = first.copy()
            second[0] = 1235
            if fault == "excess_quantity":
                second[4] = Decimal(2)
            elif fault == "wrong_price":
                second[5] = Decimal("3926.85")
            elif fault == "future_trade":
                second[2] = harness.clock.timestamp_ns() // 1_000_000 + 1000
            if fault is None:
                harness.client._consume_private_frame([0, "tu", second])
                harness.client.confirm_terminal_reconciliation()
                assert harness.client.fee_summary(order.client_order_id).complete
                assert harness.client.execution_hold_reason is None
            else:
                with pytest.raises(BitfinexV1ExecutionError, match="conflicts with native fills"):
                    harness.client._consume_private_frame([0, "tu", second])
                assert harness.client.terminal_reconciliation_required
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
            assert len(store.intents()) == 1

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
        ({"fee_currency": "USTF0"}, "fee currency must be USD"),
        ({"raw_symbol": PAPER_RAW_SYMBOL}, "TESTUSDTF0"),
        ({"wallet_currency": "TESTUSDTF0"}, "USTF0"),
        ({"user_id": 0}, "user ID"),
        ({"mutation_ack_timeout_ms": 99}, "acknowledgment timeout"),
        ({"allow_cold_position_reconciliation": 1}, "exact bool"),
    ],
)
def test_config_locks_exact_account_instrument_symbol_wallet_and_fee_currency(
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
