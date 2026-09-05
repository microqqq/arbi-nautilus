"""Ordinary live builders/strategies/Engine; only venue IO is synthetic.

Bitfinex uses its actual execution adapter with fake authenticated WS/REST.
MT5 submission/report IO is injected here; its wire/EA contract is tested separately.
No canary strategy, direct state confirmation, or replacement strategy logic is used.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from msgspec.structs import replace as replace_config
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.reports import ExecutionMassStatus, PositionStatusReport
from nautilus_trader.model.enums import LiquiditySide, OrderSide, OrderStatus, PositionSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    ClientId,
    ClientOrderId,
    PositionId,
    TradeId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from test_bitfinex_v1_execution import _FakeRest, _FakeTransport, _Harness, _position_row
from test_live_maker import _build as _build_maker
from test_live_maker import _configs as _maker_configs
from test_live_taker import _build, _configs
from test_mt5_v1_execution import _identity, _snapshot

import py000_nautilus.live_runtime as runtime_module
from py000_nautilus.app import _book_snapshot, _quote
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, instrument_from_config
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.models import ObligationStatus, SourceDirection
from py000_nautilus.mt5_v1_data import instrument_from_snapshot
from py000_nautilus.store import JsonStateStore

D = Decimal


async def _pump() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


async def _wait_until(ready: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not ready():
            await asyncio.sleep(0.005)
    await _pump()


class _OrdinaryStrategy:
    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, maker: bool, paper: bool = False,
        source_quantity: int = 2,
    ) -> None:
        self.maker = maker
        self.source_quantity = D(source_quantity)
        if maker:
            configs: Any = _maker_configs(tmp_path)
            configs = replace(configs, strategy=replace_config(
                configs.strategy, economics=replace_config(
                    configs.strategy.economics,
                    bid=replace_config(
                        configs.strategy.economics.bid,
                        open_quantity_ounces=self.source_quantity,
                    ),
                    # Isolate one direction using normal configuration, not strategy overrides.
                    ask=replace_config(configs.strategy.economics.ask, open_quantity_ounces=D(0)),
                ),
            ))
        else:
            configs = _configs(tmp_path)
            configs = replace(configs, strategy=replace_config(
                configs.strategy, economics=replace_config(
                    configs.strategy.economics, base_book_quantity=self.source_quantity,
                    open_quantity_long=self.source_quantity,
                    open_quantity_short=self.source_quantity,
                ),
            ))
        if paper:
            configs = replace(
                configs,
                bitfinex_data=replace_config(configs.bitfinex_data, raw_symbol=PAPER_RAW_SYMBOL),
                bitfinex_exec=replace_config(
                    configs.bitfinex_exec, raw_symbol=PAPER_RAW_SYMBOL,
                    wallet_currency="TESTUSDTF0", mutation_ack_timeout_ms=100,
                ),
            )
        build = _build_maker if maker else _build
        self.node, self.strategy = build(configs, loop=asyncio.get_running_loop())
        self.store = (
            self.strategy._stores[SourceDirection.LONG] if maker else self.strategy.state_store
        )
        self.wallet_currency = configs.bitfinex_exec.wallet_currency
        self.source = self.node.kernel.exec_engine._clients[ClientId("BITFINEX")]
        self.hedge = self.node.kernel.exec_engine._clients[ClientId("MT5")]
        self.source_data = self.node.kernel.data_engine.routing_map[Venue("BITFINEX")]
        self.hedge_data = self.node.kernel.data_engine.routing_map[Venue("MT5")]
        self.transport = _FakeTransport()
        self.rest = _FakeRest(configs.bitfinex_exec.raw_symbol)
        self.source._transport = self.transport
        self.source._rest = self.rest
        self.source_instrument = instrument_from_config(configs.bitfinex_data, ts_init=0)
        snapshot = _snapshot(_identity())
        cast(dict[str, Any], snapshot["time"])["observed_utc_ms"] = str(
            self.node.kernel.clock.timestamp_ns() // 1_000_000,
        )
        self.hedge_instrument = instrument_from_snapshot(
            snapshot, configs.strategy.hedge_instrument_id, ts_init=0,
        )
        for instrument, client in (
            (self.source_instrument, self.source), (self.hedge_instrument, self.hedge),
        ):
            self.node.cache.add_instrument(instrument)
            client._instrument_provider.add(instrument)
        self.node.cache.add_account(TestExecStubs.margin_account(account_id=self.hedge.account_id))
        self.hedge._identity = _identity()
        self.hedge._snapshot = snapshot
        self.hedge._execution_hold_reason = None
        self.hedge._set_connected(True)
        self.hedge_data._snapshot_refresh_healthy = True
        self.hedge_data._set_connected(True)
        self.source_data._set_connected(True)
        self.source_data._book.apply_snapshot(
            [[D("3926.6") - D(i) / 10, 1, D(10)] for i in range(25)]
            + [[D("3926.7") + D(i) / 10, 1, D(-10)] for i in range(25)],
        )
        self.hedge_orders: list[Any] = []
        self.hedge_positions: list[PositionStatusReport] = []
        self.release_hedge = asyncio.Event()
        self.late_trade: list[object] | None = None

        async def subscription(_command: Any) -> None:
            return None  # A deterministic data feed below replaces public venue IO.

        for client in (self.source_data, self.hedge_data):
            for name in (
                "_subscribe_instrument", "_subscribe_quote_ticks", "_subscribe_funding_rates",
                "_subscribe_instrument_status", "_subscribe_order_book_deltas",
            ):
                monkeypatch.setattr(client, name, subscription)
        monkeypatch.setattr(self.hedge, "_submit_order", self._submit_hedge)
        monkeypatch.setattr(self.hedge, "generate_mass_status", self._hedge_mass)
        # Use the existing authentic wire vector builders, with this node's instrument/order.
        self.wire = object.__new__(_Harness)
        self.wire.raw_symbol = configs.bitfinex_exec.raw_symbol
        self.wire.fee_currency = "USD"

    async def start(self) -> None:
        self.node.kernel.data_engine.start()
        self.node.kernel.risk_engine.start()
        self.node.kernel.exec_engine.start()
        await self.transport.queue.put(
            {"event": "auth", "status": "OK", "chanId": 0, "userId": 269_312},
        )
        await self.transport.queue.put(
            [0, "ws", [["margin", self.wallet_currency, D(10000), D(0), D(10000)]]],
        )
        await self.source._connect()
        self.source._set_connected(True)
        await _pump()
        self.node.trader.start()
        await _pump()
        assert self.strategy.is_running

    async def close(self) -> None:
        if self.node.trader.is_running:
            self.node.trader.stop()
        await self.source._disconnect()
        await self.source.cancel_pending_tasks(timeout_secs=1)
        await self.hedge.cancel_pending_tasks(timeout_secs=1)
        self.node.kernel.exec_engine.stop()
        self.node.kernel.risk_engine.stop()
        self.node.kernel.data_engine.stop()
        await _pump()
        # asyncio.run owns this test's loop; node.dispose would stop it mid-cleanup.
        self.node.kernel.dispose()
        if self.node.kernel.executor is not None:
            self.node.kernel.executor.shutdown(wait=True, cancel_futures=True)

    async def _hedge_mass(self, _lookback: int | None = None) -> ExecutionMassStatus:
        report = ExecutionMassStatus(
            client_id=self.hedge.id, account_id=self.hedge.account_id, venue=Venue("MT5"),
            report_id=UUID4(), ts_init=self.node.kernel.clock.timestamp_ns(),
        )
        report.add_position_reports(self.hedge_positions.copy())
        return report

    async def _submit_hedge(self, command: SubmitOrder) -> None:
        order = command.order
        self.hedge_orders.append(order)
        account = self.hedge.account_id
        venue_id = VenueOrderId(f"H-{len(self.hedge_orders)}")
        self.hedge.generate_order_submitted(
            order.strategy_id, order.instrument_id, order.client_order_id,
            self.node.kernel.clock.timestamp_ns(),
        )
        self.hedge.generate_order_accepted(
            order.strategy_id, order.instrument_id, order.client_order_id, venue_id,
            self.node.kernel.clock.timestamp_ns(),
        )
        await self.release_hedge.wait()
        now = self.node.kernel.clock.timestamp_ns()
        position_id = PositionId(f"HP-{len(self.hedge_orders)}")
        self.hedge_positions.append(PositionStatusReport(
            account_id=account, instrument_id=order.instrument_id,
            venue_position_id=position_id,
            position_side=PositionSide.SHORT if order.side == OrderSide.SELL else PositionSide.LONG,
            quantity=order.quantity, avg_px_open=D("3936.7"),
            report_id=UUID4(), ts_last=now, ts_init=now,
        ))
        self.hedge.generate_order_filled(
            order.strategy_id, order.instrument_id, order.client_order_id, venue_id,
            position_id, TradeId(f"HT-{len(self.hedge_orders)}"), order.side, order.order_type,
            order.quantity, self.hedge_instrument.make_price(D("3936.7")),
            self.hedge_instrument.quote_currency, Money(0, self.hedge_instrument.quote_currency),
            LiquiditySide.NO_LIQUIDITY_SIDE, now,
        )

    async def seed_source(self, *, acknowledge: bool = True) -> tuple[Any, int]:
        await self.opportunity()
        orders = self.node.cache.orders(instrument_id=self.source_instrument.id)
        assert len(orders) == 1
        order = orders[0]
        assert order.quantity.as_decimal() == self.source_quantity
        payload = cast(dict[str, Any], cast(list[Any], self.transport.sent[-1])[3])
        cid = cast(int, payload["cid"])
        if acknowledge:
            self.source._consume_private_frame(self.wire.order_frame("on", cid, order))
            await _pump()
        assert order.status == (OrderStatus.ACCEPTED if acknowledge else OrderStatus.SUBMITTED)
        return order, cid

    async def finish_source(
        self, order: Any, cid: int, *, filled: int = 0, deliver_terminal: bool = True,
    ) -> None:
        now_ms = self.node.kernel.clock.timestamp_ns() // 1_000_000
        price = order.price.as_decimal()
        frame = self.wire.order_frame(
            "oc", cid, order, remaining=str(D(str(order.quantity)) - filled),
            status=(
                f"EXECUTED @ {price}({filled})" if filled == order.quantity.as_decimal()
                else "CANCELED" if self.maker else "IOC CANCELED"
            ),
        )
        row = cast(list[Any], frame[2])
        row[4:6] = [now_ms - 100, now_ms]
        row[17] = price if filled else D(0)
        self.rest.history = [row]
        if filled:
            trade = self.wire.trade_frame(
                cid, order, quantity=str(filled), price=str(price), maker=1 if self.maker else -1,
            )
            cast(list[Any], trade[2])[2] = now_ms
            self.rest.trades = [trade[2]]
            self.rest.position_rows = [
                _position_row(D(filled), avg_px=price, raw_symbol=self.wire.raw_symbol),
            ]
            self.late_trade = trade
        if deliver_terminal:
            self.source._consume_private_frame(frame)
        await _pump()

    async def cancel_for_changed_costs(self) -> None:
        assert self.maker
        economics = self.strategy._config.economics
        assert self.strategy.update_cost_snapshot(
            replace_config(economics.carry, bitfinex_long=D("0.000001")),
            economics.fx, self.node.kernel.clock.timestamp_ns(),
        )
        await _pump()
        assert len(self.sent_operations("oc")) == 1

    async def opportunity(self) -> None:
        now = self.node.kernel.clock.timestamp_ns()
        strategy = self.strategy
        assert strategy.update_cost_snapshot(
            strategy._config.economics.carry, strategy._config.economics.fx, now,
        )
        strategy.update_hedge_session(True, now)
        self.node.kernel.data_engine.process(_quote(
            self.hedge_instrument, "3936.7", "3936.8", "10", now,
        ))
        self.node.kernel.data_engine.process(_book_snapshot(
            self.source_instrument, "3926.6", "3926.7", "10", now,
        ))
        if self.maker:
            self.node.kernel.data_engine.process(_quote(
                self.source_instrument, "3926.6", "3926.7", "10", now,
            ))
        await _pump()

    def sent_operations(self, operation: str) -> list[dict[str, Any]]:
        return [cast(dict[str, Any], row[3]) for row in self.transport.sent
                if isinstance(row, list) and len(row) == 4 and row[1] == operation]

    def assert_second_source_sent(self, first_order_id: str) -> None:
        records = self.store.source_orders()
        assert len(records) == 2 and records[1].client_order_id != first_order_id
        order = self.node.cache.order(ClientOrderId(records[1].client_order_id))
        assert order is not None and order.status == OrderStatus.SUBMITTED
        assert order.side == OrderSide.BUY and order.quantity.as_decimal() == self.source_quantity
        assert order.account_id == self.source.account_id
        binding = self.source._cid_store.binding_for_client(records[1].client_order_id)
        assert binding is not None
        sent = self.sent_operations("on")
        assert len(sent) == 2 and sent[1]["cid"] == binding.cid
        assert D(str(sent[1]["amount"])) == self.source_quantity
        assert sent[1]["symbol"] == self.wire.raw_symbol


@asynccontextmanager
async def _ordinary_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, maker: bool, paper: bool = False,
    source_quantity: int = 2,
) -> AsyncIterator[_OrdinaryStrategy]:
    harness = _OrdinaryStrategy(
        tmp_path, monkeypatch, maker=maker, paper=paper, source_quantity=source_quantity,
    )
    try:
        await harness.start()
        yield harness
    finally:
        await harness.close()


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_zero_fill_cancel_allows_next_real_opportunity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker) as harness:
            order, cid = await harness.seed_source()
            await harness.finish_source(order, cid)
            await _pump()
            assert order.status == OrderStatus.CANCELED
            assert harness.store.can_submit_source()
            await harness.opportunity()
            harness.assert_second_source_sent(order.client_order_id.value)
            assert len(harness.hedge_orders) == 0
    asyncio.run(run())


@pytest.mark.parametrize("public_data_lost", [False, True])
@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_partial_cancel_waits_for_real_hedge_then_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public_data_lost: bool, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker) as harness:
            order, cid = await harness.seed_source()
            if public_data_lost:
                harness.source_data._set_connected(False)
            await harness.finish_source(order, cid, filled=1)
            # The normal Actor must discover the withheld fill; no test-only reconciliation call.
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: order.status == OrderStatus.CANCELED and not owner.busy)
            assert order.status == OrderStatus.CANCELED
            assert order.filled_qty.as_decimal() == D(1)
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
            assert len(harness.hedge_orders) == 1
            assert not harness.store.can_submit_source()
            await harness.opportunity()
            assert len(harness.store.source_orders()) == 1
            harness.release_hedge.set()
            await _pump()
            state = JsonStateStore(harness.store.path)
            assert len(state.intents()) == 1
            assert state.intents()[0].status is ObligationStatus.COMPLETED
            assert state.net_unhedged_ounces == 0
            positions = harness.node.cache.positions_open(instrument_id=harness.hedge_instrument.id)
            assert len(positions) == 1 and positions[0].account_id == harness.hedge.account_id
            assert D(str(positions[0].avg_px_open)) == D("3936.7")
            assert state.can_submit_source(), (
                state.halt_reason, state.source_freeze_reason, state.active_source_order_id,
                get_source_terminal_reconciler(harness.node).last_failure,
            )
            assert harness.late_trade is not None
            harness.source._consume_private_frame(harness.late_trade)
            harness.source._consume_private_frame(harness.late_trade)
            await _pump()
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
            assert len(harness.store.intents()) == len(harness.hedge_orders) == 1
            if public_data_lost:
                await harness.opportunity()
                assert len(harness.store.source_orders()) == 1
                harness.source_data._set_connected(True)
            await harness.opportunity()
            harness.assert_second_source_sent(order.client_order_id.value)
            assert len(harness.hedge_orders) == 1
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_stop_cancels_query_and_preserves_unfinished_hedge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker) as harness:
            report_entered = asyncio.Event()

            async def blocked_report(_command: Any) -> None:
                report_entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(harness.source, "generate_order_status_report", blocked_report)
            order, cid = await harness.seed_source()
            await harness.finish_source(order, cid, filled=1)
            # No polling wakeup: the actual LiveClock must wake this otherwise idle loop.
            await asyncio.wait_for(report_entered.wait(), timeout=1)
            await _pump()
            owner = get_source_terminal_reconciler(harness.node)
            root = owner._root
            assert root is not None and not root.done()
            assert owner.busy and len(harness.hedge_orders) == 1
            assert harness.hedge_orders[0].filled_qty.as_decimal() == 0
        # Actual Trader.stop + client task drain above, not direct source confirmation.
        assert root.cancelled()
        assert all(task.done() for task in harness.source._tasks)
        assert all(task.done() for task in harness.hedge._tasks)
        state = JsonStateStore(harness.store.path)
        assert len(state.source_orders()) == len(state.intents()) == 1
        assert state.intents()[0].status is not ObligationStatus.COMPLETED
        assert state.net_unhedged_ounces == 1 and not state.can_submit_source()
        assert order.filled_qty.as_decimal() == 1
        assert len(harness.hedge_orders) == 1

    asyncio.run(run())


@pytest.mark.parametrize("filled", [0, 1, 2], ids=["zero", "partial", "full"])
@pytest.mark.parametrize(
    ("maker", "acknowledge"), [(False, False), (False, True), (True, True)],
    ids=["ioc-no-ack", "ioc-accepted", "maker-cancel"],
)
def test_ordinary_paper_silent_terminal_recovers_without_manual_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, acknowledge: bool, filled: int,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            order, cid = await harness.seed_source(acknowledge=acknowledge)
            if maker:
                # A real cost change causes the default Maker to send cancel.
                await harness.cancel_for_changed_costs()
            await harness.finish_source(order, cid, filled=filled, deliver_terminal=False)
            owner = get_source_terminal_reconciler(harness.node)
            expected_status = OrderStatus.FILLED if filled == 2 else OrderStatus.CANCELED
            await _wait_until(lambda: order.status == expected_status and not owner.busy)
            assert owner.last_failure is None and owner.source_submission_ready
            assert harness.source.execution_hold_reason is None
            assert order.filled_qty.as_decimal() == filled
            assert len(harness.store.source_orders()) == 1
            assert len(harness.hedge_orders) == int(filled > 0)
            if filled:
                assert harness.hedge_orders[0].quantity.as_decimal() == filled
                assert not harness.store.can_submit_source()
                harness.release_hedge.set()
                await _pump()
                state = JsonStateStore(harness.store.path)
                assert len(state.intents()) == 1
                assert state.intents()[0].status is ObligationStatus.COMPLETED
                assert state.net_unhedged_ounces == 0
                assert harness.late_trade is not None
                harness.source._consume_private_frame(harness.late_trade)
                harness.source._consume_private_frame(harness.late_trade)
                await _pump()
                assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
                assert len(harness.hedge_orders) == len(harness.store.intents()) == 1
            await harness.opportunity()
            harness.assert_second_source_sent(order.client_order_id.value)
            assert len(harness.sent_operations("oc")) == int(maker)
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["no_history", "missing_trade", "wrong_trade_time", "active"])
@pytest.mark.parametrize("maker", [False, True], ids=["ioc", "maker-cancel"])
def test_ordinary_paper_silent_terminal_missing_facts_keeps_original_order_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, fault: str,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            order, cid = await harness.seed_source()
            if maker:
                await harness.cancel_for_changed_costs()
            await harness.finish_source(order, cid, filled=1, deliver_terminal=False)
            if fault == "no_history":
                harness.rest.history = []
                harness.rest.trades = []
            elif fault == "missing_trade":
                harness.rest.trades = []
            elif fault == "wrong_trade_time":
                trade = cast(list[Any], harness.rest.trades[0])
                trade[2] = cast(list[Any], harness.rest.history[0])[4] - 1
            else:
                harness.rest.active = [harness.wire.order_frame("on", cid, order)[2]]
                harness.rest.history = []
                harness.rest.trades = []
                harness.rest.position_rows = []
            attempts = 0
            generate_mass = harness.source.generate_mass_status

            async def observe_mass(lookback_mins: int | None = None) -> Any:
                nonlocal attempts
                attempts += 1
                return await generate_mass(lookback_mins)

            monkeypatch.setattr(harness.source, "generate_mass_status", observe_mass)
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: owner.last_failure is not None and not owner.busy)
            assert attempts == 2
            assert order.filled_qty.as_decimal() == 0 and not order.is_closed
            assert not owner.source_submission_ready and not harness.store.can_submit_source()
            assert len(harness.store.source_orders()) == 1 and not harness.hedge_orders
            await harness.opportunity()
            # Native timer crosses another tick, without a test calling the recovery API.
            await asyncio.sleep(0.3)
            assert attempts == 2 and len(harness.store.source_orders()) == 1
            assert len(harness.sent_operations("on")) == 1
            assert len(harness.sent_operations("oc")) == int(maker)
    asyncio.run(run())


def test_ordinary_paper_silent_zero_rejection_allows_a_new_opportunity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=False, paper=True) as harness:
            order, cid = await harness.seed_source(acknowledge=False)
            await harness.finish_source(order, cid, deliver_terminal=False)
            cast(list[Any], harness.rest.history[0])[13] = "INSUFFICIENT MARGIN"
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: order.status == OrderStatus.REJECTED and not owner.busy)
            assert owner.last_failure is None and owner.source_submission_ready
            # Native OrderRejected has no venue ID; do not synthesize acceptance to add one.
            assert order.venue_order_id is None and order.filled_qty.as_decimal() == 0
            assert harness.source.execution_hold_reason is None
            assert harness.store.can_submit_source() and not harness.hedge_orders
            binding = harness.source._cid_store.binding_for_client(order.client_order_id.value)
            assert binding is not None and binding.cid == cid
            await harness.opportunity()
            harness.assert_second_source_sent(order.client_order_id.value)
            assert not harness.sent_operations("oc")
    asyncio.run(run())


@pytest.mark.parametrize(
    ("terminal", "fault"),
    [(terminal, fault) for terminal in ("rejected", "partial")
     for fault in (None, "quantity", "flags", "missing")] + [("full", None)],
)
def test_ordinary_retry_checks_current_source_facts_after_other_venue_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str, fault: str | None,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=False, paper=True) as harness:
            order, cid = await harness.seed_source(acknowledge=terminal != "rejected")
            filled = {"rejected": 0, "partial": 1, "full": 2}[terminal]
            expected_status = {
                "rejected": OrderStatus.REJECTED, "partial": OrderStatus.CANCELED,
                "full": OrderStatus.FILLED,
            }[terminal]
            await harness.finish_source(order, cid, filled=filled, deliver_terminal=False)
            if terminal == "rejected":
                cast(list[Any], harness.rest.history[0])[13] = "INSUFFICIENT MARGIN"
            harness.release_hedge.set()
            attempts = 0
            history_calls = 0
            original_mass = harness.hedge.generate_mass_status
            original_history = harness.rest.order_history_by_symbol

            async def temporarily_missing_hedge_report(lookback: int | None = None) -> Any:
                nonlocal attempts
                attempts += 1
                # Native reconciles BFX even though this other client's first report is absent.
                return None if attempts == 1 else await original_mass(lookback)

            async def current_source_history(
                symbol: str, *, start: int | None = None, end: int | None = None, limit: int = 2500,
            ) -> object:
                nonlocal history_calls
                history_calls += 1
                if history_calls == 2:
                    assert order.status == expected_status and order.is_closed
                    live = harness.source._live_for_client(order.client_order_id)
                    assert live is not None and live.reconciled_terminal is not None
                    row = cast(list[Any], harness.rest.history[0])
                    if fault == "quantity":
                        # Same actual fills/venue position, different original quantity.
                        row[6] += 1
                        row[7] += 1
                    elif fault == "flags":
                        row[12] = 1024  # Contradicts the original ordinary, non-reduce-only order.
                    elif fault == "missing":
                        harness.rest.history = []
                return await original_history(symbol, start=start, end=end, limit=limit)

            monkeypatch.setattr(
                harness.hedge, "generate_mass_status", temporarily_missing_hedge_report,
            )
            monkeypatch.setattr(harness.rest, "order_history_by_symbol", current_source_history)
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: owner._root is not None and owner._root.done())
            assert attempts == 2 and history_calls >= 2
            assert order.status == expected_status and order.quantity.as_decimal() == 2
            assert order.filled_qty.as_decimal() == filled
            assert len(harness.hedge_orders) == len(harness.store.intents()) == int(filled > 0)
            if filled:
                assert harness.store.intents()[0].status is ObligationStatus.COMPLETED
                # Do not roll back real first-round fills.
                assert harness.store.net_unhedged_ounces == 0
            if fault is None:
                assert owner.last_failure is None and owner.source_submission_ready
                await harness.opportunity()
                harness.assert_second_source_sent(order.client_order_id.value)
            else:
                assert owner.last_failure is not None and not owner.source_submission_ready
                await harness.opportunity()
                await asyncio.sleep(0.3)
                assert attempts == 2 and len(harness.store.source_orders()) == 1
                assert len(harness.sent_operations("on")) == 1
    asyncio.run(run())


def _fast_working_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only accelerate the interval; actual native Actor/LiveClock dispatch stays intact.
    monkeypatch.setattr(runtime_module, "_WORKING_ORDER_CHECK_NS", 50_000_000, raising=False)


@pytest.mark.parametrize("filled", [0, 1, 2], ids=["zero", "partial", "full"])
def test_ordinary_maker_discovers_unrequested_silent_terminal_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filled: int,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True, paper=True) as harness:
            order, cid = await harness.seed_source()
            assert not harness.sent_operations("oc")
            await harness.finish_source(order, cid, filled=filled, deliver_terminal=False)
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: order.is_closed and not owner.busy)
            assert order.status == (OrderStatus.FILLED if filled == 2 else OrderStatus.CANCELED)
            assert order.filled_qty.as_decimal() == filled
            assert harness.source._live_for_client(order.client_order_id) is None
            assert owner.last_failure is None and owner.source_submission_ready
            fills = [event for event in order.events if isinstance(event, OrderFilled)]
            assert len(fills) == len(harness.hedge_orders) == int(filled > 0)
            assert all(fill.trade_id.value.isdecimal() for fill in fills)
            if filled:
                assert harness.hedge_orders[0].quantity.as_decimal() == filled
                assert not harness.store.can_submit_source()
                harness.release_hedge.set()
                await _wait_until(harness.store.can_submit_source)
                assert harness.store.intents()[0].status is ObligationStatus.COMPLETED
                assert harness.store.net_unhedged_ounces == 0
                assert harness.late_trade is not None
                harness.source._consume_private_frame(harness.late_trade)
                harness.source._consume_private_frame(harness.late_trade)
                await _pump()
                assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
                assert len(harness.hedge_orders) == 1
            await harness.opportunity()
            harness.assert_second_source_sent(order.client_order_id.value)
    asyncio.run(run())


def test_ordinary_maker_discovers_still_active_fill_before_normal_protective_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True, paper=True) as harness:
            order, cid = await harness.seed_source()
            await harness.finish_source(order, cid, filled=1, deliver_terminal=False)
            canceled = cast(list[Any], harness.rest.history[0]).copy()
            active = canceled.copy()
            active[13] = f"PARTIALLY FILLED @ {order.price}(1)"
            harness.rest.active, harness.rest.history = [active], []
            assert not harness.sent_operations("oc")

            def acknowledge_protective_cancel(payload: dict[str, object] | list[object]) -> None:
                if isinstance(payload, list) and len(payload) == 4 and payload[1] == "oc":
                    # Venue reacts only to the original Maker's real cancel command.
                    harness.rest.active, harness.rest.history = [], [canceled]
                    harness.source._consume_private_frame([0, "oc", canceled])

            harness.transport.after_send = acknowledge_protective_cancel
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: order.status == OrderStatus.CANCELED and not owner.busy)
            assert order.filled_qty.as_decimal() == 1
            fills = [event for event in order.events if isinstance(event, OrderFilled)]
            assert len(fills) == len(harness.hedge_orders) == 1
            assert fills[0].trade_id.value.isdecimal()
            assert len(harness.sent_operations("oc")) == 1
            assert owner.last_failure is None
            live = harness.source._live_for_client(order.client_order_id)
            # REST can finish before the normal OC: retain the existing WS lifecycle.
            assert live is None or (live.terminal_emitted and live.filled_qty == 1)
            assert not harness.source.terminal_reconciliation_required
            assert harness.source.execution_hold_reason is None
            assert harness.late_trade is not None
            harness.source._consume_private_frame(harness.late_trade)
            harness.source._consume_private_frame(harness.late_trade)
            harness.release_hedge.set()
            await _wait_until(harness.store.can_submit_source)
            assert harness.store.net_unhedged_ounces == 0
            assert len(harness.store.intents()) == 1
            await harness.opportunity()
            harness.assert_second_source_sent(order.client_order_id.value)
    asyncio.run(run())


@pytest.mark.parametrize("filled", [1, 2], ids=["partial", "full"])
@pytest.mark.parametrize("fault", ["missing_history", "missing_trade", "type", "price", "time"])
def test_ordinary_maker_discovery_never_infers_missing_or_conflicting_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filled: int, fault: str,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True, paper=True) as harness:
            order, cid = await harness.seed_source()
            await harness.finish_source(order, cid, filled=filled, deliver_terminal=False)
            if fault == "missing_history":
                harness.rest.history, harness.rest.trades = [], []
            elif fault == "missing_trade":
                harness.rest.trades = []
            else:
                trade = cast(list[Any], harness.rest.trades[0])
                if fault == "type":
                    trade[6] = "IOC"
                elif fault == "price":
                    trade[7] = order.price.as_decimal() + 1
                else:
                    trade[2] = cast(list[Any], harness.rest.history[0])[4] - 1
            attempts = 0
            original = harness.source.generate_mass_status

            async def observe_mass(lookback: int | None = None) -> Any:
                nonlocal attempts
                attempts += 1
                return await original(lookback)

            monkeypatch.setattr(harness.source, "generate_mass_status", observe_mass)
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: owner.last_failure is not None and not owner.busy)
            assert attempts == 2 and not owner.source_submission_ready
            assert order.status == OrderStatus.ACCEPTED and order.filled_qty.as_decimal() == 0
            assert not harness.hedge_orders and not harness.sent_operations("oc")
            # Timer alone must not reopen this failed discovery.
            await asyncio.sleep(0.3)
            assert attempts == 2 and len(harness.sent_operations("on")) == 1
            assert not harness.hedge_orders and not harness.sent_operations("oc")
            # A real market callback now sees a failed execution channel, so the
            # original Maker makes one protective cancel with its own bounded query.
            await harness.opportunity()
            assert len(harness.sent_operations("oc")) == 1
            await _wait_until(lambda: attempts == 4 and not owner.busy)
            await harness.opportunity()
            await asyncio.sleep(0.3)
            assert attempts == 4 and len(harness.sent_operations("oc")) == 1
            assert len(harness.sent_operations("on")) == 1 and not harness.hedge_orders
            assert not owner.source_submission_ready and order.filled_qty.as_decimal() == 0
    asyncio.run(run())


@pytest.mark.parametrize("post_only_flag", [0, 4096], ids=["paper-opaque", "exact"])
def test_ordinary_maker_healthy_active_observation_does_not_query_history_or_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, post_only_flag: int,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True, paper=True) as harness:
            order, cid = await harness.seed_source()
            row = cast(list[Any], harness.wire.order_frame("on", cid, order)[2])
            row[12] = post_only_flag
            harness.rest.active = [row]
            calls = 0
            original = harness.rest.active_orders_by_symbol

            async def active(symbol: str) -> object:
                nonlocal calls
                calls += 1
                return await original(symbol)

            async def forbidden_mass(_lookback: int | None = None) -> Any:
                pytest.fail("unchanged active order must not start full history reconciliation")

            monkeypatch.setattr(harness.rest, "active_orders_by_symbol", active)
            monkeypatch.setattr(harness.source, "generate_mass_status", forbidden_mass)
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: calls >= 2 and not owner.busy)
            assert owner.last_failure is None and owner.source_submission_ready
            assert order.status == OrderStatus.ACCEPTED and order.filled_qty.as_decimal() == 0
            assert len(harness.sent_operations("on")) == 1
            assert not harness.sent_operations("oc") and not harness.hedge_orders
    asyncio.run(run())


@pytest.mark.parametrize("stop", [False, True], ids=["ws-wins", "actor-stops"])
def test_ordinary_maker_discovery_await_cannot_overwrite_new_ws_or_outlive_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: bool,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True, paper=True) as harness:
            order, cid = await harness.seed_source()
            stale_active = [harness.wire.order_frame("on", cid, order)[2]]
            entered, release, canceled = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original = harness.rest.active_orders_by_symbol
            calls = 0

            async def delayed_active(symbol: str) -> object:
                nonlocal calls
                calls += 1
                if calls != 1:
                    return await original(symbol)
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    canceled.set()
                    raise
                return stale_active

            monkeypatch.setattr(harness.rest, "active_orders_by_symbol", delayed_active)
            owner = get_source_terminal_reconciler(harness.node)
            await asyncio.wait_for(entered.wait(), 1)
            assert owner.busy and not owner.source_submission_ready
            if stop:
                harness.node.trader.stop()
                await asyncio.wait_for(canceled.wait(), 1)
                release.set()
                await _pump()
                # Maker.stop owns its existing protective cancel; the late read owns none.
                assert len(harness.sent_operations("oc")) == 1
                await asyncio.sleep(0.3)
                assert not owner.source_submission_ready and not owner.clock.timer_names
                assert order.status == OrderStatus.PENDING_CANCEL
                assert order.filled_qty.as_decimal() == 0
                assert len(harness.sent_operations("on")) == 1
                assert len(harness.sent_operations("oc")) == 1 and not harness.hedge_orders
                assert owner._root is not None and owner._root.cancelled()
            else:
                # A real terminal/fill overtakes the deliberately old active snapshot.
                await harness.finish_source(order, cid, filled=1)
                assert harness.late_trade is not None
                harness.source._consume_private_frame(harness.late_trade)
                harness.release_hedge.set()
                await _pump()
                assert order.status == OrderStatus.CANCELED and order.filled_qty.as_decimal() == 1
                release.set()
                await _wait_until(lambda: not owner.busy and harness.store.can_submit_source())
                assert owner.last_failure is None
                assert len(harness.hedge_orders) == len(harness.store.intents()) == 1
                assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
                assert harness.store.net_unhedged_ounces == 0
                await harness.opportunity()
                harness.assert_second_source_sent(order.client_order_id.value)
    asyncio.run(run())


def test_ordinary_maker_rest_fill_then_distinct_ws_fill_preserves_cumulative_quantity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True, paper=True) as harness:
            order, cid = await harness.seed_source()
            await harness.finish_source(order, cid, filled=1, deliver_terminal=False)
            partial = cast(list[Any], harness.rest.history[0]).copy()
            partial[13] = f"PARTIALLY FILLED @ {order.price}(1)"
            harness.rest.active, harness.rest.history = [partial], []
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: order.filled_qty.as_decimal() == 1 and not owner.busy)
            live = harness.source._live_for_client(order.client_order_id)
            assert live is not None and live.filled_qty == 1
            assert len(harness.hedge_orders) == 1

            second = harness.wire.trade_frame(
                cid, order, quantity="1", price=str(order.price), maker=1,
            )
            trade = cast(list[Any], second[2])
            first_trade = cast(list[Any], harness.rest.trades[0])
            trade[0] = first_trade[0] + 1
            trade[2] = harness.node.kernel.clock.timestamp_ns() // 1_000_000
            harness.rest.trades = [first_trade, trade]
            harness.rest.position_rows = [_position_row(
                D(2), avg_px=order.price.as_decimal(), raw_symbol=harness.wire.raw_symbol,
            )]
            terminal = harness.wire.order_frame(
                "oc", cid, order, remaining="0", status=f"EXECUTED @ {order.price}(2)",
            )
            row = cast(list[Any], terminal[2])
            row[4:6], row[17] = [partial[4], trade[2]], order.price.as_decimal()
            harness.rest.active, harness.rest.history = [], [row]
            harness.source._consume_private_frame(second)
            harness.source._consume_private_frame(second)
            harness.source._consume_private_frame(terminal)
            harness.release_hedge.set()
            await _wait_until(lambda: harness.store.can_submit_source() and not owner.busy)
            assert order.status == OrderStatus.FILLED and order.filled_qty.as_decimal() == 2
            fills = [event for event in order.events if isinstance(event, OrderFilled)]
            assert {event.trade_id.value for event in fills} == {
                str(first_trade[0]), str(trade[0]),
            }
            assert len(fills) == len(harness.hedge_orders) == len(harness.store.intents()) == 2
            assert sum((item.quantity.as_decimal() for item in harness.hedge_orders), D(0)) == 2
            assert all(
                item.status is ObligationStatus.COMPLETED for item in harness.store.intents()
            )
            assert harness.store.net_unhedged_ounces == 0
            assert harness.source.execution_hold_reason is None and owner.last_failure is None
            await harness.opportunity()
            harness.assert_second_source_sent(order.client_order_id.value)
    asyncio.run(run())


@pytest.mark.parametrize("public_lost", [False, True], ids=["healthy", "real-health-loss"])
def test_ordinary_maker_market_during_healthy_observation_pauses_without_canceling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public_lost: bool,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True, paper=True) as harness:
            order, cid = await harness.seed_source()
            harness.rest.active = [harness.wire.order_frame("on", cid, order)[2]]
            entered, release = asyncio.Event(), asyncio.Event()
            original = harness.rest.active_orders_by_symbol

            async def delayed_active(symbol: str) -> object:
                entered.set()
                await release.wait()
                return await original(symbol)

            monkeypatch.setattr(harness.rest, "active_orders_by_symbol", delayed_active)
            owner = get_source_terminal_reconciler(harness.node)
            await asyncio.wait_for(entered.wait(), 1)
            assert owner.busy and not owner.source_submission_ready
            if public_lost:
                harness.source_data._set_connected(False)
            await harness.opportunity()
            assert owner.busy  # Real market callbacks interleave with a read, not a terminal.
            assert len(harness.sent_operations("oc")) == int(public_lost)
            assert len(harness.sent_operations("on")) == 1
            assert not harness.sent_operations("ou") and not harness.hedge_orders
            assert order.status == (
                OrderStatus.PENDING_CANCEL if public_lost else OrderStatus.ACCEPTED
            )
            release.set()
            await _wait_until(lambda: not owner.busy)
            if not public_lost:
                assert owner.last_failure is None and bool(owner.source_submission_ready)
                assert not harness.strategy._source_hold
                await harness.opportunity()
                assert not harness.sent_operations("oc")
                assert len(harness.sent_operations("on")) == 1
    asyncio.run(run())


@pytest.mark.parametrize("fault", [None, "quantity", "trade_id", "missing_trade"])
def test_ordinary_maker_working_fill_then_ws_close_rechecks_second_venue_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True, paper=True) as harness:
            order, cid = await harness.seed_source()
            await harness.finish_source(order, cid, filled=1, deliver_terminal=False)
            terminal = cast(list[Any], harness.rest.history[0]).copy()
            active = terminal.copy()
            active[13] = f"PARTIALLY FILLED @ {order.price}(1)"
            harness.rest.active, harness.rest.history = [active], []
            harness.release_hedge.set()
            attempts = 0
            original_mass = harness.hedge.generate_mass_status
            original_source_mass = harness.source.generate_mass_status
            source_attempts = 0

            async def missing_first_hedge_report(lookback: int | None = None) -> Any:
                nonlocal attempts
                attempts += 1
                return None if attempts == 1 else await original_mass(lookback)

            async def source_mass_after_ws_close(lookback: int | None = None) -> Any:
                nonlocal source_attempts
                source_attempts += 1
                if source_attempts == 2:
                    assert order.filled_qty.as_decimal() == 1
                    live = harness.source._live_for_client(order.client_order_id)
                    assert live is not None and live.reconciled_working is not None
                    # The venue's genuine cancel response races the second mass round.
                    assert len(harness.sent_operations("oc")) == 1
                    harness.source._consume_private_frame([0, "oc", terminal])
                    await _pump()
                    assert order.status == OrderStatus.CANCELED
                    current = terminal.copy()
                    if fault == "quantity":
                        current[6] += 1
                        current[7] += 1
                    elif fault == "trade_id":
                        changed_trade = cast(list[Any], harness.rest.trades[0]).copy()
                        changed_trade[0] += 1
                        harness.rest.trades = [changed_trade]
                    elif fault == "missing_trade":
                        harness.rest.trades = []
                    harness.rest.active, harness.rest.history = [], [current]
                return await original_source_mass(lookback)

            monkeypatch.setattr(harness.hedge, "generate_mass_status", missing_first_hedge_report)
            monkeypatch.setattr(harness.source, "generate_mass_status", source_mass_after_ws_close)
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: source_attempts == 2 and not owner.busy)
            assert attempts == 2
            assert order.status == OrderStatus.CANCELED and order.filled_qty.as_decimal() == 1
            assert len(harness.hedge_orders) == len(harness.store.intents()) == 1
            assert harness.store.intents()[0].status is ObligationStatus.COMPLETED
            assert harness.store.net_unhedged_ounces == 0
            if fault is None:
                assert owner.last_failure is None and owner.source_submission_ready
                await harness.opportunity()
                harness.assert_second_source_sent(order.client_order_id.value)
            else:
                assert owner.last_failure is not None and not owner.source_submission_ready
                await harness.opportunity()
                await asyncio.sleep(0.3)
                assert source_attempts == 2 and len(harness.sent_operations("on")) == 1
                assert len(harness.hedge_orders) == 1
    asyncio.run(run())


@pytest.mark.parametrize("missing_previous_trade", [False, True], ids=["complete", "missing-old"])
def test_ordinary_maker_increasing_rest_fill_must_cover_already_applied_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_previous_trade: bool,
) -> None:
    async def run() -> None:
        _fast_working_observation(monkeypatch)
        async with _ordinary_strategy(
            tmp_path, monkeypatch, maker=True, paper=True, source_quantity=4,
        ) as harness:
            order, cid = await harness.seed_source()
            await harness.finish_source(order, cid, filled=1, deliver_terminal=False)
            active = cast(list[Any], harness.rest.history[0]).copy()
            active[13] = f"PARTIALLY FILLED @ {order.price}(1)"
            harness.rest.active, harness.rest.history = [active], []
            first_trade = cast(list[Any], harness.rest.trades[0]).copy()
            harness.release_hedge.set()
            source_attempts = hedge_attempts = 0
            original_source_mass = harness.source.generate_mass_status
            original_hedge_mass = harness.hedge.generate_mass_status

            async def missing_first_hedge_report(lookback: int | None = None) -> Any:
                nonlocal hedge_attempts
                hedge_attempts += 1
                return None if hedge_attempts == 1 else await original_hedge_mass(lookback)

            async def growing_source_fill(lookback: int | None = None) -> Any:
                nonlocal source_attempts
                source_attempts += 1
                if source_attempts == 2:
                    assert order.filled_qty.as_decimal() == 1
                    assert len(harness.hedge_orders) == 1
                    # The first real fill sent a protective cancel. Observe its
                    # actual deadline within this round, so a later timer tick
                    # is not a distinct, newly due cancel-recovery episode.
                    await _wait_until(lambda: harness.source.terminal_reconciliation_required)
                    # Venue claims total 2oz in either case. A new 2oz trade that
                    # omits the applied 1oz is not proof of the missing 1oz delta.
                    current = active.copy()
                    current[6] = D(2)
                    current[13] = f"PARTIALLY FILLED @ {order.price}(2)"
                    second_trade = first_trade.copy()
                    second_trade[0] += 1
                    second_trade[4] = D(2 if missing_previous_trade else 1)
                    harness.rest.active = [current]
                    harness.rest.trades = (
                        [second_trade] if missing_previous_trade else [first_trade, second_trade]
                    )
                    harness.rest.position_rows = [_position_row(
                        D(2), avg_px=order.price.as_decimal(), raw_symbol=harness.wire.raw_symbol,
                    )]
                return await original_source_mass(lookback)

            monkeypatch.setattr(harness.hedge, "generate_mass_status", missing_first_hedge_report)
            monkeypatch.setattr(harness.source, "generate_mass_status", growing_source_fill)
            owner = get_source_terminal_reconciler(harness.node)
            await _wait_until(lambda: source_attempts == 2 and not owner.busy)
            expected = 1 if missing_previous_trade else 2
            assert order.filled_qty.as_decimal() == expected
            fills = [event for event in order.events if isinstance(event, OrderFilled)]
            assert len(fills) == len(harness.hedge_orders) == expected
            assert len(harness.store.intents()) == expected
            assert sum(
                (item.quantity.as_decimal() for item in harness.hedge_orders), D(0),
            ) == expected
            assert len(harness.sent_operations("on")) == len(harness.sent_operations("oc")) == 1
            if missing_previous_trade:
                assert fills[0].trade_id.value == str(first_trade[0])
                assert owner.last_failure is not None and not owner.source_submission_ready
                await asyncio.sleep(0.3)
                assert source_attempts == 2 and order.filled_qty.as_decimal() == 1
                assert len(harness.hedge_orders) == 1
    asyncio.run(run())
