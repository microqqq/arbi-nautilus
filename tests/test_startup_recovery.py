"""Ordinary startup with real adapters and synthetic native/venue history.

No live account or EA is used; process durability has its separate Redis tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from continuous_mt5_wire import ContinuousMt5Wire
from nautilus_trader.execution.reports import ExecutionMassStatus
from nautilus_trader.model.enums import OmsType, OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from test_adapter_continuity import (
    _accepted_source,
    _continuous,
    _drive,
    _settle_cycle,
    _SourceWire,
)
from test_strategy_continuity import _OrdinaryStrategy, _pump

from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.models import ObligationStatus
from py000_nautilus.restart_recovery import reconcile_startup


async def _check(h: _OrdinaryStrategy) -> None:
    await reconcile_startup(
        h.node.cache, h.strategy._state_store if h.maker else h.store,
        trader_id=h.node.trader.id, strategy_id=h.strategy.id,
        source=h.source, hedge=h.hedge,
        source_instrument_id=h.source_instrument.id, hedge_instrument_id=h.hedge_instrument.id,
    )


@asynccontextmanager
async def _settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, maker: bool,
) -> AsyncIterator[tuple[_OrdinaryStrategy, _SourceWire, ContinuousMt5Wire]]:
    async with _continuous(
        tmp_path, monkeypatch, maker=maker, long_quantity=2, short_quantity=2,
    ) as (h, source, wire):
        cid = await _accepted_source(h, source, wire, 2)
        source.fill(cid, h.source_quantity)
        await _settle_cycle(h, source, wire, cid=cid, expected=1)
        assert await h.node.kernel.exec_engine.reconcile_execution_state(timeout_secs=2)
        h.source.confirm_terminal_reconciliation()
        yield h, source, wire


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_settled_ordinary_history_passes_complete_startup_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=maker) as (h, _, _):
            await _check(h)
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("fault", [None, "old-hold", "missing-source", "missing-native"])
def test_new_ordinary_node_only_resumes_complete_settled_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, fault: str | None,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=maker) as (first, source, wire):
            cache = first.node.cache
            orders = [(tuple(order.events), cache.client_id(order.client_order_id),
                       cache.position_id(order.client_order_id)) for order in cache.orders()]
            positions = [(position.instrument_id, tuple(position.events))
                         for position in cache.positions()]
            source_facts = deepcopy((source.rows, source.trades, source.net, source.average_price))
            hedge_facts = deepcopy((
                wire.identity, wire.current_snapshot, wire.journal, wire._serial,
            ))
            old_intents = deepcopy(first.store.intents())
            old_positions = {position.id: position.signed_decimal_qty()
                             for position in cache.positions_open()}
        second = _OrdinaryStrategy(
            tmp_path, monkeypatch, maker=maker, two_sided=maker,
            native_mt5_transport=True, inject_mt5_io=False,
        )
        owner = get_source_terminal_reconciler(second.node)
        assert owner.restart_pending  # The actual builder loaded existing business files.
        # Materialize a fresh native cache from native events/indices. This is
        # explicitly not a Redis durability or new-process claim.
        for events, client, position_id in ([] if fault == "missing-native" else orders):
            order = OrderUnpacker.from_init(events[0])
            for event in events[1:]:
                order.apply(event)
            second.node.cache.add_order(order, client_id=client, position_id=position_id)
            second.node.cache.update_order(order)
        for instrument_id, events in ([] if fault == "missing-native" else positions):
            position = Position(second.node.cache.instrument(instrument_id), events[0])
            for event in events[1:]:
                position.apply(event)
            second.node.cache.add_position(
                position, OmsType.NETTING if instrument_id == second.source_instrument.id
                else OmsType.HEDGING,
            )
        source2 = _SourceWire(second)
        source2.rows, source2.trades, source2.net, source2.average_price = deepcopy(source_facts)
        if fault == "missing-source":
            source2.rows.clear()
        source2.publish()
        identity, snapshot, journal, serial = hedge_facts
        wire2 = ContinuousMt5Wire(identity, snapshot, now_ns=second.node.kernel.clock.timestamp_ns)
        wire2.journal, wire2._serial = journal, serial
        second.hedge._transport = wire2
        if fault == "old-hold":
            second.store._state.halt_reason = "external operator hold"
            second.store._persist()
        old_state = deepcopy(second.store._state)
        try:
            assert second.node.cache.check_integrity()
            second.hedge.connect()
            async with asyncio.timeout(2):
                while second.hedge._poll_task is None:
                    await asyncio.sleep(.005)
            await second.start(initial_reconciliation=fault is None)
            if fault is not None:
                await _drive(
                    second, wire2, lambda: not owner.busy and owner.last_failure is not None,
                    direction=0,
                )
                assert owner.restart_pending
                assert second.store._state == old_state
                assert second.reload_stores()[0]._state == old_state
                assert not wire2.submit_calls and not wire2.close_calls
                assert not second.source_cancel_commands
                assert set(source2.rows) == (
                    set() if fault == "missing-source" else set(source_facts[0])
                )
                return
            try:
                await _drive(second, wire2, lambda: not owner.restart_pending, direction=0)
            except TimeoutError:
                await _check(second)  # Expose the concrete synthetic-history mismatch.
                raise AssertionError(owner.last_failure) from None
            assert owner.last_failure is None
            assert second.store.intents() == old_intents
            assert {position.id: position.signed_decimal_qty()
                    for position in second.node.cache.positions_open()} == old_positions
            assert not wire2.submit_calls and not wire2.close_calls
            assert not second.source_cancel_commands
            cid = await _accepted_source(second, source2, wire2, 2)
            assert cid not in source_facts[0]
            source2.fill(cid, second.source_quantity)
            await _settle_cycle(second, source2, wire2, cid=cid, expected=2)
            assert not wire2.close_calls  # Same direction appends a ticket, never auto-flattens.
            assert len(wire2.submit_calls) == 1
        finally:
            await second.hedge._disconnect()
            await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_newer_private_position_cannot_be_certified_by_older_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=maker) as (h, _, _):
            h.strategy.bind_restart_gate(lambda: True)
            row = deepcopy(cast(list[Any], h.rest.position_rows[0]))
            row[2], row[13] = Decimal(3), int(row[13]) + 1
            h.source._consume_private_frame([0, "pu", row])
            with pytest.raises(ValueError, match="current private observation"):
                await _check(h)
            assert h.source._margin_position.quantity == 3
            assert not h.source._margin_positions_current
            assert not h.source._margin_positions_complete
    asyncio.run(scenario())


@pytest.mark.parametrize("when", ["during-query", "before-query"])
def test_retired_late_tu_blocks_recovery_without_native_event_or_action_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=False) as (h, source, _):
            h.strategy.bind_restart_gate(lambda: True)
            # A cold client has no live entry for this cached closed order.
            # Arrange the same retired state via the adapter's exact terminal
            # verifier/retirement, without replacing its late-TU handler.
            live = h.source._by_cid[next(iter(source.rows))]
            h.source._retire_reconciled_terminal(
                live, h.source._confirmed_terminal_quantity(live),
            )
            events = tuple(event.id for order in h.node.cache.orders() for event in order.events)
            revision = h.source._account_action_revision
            trade = deepcopy(source.trades[0])
            trade[0] += 1  # An extra authenticated real TradeId on an already retired CID.
            assert next(iter(source.rows)) not in h.source._by_cid

            def late_tu() -> None:
                h.source._consume_private_frame([0, "tu", trade])

            if when == "during-query":
                original = h.source.generate_mass_status

                async def read_then_tu(lookback: int | None = None) -> ExecutionMassStatus | None:
                    mass = await original(lookback)
                    late_tu()
                    return cast(ExecutionMassStatus | None, mass)

                monkeypatch.setattr(h.source, "generate_mass_status", read_then_tu)
            else:
                late_tu()
            expected = "changed during report collection" if when == "during-query" else "final fee"
            with pytest.raises(ValueError, match=expected):
                await _check(h)
            assert h.source._account_action_revision == revision
            assert tuple(event.id for order in h.node.cache.orders()
                         for event in order.events) == events
            assert h.source.accounting_ready and not h.source.fee_summary().complete
    asyncio.run(scenario())


def test_offsetting_native_fills_during_read_are_not_hidden_by_unchanged_net_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=False) as (h, _, _):
            h.strategy.bind_restart_gate(lambda: True)
            original = h.source.generate_mass_status
            position = h.node.cache.positions_open(instrument_id=h.source_instrument.id)[0]
            before = position.signed_decimal_qty()
            event_count = len(position.events)

            async def read_then_fills(lookback: int | None = None) -> ExecutionMassStatus | None:
                mass = await original(lookback)
                for index, side in enumerate((OrderSide.SELL, OrderSide.BUY)):
                    # Native event delivery only: two source executions whose
                    # aggregate quantity returns to its pre-read value.
                    order = TestExecStubs.limit_order(
                        instrument=h.source_instrument, trader_id=h.node.trader.id,
                        strategy_id=h.strategy.id, client_order_id=ClientOrderId(f"RACE-{index}"),
                        order_side=side, quantity=h.source_instrument.make_qty(1),
                    )
                    h.node.cache.add_order(order, client_id=h.source.id, position_id=position.id)
                    engine = h.node.kernel.exec_engine
                    engine.process(TestEventStubs.order_submitted(order, h.source.account_id))
                    engine.process(TestEventStubs.order_accepted(
                        order, h.source.account_id, VenueOrderId(f"RACE-{index}"),
                    ))
                    fill = TestEventStubs.order_filled(
                        order, h.source_instrument, account_id=h.source.account_id,
                        venue_order_id=VenueOrderId(f"RACE-{index}"), position_id=position.id,
                        last_px=h.source_instrument.make_price(position.avg_px_open),
                        ts_event=h.node.kernel.clock.timestamp_ns(),
                    )
                    values = OrderFilled.to_dict(fill)
                    values["trader_id"] = h.node.trader.id.value
                    engine.process(OrderFilled.from_dict(values))
                await _pump()
                return cast(ExecutionMassStatus | None, mass)

            monkeypatch.setattr(h.source, "generate_mass_status", read_then_fills)
            with pytest.raises(ValueError, match="changed during report collection"):
                await _check(h)
            assert position.signed_decimal_qty() == before
            assert len(position.events) == event_count + 2
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "cancellation"])
def test_report_group_joins_pending_child_before_recovery_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=False) as (h, _, _):
            entered, finished = asyncio.Event(), asyncio.Event()

            async def pending(lookback: int | None = None) -> ExecutionMassStatus | None:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished.set()
                return None

            async def fail(lookback: int | None = None) -> ExecutionMassStatus | None:
                await entered.wait()
                if failure == "exception":
                    raise RuntimeError("synthetic report failure")
                await asyncio.Event().wait()
                return None

            monkeypatch.setattr(h.source, "generate_mass_status", pending)
            monkeypatch.setattr(h.hedge, "generate_mass_status", fail)
            task = asyncio.create_task(_check(h))
            await entered.wait()
            if failure == "cancellation":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ExceptionGroup, match="TaskGroup"):
                    await task
            assert finished.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_missing_source_suffix_publishes_held_even_if_hedge_projection_cannot_bind_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=maker, long_quantity=2, short_quantity=2,
        ) as (h, source, wire):
            cid = await _accepted_source(h, source, wire, 2)
            checkpoint = (
                h.strategy._state_store._snapshot() if maker else deepcopy(h.store._state)
            )
            source.fill(cid, h.source_quantity)
            await _settle_cycle(h, source, wire, cid=cid, expected=1)
            assert await h.node.kernel.exec_engine.reconcile_execution_state(timeout_secs=2)
            h.strategy.bind_restart_gate(lambda: True)
            if maker:
                h.strategy._state_store._restore(checkpoint)
            else:
                h.store._state = checkpoint
            h.store._persist()
            active = h.store.active_source_order_id
            mutation_count = len(wire.submit_calls) + len(wire.close_calls)
            with pytest.raises(ValueError, match="complete hedge order set"):
                await _check(h)
            assert h.store.source_order(active).filled_ounces == h.source_quantity
            assert h.store.active_source_order_id == active
            assert h.store.halt_reason and h.store.source_freeze_reason
            assert h.store.intents()[0].status is ObligationStatus.BLOCKED
            assert h.reload_stores()[0]._state == h.store._state
            assert mutation_count == len(wire.submit_calls) + len(wire.close_calls)
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_missing_hedge_fill_is_projected_held_and_never_normalized_to_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=maker) as (h, _, wire):
            h.strategy.bind_restart_gate(lambda: True)
            intent = h.store.intents()[0]
            h.store._state.hedge_intents[intent.intent_id] = replace(
                intent, hedge_client_order_id=intent.hedge_order_ids[0],
                hedge_filled_ounces=Decimal(0), hedge_leg_index=0,
                hedge_leg_filled_ounces=Decimal(0), status=ObligationStatus.UNKNOWN,
            )
            h.store._state.seen_hedge_fills.clear()
            h.store._state.halt_reason = "original unresolved hedge"
            h.store._persist()  # Persist an older known prefix.
            mutation_count = len(wire.submit_calls) + len(wire.close_calls)
            with pytest.raises(ValueError, match="held or unsettled"):
                await _check(h)
            projected = h.store.intents()[0]
            assert projected.status is ObligationStatus.BLOCKED
            assert projected.hedge_filled_ounces == projected.hedge_quantity_ounces
            assert projected.hedge_leg_index == 0
            assert h.store.halt_reason == "original unresolved hedge"
            assert h.reload_stores()[0].intents() == h.store.intents()
            assert mutation_count == len(wire.submit_calls) + len(wire.close_calls)
            with pytest.raises(ValueError, match="held or unsettled"):
                await _check(h)
            assert h.store.intents()[0] == projected
    asyncio.run(scenario())
