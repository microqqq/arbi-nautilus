"""Online drain on ordinary strategies/adapters; venue IO is finite synthetic data."""

from __future__ import annotations

import asyncio
import signal
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from continuous_mt5_wire import ContinuousMt5Wire
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.trading.strategy import Strategy
from test_adapter_continuity import (
    _accepted_source,
    _continuous,
    _hold_source_acceptance,
    _market,
    _SourceWire,
)
from test_mt5_v1_execution import _identity, _snapshot
from test_strategy_continuity import _OrdinaryStrategy, _pump

from py000_nautilus import live_lifecycle
from py000_nautilus.live_lifecycle import DrainingTradingNode, DrainResult, drain_strategy
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.models import BusinessOrderSide, ObligationStatus, SourceDirection
from py000_nautilus.mt5_v1_protocol import JsonObject


@pytest.mark.parametrize("accept_during_drain", [False, True])
def test_maker_pre_acceptance_cancel_and_original_drain_share_one_bounded_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accept_during_drain: bool,
) -> None:
    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True, long_quantity=2, short_quantity=0,
        ) as (h, source, wire):
            held = _hold_source_acceptance(source, monkeypatch)
            await _market(h, wire, 1)
            assert len(held) == 1
            order = source.order(int(held[0][2]))
            h.strategy.update_hedge_session(False, h.node.kernel.clock.timestamp_ns())
            await _pump()
            assert order.status is OrderStatus.SUBMITTED and not h.source_cancel_commands
            draining = asyncio.create_task(drain_strategy(
                h.node, h.strategy, timeout_seconds=.15 if not accept_during_drain else 2,
            ))
            await _pump()
            assert h.strategy._draining and not h.source_cancel_commands
            if accept_during_drain:
                h.transport.queue.put_nowait([0, "on", held[0]])
            result = await draining
            assert result.complete is accept_during_drain, result
            assert h.source_cancel_commands == (
                [order.client_order_id] if accept_during_drain else []
            )
            if accept_during_drain:
                assert order.status is OrderStatus.CANCELED
            else:
                assert result.reason == "drain_timeout"
                assert f"source:{order.client_order_id}" in result.pending
                assert order.status is OrderStatus.SUBMITTED
            h.node.trader.stop()
            await _pump()
            assert not h.strategy._pending_source_cancels
            assert len(h.source_cancel_commands) == int(accept_during_drain)
            assert len(source.rows) == 1 and not wire.submit_calls and not wire.close_calls
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", [None, "old-freeze", "wal", "sibling-active", "unknown"])
def test_standalone_maker_soft_pause_drain_still_requires_all_facts_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True, long_quantity=2, short_quantity=0,
        ) as (h, source, wire):
            actor = get_source_terminal_reconciler(h.node)
            assert await actor.reconcile()
            # Isolate the existing instance-only pause state from the separate
            # market-input classification bug; all business/native facts are settled.
            h.strategy._source_hold = True
            assert all(view.cycle_evidence_complete() and view.source_freeze_reason is None
                       for view in h.strategy._state_store.all_views())
            if fault == "old-freeze":
                h.strategy._state_store.freeze_sources("Maker costs changed")
            elif fault == "wal":
                h.strategy._state_store._freeze_publication_failed = True
            elif fault in {"sibling-active", "unknown"}:
                sibling = h.strategy._stores[SourceDirection.SHORT]
                sibling.begin_source("ASK-PENDING", BusinessOrderSide.SELL, Decimal(2))
                if fault == "unknown":
                    sibling.mark_source_unknown("ASK-PENDING", "unresolved source")
            result = await drain_strategy(h.node, h.strategy, timeout_seconds=.05)
            assert result.complete is (fault is None), result
            if fault is not None:
                expected = {
                    "old-freeze": "freeze:Maker costs changed",
                    "wal": "maker_pause_publication_failed",
                    "sibling-active": "source:ASK-PENDING",
                    "unknown": "hold:unresolved source",
                }
                assert expected[fault] in result.pending
            assert h.strategy._source_hold and h.strategy._draining
            assert not source.rows and not wire.submit_calls and not wire.close_calls
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("fill_on_cancel", [False, True])
def test_drain_cancels_source_but_hedges_racing_fill_and_keeps_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, fill_on_cancel: bool,
) -> None:
    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=maker, long_quantity=2, short_quantity=0,
        ) as (h, source, wire):
            cid = await _accepted_source(h, source, wire, 2)
            original = source.respond

            def racing_response(message: Any) -> None:
                if fill_on_cancel and isinstance(message, list) and message[1] == "oc":
                    # One real venue fill after the stop request, before its cancel reply.
                    source.fill(cid, Decimal(1))
                original(message)

            h.transport.after_send = racing_response
            task = asyncio.create_task(drain_strategy(h.node, h.strategy, timeout_seconds=2))
            async with asyncio.timeout(3):
                while not task.done():
                    await _market(h, wire, 1)  # Still a genuine new-source opportunity.
                    await asyncio.sleep(.01)
            result = await task
            assert result.complete, result
            assert result.reason == "obligations_settled" and not result.pending
            assert len(source.rows) == 1  # No replacement quote/source, even with fresh ticks.
            assert h.source_cancel_commands == [source.order(cid).client_order_id]
            assert get_source_terminal_reconciler(h.node)._active
            assert h.strategy.is_running and h.source.is_connected and h.hedge.is_connected
            assert all(store.active_source_order_id is None and store.halt_reason is None
                       for store in h.reload_stores())
            if fill_on_cancel:
                assert source.net == 1
                assert len(wire.submit_calls) == 1 and not wire.close_calls
                assert all(intent.status is ObligationStatus.COMPLETED
                           for store in h.reload_stores() for intent in store.intents())
                assert sum(position.signed_qty for position in h.node.cache.positions_open(
                    instrument_id=h.hedge_instrument.id,
                )) == -1
            else:
                assert source.net == 0 and not wire.submit_calls
                assert source.order(cid).status == OrderStatus.CANCELED
            # Final native Trader stop must not issue a second cancel after drain.
            h.node.trader.stop()
            await _pump()
            assert h.source_cancel_commands == [source.order(cid).client_order_id]
    asyncio.run(scenario())


