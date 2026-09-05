"""Ordinary live builders/strategies/Engine; only venue IO is synthetic.

Bitfinex uses its actual execution adapter with fake authenticated WS/REST.
Most MT5 lifecycle cases inject submission/report IO. The planned-leg matrix
delegates submissions to the actual MT5 adapter with the existing fake transport;
its unrelated mass reports remain synthetic, and no test runs the EA.
Native reported-account construction is explicit to isolate prior in-process backtests.
No canary strategy, direct state confirmation, or replacement strategy logic is used.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from msgspec.structs import replace as replace_config
from nautilus_trader.accounting.accounts.margin import MarginAccount
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder, QueryAccount, SubmitOrder
from nautilus_trader.execution.reports import ExecutionMassStatus, PositionStatusReport
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OrderSide,
    OrderStatus,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.events import AccountState, OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    PositionId,
    TradeId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from test_bitfinex_v1_execution import _FakeRest, _FakeTransport, _Harness, _position_row
from test_live_maker import _build as _build_maker
from test_live_maker import _configs as _maker_configs
from test_live_taker import _build, _configs
from test_mt5_v1_execution import _capacity_harness, _identity, _outcome, _snapshot
from test_mt5_v1_execution import _FakeTransport as _Mt5FakeTransport

import py000_nautilus.live_runtime as runtime_module
import py000_nautilus.margin as margin_module
from py000_nautilus.app import _book_snapshot, _quote
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, instrument_from_config
from py000_nautilus.config import HedgeAccountRoute
from py000_nautilus.economics import evaluate_taker
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.maker_economics import maker_quote
from py000_nautilus.models import (
    BookTop,
    HedgeAccount,
    MakerAccount,
    ObligationStatus,
    SourceAccount,
    SourceDirection,
)
from py000_nautilus.mt5_v1_data import instrument_from_snapshot
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionClient
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
        source_quantity: int = 2, two_sided: bool = False, native_mt5_transport: bool = False,
        source_short_quantity: int | None = None, inject_mt5_io: bool = True,
    ) -> None:
        self.maker = maker
        self.source_quantity = D(source_quantity)
        short_quantity = D(
            source_quantity if source_short_quantity is None else source_short_quantity,
        )
        if maker:
            configs: Any = _maker_configs(tmp_path)
            configs = replace(configs, strategy=replace_config(
                configs.strategy, economics=replace_config(
                    configs.strategy.economics,
                    bid=replace_config(
                        configs.strategy.economics.bid,
                        open_quantity_ounces=self.source_quantity,
                    ),
                    # Lifecycle tests isolate one direction using normal configuration.
                    ask=replace_config(
                        configs.strategy.economics.ask,
                        open_quantity_ounces=short_quantity if two_sided else D(0),
                    ),
                ),
            ))
        else:
            configs = _configs(tmp_path)
            configs = replace(configs, strategy=replace_config(
                configs.strategy, economics=replace_config(
                    configs.strategy.economics, base_book_quantity=self.source_quantity,
                    open_quantity_long=self.source_quantity,
                    open_quantity_short=short_quantity,
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
        # The synthetic MT5 producer below uses this stream; the live reader
        # must check the configured stream, not inherit a mismatching fixture.
        configs = replace(configs, mt5_exec=replace_config(
            configs.mt5_exec, expected_stream_id=_identity().stream_id,
        ))
        if native_mt5_transport:
            # Unlike the legacy injected MT5 IO, the real adapter authenticates
            # hello. Bind this offline profile to the valid synthetic identity.
            identity = _identity()
            expected = dict(
                expected_account_id=identity.account_id, expected_ea_build_id=identity.ea_build_id,
                expected_source_sha256=identity.declared_source_sha256,
            )
            configs = replace(
                configs, mt5_data=replace_config(configs.mt5_data, **expected),
                mt5_exec=replace_config(configs.mt5_exec, **expected),
                strategy=replace_config(configs.strategy, **({
                    "hedge_accounts": (replace_config(
                        configs.strategy.hedge_accounts[0],
                        account_id=AccountId(f"MT5-{identity.account_id}"),
                    ),),
                } if maker else {"hedge_account_id": AccountId(f"MT5-{identity.account_id}")})),
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
        self.rest.wallet_rows = [
            ["margin", self.wallet_currency, D(10000), D(0), D(10000)],
        ]
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
        # Nautilus backtests permanently register calculated accounts per issuer.
        # A fresh live process instead consumes reported balances. Construct that
        # native account mode explicitly, without resetting framework private state.
        source_initial = AccountState(
            account_id=self.source.account_id, account_type=AccountType.MARGIN,
            base_currency=USDT, balances=[], margins=[], reported=False, info={},
            event_id=UUID4(), ts_event=0, ts_init=0,
        )
        for initial in (
            source_initial, TestEventStubs.margin_account_state(account_id=self.hedge.account_id),
        ):
            account = MarginAccount(initial, calculate_account_state=False)
            self.node.cache.add_account(account)
            assert not account.calculate_account_state
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
        if inject_mt5_io:
            monkeypatch.setattr(self.hedge, "_submit_order", self._submit_hedge)
            monkeypatch.setattr(self.hedge, "generate_mass_status", self._hedge_mass)
        self.source_cancel_commands: list[ClientOrderId] = []
        cancel_order = self.source.cancel_order

        def observe_cancel(command: CancelOrder) -> None:
            self.source_cancel_commands.append(command.client_order_id)
            cancel_order(command)

        monkeypatch.setattr(self.source, "cancel_order", observe_cancel)
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

    def query_source_account(self) -> None:
        self.node.kernel.exec_engine.execute(QueryAccount(
            trader_id=self.strategy.trader_id, account_id=self.source.account_id,
            client_id=self.source.id, command_id=UUID4(),
            ts_init=self.node.kernel.clock.timestamp_ns(),
        ))

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
        # The synthetic MT5 venue reports its updated complete account after
        # the fill, just as a subsequent real snapshot would. No capacity/cache
        # field is backfilled directly by the test.
        sample = _snapshot(_identity())
        template = cast(list[dict[str, Any]], sample["positions"])[0]
        sample["positions"] = [
            {
                **template, "identifier": str(position.venue_position_id),
                "side": "buy" if position.position_side == PositionSide.LONG else "sell",
                "volume_lots": str(position.quantity.as_decimal() / D(100)),
            }
            for position in self.hedge_positions
        ]
        cast(dict[str, Any], sample["time"])["observed_utc_ms"] = str(now // 1_000_000)
        self.hedge._install_snapshot(sample)

    async def seed_source(
        self, *, acknowledge: bool = True, publish_accounts: bool = True,
    ) -> tuple[Any, int]:
        if publish_accounts:
            # An actual live order now requires complete venue facts. Keep start()
            # incomplete for account-query tests; seed only these lifecycle cases.
            await _publish_capacity_samples(
                self, source_quantity=D(0), hedge_quantity=D(0),
                available=D(10000), equity=D(10000),
            )
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
                [*_position_row(D(filled), avg_px=price, raw_symbol=self.wire.raw_symbol),
                 None, D(1000), D(200), None],
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
            strategy._carry, strategy._fx, now,
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
    source_quantity: int = 2, two_sided: bool = False, native_mt5_transport: bool = False,
) -> AsyncIterator[_OrdinaryStrategy]:
    harness = _OrdinaryStrategy(
        tmp_path, monkeypatch, maker=maker, paper=paper, source_quantity=source_quantity,
        two_sided=two_sided,
        native_mt5_transport=native_mt5_transport,
    )
    try:
        await harness.start()
        yield harness
    finally:
        await harness.close()


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("close_first", [False, True], ids=["open", "close"])
@pytest.mark.parametrize("mode", ["exact", "drift", "pending", "rejected", "unknown"])
def test_ordinary_hedge_plan_reaches_real_mt5_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
    close_first: bool, mode: str,
) -> None:
    """Real ordinary strategies/Engine/adapters; both venues' transport IO is synthetic.

    The optional unmarked native order is pre-existing inventory setup, not a
    strategy cycle. All subsequent hedge legs follow the real source fill.
    """
    async def run() -> None:
        drift = mode == "drift"
        async with _ordinary_strategy(
            tmp_path, monkeypatch, maker=maker, native_mt5_transport=True,
        ) as harness:
            config = harness.hedge._mt5_config
            identity = _identity()
            snapshot = _snapshot(identity)
            cast(dict[str, Any], snapshot["execution_limits"])["max_order_lots"] = str(
                config.expected_max_order_lots,
            )
            template = deepcopy(cast(list[dict[str, Any]], snapshot["positions"])[0])
            snapshot["positions"] = []
            cast(dict[str, Any], snapshot["time"])["observed_utc_ms"] = str(
                harness.node.kernel.clock.timestamp_ns() // 1_000_000,
            )
            wire = _Mt5FakeTransport(identity, snapshot)
            monkeypatch.setattr(harness.hedge, "_transport", wire)
            await harness.hedge._connect()
            captured: list[SubmitOrder] = []
            entered, release = asyncio.Event(), asyncio.Event()
            if mode == "pending":
                name = "close_position" if close_first else "submit_market_delta"
                original_mutation = getattr(wire, name)

                async def pending_mutation(*args: Any, **kwargs: Any) -> Any:
                    entered.set()
                    await release.wait()
                    return await original_mutation(*args, **kwargs)

                monkeypatch.setattr(wire, name, pending_mutation)

            async def submit(command: SubmitOrder) -> None:
                # Configure only the venue response; delegate every command to
                # the actual adapter, including its fresh snapshot and journal.
                assert all(previous.order.status == OrderStatus.FILLED for previous in captured)
                captured.append(command)
                order = command.order
                seq = int(harness.hedge._cursor) + 2
                target = command.position_id.value if command.position_id is not None else None
                response = f"order_{mode}" if mode in {"rejected", "unknown"} and order.tags else (
                    "order_filled"
                )
                wire.outcome = _outcome(
                    identity, order, response, sequence=seq,
                    quantity_lots=str(order.quantity.as_decimal() / 100),
                    position_ticket="700000001" if target else None,
                    position_identifier=target,
                    venue_position_id=target or ("800000001" if seq == 3 else str(800000000 + seq)),
                )
                payload = cast(dict[str, Any], wire.outcome["payload"])
                if response == "order_filled":
                    payload["venue_deal_id"] = str(810000000 + seq)
                    payload["venue_order_id"] = str(710000000 + seq)
                wire.current_snapshot = deepcopy(wire.current_snapshot)
                cast(dict[str, Any], wire.current_snapshot["time"])["observed_utc_ms"] = str(
                    harness.node.kernel.clock.timestamp_ns() // 1_000_000,
                )
                await Mt5V1ExecutionClient._submit_order(harness.hedge, command)
                if (target is not None and wire.close_calls
                        and wire.close_calls[-1][0] == order.client_order_id.value
                        and response == "order_filled"):
                    # A confirmed full close removes the one fixture ticket.
                    # The queued residual still must read the next full snapshot.
                    wire.current_snapshot = deepcopy(wire.current_snapshot)
                    wire.current_snapshot["positions"] = []

            monkeypatch.setattr(harness.hedge, "_submit_order", submit)
            try:
                if close_first:
                    initial = harness.strategy.order_factory.market(
                        instrument_id=harness.hedge_instrument.id, order_side=OrderSide.BUY,
                        quantity=harness.hedge_instrument.make_qty(D(1)),
                        time_in_force=TimeInForce.FOK,
                    )
                    harness.strategy.submit_order(initial, client_id=harness.hedge.id)
                    await _wait_until(lambda: initial.status == OrderStatus.FILLED)
                    assert len(harness.node.cache.positions_open(
                        instrument_id=harness.hedge_instrument.id,
                    )) == 1
                    assert not harness.store.intents()
                    captured.clear()
                    wire.submit_calls.clear()
                await _publish_capacity_samples(
                    harness, source_quantity=D(0), hedge_quantity=D(1) if close_first else D(0),
                    available=D(10000), equity=D(10000),
                )
                source_order, cid = await harness.seed_source(publish_accounts=False)
                fresh = deepcopy(snapshot)
                fresh["positions"] = [dict(
                    template, side="buy", volume_lots="0.02" if drift and close_first else "0.01",
                )] if close_first or drift else []
                wire.current_snapshot = fresh
                await harness.finish_source(source_order, cid, filled=2)
                if mode == "pending":
                    await asyncio.wait_for(entered.wait(), 1)
                    await _pump()
                    assert captured[0].order.status == OrderStatus.SUBMITTED
                    assert harness.hedge.pending_client_order_ids == (
                        captured[0].order.client_order_id.value,
                    )
                    assert harness.store.intents()[0].status is ObligationStatus.SUBMITTED
                    assert harness.store.net_unhedged_ounces == D(2)
                    await harness.opportunity()
                    assert len(captured) == len(harness.store.source_orders()) == 1
                    assert wire.submit_calls == [] and wire.close_calls == []
                    release.set()
                if mode == "unknown":
                    await _wait_until(lambda: bool(captured) and bool(harness.hedge._pending)
                                      and next(iter(harness.hedge._pending.values())).unknown)
                else:
                    await _wait_until(lambda: bool(captured) and captured[0].order.is_closed)
                await _pump()
                first = captured[0]
                assert first.params == ({
                    "py000_hedge_plan": True, "py000_expected_position_ounces": D(1),
                } if close_first else {"py000_hedge_plan": True})
                assert first.order.is_reduce_only is close_first
                assert first.position_id == (PositionId("800000001") if close_first else None)
                assert len(harness.store.intents()) == 1
                intent = harness.store.intents()[0]
                if mode in {"drift", "rejected", "unknown"}:
                    assert first.order.status == {
                        "drift": OrderStatus.DENIED, "rejected": OrderStatus.REJECTED,
                        "unknown": OrderStatus.SUBMITTED,
                    }[mode]
                    assert intent.status is (ObligationStatus.SUBMITTED if mode == "unknown"
                                             else ObligationStatus.REJECTED)
                    assert harness.store.net_unhedged_ounces == D(2)
                    assert not harness.store.can_submit_source()
                    assert len(captured) == 1
                    assert len(wire.submit_calls) + len(wire.close_calls) == int(not drift)
                    await harness.opportunity()
                    assert len(captured) == len(harness.store.source_orders()) == 1
                else:
                    await _wait_until(lambda: harness.store.intents()[0].status
                                      is ObligationStatus.COMPLETED)
                    assert len(captured) == (2 if close_first else 1)
                    assert len(wire.close_calls) == int(close_first)
                    assert len(wire.submit_calls) == 1
                    assert captured[-1].params == {"py000_hedge_plan": True}
            finally:
                release.set()
                await harness.hedge._disconnect()
    asyncio.run(run())


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
            if public_data_lost:
                assert state.active_source_order_id is None and state.halt_reason is None
                if maker:
                    # Account wakeup also reruns the existing public-data gate.
                    assert state.source_freeze_reason == "stale, closed, or unresolved"
            elif state.active_source_order_id is None:
                assert state.can_submit_source(), (
                    state.halt_reason, state.source_freeze_reason,
                    get_source_terminal_reconciler(harness.node).last_failure,
                )
            else:
                # The unclaimed market event above can now be consumed when
                # account recovery wakes the strategy after the hedge completes.
                harness.assert_second_source_sent(order.client_order_id.value)
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
                await _wait_until(lambda: (
                    harness.store.can_submit_source() or len(harness.sent_operations("on")) == 2
                ))
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
            await _wait_until(lambda: (
                harness.store.can_submit_source() or len(harness.sent_operations("on")) == 2
            ))
            assert harness.store.net_unhedged_ounces == 0
            assert len(harness.store.intents()) == 1
            await harness.opportunity()
            harness.assert_second_source_sent(order.client_order_id.value)
            assert harness.source_cancel_commands.count(order.client_order_id) == 1
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
            assert order.status == OrderStatus.PENDING_CANCEL and order.filled_qty.as_decimal() == 0
            assert not harness.hedge_orders
            # The account notification now runs the same protection before
            # another market tick. It must still send only one protective cancel
            # and allow only that cancel's existing bounded recovery episode.
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
                await _wait_until(lambda: not owner.busy and (
                    harness.store.can_submit_source() or len(harness.sent_operations("on")) == 2
                ))
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
            harness.rest.position_rows = [[*_position_row(
                D(2), avg_px=order.price.as_decimal(), raw_symbol=harness.wire.raw_symbol,
            ), None, D(1000), D(200), None]]
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
            await _wait_until(lambda: not owner.busy and (
                harness.store.can_submit_source() or len(harness.sent_operations("on")) == 2
            ))
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
            assert harness.source_cancel_commands.count(order.client_order_id) == 1
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
            assert harness.source_cancel_commands.count(order.client_order_id) == 1
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
                    harness.rest.position_rows = [[*_position_row(
                        D(2), avg_px=order.price.as_decimal(), raw_symbol=harness.wire.raw_symbol,
                    ), None, D(1000), D(200), None]]
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


async def _publish_capacity_samples(
    harness: _OrdinaryStrategy, *, source_quantity: Decimal, hedge_quantity: Decimal,
    available: Decimal, equity: Decimal, hedge_observed_ns: int | None = None,
) -> None:
    now_ms = harness.node.kernel.clock.timestamp_ns() // 1_000_000
    positions: list[list[object]] = []
    if source_quantity:
        row = [*_position_row(
            source_quantity, avg_px=D(4000), raw_symbol=harness.wire.raw_symbol,
        ), None, D(1000), D(200), None]
        row[13] = now_ms
        positions.append(row)
    harness.rest.position_rows = [*positions]
    harness.rest.wallet_rows = [
        ["margin", harness.wallet_currency, D(10000), D(0), available],
    ]
    harness.source._consume_private_frame([0, "ps", positions])
    harness.source._consume_private_frame(
        [0, "wu", ["margin", harness.wallet_currency, D(10000), D(0), available]],
    )
    sample = _snapshot(_identity())
    cast(dict[str, Any], sample["time"])["observed_utc_ms"] = str(
        now_ms if hedge_observed_ns is None else hedge_observed_ns // 1_000_000,
    )
    account = cast(dict[str, Any], sample["account"])
    account["equity"] = str(equity)
    account["margin_free"] = str(equity - D(account["margin"]))
    tickets = cast(list[dict[str, Any]], sample["positions"])
    if hedge_quantity:
        tickets[0]["side"] = "buy" if hedge_quantity > 0 else "sell"
        tickets[0]["volume_lots"] = str(abs(hedge_quantity) / D(100))
    else:
        sample["positions"] = []
    # Exercise the actual adapter publication and native Account/Portfolio path;
    # raw MT5 snapshot wire validation has its own existing integration tests.
    harness.hedge._install_snapshot(sample)
    await _pump()


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_capacity_query_completion_admits_without_a_new_market_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker) as harness:
            entered, release = asyncio.Event(), asyncio.Event()
            calls = 0

            async def wallets() -> object:
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return [["margin", harness.wallet_currency, D(10000), D(0), D(10000)]]

            monkeypatch.setattr(harness.rest, "wallets", wallets)
            sample = _snapshot(_identity())
            sample["positions"] = []
            cast(dict[str, Any], sample["time"])["observed_utc_ms"] = str(
                harness.node.kernel.clock.timestamp_ns() // 1_000_000,
            )
            harness.hedge._install_snapshot(sample)
            await harness.opportunity()
            await asyncio.wait_for(entered.wait(), 1)
            assert not harness.sent_operations("on")
            assert calls == 1
            source_timestamp = (
                harness.node.cache.quote_tick(harness.source_instrument.id).ts_event if maker
                else harness.node.cache.order_book(harness.source_instrument.id).ts_last
            )
            # Only the real QueryAccount result wakes the ordinary strategy now.
            release.set()
            await _wait_until(lambda: len(harness.sent_operations("on")) == 1)
            order = harness.node.cache.orders(instrument_id=harness.source_instrument.id)[0]
            assert order.status == OrderStatus.SUBMITTED
            assert order.quantity.as_decimal() == D(2)
            assert calls == 1
            latest_timestamp = (
                harness.node.cache.quote_tick(harness.source_instrument.id).ts_event if maker
                else harness.node.cache.order_book(harness.source_instrument.id).ts_last
            )
            assert latest_timestamp == source_timestamp
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_strategy_automatically_uses_capacity_for_actual_source_quantity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker) as harness:
            await _publish_capacity_samples(
                harness, source_quantity=D(0), hedge_quantity=D(0),
                available=D(375), equity=D(10000),
            )
            await harness.opportunity()
            orders = harness.node.cache.orders(instrument_id=harness.source_instrument.id)
            if not maker:
                assert len(orders) == 1
                assert orders[0].status == OrderStatus.SUBMITTED
                assert orders[0].quantity.as_decimal() == D(1)
                assert D(str(harness.sent_operations("on")[0]["amount"])) == D(1)
                return
            # Maker keeps the configured quantity; a one-ounce budget cannot
            # silently resize its two-ounce quote. Funding recovery alone wakes it.
            assert not orders and not harness.sent_operations("on")
            harness.source._consume_private_frame(
                [0, "wu", ["margin", harness.wallet_currency, D(10000), D(0), D(1000)]],
            )
            await _wait_until(lambda: len(harness.sent_operations("on")) == 1)
            orders = harness.node.cache.orders(instrument_id=harness.source_instrument.id)
            assert len(orders) == 1 and orders[0].quantity.as_decimal() == D(2)
    asyncio.run(run())


def test_ordinary_maker_retains_its_reserved_source_budget_but_obeys_hedge_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True) as harness:
            await _publish_capacity_samples(
                harness, source_quantity=D(0), hedge_quantity=D(0),
                available=D(1000), equity=D(10000),
            )
            order, cid = await harness.seed_source(publish_accounts=False)
            original_price = order.price
            harness.source._consume_private_frame(
                [0, "wu", ["margin", harness.wallet_currency, D(10000), D(0), D(0)]],
            )
            await _pump()
            assert order.status == OrderStatus.ACCEPTED and order.price == original_price
            assert len(harness.sent_operations("on")) == 1
            assert not harness.sent_operations("oc") and not harness.sent_operations("ou")
            # A current real hedge-capacity reduction is different from source
            # collateral already reserved for this exact CID.
            await _publish_capacity_samples(
                harness, source_quantity=D(0), hedge_quantity=D(0),
                available=D(0), equity=D(10),
            )
            await _wait_until(lambda: len(harness.sent_operations("oc")) == 1)
            assert order.status == OrderStatus.PENDING_CANCEL
            assert len(harness.sent_operations("on")) == 1
            binding = harness.source._cid_store.binding_for_client(order.client_order_id.value)
            assert binding is not None and binding.cid == cid
    asyncio.run(run())


def test_ordinary_maker_two_sides_wait_for_post_action_budget_without_requote_starvation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(
            tmp_path, monkeypatch, maker=True, two_sided=True,
        ) as harness:
            await _publish_capacity_samples(
                harness, source_quantity=D(0), hedge_quantity=D(0),
                available=D(1000), equity=D(10000),
            )
            await harness.opportunity()
            # Both sides saw the same market callback; the native INITIALIZED
            # first order must prevent reuse of that pre-submit account budget.
            orders = harness.node.cache.orders(instrument_id=harness.source_instrument.id)
            assert len(orders) == len(harness.sent_operations("on")) == 1
            first = orders[0]
            assert first.side == OrderSide.BUY and first.status == OrderStatus.SUBMITTED
            cid = harness.sent_operations("on")[0]["cid"]
            entered, release = asyncio.Event(), asyncio.Event()
            calls = 0

            async def wallets() -> object:
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                # The venue now reports remaining funds after accepting BUY.
                return [["margin", harness.wallet_currency, D(10000), D(0), D(600)]]

            monkeypatch.setattr(harness.rest, "wallets", wallets)
            harness.source._consume_private_frame(harness.wire.order_frame("on", cid, first))
            await _pump()
            assert first.status == OrderStatus.ACCEPTED
            await harness.opportunity()
            await asyncio.wait_for(entered.wait(), 1)
            for change in range(1, 9):
                now = harness.node.kernel.clock.timestamp_ns()
                harness.node.kernel.data_engine.process(_quote(
                    harness.hedge_instrument, str(D(3936) + change),
                    str(D("3936.1") + change), "10", now,
                ))
                harness.node.kernel.data_engine.process(_quote(
                    harness.source_instrument, str(D(3926) + change),
                    str(D("3926.1") + change), "10", now,
                ))
                await _pump()
                assert len(harness.sent_operations("on")) == calls == 1
                assert not harness.sent_operations("ou") and not harness.sent_operations("oc")
            release.set()
            await _wait_until(lambda: len(harness.sent_operations("on")) == 2)
            sent = harness.sent_operations("on")
            assert [D(str(item["amount"])) for item in sent] == [D(2), D(-2)]
            assert sent[0]["cid"] != sent[1]["cid"]
            assert calls == 1 and first.status == OrderStatus.PENDING_UPDATE
            assert len(harness.sent_operations("ou")) == 1
            assert not harness.sent_operations("oc")
            # Once the second side has secured its new budget, normal
            # price-only maintenance of the first side can resume.
            assert [
                row[1] for row in cast(list[Any], harness.transport.sent) if isinstance(row, list)
            ] == ["on", "on", "ou"]
            assert harness.sent_operations("ou")[0]["id"] == int(first.venue_order_id.value)
    asyncio.run(run())


def test_ordinary_maker_account_expiry_cancels_while_market_and_costs_remain_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=True) as harness:
            config = harness.strategy._config
            observed = (
                harness.node.kernel.clock.timestamp_ns() - config.max_cost_age_ns + 400_000_000
            )
            await _publish_capacity_samples(
                harness, source_quantity=D(0), hedge_quantity=D(0),
                available=D(1000), equity=D(10000), hedge_observed_ns=observed,
            )
            order, _cid = await harness.seed_source(publish_accounts=False)
            account = harness.hedge.get_account()
            assert account is not None
            event = account.last_event
            expected_expiry = event.info["mt5_account_observed_ns"] + config.max_cost_age_ns + 1
            assert order.status == OrderStatus.ACCEPTED and not harness.sent_operations("oc")
            assert harness.strategy._account_deadline_ns == expected_expiry
            await _wait_until(lambda: len(harness.sent_operations("oc")) == 1)
            now = harness.node.kernel.clock.timestamp_ns()
            for instrument in (harness.source_instrument, harness.hedge_instrument):
                tick = harness.node.cache.quote_tick(instrument.id)
                assert tick is not None and now - tick.ts_event < config.max_quote_age_ns
            assert now - harness.strategy._cost_ts_ns < config.max_cost_age_ns
            assert now - harness.strategy._session_ts_ns < config.max_session_age_ns
            assert now >= expected_expiry and account.last_event is event
            assert order.status == OrderStatus.PENDING_CANCEL
            assert len(harness.sent_operations("on")) == 1
    asyncio.run(run())


def _mapped_capacity_accounts(
    harness: _OrdinaryStrategy,
) -> tuple[SourceAccount | None, HedgeAccount | MakerAccount | None]:
    config = harness.strategy._config
    source_account, hedge_account = harness.source.get_account(), harness.hedge.get_account()
    assert source_account is not None and hedge_account is not None
    now = harness.node.kernel.clock.timestamp_ns()
    source_route = replace_config(
        config.source_accounts[0], max_long_ounces=D(100), max_short_ounces=D(100),
    )
    source = margin_module.bitfinex_source_account(
        source_account.last_event, route=source_route,
        instrument_id=config.source_instrument_id, wallet_currency=harness.wallet_currency,
        margin_target=D(400), max_abs_ounces=D(10), now_ns=now,
        max_account_age_ns=5_000_000_000,
        client_ready=harness.source.is_connected and harness.source.execution_hold_reason is None,
        ask=D(4000), ask_ts_ns=now, max_quote_age_ns=5_000_000_000,
        ask_actionable=harness.source_data.book_is_actionable,
    )
    hedge_route = HedgeAccountRoute(
        account_id=harness.hedge.account_id, client_id=harness.hedge.id,
        max_long_ounces=D(100), max_short_ounces=D(100),
    )
    mapper = margin_module.mt5_maker_account if harness.maker else margin_module.mt5_hedge_account
    hedge = mapper(
        hedge_account.last_event, route=hedge_route,
        symbol=_identity().symbol, stream_id=_identity().stream_id,
        margin_target=D(400), max_abs_ounces=D(10), now_ns=now,
        max_account_age_ns=5_000_000_000,
        client_ready=harness.hedge.account_capacity_ready(5_000_000_000),
        ask=D(4000), ask_ts_ns=now, max_quote_age_ns=5_000_000_000,
        ask_actionable=harness.hedge_data.snapshot_refresh_healthy,
    )
    return source, hedge


@pytest.mark.parametrize("held", [False, True], ids=["complete-flat", "held-dynamic-base"])
@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_account_capacity_mapping_drives_existing_economics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, held: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            # Auth + wallet and a native account alone are not complete margin inputs.
            assert _mapped_capacity_accounts(harness) == (None, None)
            await _publish_capacity_samples(
                harness, source_quantity=D("-0.4") if held else D(0), hedge_quantity=D(0),
                available=D(400) if held else D(1000), equity=D(10000) if held else D(184),
            )
            source_event = harness.source.get_account().last_event
            hedge_event = harness.hedge.get_account().last_event
            before = deepcopy((source_event.info, hedge_event.info))
            source, hedge = _mapped_capacity_accounts(harness)
            assert source is not None and hedge is not None
            assert source.position_ounces == (D("-0.4") if held else D(0))
            assert source.base_margin_level == (D(200) if held else D(100))
            assert (source.max_long_ounces, source.max_short_ounces) == (
                (D("1.8"), D(1)) if held else (D(5), D(5))
            )
            assert hedge.position_ounces == 0
            assert (hedge.max_long_ounces, hedge.max_short_ounces) == (
                (D(10), D(10)) if held else (D(1), D(1))
            )
            book = BookTop(D(3999), D(4000), D(100), D(100))
            config = replace_config(harness.strategy._config.economics, margin_level=D(400))
            if maker:
                assert isinstance(hedge, MakerAccount)
                assert maker_quote(SourceDirection.LONG, book, (source,), (hedge,), config) is None
                one = replace_config(
                    config, bid=replace_config(config.bid, open_quantity_ounces=D(1)),
                )
                quote = maker_quote(SourceDirection.LONG, book, (source,), (hedge,), one)
                assert quote is not None and quote.quantity_ounces == 1
                leverage = quote.leverage
            else:
                assert isinstance(hedge, HedgeAccount)
                config = replace_config(config, threshold_long=D(-1))
                opportunity = evaluate_taker(
                    book, book, (source,), hedge, config, allowed_direction=SourceDirection.LONG,
                )
                assert opportunity is not None
                assert opportunity.source_quantity_ounces == (D("1.8") if held else D(1))
                leverage = opportunity.leverage
            assert leverage == (16 if held else 20)
            assert (source_event.info, hedge_event.info) == before
            assert not harness.sent_operations("on") and not harness.hedge_orders
    asyncio.run(run())


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_account_capacity_mapping_preserves_reduction_and_bounds_crossing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, sign: int,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            await _publish_capacity_samples(
                harness, source_quantity=D(12 * sign), hedge_quantity=D(-12 * sign),
                available=D(100000), equity=D(100000),
            )
            source, hedge = _mapped_capacity_accounts(harness)
            assert source is not None and hedge is not None
            assert (source.max_long_ounces, source.max_short_ounces) == (
                (D(0), D(22)) if sign > 0 else (D(22), D(0))
            )
            assert (hedge.max_long_ounces, hedge.max_short_ounces) == (
                (source.max_short_ounces, source.max_long_ounces)
            )
            direction = SourceDirection.SHORT if sign > 0 else SourceDirection.LONG
            book = BookTop(D(3999), D(4000), D(100), D(100))
            base_config = replace_config(harness.strategy._config.economics, margin_level=D(400))
            for requested in (D(1), D(22), D(23), D(24)):
                if maker:
                    assert isinstance(hedge, MakerAccount)
                    config = replace_config(
                        base_config,
                        bid=replace_config(base_config.bid, open_quantity_ounces=requested),
                        ask=replace_config(base_config.ask, open_quantity_ounces=requested),
                    )
                    quote = maker_quote(direction, book, (source,), (hedge,), config)
                    if requested > 22:
                        assert quote is None
                        continue
                    assert quote is not None and quote.quantity_ounces == requested
                    quantity = quote.quantity_ounces
                else:
                    assert isinstance(hedge, HedgeAccount)
                    config = replace_config(
                        base_config, open_quantity_long=requested, open_quantity_short=requested,
                        threshold_long=D(-1), threshold_short=D(-1),
                    )
                    opportunity = evaluate_taker(
                        book, book, (source,), hedge, config, allowed_direction=direction,
                    )
                    assert opportunity is not None
                    quantity = opportunity.source_quantity_ounces
                    assert quantity == min(requested, D(22))
                after = source.position_ounces - D(sign) * quantity
                assert after == (D(sign * 11) if requested == 1 else D(-sign * 10))
            assert not harness.sent_operations("on") and not harness.hedge_orders
    asyncio.run(run())


def test_mt5_actual_fill_revokes_mapping_even_when_historical_account_event_stays_valid() -> None:
    async def run() -> None:
        harness, clock = await _capacity_harness(asyncio.get_running_loop())
        try:
            event = harness.account_states[-1]
            old_info = deepcopy(event.info)
            route = HedgeAccountRoute(
                account_id=harness.client.account_id, max_long_ounces=D(100),
                max_short_ounces=D(100), client_id=harness.client.id,
            )

            def view() -> HedgeAccount | None:
                return margin_module.mt5_hedge_account(
                    event, route=route, symbol=harness.identity.symbol,
                    stream_id=harness.identity.stream_id, margin_target=D(400),
                    max_abs_ounces=D(10), now_ns=clock.timestamp_ns(),
                    max_account_age_ns=5_000_000_000,
                    client_ready=harness.client.account_capacity_ready(5_000_000_000),
                    ask=D(4000), ask_ts_ns=clock.timestamp_ns(),
                    max_quote_age_ns=5_000_000_000, ask_actionable=True,
                )

            assert view() is not None
            order = harness.market(quantity="1")
            harness.fake.outcome = _outcome(
                harness.identity, order, "order_filled", quantity_lots="0.01",
            )
            await harness.submit(order)
            assert len([event for event in harness.events if isinstance(event, OrderFilled)]) == 1
            assert event.info == old_info and event.info["mt5_account_sample_valid"] is True
            assert view() is None
            assert harness.client.execution_admitted
        finally:
            await harness.client._disconnect()
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_native_account_query_coalesces_and_publishes_one_complete_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            account = harness.source.get_account()
            assert account is not None
            old_event, event_count = account.last_event, account.event_count
            old_info = deepcopy(old_event.info)
            calls: list[str] = []
            wallet_returned: list[int] = []
            position_entered, release = asyncio.Event(), asyncio.Event()

            async def wallets() -> object:
                calls.append("wallets")
                wallet_returned.append(harness.node.kernel.clock.timestamp_ns())
                return [["margin", harness.wallet_currency, D(12000), D(0), None]]

            async def positions() -> object:
                calls.append("positions")
                position_entered.set()
                await release.wait()
                return []

            monkeypatch.setattr(harness.rest, "wallets", wallets, raising=False)
            monkeypatch.setattr(harness.rest, "positions", positions)
            harness.query_source_account()
            await _wait_until(position_entered.is_set)
            for _ in range(9):
                harness.query_source_account()
            await _pump()
            before_release = harness.node.kernel.clock.timestamp_ns()
            assert calls == ["wallets", "positions"]
            assert account.last_event is old_event and old_event.info == old_info

            release.set()
            await _wait_until(lambda: account.event_count == event_count + 1)
            facts = account.last_event.info["bitfinex_margin"]
            assert facts["wallet"]["balance"] == "12000"
            assert facts["wallet"]["available_balance"] is None
            assert facts["wallet"]["current"] is True
            assert wallet_returned[0] <= facts["wallet"]["observed_ns"] <= before_release
            assert facts["positions"]["observed_ns"] >= before_release
            assert facts["positions"]["complete"] is True
            assert facts["positions"]["current"] is True
            assert facts["positions"]["position"] is None
            assert account.balance_total(USDT).as_decimal() == 12000
            assert old_event.info == old_info
            assert calls == ["wallets", "positions"]
            assert not harness.sent_operations("on") and not harness.hedge_orders
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_native_account_query_discards_old_wallet_then_allows_new_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            account = harness.source.get_account()
            assert account is not None
            entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
            calls: list[str] = []

            async def wallets() -> object:
                calls.append("wallets")
                return [["margin", harness.wallet_currency, D(12000), D(0), D(12000)]]

            async def positions() -> object:
                calls.append("positions")
                entered.set()
                await release.wait()
                returned.set()
                return []

            monkeypatch.setattr(harness.rest, "wallets", wallets, raising=False)
            monkeypatch.setattr(harness.rest, "positions", positions)
            harness.query_source_account()
            await _wait_until(entered.is_set)
            harness.source._consume_private_frame(
                [0, "wu", ["margin", harness.wallet_currency, D(11000), D(0), D(11000)]],
            )
            await _pump()
            newer_event = account.last_event
            newer_info = deepcopy(newer_event.info)
            release.set()
            await _wait_until(returned.is_set)
            assert account.last_event is newer_event
            assert account.last_event.info == newer_info
            assert newer_info["bitfinex_margin"]["wallet"]["balance"] == "11000"
            assert newer_info["bitfinex_margin"]["positions"]["complete"] is False

            # Completion does not schedule another read; only the next command does.
            assert calls == ["wallets", "positions"]
            harness.query_source_account()
            await _wait_until(
                lambda: account.last_event.info["bitfinex_margin"]["positions"]["complete"],
            )
            assert calls == ["wallets", "positions", "wallets", "positions"]
            assert account.last_event.info["bitfinex_margin"]["wallet"]["balance"] == "12000"
            assert newer_event.info == newer_info
            assert not harness.sent_operations("on") and not harness.hedge_orders
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["wallet", "positions"])
@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_native_account_query_decimal_overflow_revokes_candidate_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, stage: str,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            harness.source._consume_private_frame([0, "ps", []])
            await _pump()
            account = harness.source.get_account()
            assert account is not None
            old_event, old_count = account.last_event, account.event_count
            old_info = deepcopy(old_event.info)
            before = old_info["bitfinex_margin"]
            entered, release = asyncio.Event(), asyncio.Event()

            async def wallets() -> object:
                if stage == "wallet":
                    entered.set()
                    await release.wait()
                available = D("1e1000000") if stage == "wallet" else D(500)
                return [["margin", harness.wallet_currency, D(900), D(0), available]]

            async def positions() -> object:
                entered.set()
                await release.wait()
                row = _position_row(
                    D("1e1000000"), avg_px=D(4000), raw_symbol=harness.wire.raw_symbol,
                )
                row[13] = harness.node.kernel.clock.timestamp_ns() // 1_000_000
                return [row]

            monkeypatch.setattr(harness.rest, "wallets", wallets, raising=False)
            monkeypatch.setattr(harness.rest, "positions", positions)
            harness.query_source_account()
            await _wait_until(entered.is_set)
            task = harness.source._account_refresh_task
            assert task is not None
            release.set()
            with pytest.raises(ArithmeticError):
                await task
            await _pump()
            facts = account.last_event.info["bitfinex_margin"]
            expected = deepcopy(before)
            expected[stage]["current"] = False
            if stage == "positions":
                expected[stage]["complete"] = False
            assert facts == expected
            assert account.event_count == old_count + 1
            assert account.balance_total(USDT).as_decimal() == 10000
            assert old_event.info == old_info
            assert harness.source._account_refresh_task is None
            assert harness.source.execution_hold_reason is None
            assert not harness.sent_operations("on") and not harness.hedge_orders
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_native_account_query_does_not_block_fill_or_install_pre_fill_wallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            await _publish_capacity_samples(
                harness, source_quantity=D(0), hedge_quantity=D(0),
                available=D(10000), equity=D(10000),
            )
            account = harness.source.get_account()
            assert account is not None
            entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_positions = harness.rest.positions
            position_calls = 0

            async def wallets() -> object:
                return [["margin", harness.wallet_currency, D(12000), D(0), D(12000)]]

            async def positions() -> object:
                nonlocal position_calls
                position_calls += 1
                if position_calls == 1:
                    entered.set()
                    await release.wait()
                    returned.set()
                # Any normal reconciliation remains on its existing venue read.
                return await original_positions()

            monkeypatch.setattr(harness.rest, "wallets", wallets, raising=False)
            monkeypatch.setattr(harness.rest, "positions", positions)
            harness.query_source_account()
            await _wait_until(entered.is_set)
            order, cid = await harness.seed_source(publish_accounts=False)
            await harness.finish_source(order, cid, filled=1)
            assert harness.late_trade is not None
            harness.source._consume_private_frame(harness.late_trade)
            await _wait_until(lambda: len(harness.hedge_orders) == 1)
            assert order.filled_qty.as_decimal() == 1
            assert harness.hedge_orders[0].quantity.as_decimal() == 1
            release.set()
            await _wait_until(returned.is_set)
            wallet = account.last_event.info["bitfinex_margin"]["wallet"]
            assert wallet["balance"] == "10000" and wallet["current"] is False
            assert len([event for event in order.events if isinstance(event, OrderFilled)]) == 1
            assert len(harness.hedge_orders) == len(harness.store.intents()) == 1
            harness.release_hedge.set()
            await _wait_until(
                lambda: harness.store.intents()[0].status is ObligationStatus.COMPLETED,
            )
            assert harness.store.net_unhedged_ounces == 0
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_account_events_keep_independent_complete_margin_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            account = harness.source.get_account()
            assert account is not None
            initial = account.last_event
            initial_info = deepcopy(initial.info)
            margin = initial.info["bitfinex_margin"]
            assert margin["instrument_id"] == harness.source_instrument.id.value
            assert margin["wallet"]["available_balance"] == "10000"
            assert margin["positions"]["complete"] is False
            assert margin["positions"]["current"] is False
            assert margin["positions"]["position"] is None

            row = [*_position_row(
                D("-0.75"), avg_px=D("3926.5"), raw_symbol=harness.wire.raw_symbol,
            ), None, D("150.125"), D("15.0125"), None]
            row[13] = harness.node.kernel.clock.timestamp_ns() // 1_000_000
            harness.source._consume_private_frame([0, "ps", [row]])
            await _pump()
            positioned = account.last_event
            positioned_info = deepcopy(positioned.info)
            facts = positioned.info["bitfinex_margin"]
            assert facts["wallet"] == margin["wallet"]
            assert facts["positions"]["complete"] is True
            assert facts["positions"]["current"] is True
            assert facts["positions"]["position"]["quantity"] == "-0.75"
            assert facts["positions"]["position"]["collateral"] == "150.125"
            assert facts["positions"]["observed_ns"] >= facts["wallet"]["observed_ns"]

            harness.source._consume_private_frame(
                [0, "wu", ["margin", harness.wallet_currency, D(10000), D(0), None]],
            )
            await _pump()
            updated = account.last_event.info["bitfinex_margin"]
            assert updated["wallet"]["available_balance"] is None
            assert updated["positions"] == facts["positions"]
            assert updated["wallet"]["observed_ns"] >= facts["wallet"]["observed_ns"]
            assert initial.info == initial_info and positioned.info == positioned_info
            assert not harness.sent_operations("on") and not harness.hedge_orders
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_ambiguous_positions_cannot_become_flat_from_one_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            account = harness.source.get_account()
            assert account is not None
            row = [*_position_row(
                D("-0.75"), avg_px=D("3926.5"), raw_symbol=harness.wire.raw_symbol,
            ), None, D(150), D(15)]
            row[13] = 100
            harness.source._consume_private_frame([0, "ps", [row]])
            await _pump()
            last_good = deepcopy(account.last_event.info["bitfinex_margin"]["positions"])

            other = row.copy()
            other[11], other[13] = 99, 101
            changed = row.copy()
            changed[13] = 101
            harness.source._consume_private_frame([0, "ps", [changed, other]])
            closed = row.copy()
            closed[1], closed[2], closed[13] = "CLOSED", D(0), 102
            harness.source._consume_private_frame([0, "pc", closed])
            await _pump()
            after = account.last_event.info["bitfinex_margin"]["positions"]
            assert after["complete"] is False and after["current"] is False
            assert after["position"] == last_good["position"]
            assert after["observed_ns"] == last_good["observed_ns"]

            # A complete later observation can identify the remaining position.
            other[13] = 103
            harness.source._consume_private_frame([0, "ps", [other]])
            await _pump()
            recovered = account.last_event.info["bitfinex_margin"]["positions"]
            assert recovered["complete"] is True and recovered["current"] is True
            assert recovered["position"]["position_id"] == 99
            assert recovered["position"]["quantity"] == "-0.75"
            assert not harness.sent_operations("on") and not harness.hedge_orders
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_ordinary_real_fill_invalidates_margin_facts_without_interrupting_hedge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            order, cid = await harness.seed_source()
            account = harness.source.get_account()
            assert account is not None
            harness.source._consume_private_frame([0, "ps", []])
            await _pump()
            before = account.last_event
            before_info = deepcopy(before.info)
            assert before.info["bitfinex_margin"]["positions"]["complete"] is True
            assert before.info["bitfinex_margin"]["positions"]["current"] is True

            release_positions = asyncio.Event()
            original_positions = harness.rest.positions

            async def delayed_positions() -> object:
                # Hold the venue read so we can inspect the actual fill's
                # invalidation before a legitimate complete REST refresh wins.
                await release_positions.wait()
                return await original_positions()

            monkeypatch.setattr(harness.rest, "positions", delayed_positions)
            await harness.finish_source(order, cid, filled=1)
            assert harness.late_trade is not None
            harness.source._consume_private_frame(harness.late_trade)
            await _wait_until(lambda: len(harness.hedge_orders) == 1)
            after = account.last_event.info["bitfinex_margin"]
            assert after["wallet"]["current"] is False
            assert after["positions"]["current"] is False
            assert before.info == before_info
            assert order.filled_qty.as_decimal() == 1
            assert harness.hedge_orders[0].quantity.as_decimal() == 1

            position = [*_position_row(
                D(1), avg_px=order.price.as_decimal(), raw_symbol=harness.wire.raw_symbol,
            ), None, D(200), D(20), None]
            position[13] = harness.node.kernel.clock.timestamp_ns() // 1_000_000
            harness.source._consume_private_frame([0, "ps", [position]])
            harness.source._consume_private_frame(
                [0, "wu", ["margin", harness.wallet_currency, D(10000), D(0), D(9800)]],
            )
            await _pump()
            refreshed = deepcopy(account.last_event.info)
            assert refreshed["bitfinex_margin"]["positions"]["current"] is True
            assert refreshed["bitfinex_margin"]["positions"]["position"]["quantity"] == "1"
            harness.source._consume_private_frame(harness.late_trade)
            harness.source._consume_private_frame(harness.late_trade)
            await _pump()
            assert account.last_event.info == refreshed
            fills = [event for event in order.events if isinstance(event, OrderFilled)]
            assert len(fills) == len(harness.hedge_orders) == len(harness.store.intents()) == 1
            release_positions.set()
            harness.release_hedge.set()
            await _wait_until(
                lambda: harness.store.intents()[0].status is ObligationStatus.COMPLETED,
            )
            assert harness.store.net_unhedged_ounces == 0
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale-account"])
def test_ordinary_mt5_account_capacity_uses_its_own_complete_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, stale: bool,
) -> None:
    async def run() -> None:
        async with _ordinary_strategy(tmp_path, monkeypatch, maker=maker, paper=True) as harness:
            sample = _snapshot(_identity())
            observed_ms = harness.node.kernel.clock.timestamp_ns() // 1_000_000
            if stale:
                observed_ms -= 5_001
            cast(dict[str, Any], sample["time"])["observed_utc_ms"] = str(observed_ms)
            positions = cast(list[dict[str, Any]], sample["positions"])
            short = positions[0].copy()
            short.update(ticket="700000002", identifier="800000002", side="sell")
            positions.append(short)
            # The actual adapter publishes through the ordinary node's Account
            # and Portfolio. Snapshot wire validation is exercised separately.
            harness.hedge._install_snapshot(sample)
            await _pump()
            account = harness.hedge.get_account()
            assert account is not None
            event = account.last_event
            original_info = deepcopy(event.info)
            assert event.info["mt5_positions_complete"] is True
            assert event.info["mt5_position_count"] == 2
            assert D(event.info["mt5_net_position_ounces"]) == 0  # Hedged, not an empty list.
            assert event.info["mt5_symbol"] == "XAUUSD"
            assert event.info["mt5_stream_id"] == _identity().stream_id
            assert event.info["mt5_account_observed_ns"] == event.ts_event
            assert event.ts_event == observed_ms * 1_000_000
            assert harness.hedge_data._snapshot_refresh_healthy
            assert harness.hedge.account_capacity_ready(5_000_000_000) == (not stale)

            empty_sample = deepcopy(sample)
            empty_sample["positions"] = []
            cast(dict[str, Any], empty_sample["time"])["observed_utc_ms"] = str(
                harness.node.kernel.clock.timestamp_ns() // 1_000_000,
            )
            harness.hedge._install_snapshot(empty_sample)
            await _pump()
            assert account.last_event.info["mt5_position_count"] == 0
            assert D(account.last_event.info["mt5_net_position_ounces"]) == 0
            assert harness.hedge.account_capacity_ready(5_000_000_000)
            assert event.info == original_info
            assert not harness.sent_operations("on") and not harness.hedge_orders
    asyncio.run(run())
