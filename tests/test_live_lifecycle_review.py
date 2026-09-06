"""Independent ordinary-adapter counterexamples for the online stop boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.trading.strategy import Strategy
from test_startup_recovery import _settled

from py000_nautilus import live_lifecycle
from py000_nautilus import store as store_module
from py000_nautilus.live_lifecycle import DrainingTradingNode, DrainResult, drain_strategy


@pytest.mark.parametrize("old_freeze", [None, "operator review still required"],
                         ids=["settled-control", "old-external-freeze"])
def test_maker_drain_does_not_adopt_and_clear_a_preexisting_external_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_freeze: str | None,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=True) as (h, source, wire):
            owner = h.strategy._state_store
            if old_freeze is not None:
                owner.freeze_sources(old_freeze)
                assert not owner.cycle_freeze_only
            disk = owner.path.read_bytes()
            orders = {order.client_order_id: tuple(event.id for event in order.events)
                      for order in h.node.cache.orders()}
            positions = {position.id: (position.signed_decimal_qty(),
                                      tuple(event.id for event in position.events))
                         for position in h.node.cache.positions()}
            requests = (len(source.rows), len(wire.submit_calls), len(wire.close_calls))
            result = await drain_strategy(h.node, h.strategy, timeout_seconds=.04)
            assert result.complete is (old_freeze is None), result
            assert owner.path.read_bytes() == disk
            assert all(view.source_freeze_reason == old_freeze for view in h.reload_stores())
            assert {order.client_order_id: tuple(event.id for event in order.events)
                    for order in h.node.cache.orders()} == orders
            assert {position.id: (position.signed_decimal_qty(),
                                  tuple(event.id for event in position.events))
                    for position in h.node.cache.positions()} == positions
            assert (len(source.rows), len(wire.submit_calls), len(wire.close_calls)) == requests
    asyncio.run(scenario())


@pytest.mark.parametrize("cancelled", [False, True], ids=["error", "cancelled"])
def test_native_stop_failure_cannot_leave_a_successful_drain_result(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool,
) -> None:
    # Isolate the final native boundary; ordinary-adapter drain is covered above.
    loop = asyncio.new_event_loop()
    node = DrainingTradingNode(TradingNodeConfig(timeout_post_stop=.001), loop=loop)
    node.build()
    strategy = Strategy()
    node.trader.add_strategy(strategy)
    node.bind_strategy_drain(cast(Any, strategy), timeout_seconds=.1)
    native_stop = TradingNode.stop_async
    residuals = {"retained-bounded-residual": "0.25"}

    async def completed_drain(*args: Any, **kwargs: Any) -> DrainResult:
        assert node.is_running() and strategy.is_running
        return DrainResult(True, "obligations_settled", (), residuals)

    async def failed_native_stop(self: TradingNode) -> None:
        await native_stop(self)
        raise asyncio.CancelledError if cancelled else RuntimeError("synthetic native stop failure")

    monkeypatch.setattr(live_lifecycle, "drain_strategies", completed_drain)
    monkeypatch.setattr(TradingNode, "stop_async", failed_native_stop)

    async def scenario() -> None:
        run = asyncio.create_task(node.run_async())
        try:
            async with asyncio.timeout(2):
                while not (node.is_running() and strategy.is_running):
                    await asyncio.sleep(.001)
            cancellation_propagated = False
            try:
                await node.stop_async()
            except asyncio.CancelledError:
                cancellation_propagated = True
            except RuntimeError:
                pass
            assert cancellation_propagated is cancelled
            assert not node.is_running() and not strategy.is_running
            await asyncio.wait_for(run, 2)
            assert node.drain_result is not None and not node.drain_result.complete
            assert node.drain_result.residuals == residuals
        finally:
            if node.is_running():
                await native_stop(node)
            if not run.done():
                await asyncio.wait_for(run, 2)
    try:
        loop.run_until_complete(scenario())
    finally:
        node.kernel.cancel_all_tasks()
        node.dispose()


def test_maker_drain_does_not_hide_a_failed_external_pause_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=True) as (h, source, wire):
            owner = h.strategy._state_store
            disk = owner.path.read_bytes()

            def fail_before_replace(source: Path, destination: Path) -> None:
                raise OSError("synthetic external pause publication failure")

            with monkeypatch.context() as patch:
                patch.setattr(store_module, "replace_and_sync_parent", fail_before_replace)
                with pytest.raises(OSError, match="publication failure"):
                    owner.freeze_sources("operator review still required")
            assert owner._freeze_publication_failed and h.strategy._global_obligation_block()
            assert all(view.source_freeze_reason is None for view in owner.stores.values())
            assert owner.path.read_bytes() == disk  # The original transaction rolled back.
            requests = (len(source.rows), len(wire.submit_calls), len(wire.close_calls))
            result = await drain_strategy(h.node, h.strategy, timeout_seconds=.02)
            assert not result.complete, result
            assert "maker_pause_publication_failed" in result.pending
            assert owner.path.read_bytes() == disk and owner._freeze_publication_failed
            assert (len(source.rows), len(wire.submit_calls), len(wire.close_calls)) == requests
    asyncio.run(scenario())