def test_drain_partial_ioc_cancel_echo_across_milliseconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _SourceWire.respond

    def delayed_cancel(self: _SourceWire, message: Any) -> None:
        if isinstance(message, list) and message[1] == "oc":
            time.sleep(.005)  # Force the cancel echo into a later clock millisecond.
        original(self, message)

    monkeypatch.setattr(_SourceWire, "respond", delayed_cancel)
    test_drain_cancels_source_but_hedges_racing_fill_and_keeps_positions(
        tmp_path, monkeypatch, maker=False, fill_on_cancel=True,
    )


@pytest.mark.parametrize("maker", [False, True])
def test_drain_timeout_retains_cancel_and_does_not_retry_or_clear_old_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=maker, long_quantity=2, short_quantity=0,
        ) as (h, source, wire):
            cid = await _accepted_source(h, source, wire, 2)
            h.transport.after_send = None  # Venue does not answer the cancel.
            result = await drain_strategy(h.node, h.strategy, timeout_seconds=.08)
            assert not result.complete and result.reason == "drain_timeout"
            assert any(item.startswith("source:") for item in result.pending)
            assert len(h.source_cancel_commands) == 1
            assert not wire.submit_calls and not wire.close_calls
            assert source.order(cid).filled_qty.as_decimal() == 0
            assert any(store.active_source_order_id is not None for store in h.reload_stores())
            h.node.trader.stop()
            await _pump()
            assert len(h.source_cancel_commands) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True])
def test_drain_startup_hold_performs_no_mutation_and_returns_specific_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=maker, long_quantity=2, short_quantity=0,
        ) as (h, source, wire):
            cid = await _accepted_source(h, source, wire, 2)
            reconciler = get_source_terminal_reconciler(h.node)
            reconciler._restart_pending = True
            old = [store._to_payload() for store in h.reload_stores()]
            result = await drain_strategy(h.node, h.strategy, timeout_seconds=.02)
            assert not result.complete and "startup_reconciliation_pending" in result.pending
            assert not h.source_cancel_commands and not wire.submit_calls
            assert [store._to_payload() for store in h.reload_stores()] == old
            assert source.order(cid).status == OrderStatus.ACCEPTED
    asyncio.run(scenario())


def test_native_signal_and_concurrent_explicit_stop_share_one_online_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes: list[DrainingTradingNode] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        node = DrainingTradingNode(TradingNodeConfig(timeout_post_stop=.01), loop=loop)
        nodes.append(node)
        node.build()
        strategy = Strategy()
        node.trader.add_strategy(strategy)
        node.bind_strategy_drain(cast(Any, strategy), timeout_seconds=.5)
        entered, release = asyncio.Event(), asyncio.Event()
        calls: list[str] = []

        async def controlled_drain(*args: Any, **kwargs: Any) -> DrainResult:
            assert node.is_running() and strategy.is_running
            calls.append("drain")
            entered.set()
            await release.wait()
            assert node.is_running() and strategy.is_running
            return DrainResult(True, "obligations_settled", (), {})

        monkeypatch.setattr(live_lifecycle, "drain_strategies", controlled_drain)
        run = asyncio.create_task(node.run_async())
        try:
            async with asyncio.timeout(2):
                while not node.is_running():
                    await asyncio.sleep(.005)
            node._loop_sig_handler(signal.SIGTERM)  # Actual native signal callback calls stop().
            await asyncio.wait_for(entered.wait(), .5)
            second = asyncio.create_task(node.stop_async())
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert node.is_running() and strategy.is_running
            release.set()
            await asyncio.wait_for(node.stop_async(), 2)
            await asyncio.wait_for(run, 2)
            assert calls == ["drain"]
            assert node.drain_result is not None and node.drain_result.complete
            assert not node.is_running() and not strategy.is_running
        finally:
            release.set()
            if node.is_running():
                await node.stop_async()
            if not run.done():
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        if nodes:
            nodes[0].kernel.cancel_all_tasks()
            nodes[0].dispose()  # Native disposal owns this stopped loop, as the CLI does.
        elif not loop.is_closed():
            loop.close()


