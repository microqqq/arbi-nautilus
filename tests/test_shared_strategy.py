"""One ordinary both node; only external venue IO and delivery order are synthetic.

Orders, fills, strategy decisions, shared persistence and native reconciliation
are real. No canary, replacement reducer, second concurrent node or native trade
seed is used. A later-node event copy is explicitly not a process/Redis claim.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from continuous_mt5_wire import ContinuousMt5Wire
from msgspec.structs import replace
from nautilus_trader.accounting.accounts.margin import MarginAccount
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.enums import AccountType, OrderSide, OrderStatus
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import AccountId, ClientId, ClientOrderId, Venue
from ordinary_restart_worker import _native
from test_adapter_continuity import _SourceWire
from test_bitfinex_v1_execution import _FakeRest, _FakeTransport, _Harness
from test_live_maker import _configs as _maker_configs
from test_live_taker import _configs as _taker_configs
from test_mt5_v1_data import _tick_at_utc_ms
from test_mt5_v1_execution import _identity, _snapshot
from test_startup_recovery import _History
from test_strategy_continuity import _pump

from py000_nautilus.app import _book_snapshot, _quote
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport
from py000_nautilus.live_both import build_live_both_node
from py000_nautilus.live_lifecycle import DrainingTradingNode
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.models import BookTop, ObligationStatus
from py000_nautilus.mt5_v1_data import instrument_from_snapshot
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.mt5_v1_transport import Mt5V1Transport

D = Decimal


class _JointSource(_SourceWire):
    def __init__(self, joint: _Joint) -> None:
        self.joint = joint
        self.attempts: list[dict[str, Any]] = []
        harness = SimpleNamespace(
            node=joint.node, source=joint.source, transport=joint.transport,
            rest=joint.rest, wire=joint.frame_builder, maker=False,
        )
        super().__init__(cast(Any, harness))

    def respond(self, message: dict[str, object] | list[object]) -> None:
        if isinstance(message, list) and len(message) == 4 and message[1] == "on":
            payload = cast(dict[str, Any], message[3])
            order = self.order(int(payload["cid"]))
            self.attempts.append({
                "cid": int(payload["cid"]), "owner": str(order.strategy_id),
                "intents": {intent.intent_id: intent.status.value
                            for view in self.joint.owner.all_views() for intent in view.intents()},
                "draining": (self.joint.maker._draining, self.joint.taker._draining),
            })
        super().respond(message)

    def fill(self, cid: int, quantity: Decimal) -> None:
        # The ordinary wire's partial/IOC and liquidity semantics belong to
        # this actual order, not one global choice for the joint account.
        self.harness.maker = self.order(cid).strategy_id == self.joint.maker.id
        super().fill(cid, quantity)


class _Joint:
    def __init__(
        self, path: Path, monkeypatch: pytest.MonkeyPatch, *, limit: int = 4,
        maker_quantity: int = 2, maker_ask: int = 0, taker_quantity: int = 2,
        stop_timeout: float = 2,
    ) -> None:
        maker, taker = _maker_configs(path), _taker_configs(path)
        identity = _identity()
        binding = dict(expected_account_id=identity.account_id,
                       expected_ea_build_id=identity.ea_build_id,
                       expected_source_sha256=identity.declared_source_sha256)
        hedge_route = replace(maker.strategy.hedge_accounts[0],
                              account_id=AccountId(f"MT5-{identity.account_id}"))
        risk = replace(maker.strategy.economics.risk,
                       source_max_abs=D(limit), hedge_max_abs=D(limit))
        self.maker_config = replace(
            maker.strategy, hedge_accounts=(hedge_route,),
            economics=replace(maker.strategy.economics, risk=risk,
                              bid=replace(maker.strategy.economics.bid,
                                          open_quantity_ounces=D(maker_quantity)),
                              ask=replace(maker.strategy.economics.ask,
                                          open_quantity_ounces=D(maker_ask))),
        )
        self.taker_config = replace(
            taker.strategy, hedge_account_id=hedge_route.account_id,
            source_accounts=self.maker_config.source_accounts,
            economics=replace(taker.strategy.economics, risk=risk,
                              base_book_quantity=D(taker_quantity),
                              open_quantity_long=D(taker_quantity),
                              open_quantity_short=D(taker_quantity)),
        )
        self.node, (self.maker, self.taker) = build_live_both_node(
            bitfinex_data_config=maker.bitfinex_data,
            bitfinex_exec_config=maker.bitfinex_exec,
            mt5_data_config=replace(maker.mt5_data, **binding),
            mt5_exec_config=replace(maker.mt5_exec, **binding,
                                    expected_stream_id=identity.stream_id),
            maker_config=self.maker_config, taker_config=self.taker_config,
            shared_store_prefix=str(path / "both"), loop=asyncio.get_running_loop(),
            connection_timeout_seconds=3, stop_timeout_seconds=stop_timeout,
        )
        self.owner = self.maker._state_store
        self.source = self.node.kernel.exec_engine._clients[ClientId("BITFINEX")]
        self.hedge = self.node.kernel.exec_engine._clients[ClientId("MT5")]
        self.source_data = self.node.kernel.data_engine.routing_map[Venue("BITFINEX")]
        self.hedge_data = self.node.kernel.data_engine.routing_map[Venue("MT5")]
        # Backtest clients leave a process-global calculated-account registration
        # behind. Select the fresh live process's reported mode explicitly, with
        # no balance, capacity, timestamp or trade facts supplied by this fixture.
        for client, currency in ((self.source, USDT), (self.hedge, USD)):
            initial = AccountState(
                account_id=client.account_id, account_type=AccountType.MARGIN,
                base_currency=currency, balances=[], margins=[], reported=False, info={},
                event_id=UUID4(), ts_event=0, ts_init=0,
            )
            account = MarginAccount(initial, calculate_account_state=False)
            self.node.cache.add_account(account)
            assert not account.calculate_account_state and not account.last_event.is_reported
            assert account.last_event.balances == [] and account.last_event.margins == []
            assert account.last_event.info == {} and account.last_event.ts_event == 0
        assert not any(client.is_connected for client in (
            self.source, self.hedge, self.source_data, self.hedge_data,
        ))
        assert not self.source.account_budget_refresh_ready and not self.source.account_budget_ready
        assert not self.hedge.execution_admitted
        book = BookTop(D(1), D(2), D(1), D(1))
        for strategy in (self.maker, self.taker):
            assert strategy._live_account_reader is not None
            assert strategy._live_account_reader(book, 0, book, 0, True) is None
        self.transport, self.rest = _FakeTransport(), _FakeRest(maker.bitfinex_exec.raw_symbol)
        self.rest.wallet_rows = [["margin", maker.bitfinex_exec.wallet_currency,
                                 D(10000), D(0), D(10000)]]
        self.source._transport, self.source._rest = self.transport, self.rest
        self.source_data._transport = _FakeTransport()
        self.frame_builder = object.__new__(_Harness)
        self.frame_builder.raw_symbol = maker.bitfinex_exec.raw_symbol
        self.frame_builder.fee_currency = "USD"
        snapshot = _snapshot(identity)
        snapshot["positions"] = []
        cast(JsonObject, snapshot["authority_flags"])["mql_trade_allowed"] = True
        cast(JsonObject, snapshot["execution_limits"])["max_order_lots"] = "0.02"
        self.wire = ContinuousMt5Wire(
            identity, snapshot, now_ns=self.node.kernel.clock.timestamp_ns,
        )
        self.pub: asyncio.Queue[tuple[bytes, JsonObject]] = asyncio.Queue()
        self.install_wire(self.wire)
        self.venue = _JointSource(self)
        self.run_task: asyncio.Task[None] | None = None
        self.callback_order: list[str] = []

        async def forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("joint fixture attempted an actual venue connection")

        monkeypatch.setattr(BitfinexV1Transport, "open", forbidden)
        monkeypatch.setattr(Mt5V1Transport, "open", forbidden)

    def install_wire(self, wire: ContinuousMt5Wire) -> None:
        self.wire = wire
        wire.topic = b"XAUUSD"  # type: ignore[attr-defined]
        wire.recv_pub = self.pub.get  # type: ignore[attr-defined]
        self.hedge._transport = self.hedge_data._transport = wire

    def diagnostic(self) -> dict[str, Any]:
        actor = get_source_terminal_reconciler(self.node)
        return {"native": _native(self.node), "business": self.owner._to_payload(),
                "actor": (actor.busy, actor.restart_pending, actor.last_failure),
                "source": (self.source.is_connected, self.source.accounting_ready,
                           self.source.execution_hold_reason),
                "hedge": (self.hedge.is_connected, self.hedge.execution_admitted,
                          self.hedge.execution_hold_reason)}

    async def until(self, ready: Callable[[], bool], *, direction: int | None = None) -> None:
        try:
            async with asyncio.timeout(8):
                while not ready():
                    if direction is not None:
                        await self.market(direction)
                    await asyncio.sleep(.01)
        except TimeoutError as exc:
            raise AssertionError(self.diagnostic()) from exc
        await _pump()

    async def start(self, history: tuple[_History, Any, Any] | None = None) -> None:
        assert not self.node.cache.orders() and not self.node.cache.positions()
        if history is not None:
            recorded, source_instrument, hedge_instrument = history
            for instrument in (source_instrument, hedge_instrument):
                # The live data queue is not running yet. Materialize the
                # actually observed first-node metadata with the native cache API.
                self.node.cache.add_instrument(instrument)
            io = SimpleNamespace(
                node=self.node, source=self.source, transport=self.transport,
                rest=self.rest, wire=self.frame_builder, hedge=self.hedge,
                source_instrument=source_instrument,
            )
            venue, wire = recorded.restore(cast(Any, io))
            self.venue.rows, self.venue.trades = venue.rows, venue.trades
            self.venue.net, self.venue.average_price = venue.net, venue.average_price
            self.venue.publish()
            self.transport.after_send = self.venue.respond
            self.install_wire(wire)
        self.transport.queue.put_nowait(
            {"event": "auth", "status": "OK", "chanId": 0, "userId": 269_312},
        )
        self.transport.queue.put_nowait([0, "ws", self.rest.wallet_rows])
        self.run_task = asyncio.create_task(self.node.run_async())
        await self.until(lambda: self.node.trader.is_running
                         and self.maker.is_running and self.taker.is_running)
        self.source_instrument = self.node.cache.instrument(self.maker_config.source_instrument_id)
        self.hedge_instrument = self.node.cache.instrument(self.maker_config.hedge_instrument_id)
        assert self.source_instrument is not None and self.hedge_instrument is not None
        assert len(self.node.kernel.exec_engine._clients) == 2
        assert len(self.node.kernel.data_engine.routing_map) == 2
        assert self.taker.state_store is self.owner.taker_store
        assert len(self.owner.stores) == 2 and len(self.owner.all_views()) == 3
        assert self.owner.shared_strategy_ids == (str(self.maker.id), str(self.taker.id))

    def quote_order(self, first: str) -> None:
        # Change only delivery order of the two original native handlers.
        # Keep priority zero: native subscriptions reject negative priorities,
        # and existing core subscriptions retain their original precedence.
        strategies: tuple[Any, Any] = (
            (self.maker, self.taker) if first == "maker" else (self.taker, self.maker)
        )
        bus = self.node.kernel.msgbus
        handlers = []
        for strategy in strategies:
            matches = [sub for sub in bus.subscriptions()
                       if sub.handler == strategy.handle_quote_tick and ".MT5." in sub.topic]
            assert len(matches) == 1
            topic = matches[0].topic
            original, name = strategy.handle_quote_tick, str(strategy.id)
            bus.unsubscribe(topic, original)
            handlers.append((topic, original, name))

        for topic, original, name in handlers:
            def observed(tick: Any, handler: Any = original, owner: str = name) -> None:
                self.callback_order.append(owner)
                handler(tick)

            bus.subscribe(topic, observed)

    async def market(self, direction: int) -> None:
        now = self.node.kernel.clock.timestamp_ns()
        snapshot = await self.wire.snapshot(self.wire.identity.binding())
        self.node.kernel.data_engine.process(instrument_from_snapshot(
            snapshot, self.hedge_instrument.id, ts_init=now,
        ))
        for strategy in (self.maker, self.taker):
            assert strategy.update_cost_snapshot(strategy._carry, strategy._fx, now)
            strategy.update_hedge_session(True, now)
        bid, ask = {1: ("3926.6", "3926.7"), 0: ("3936.7", "3936.8"),
                    -1: ("3946.8", "3946.9")}[direction]
        self.source_data._book.apply_snapshot(
            [[D(bid) - D(i) / 10, 1, D(10)] for i in range(25)]
            + [[D(ask) + D(i) / 10, 1, D(-10)] for i in range(25)],
        )
        self.node.kernel.data_engine.process(_book_snapshot(
            self.source_instrument, bid, ask, "10", now,
        ))
        self.node.kernel.data_engine.process(_quote(self.source_instrument, bid, ask, "10", now))
        message = _tick_at_utc_ms(now // 1_000_000)
        message.update(identity=self.wire.identity.to_wire(), bid="3936.7", ask="3936.8")
        self.pub.put_nowait((b"XAUUSD", message))
        await _pump()

    def active(self, owner: Any, side: OrderSide = OrderSide.BUY) -> list[int]:
        return [cid for cid in self.venue.rows if self.venue.order(cid).strategy_id == owner.id
                and self.venue.order(cid).side == side
                and self.venue.order(cid).status is OrderStatus.ACCEPTED
                and self.venue.order(cid).filled_qty == 0]

    def intents(self) -> list[Any]:
        return [intent for view in self.owner.all_views() for intent in view.intents()]

    def source_worst(self) -> tuple[Decimal, Decimal]:
        orders = self.node.cache.orders(instrument_id=self.source_instrument.id)
        buy = sum((order.leaves_qty.as_decimal() for order in orders
                   if not order.is_closed and order.side is OrderSide.BUY), D(0))
        sell = sum((order.leaves_qty.as_decimal() for order in orders
                    if not order.is_closed and order.side is OrderSide.SELL), D(0))
        return self.venue.net + buy, self.venue.net - sell

    async def settled(self, count: int) -> None:
        await self.until(lambda: len(self.intents()) == count and all(
            intent.status is ObligationStatus.COMPLETED for intent in self.intents()
        ) and not get_source_terminal_reconciler(self.node).busy, direction=0)

    async def close(self) -> None:
        if self.node.is_running():
            await self.node.stop_async()
        if self.run_task is not None:
            done, _ = await asyncio.wait({self.run_task}, timeout=5)
            assert done, self.diagnostic()  # Do not cancel native run_async to manufacture exit.
            await self.run_task
        await self.source.cancel_pending_tasks(timeout_secs=1)
        await self.hedge.cancel_pending_tasks(timeout_secs=1)
        self.node.kernel.dispose()
        if self.node.kernel.executor is not None:
            self.node.kernel.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("closed", [True, False])
def test_both_cold_start_classifies_imported_external_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed: bool,
) -> None:
    async def scenario() -> None:
        h = _Joint(tmp_path, monkeypatch, stop_timeout=.2)
        await h.wire.submit_market_delta(h.wire.identity.binding(),
                                        client_request_id="OLD-OPEN", side="buy",
                                        quantity_lots="0.02")
        position = cast(list[JsonObject], h.wire.current_snapshot["positions"])[0]
        if closed:
            await h.wire.close_position(
                h.wire.identity.binding(), client_request_id="OLD-CLOSE", side="sell",
                quantity_lots="0.02", position_ticket=str(position["ticket"]),
                position_identifier=str(position["identifier"]),
            )
        h.wire.submit_calls.clear()
        h.wire.close_calls.clear()
        actor = get_source_terminal_reconciler(h.node)
        assert not actor.restart_pending
        try:
            await h.start()
            await h.until(lambda: not actor.busy and (
                not actor.restart_pending or actor.last_failure is not None
            ))
            if closed:
                assert not actor.restart_pending, actor.last_failure
                await h.until(lambda: len(h.venue.rows) == 2, direction=1)
                for strategy in (h.maker, h.taker):
                    cid, = h.active(strategy)
                    h.venue.fill(cid, D(2))
                await h.settled(2)
                assert len(h.wire.submit_calls) == 2 and not h.wire.close_calls
            else:
                assert bool(actor.restart_pending) is True
                assert actor.last_failure is not None
                await h.market(1)
                assert not h.venue.rows and not h.wire.submit_calls and not h.wire.close_calls
        finally:
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["maker", "taker"])
def test_joint_same_tick_both_owners_trade_with_one_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str,
) -> None:
    async def scenario() -> None:
        h = _Joint(tmp_path, monkeypatch)
        try:
            await h.start()
            h.quote_order(first)
            await h.market(1)
            await h.until(lambda: bool(h.active(h.maker)) and bool(h.active(h.taker)))
            other = "taker" if first == "maker" else "maker"
            assert h.callback_order[:2] == [str(getattr(h, first).id), str(getattr(h, other).id)]
            assert h.venue.net == 0 and not h.wire.submit_calls
            assert sum(abs(row[6]) for row in h.venue.rows.values()) == 4
            owners = (h.maker, h.taker) if first == "maker" else (h.taker, h.maker)
            for strategy in owners:
                cid, = h.active(strategy)
                h.venue.fill(cid, D(2))
            await h.settled(2)
            assert len(h.wire.submit_calls) == 2 and not h.wire.close_calls
            hedge_orders = [h.node.cache.order(ClientOrderId(call[0]))
                            for call in h.wire.submit_calls]
            assert [order.strategy_id for order in hedge_orders] == [owner.id for owner in owners]
            assert all(order.status is OrderStatus.FILLED and order.filled_qty == 2
                       for order in hedge_orders)
            assert h.venue.net == 4
            assert sum(position.signed_decimal_qty() for position in h.node.cache.positions_open(
                instrument_id=h.source_instrument.id,
            )) == 4
            assert sum(position.signed_decimal_qty() for position in h.node.cache.positions_open(
                instrument_id=h.hedge_instrument.id,
            )) == -4
            before = deepcopy(_native(h.node))
            assert await get_source_terminal_reconciler(h.node).reconcile()
            assert _native(h.node) == before  # True alone does not prove preserved ownership.
            assert h.node.cache.check_integrity()
        finally:
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("opposite", [False, True], ids=["same-way", "cross-owner-flat"])
def test_joint_node_drain_late_fills_then_later_node_preserves_both_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opposite: bool,
) -> None:
    async def scenario() -> None:
        maker_side = OrderSide.SELL if opposite else OrderSide.BUY
        first = _Joint(tmp_path, monkeypatch, limit=6, stop_timeout=3,
                       maker_quantity=0 if opposite else 2, maker_ask=2 if opposite else 0)
        second: _Joint | None = None
        disposed = False
        try:
            await first.start()
            await first.until(lambda: bool(first.active(first.maker, maker_side))
                              and bool(first.active(first.taker)), direction=1)
            original_sources = set(first.venue.rows)
            cancellations: list[int] = []
            stop_clients: list[tuple[bool, bool, bool, bool]] = []

            def fill_during_cancel(message: dict[str, object] | list[object]) -> None:
                if isinstance(message, list) and message[1] == "oc":
                    payload = cast(dict[str, Any], message[3])
                    cid = next(cid for cid, row in first.venue.rows.items()
                               if row[0] == payload["id"])
                    assert cid not in cancellations
                    cancellations.append(cid)
                    stop_clients.append((first.maker._draining, first.taker._draining,
                                         first.source.is_connected, first.hedge.is_connected))
                    first.venue.fill(cid, D(1))
                first.venue.respond(message)

            first.transport.after_send = fill_during_cancel
            await asyncio.gather(first.node.stop_async(), first.node.stop_async())
            assert isinstance(first.node, DrainingTradingNode)
            assert first.node.drain_result is not None and first.node.drain_result.complete
            assert set(cancellations) == original_sources and len(cancellations) == 2
            assert all(flags == (True, True, True, True) for flags in stop_clients)
            assert set(first.venue.rows) == original_sources
            assert len(first.wire.submit_calls) == (1 if opposite else 2)
            assert len(first.wire.close_calls) == (1 if opposite else 0)
            assert all(intent.status is ObligationStatus.COMPLETED for intent in first.intents())
            assert all(first.venue.order(cid).is_closed
                       and first.venue.order(cid).filled_qty == 1 for cid in original_sources)
            assert not first.node.is_running() and not first.source.is_connected
            assert not first.hedge.is_connected
            before_native = deepcopy(_native(first.node))
            before_business = deepcopy(first.owner._to_payload())
            history = (_History(cast(Any, first), first.venue, first.wire),
                       first.source_instrument, first.hedge_instrument)
            await first.close()
            disposed = True

            # One later node, not two concurrent runners and not a claim of
            # Redis/process durability: reconstruct only actual first-node events.
            second = _Joint(tmp_path, monkeypatch, limit=6, stop_timeout=3,
                            maker_quantity=0 if opposite else 2, maker_ask=2 if opposite else 0)
            assert second.owner._to_payload() == before_business
            assert get_source_terminal_reconciler(second.node).restart_pending
            await second.start(history)
            await second.until(lambda: not get_source_terminal_reconciler(
                second.node,
            ).restart_pending)
            assert get_source_terminal_reconciler(second.node).last_failure is None
            assert _native(second.node) == before_native
            assert second.owner._to_payload() == before_business
            assert not second.venue.attempts
            assert not second.wire.submit_calls and not second.wire.close_calls
            await second.until(lambda: bool(second.active(second.maker, maker_side))
                               and bool(second.active(second.taker)), direction=1)
            for strategy in (second.maker, second.taker):
                cid, = second.active(strategy, maker_side if strategy is second.maker
                                     else OrderSide.BUY)
                second.venue.fill(cid, D(2))
            await second.settled(4)
            assert second.venue.net == (0 if opposite else 6)
            assert sum(position.signed_decimal_qty()
                       for position in second.node.cache.positions_open(
                           instrument_id=second.hedge_instrument.id,
                       )) == (0 if opposite else -6)
            virtual = {str(position.strategy_id): position.signed_decimal_qty()
                       for position in second.node.cache.positions_open(
                           instrument_id=second.source_instrument.id,
                       )}
            assert virtual == {str(second.maker.id): D(-3 if opposite else 3),
                               str(second.taker.id): D(3)}
            if opposite:
                assert not second.wire._positions()
                assert not second.node.cache.positions_open(
                    instrument_id=second.hedge_instrument.id,
                )
            for cid, facts in before_native["orders"].items():
                assert _native(second.node)["orders"][cid] == facts
        finally:
            if not disposed:
                first.transport.after_send = first.venue.respond
                await first.close()
            if second is not None:
                await second.close()

    asyncio.run(scenario())


def test_joint_late_other_owner_fill_cannot_take_close_open_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        h = _Joint(tmp_path, monkeypatch, limit=6, maker_quantity=0,
                   maker_ask=2, taker_quantity=4)
        try:
            await h.start()
            h.quote_order("maker")
            await h.until(lambda: bool(h.active(h.maker, OrderSide.SELL)), direction=0)
            first, = h.active(h.maker, OrderSide.SELL)
            h.venue.fill(first, D(2))
            await h.settled(1)
            initial_ticket, = h.node.cache.positions_open(instrument_id=h.hedge_instrument.id)
            assert initial_ticket.strategy_id == h.maker.id
            assert initial_ticket.signed_decimal_qty() == 2
            await h.until(lambda: bool(h.active(h.maker, OrderSide.SELL)), direction=0)
            competing, = h.active(h.maker, OrderSide.SELL)
            await h.until(lambda: bool(h.active(h.taker)), direction=1)
            taker_cid, = h.active(h.taker)
            mutation_order: list[tuple[str, str, str | None]] = []
            waiting: list[str] = []
            canceled_late: list[dict[str, object] | list[object]] = []

            def delayed_cancel(message: dict[str, object] | list[object]) -> None:
                if isinstance(message, list) and message[1] == "oc":
                    canceled_late.append(deepcopy(message))
                else:
                    h.venue.respond(message)

            h.transport.after_send = delayed_cancel

            async def before(payload: JsonObject) -> None:
                cid = cast(str, payload["client_request_id"])
                order = h.node.cache.order(ClientOrderId(cid))
                mutation_order.append((str(order.strategy_id), cast(str, payload["side"]),
                                       cast(str | None, payload.get("position_identifier"))))
                if len(mutation_order) == 1:
                    assert order.strategy_id == h.taker.id and order.is_reduce_only
                    assert payload["position_identifier"] == initial_ticket.id.value
                    # An already accepted other-owner source fill arrives while
                    # this actual close request is reserved at the venue.
                    h.venue.fill(competing, D(2))
                    h.transport.after_send = h.venue.respond
                    for message in canceled_late:
                        h.venue.respond(message)
                    await h.until(lambda: len(h.intents()) == 3)
                    pending = [intent for intent in h.intents()
                               if intent.source_client_order_id
                               == h.venue.order(competing).client_order_id.value]
                    assert len(pending) == 1 and pending[0].hedge_client_order_id is None
                    waiting.append(pending[0].intent_id)
                if len(mutation_order) == 2:
                    assert order.strategy_id == h.taker.id and not order.is_reduce_only
                    held = next(intent for intent in h.intents() if intent.intent_id == waiting[0])
                    assert held.status is ObligationStatus.PENDING
                    assert held.hedge_client_order_id is None and not held.hedge_plan

            h.wire.before_mutation = before
            h.venue.fill(taker_cid, D(4))
            await h.settled(3)
            assert [owner for owner, _, _ in mutation_order] == [
                str(h.taker.id), str(h.taker.id), str(h.maker.id),
            ]
            assert [side for _, side, _ in mutation_order] == ["sell", "sell", "buy"]
            assert mutation_order[0][2] == initial_ticket.id.value
            assert mutation_order[1][2] is None and mutation_order[2][2] is not None
            assert h.venue.net == 0 and not h.wire._positions()
            assert not h.node.cache.positions_open(instrument_id=h.hedge_instrument.id)
            assert len(h.wire.submit_calls) == 2 and len(h.wire.close_calls) == 2
            assert initial_ticket.is_closed and initial_ticket.strategy_id == h.maker.id
            assert {str(fill.strategy_id) for fill in initial_ticket.events} == {
                str(h.maker.id), str(h.taker.id),
            }
            before_native = deepcopy(_native(h.node))
            assert await get_source_terminal_reconciler(h.node).reconcile()
            assert _native(h.node) == before_native
        finally:
            h.transport.after_send = h.venue.respond
            h.wire.before_mutation = None
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["ticket-quantity", "same-magic-extra"])
def test_joint_current_snapshot_drift_blocks_before_explicit_mass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    async def scenario() -> None:
        h = _Joint(tmp_path, monkeypatch, limit=8)
        try:
            await h.start()
            await h.until(lambda: bool(h.active(h.maker)) and bool(h.active(h.taker)),
                          direction=1)
            for strategy in (h.maker, h.taker):
                cid, = h.active(strategy)
                h.venue.fill(cid, D(2))
            await h.settled(2)
            before = deepcopy(_native(h.node))
            attempts = len(h.venue.attempts)
            if fault == "ticket-quantity":
                h.wire._positions()[0]["volume_lots"] = "0.03"
            else:
                extra = deepcopy(h.wire._positions()[0])
                extra.update(identifier="9000000999", ticket="9000000998")
                h.wire._positions().append(extra)
            # Real periodic snapshot implementation, not mass/recovery and
            # not a native cache mutation. Qualified account facts can drift.
            await h.hedge._refresh_snapshot_if_due(force=True)
            for direction in (1, -1, 1):
                await h.market(direction)
            assert len(h.venue.attempts) == attempts, h.diagnostic()
            assert all(view.source_freeze_reason is not None for view in h.owner.all_views())
            assert all(not view.can_submit_source() for view in h.owner.all_views())
            assert not h.owner.cycle_freeze_only
            actor = get_source_terminal_reconciler(h.node)
            await h.until(lambda: not actor.busy and actor.last_failure is not None)
            assert not actor.source_submission_ready
            assert len(h.wire.submit_calls) == 2 and not h.wire.close_calls
            assert _native(h.node) == before
        finally:
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["maker", "taker"])
def test_joint_working_leaves_are_reserved_without_opposite_netting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str,
) -> None:
    async def scenario() -> None:
        h = _Joint(tmp_path, monkeypatch, limit=2, maker_ask=2)
        held_cancels: list[dict[str, object] | list[object]] = []
        try:
            await h.start()
            h.quote_order(first)
            await h.market(1)
            await h.until(lambda: bool(h.active(getattr(h, first))))
            for _ in range(3):
                await h.market(1)
                assert h.source_worst()[0] <= 2 and h.source_worst()[1] >= -2
            assert not h.wire.submit_calls and not h.venue.trades
            buys = h.active(h.maker) + h.active(h.taker)
            assert len(buys) == 1
            if first == "maker":
                assert h.active(h.maker, OrderSide.SELL)  # Netting leaves would admit BUY4.
                assert not h.active(h.taker)

                def delay_cancel(message: dict[str, object] | list[object]) -> None:
                    if isinstance(message, list) and message[1] == "oc":
                        held_cancels.append(deepcopy(message))
                    else:
                        h.venue.respond(message)

                h.transport.after_send = delay_cancel
                order = h.venue.order(buys[0])
                h.maker.cancel_order(order)
                await h.until(lambda: bool(held_cancels) and order.is_pending_cancel)
                before = len(h.venue.attempts)
                for _ in range(3):
                    await h.market(1)
                    assert order.is_pending_cancel and h.source_worst()[0] == 2
                    assert not h.active(h.taker)
                assert len(h.venue.attempts) == before
                h.transport.after_send = h.venue.respond
                for message in held_cancels:
                    h.venue.respond(message)
                held_cancels.clear()
                await h.until(lambda: order.status is OrderStatus.CANCELED)
                assert order.filled_qty == 0
        finally:
            h.transport.after_send = h.venue.respond
            for message in held_cancels:
                h.venue.respond(message)
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["ticket-quantity", "foreign-ticket"])
def test_joint_venue_drift_blocks_both_owners_without_auto_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    async def scenario() -> None:
        h = _Joint(tmp_path, monkeypatch, limit=8)
        try:
            await h.start()
            await h.until(lambda: bool(h.active(h.maker)) and bool(h.active(h.taker)),
                          direction=1)
            for strategy in (h.maker, h.taker):
                cid, = h.active(strategy)
                h.venue.fill(cid, D(2))
            await h.settled(2)
            before_native = deepcopy(_native(h.node))
            before_calls = (len(h.venue.attempts), len(h.wire.submit_calls),
                            len(h.wire.close_calls))
            if fault == "ticket-quantity":
                h.wire._positions()[0]["volume_lots"] = "0.03"
            else:
                foreign = deepcopy(h.wire._positions()[0])
                foreign.update(identifier="9000000999", ticket="9000000998", magic="1")
                h.wire._positions().append(foreign)
            actor = get_source_terminal_reconciler(h.node)
            assert not await actor.reconcile()
            assert not actor.source_submission_ready and actor.last_failure is not None
            for direction in (1, -1, 1):
                await h.market(direction)
            assert (len(h.venue.attempts), len(h.wire.submit_calls),
                    len(h.wire.close_calls)) == before_calls
            assert _native(h.node) == before_native
            assert h.node.cache.check_integrity()
        finally:
            await h.close()

    asyncio.run(scenario())