@pytest.mark.parametrize("maker", [False, True])
def test_ordinary_node_run_and_signal_drains_actual_adapters_before_disconnection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def scenario() -> None:
        h = _OrdinaryStrategy(
            tmp_path, monkeypatch, maker=maker, source_quantity=2,
            native_mt5_transport=True, inject_mt5_io=False,
        )
        snapshot = _snapshot(_identity())
        snapshot["positions"] = []
        cast(JsonObject, snapshot["execution_limits"])["max_order_lots"] = "0.02"
        wire = ContinuousMt5Wire(_identity(), snapshot, now_ns=h.node.kernel.clock.timestamp_ns)
        h.hedge._transport = wire
        source = _SourceWire(h)
        # Replace only external IO; native kernel start, strategies and both execution clients run.
        async def connect_data() -> None:
            return None  # Fixture already seeded each data client's connected instrument snapshot.

        async def synthetic_data_reader() -> None:
            # _market supplies the finite feed; native disconnect owns cancellation.
            await asyncio.Event().wait()

        for client in (h.source_data, h.hedge_data):
            monkeypatch.setattr(client, "_connect", connect_data)
        monkeypatch.setattr(h.source_data, "_read_loop", synthetic_data_reader)
        monkeypatch.setattr(h.hedge_data, "_run_pub", synthetic_data_reader)
        monkeypatch.setattr(h.hedge_data, "_run_snapshots", synthetic_data_reader)
        await h.transport.queue.put(
            {"event": "auth", "status": "OK", "chanId": 0, "userId": 269_312},
        )
        await h.transport.queue.put(
            [0, "ws", [["margin", h.wallet_currency, Decimal(10000), Decimal(0), Decimal(10000)]]],
        )
        run = asyncio.create_task(h.node.run_async())
        try:
            async with asyncio.timeout(4):
                while not h.node.trader.is_running or not h.strategy.is_running:
                    if run.done():
                        await run
                        raise AssertionError("ordinary native startup returned before running")
                    await asyncio.sleep(.005)
            try:
                cid = await _accepted_source(h, source, wire, 2)
            except AssertionError as exc:
                raise AssertionError({
                    "gate": getattr(h.strategy, "_last_decision_gate", None),
                    "strategy_running": h.strategy.is_running,
                    "source_data_connected": h.source_data.is_connected,
                    "source_book_actionable": h.source_data.book_is_actionable,
                    "source_execution_connected": h.source.is_connected,
                    "source_accounting_ready": h.source.accounting_ready,
                    "hedge_data_connected": h.hedge_data.is_connected,
                    "hedge_snapshot_healthy": h.hedge_data.snapshot_refresh_healthy,
                    "hedge_execution_connected": h.hedge.is_connected,
                    "hedge_execution_admitted": h.hedge.execution_admitted,
                    "source_gate": get_source_terminal_reconciler(h.node).source_submission_ready,
                }) from exc
            original = source.respond

            def racing_response(message: Any) -> None:
                if isinstance(message, list) and message[1] == "oc":
                    assert h.node.is_running() and h.strategy.is_running
                    assert h.source.is_connected and h.hedge.is_connected
                    source.fill(cid, Decimal(1))
                original(message)

            h.transport.after_send = racing_response
            h.node._loop_sig_handler(signal.SIGTERM)
            # Native run_async catches CancelledError. wait_for would cancel all its engine
            # queues at 4s and silently return, sabotaging a still-online 10s drain.
            done, _ = await asyncio.wait({run}, timeout=4)
            intermediate = None
            if not done:
                intermediate = live_lifecycle._snapshot(h.node, h.strategy)
                done, _ = await asyncio.wait({run}, timeout=10)
            assert done, (intermediate, live_lifecycle._snapshot(h.node, h.strategy))
            await run
            result = cast(DrainingTradingNode, h.node).drain_result
            assert result is not None and result.complete, (
                result, cast(DrainingTradingNode, h.node)._drain_stop_task,
                h.node.is_running(), h.strategy.is_running,
                cast(DrainingTradingNode, h.node)._drain_strategy is h.strategy,
                intermediate,
            )
            assert not h.node.is_running() and not h.strategy.is_running
            assert not h.source.is_connected and not h.hedge.is_connected
            assert len(source.rows) == 1 and len(wire.submit_calls) == 1
            assert not wire.close_calls and source.net == 1
            assert len(h.source_cancel_commands) == 1
        finally:
            if h.node.is_running():
                await h.node.stop_async()
            if not run.done():
                run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            await h.close()
    asyncio.run(scenario())
