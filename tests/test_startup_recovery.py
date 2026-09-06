"""Ordinary startup with real adapters and synthetic native/venue history.

No live account or EA is used; process durability has its separate Redis tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from continuous_mt5_wire import ContinuousMt5Wire
from nautilus_trader.cache.cache import Cache
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import ExecutionMassStatus, PositionStatusReport
from nautilus_trader.model.enums import OmsType, OrderSide, OrderStatus, PositionSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import AccountId, ClientId, ClientOrderId, VenueOrderId
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from test_adapter_continuity import (
    _accepted_source,
    _continuous,
    _drive,
    _market,
    _settle_cycle,
    _SourceWire,
)
from test_mt5_v1_execution import _identity, _snapshot
from test_strategy_continuity import _OrdinaryStrategy, _pump

from py000_nautilus import store as store_module
from py000_nautilus.accounting_report import build_run_accounting_report
from py000_nautilus.app import _source_instrument
from py000_nautilus.bitfinex_v1_reports import map_position_status_reports
from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import HedgeIntent, ObligationStatus
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.restart_recovery import _positions, reconcile_startup


async def _check(h: _OrdinaryStrategy) -> None:
    await reconcile_startup(
        h.node.cache, h.strategy._state_store if h.maker else h.store,
        trader_id=h.node.trader.id, strategy_id=h.strategy.id,
        source=h.source, hedge=h.hedge,
        source_instrument_id=h.source_instrument.id, hedge_instrument_id=h.hedge_instrument.id,
    )


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("history", ["closed", "open", "opposing-open"])
def test_cold_native_external_history_is_checked_before_business_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, history: str,
) -> None:
    async def scenario() -> None:
        h = _OrdinaryStrategy(tmp_path, monkeypatch, maker=maker, two_sided=maker,
                              native_mt5_transport=True, inject_mt5_io=False)
        snapshot = _snapshot(_identity())
        snapshot["positions"] = []
        cast(JsonObject, snapshot["execution_limits"])["max_order_lots"] = "0.02"
        wire = ContinuousMt5Wire(_identity(), snapshot, now_ns=h.node.kernel.clock.timestamp_ns)
        h.hedge._transport = wire
        source = _SourceWire(h)
        await wire.submit_market_delta(wire.identity.binding(), client_request_id="OLD-OPEN",
                                       side="buy", quantity_lots="0.02")
        position = cast(list[JsonObject], wire.current_snapshot["positions"])[0]
        if history == "closed":
            await wire.close_position(
                wire.identity.binding(), client_request_id="OLD-CLOSE", side="sell",
                quantity_lots="0.02", position_ticket=str(position["ticket"]),
                position_identifier=str(position["identifier"]),
            )
        elif history == "opposing-open":
            await wire.submit_market_delta(wire.identity.binding(), client_request_id="OLD-SELL",
                                           side="sell", quantity_lots="0.02")
        # Only synthetic venue setup above. Ordinary runtime starts with no native orders.
        wire.submit_calls.clear()
        wire.close_calls.clear()
        assert h.node.cache.orders() == []
        owner = get_source_terminal_reconciler(h.node)
        assert bool(owner.restart_pending) is False
        try:
            h.hedge.connect()
            async with asyncio.timeout(2):
                while h.hedge._poll_task is None:
                    await asyncio.sleep(.005)
            await h.start(initial_reconciliation=True)
            await _drive(h, wire, lambda: not owner.busy and (
                not owner.restart_pending or owner.last_failure is not None
            ), direction=0)
            if history != "closed":
                assert owner.restart_pending, "cold external exposure escaped startup check"
                assert owner.last_failure is not None
                assert not owner.source_submission_ready
                await _market(h, wire, 1)
                assert not source.rows and not wire.submit_calls and not wire.close_calls
                return
            assert not owner.restart_pending, owner.last_failure
            await _check(h)
            old = {order.client_order_id: tuple(event.id for event in order.events)
                   for order in h.node.cache.orders()}
            cid = await _accepted_source(h, source, wire, 2)
            source.fill(cid, h.source_quantity)
            await _settle_cycle(h, source, wire, cid=cid, expected=1)
            await _check(h)
            report = build_run_accounting_report(
                h.node.cache, h.source, h.hedge, trader_id=h.node.trader.id,
                strategy_id=h.strategy.id, fx=h.strategy.config.economics.fx,
            )
            assert report.status == "FINAL", report.pending_reasons
            assert old == {order.client_order_id: tuple(event.id for event in order.events)
                           for order in h.node.cache.orders() if order.strategy_id.is_external()}
            assert len(wire.submit_calls) == 1 and not wire.close_calls
        finally:
            await h.hedge._disconnect()
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", [None, "missing", "duplicate", "nonzero", "wrong-side"])
def test_startup_netting_requires_one_explicit_flat_report(fault: str | None) -> None:
    instrument = _source_instrument()
    account = AccountId("BITFINEX-269312")
    mass = ExecutionMassStatus(
        client_id=ClientId("BITFINEX"), account_id=account, venue=instrument.id.venue,
        report_id=UUID4(), ts_init=1,
    )
    reports = map_position_status_reports(
        rows=[], instrument=instrument, account_id=account, ts_init=1,
    )
    if fault == "missing":
        reports = []
    elif fault == "duplicate":
        reports *= 2
    elif fault in {"nonzero", "wrong-side"}:
        reports = [PositionStatusReport(
            account_id=account, instrument_id=instrument.id, position_side=PositionSide.LONG,
            quantity=instrument.make_qty(1 if fault == "nonzero" else 0),
            report_id=UUID4(), ts_last=1, ts_init=1,
        )]
    mass.add_position_reports(reports)
    if fault is None:
        assert _positions(Cache(), mass, instrument.id, netting=True) == 0
    else:
        with pytest.raises(ValueError, match="NETTING position differs"):
            _positions(Cache(), mass, instrument.id, netting=True)


@asynccontextmanager
async def _settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, maker: bool,
    deltas: tuple[int, ...] = (2,),
) -> AsyncIterator[tuple[_OrdinaryStrategy, _SourceWire, ContinuousMt5Wire]]:
    async with _continuous(
        tmp_path, monkeypatch, maker=maker, long_quantity=2, short_quantity=2,
    ) as (h, source, wire):
        for expected, delta in enumerate(deltas, 1):
            cid = await _accepted_source(h, source, wire, delta)
            source.fill(cid, h.source_quantity)
            await _settle_cycle(h, source, wire, cid=cid, expected=expected)
        assert await h.node.kernel.exec_engine.reconcile_execution_state(timeout_secs=2)
        h.source.confirm_terminal_reconciliation()
        yield h, source, wire


class _History:
    """Copy native events and synthetic venue facts, never business progress."""

    def __init__(
        self, h: _OrdinaryStrategy, source: _SourceWire, wire: ContinuousMt5Wire,
    ) -> None:
        cache = h.node.cache
        self.orders = [(tuple(order.events), cache.client_id(order.client_order_id),
                        cache.position_id(order.client_order_id)) for order in cache.orders()]
        self.positions = [(position.instrument_id, tuple(position.events))
                          for position in cache.positions()]
        self.source_facts = deepcopy((source.rows, source.trades, source.net, source.average_price))
        self.hedge_facts = deepcopy((
            wire.identity, wire.current_snapshot, wire.journal, wire._serial,
        ))

    def restore(
        self, second: _OrdinaryStrategy, fault: str | None = None,
    ) -> tuple[_SourceWire, ContinuousMt5Wire]:
        # Materialize a fresh native cache from native events/indices. This is
        # explicitly not a Redis durability or new-process claim.
        for events, client, position_id in ([] if fault == "missing-native" else self.orders):
            order = OrderUnpacker.from_init(events[0])
            for event in events[1:]:
                order.apply(event)
            second.node.cache.add_order(order, client_id=client, position_id=position_id)
            second.node.cache.update_order(order)
        for instrument_id, events in ([] if fault == "missing-native" else self.positions):
            position = Position(second.node.cache.instrument(instrument_id), events[0])
            for event in events[1:]:
                position.apply(event)
            second.node.cache.add_position(
                position, OmsType.NETTING if instrument_id == second.source_instrument.id
                else OmsType.HEDGING,
            )
            # add_position initializes the open index. Restore the actual
            # open/closed classification through the same native update API;
            # this never fills a missing order-to-position secondary index.
            second.node.cache.update_position(position)
        source = _SourceWire(second)
        source.rows, source.trades, source.net, source.average_price = deepcopy(self.source_facts)
        if fault == "missing-source":
            source.rows.clear()
        source.publish()
        identity, snapshot, journal, serial = deepcopy(self.hedge_facts)
        wire = ContinuousMt5Wire(identity, snapshot, now_ns=second.node.kernel.clock.timestamp_ns)
        wire.journal, wire._serial = journal, serial
        second.hedge._transport = wire
        return source, wire


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_settled_ordinary_history_passes_complete_startup_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=maker) as (h, _, _):
            await _check(h)
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("deltas", [(2, 2), (2, -2), (2, -2, 2), (2, -2, 2, -2)],
                         ids=["same-way", "flat", "reopened", "flat-again"])
def test_new_ordinary_node_resumes_native_netting_history_without_repairing_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, deltas: tuple[int, ...],
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=maker, deltas=deltas) as (
            first, source, wire,
        ):
            cache = first.node.cache
            indexes = {order.client_order_id: cache.position_id(order.client_order_id)
                       for order in cache.orders()}
            assert cache.check_integrity()
            assert any(order.position_id is not None and indexes[order.client_order_id] is None
                       for order in cache.orders(instrument_id=first.source_instrument.id))
            await _check(first)  # The original live-cache guard rejected the real close here.
            assert indexes == {order.client_order_id: cache.position_id(order.client_order_id)
                               for order in cache.orders()}
            history = _History(first, source, wire)
            business_store = first.strategy._state_store if maker else first.store
            business = deepcopy(business_store._to_payload())
            positions = {position.id: (position.signed_decimal_qty(),
                                      tuple(event.id for event in position.events))
                         for position in cache.positions()}
        second = _OrdinaryStrategy(
            tmp_path, monkeypatch, maker=maker, two_sided=maker,
            native_mt5_transport=True, inject_mt5_io=False,
        )
        assert get_source_terminal_reconciler(second.node).restart_pending
        owner = get_source_terminal_reconciler(second.node)
        source2, wire2 = history.restore(second)
        try:
            second.hedge.connect()
            async with asyncio.timeout(2):
                while second.hedge._poll_task is None:
                    await asyncio.sleep(.005)
            await second.start(initial_reconciliation=True)
            await _drive(second, wire2, lambda: not owner.busy and (
                not owner.restart_pending or owner.last_failure is not None
            ), direction=0)
            assert not owner.restart_pending, owner.last_failure
            business_store = second.strategy._state_store if maker else second.store
            assert business_store._to_payload() == business
            assert indexes == {
                order.client_order_id: second.node.cache.position_id(order.client_order_id)
                for order in second.node.cache.orders()
            }
            assert positions == {
                position.id: (position.signed_decimal_qty(),
                              tuple(event.id for event in position.events))
                for position in second.node.cache.positions()
            }
            assert {order.client_order_id: tuple(event.id for event in order.events)
                    for order in second.node.cache.orders()} == {
                events[0].client_order_id: tuple(event.id for event in events)
                for events, _, _ in history.orders
            }
            assert not wire2.submit_calls and not wire2.close_calls
            assert not second.source_cancel_commands
            cid = await _accepted_source(second, source2, wire2, 2)
            assert cid not in history.source_facts[0]
            source2.fill(cid, second.source_quantity)
            await _settle_cycle(second, source2, wire2, cid=cid, expected=len(deltas) + 1)
            assert len(wire2.submit_calls) == 1 and not wire2.close_calls
        finally:
            await second.hedge._disconnect()
            await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("cut,fault", [
    ("unbound", None), ("planned", None), ("between-legs", None), ("current-filled", None),
    ("between-legs", "old-hold"), ("unbound", "before-publish"),
    ("current-filled", "after-publish"),
])
def test_startup_continues_only_unbound_remaining_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str, fault: str | None, maker: bool,
) -> None:
    submit_method = "_submit_hedge" if maker else "_submit_hedge_intent"

    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=maker, long_quantity=4, short_quantity=2,
        ) as (first, source, wire):
            initial = await _accepted_source(first, source, wire, -2)
            assert source.order(initial).quantity.as_decimal() == 2
            source.fill(initial, Decimal(2))
            await _settle_cycle(first, source, wire, cid=initial, expected=1)
            original_submit = getattr(first.strategy, submit_method)

            def stop_before_binding(*args: Any) -> None:
                intent: HedgeIntent = args[-1]
                # Inject a stop at an existing durable boundary; never undo a
                # binding or pretend that a previously sent request was absent.
                if cut == "planned":
                    coordinator = (first.strategy._hedges[args[0]] if maker
                                   else first.strategy._hedges)
                    positions = (first.strategy._hedge_positions(args[1]) if maker
                                 else first.strategy._hedge_positions())
                    coordinator.next_hedge_leg(intent.intent_id, positions)
                if cut in {"unbound", "planned"} or (
                    cut == "between-legs" and intent.hedge_leg_index == 1
                ):
                    return
                original_submit(*args)

            monkeypatch.setattr(first.strategy, submit_method, stop_before_binding)
            if cut == "current-filled":
                verify_serial = wire.before_mutation
                assert verify_serial is not None

                async def stop_business_delivery(payload: JsonObject) -> None:
                    await verify_serial(payload)
                    first.strategy.bind_restart_gate(lambda: True)

                wire.before_mutation = stop_business_delivery
            cid = await _accepted_source(first, source, wire, 4)
            assert source.order(cid).quantity.as_decimal() == 4
            source.fill(cid, Decimal(4))

            def captured() -> bool:
                if len(first.store.intents()) != (1 if maker else 2):
                    return False
                intent = first.store.intents()[-1]
                if cut == "between-legs":
                    return intent.hedge_leg_index == 1 and intent.hedge_client_order_id is None
                if cut == "current-filled":
                    return bool(intent.hedge_client_order_id) and (
                        first.node.cache.order(ClientOrderId(intent.hedge_client_order_id)).status
                        is OrderStatus.FILLED
                    )
                return bool(intent.hedge_plan) is (cut == "planned")

            await _drive(first, wire, captured, direction=0)
            assert await first.node.kernel.exec_engine.reconcile_execution_state(timeout_secs=2)
            first.source.confirm_terminal_reconciliation()
            intent_before = first.store.intents()[-1]
            assert len(wire.submit_calls) == 1  # Only the initial BUY2 hedge open.
            assert len(wire.close_calls) == int(cut in {"between-legs", "current-filled"})
            assert first.store.halt_reason is None
            assert bool(first.store.source_freeze_reason) is maker
            if maker:
                assert first.strategy._state_store.cycle_freeze_only
            if fault == "old-hold":
                first.store._state.halt_reason = "operator inspection still required"
                first.store._persist()
            history = _History(first, source, wire)
            old_positions = {position.id: (position.signed_decimal_qty(),
                                           tuple(event.id for event in position.events))
                             for position in first.node.cache.positions()}

        second = _OrdinaryStrategy(
            tmp_path, monkeypatch, maker=maker, source_quantity=4, source_short_quantity=2,
            two_sided=maker,
            native_mt5_transport=True, inject_mt5_io=False,
        )
        owner = get_source_terminal_reconciler(second.node)
        source2, wire2 = history.restore(second)
        dispatch_enabled = False
        next_submit = getattr(second.strategy, submit_method)

        async def verify_remaining_serial(payload: JsonObject) -> None:
            request_id = str(payload["client_request_id"])
            assert request_id not in {events[0].client_order_id.value
                                      for events, _, _ in history.orders}
            assert set(source2.rows) == set(history.source_facts[0])
            assert all(order.status is OrderStatus.FILLED for order in
                       second.node.cache.orders(instrument_id=second.hedge_instrument.id)
                       if order.client_order_id.value != request_id)

        wire2.before_mutation = verify_remaining_serial

        def observe_before_dispatch(*args: Any) -> None:
            assert not second.store.can_submit_source()
            assert set(source2.rows) == set(history.source_facts[0])
            if dispatch_enabled:
                next_submit(*args)

        monkeypatch.setattr(second.strategy, submit_method, observe_before_dispatch)
        publications = 0

        def publish(candidate: Path, destination: Path) -> None:
            nonlocal publications
            if destination == second.store.path and second.store.halt_reason is None:
                publications += 1
                if publications == 1:
                    if fault == "after-publish":
                        os.replace(candidate, destination)
                        raise ParentDirectorySyncError("pending candidate published")
                    raise OSError("pending candidate not yet published")
            replace_and_sync_parent(candidate, destination)

        if fault in {"before-publish", "after-publish"}:
            monkeypatch.setattr(store_module, "replace_and_sync_parent", publish)
        try:
            second.hedge.connect()
            async with asyncio.timeout(2):
                while second.hedge._poll_task is None:
                    await asyncio.sleep(.005)
            await second.start(initial_reconciliation=True)
            await _drive(second, wire2, lambda: not owner.busy and (
                not owner.restart_pending or owner.last_failure is not None
            ), direction=0)
            resumed = second.store.intent(intent_before.intent_id)
            assert resumed.intent_id == intent_before.intent_id
            assert resumed.hedge_plan == intent_before.hedge_plan
            assert resumed.hedge_order_ids == intent_before.hedge_order_ids
            assert {order.client_order_id: tuple(event.id for event in order.events)
                    for order in second.node.cache.orders()} == {
                events[0].client_order_id: tuple(event.id for event in events)
                for events, _, _ in history.orders
            }
            assert {position.id: (position.signed_decimal_qty(),
                                  tuple(event.id for event in position.events))
                    for position in second.node.cache.positions()} == old_positions
            assert all(second.node.cache.position_id(events[0].client_order_id) == position_id
                       for events, _, position_id in history.orders)
            assert not wire2.submit_calls and not wire2.close_calls
            assert not second.source_cancel_commands
            assert second.reload_stores()[0]._state == second.store._state
            assert not second.store.can_submit_source()
            if fault in {"old-hold", "after-publish"}:
                assert owner.restart_pending and owner.last_failure is not None
                if fault == "old-hold":
                    assert second.store.halt_reason == "operator inspection still required"
                else:
                    assert resumed.status is ObligationStatus.PENDING and publications == 1
                    assert owner._restart_recovery is not None
                    with pytest.raises(RuntimeError, match="invalid after publication"):
                        await owner._restart_recovery()
                    assert publications == 1 and owner.restart_pending
                return
            if owner.restart_pending:
                # Expose the exact ordinary recovery exception on a RED run,
                # instead of hiding it behind the Actor's bounded failure label.
                assert owner._restart_recovery is not None
                await owner._restart_recovery()
            assert not owner.restart_pending, owner.last_failure
            assert resumed.status is ObligationStatus.PENDING
            assert resumed.hedge_client_order_id is None and resumed.hedge_leg_filled_ounces == 0
            assert resumed.hedge_leg_index == len(resumed.hedge_order_ids)
            if maker:
                assert second.strategy._state_store.cycle_freeze_only
                assert all(view.source_freeze_reason for view in second.strategy._stores.values())
            assert resumed.hedge_filled_ounces == (
                Decimal(2) if cut in {"between-legs", "current-filled"} else Decimal(0)
            )
            if fault == "before-publish":
                assert publications == 2
            for direction in (1, -1):
                await _market(second, wire2, direction)
                assert set(source2.rows) == set(history.source_facts[0])
                assert not wire2.submit_calls and not wire2.close_calls
            dispatch_enabled = True
            await _settle_cycle(second, source2, wire2, cid=cid, expected=2)
            assert len(wire2.submit_calls) == 1
            assert len(wire2.close_calls) == int(cut in {"unbound", "planned"})
            completed = second.store.intent(intent_before.intent_id)
            assert completed.hedge_filled_ounces == completed.hedge_quantity_ounces == 4
            assert completed.hedge_order_ids[:len(intent_before.hedge_order_ids)] == (
                intent_before.hedge_order_ids
            )
            monkeypatch.setattr(second.strategy, submit_method, next_submit)
            wire2.before_mutation = None
            next_cid = await _accepted_source(second, source2, wire2, -2)
            assert next_cid not in history.source_facts[0]
            source2.fill(next_cid, Decimal(2))
            await _settle_cycle(second, source2, wire2, cid=next_cid, expected=3)
            assert await second.node.kernel.exec_engine.reconcile_execution_state(timeout_secs=2)
            second.source.confirm_terminal_reconciliation()
            await _check(second)
        finally:
            await second.hedge._disconnect()
            await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("fault", [None, "old-hold", "missing-source", "missing-native"])
def test_new_ordinary_node_only_resumes_complete_settled_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, fault: str | None,
) -> None:
    async def scenario() -> None:
        async with _settled(tmp_path, monkeypatch, maker=maker) as (first, source, wire):
            cache = first.node.cache
            history = _History(first, source, wire)
            old_intents = deepcopy(first.store.intents())
            old_positions = {position.id: position.signed_decimal_qty()
                             for position in cache.positions_open()}
        second = _OrdinaryStrategy(
            tmp_path, monkeypatch, maker=maker, two_sided=maker,
            native_mt5_transport=True, inject_mt5_io=False,
        )
        owner = get_source_terminal_reconciler(second.node)
        assert owner.restart_pending  # The actual builder loaded existing business files.
        source2, wire2 = history.restore(second, fault)
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
                    set() if fault == "missing-source" else set(history.source_facts[0])
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
            assert cid not in history.source_facts[0]
            source2.fill(cid, second.source_quantity)
            await _settle_cycle(second, source2, wire2, cid=cid, expected=2)
            assert not wire2.close_calls  # Same direction appends a ticket, never auto-flattens.
            assert len(wire2.submit_calls) == 1
        finally:
            await second.hedge._disconnect()
            await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("maker,fault", [
    (False, None), (True, None), (False, "before-publish"), (False, "after-publish"),
    (True, "old-format"), (True, "same-text-external"), (True, "invalid-cost"),
    (True, "closed-session"), (True, "after-capture"),
    (True, "before-publish"), (True, "after-publish"),
], ids=["taker-resumes", "maker-cycle-resumes", "retry-before-publish", "hold-after-publish",
        "maker-old-format-held", "maker-same-text-external-held", "maker-cost-held",
        "maker-session-held", "maker-capture-revoked", "maker-retry-before-publish",
        "maker-hold-after-publish"])
def test_new_node_settles_actual_hedge_fill_with_lagging_business_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, fault: str | None,
) -> None:
    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=maker, long_quantity=2, short_quantity=2,
        ) as (first, source, wire):
            cid = await _accepted_source(first, source, wire, 2)
            verify_serial = wire.before_mutation
            assert verify_serial is not None

            async def stop_business_delivery(payload: JsonObject) -> None:
                await verify_serial(payload)
                if maker:
                    # A real healthy quote while the hedge is still pending
                    # must not relabel normal cycle waiting as an external pause.
                    rows_before = set(source.rows)
                    await _market(first, wire, 0)
                    assert first.strategy._state_store.cycle_freeze_only
                    assert set(source.rows) == rows_before
                # The venue still fills and Nautilus records its actual events;
                # only the strategy's last business update is withheld.
                first.strategy.bind_restart_gate(lambda: True)

            wire.before_mutation = stop_business_delivery
            source.fill(cid, first.source_quantity)
            await _drive(first, wire, lambda: bool(wire.submit_calls) and all(
                order.status is OrderStatus.FILLED for order in first.node.cache.orders()
            ), direction=0)
            assert await first.node.kernel.exec_engine.reconcile_execution_state(timeout_secs=2)
            first.source.confirm_terminal_reconciliation()
            old_intent = first.store.intents()[0]
            assert old_intent.status in (
                ObligationStatus.SUBMITTING, ObligationStatus.SUBMITTED, ObligationStatus.ACCEPTED,
            )
            assert old_intent.hedge_filled_ounces == 0
            assert old_intent.hedge_client_order_id == old_intent.hedge_order_ids[-1]
            assert first.store.halt_reason is None
            old_freeze = first.store.source_freeze_reason
            assert bool(old_freeze) is maker
            old_seen = first.store._state.seen_hedge_fills.copy()
            history = _History(first, source, wire)
            old_positions = {position.id: position.signed_decimal_qty()
                             for position in first.node.cache.positions_open()}
            if fault == "old-format":
                payload = json.loads(first.store.path.read_text())
                payload["schema_version"] = 3
                payload.pop("cycle_freeze_only", None)
                for direction in payload["directions"].values():
                    direction["schema_version"] = 1
                    for record in direction["hedge_intents"].values():
                        assert record.pop("rejected_attempt") is None
                first.store.path.write_text(json.dumps(payload))
            elif fault == "same-text-external":
                assert old_freeze is not None
                first.strategy._state_store.freeze_sources(old_freeze)
            elif fault == "invalid-cost":
                first.strategy.update_cost_snapshot(
                    first.strategy._carry, first.strategy._fx, 0,
                )
            elif fault == "closed-session":
                first.strategy.update_hedge_session(False, first.strategy._session_ts_ns + 1)

        second = _OrdinaryStrategy(
            tmp_path, monkeypatch, maker=maker, two_sided=maker,
            native_mt5_transport=True, inject_mt5_io=False,
        )
        owner = get_source_terminal_reconciler(second.node)
        assert owner.restart_pending
        source2, wire2 = history.restore(second)
        if fault == "after-capture":
            # The receipt was captured by the ordinary builder. A real later
            # health invalidation must revoke it even while callbacks are gated.
            second.strategy.update_cost_snapshot(
                second.strategy._carry, second.strategy._fx, 0,
            )
        publications = 0

        def publish(candidate: Path, destination: Path) -> None:
            nonlocal publications
            finalizing = (
                destination == second.store.path and second.store.halt_reason is None
                and all(intent.status is ObligationStatus.COMPLETED
                        for intent in second.store.intents())
            )
            if finalizing:
                publications += 1
                if publications == 1:
                    if fault == "after-publish":
                        os.replace(candidate, destination)
                        raise ParentDirectorySyncError("synthetic final publication sync failure")
                    raise OSError("synthetic failure before final publication")
            replace_and_sync_parent(candidate, destination)

        if fault in {"before-publish", "after-publish"}:
            monkeypatch.setattr(store_module, "replace_and_sync_parent", publish)
        try:
            second.hedge.connect()
            async with asyncio.timeout(2):
                while second.hedge._poll_task is None:
                    await asyncio.sleep(.005)
            await second.start(initial_reconciliation=True)
            await _drive(second, wire2, lambda: not owner.busy and (
                not owner.restart_pending or owner.last_failure is not None
            ), direction=0)
            current = second.store.intents()[0]
            assert current.intent_id == old_intent.intent_id
            assert current.hedge_order_ids == old_intent.hedge_order_ids
            assert current.hedge_plan == old_intent.hedge_plan
            assert current.hedge_filled_ounces == (
                old_intent.hedge_filled_ounces if fault == "after-capture"
                else current.hedge_quantity_ounces
            )
            assert {order.client_order_id: tuple(event.id for event in order.events)
                    for order in second.node.cache.orders()} == {
                events[0].client_order_id: tuple(event.id for event in events)
                for events, _, _ in history.orders
            }
            expected_seen = {
                f"{order.client_order_id.value}|{event.trade_id.value}"
                for order in second.node.cache.orders(instrument_id=second.hedge_instrument.id)
                for event in order.events if isinstance(event, OrderFilled)
            }
            assert second.store._state.seen_hedge_fills == (
                old_seen if fault == "after-capture" else expected_seen
            )
            assert {position.id: position.signed_decimal_qty()
                    for position in second.node.cache.positions_open()} == old_positions
            assert not wire2.submit_calls and not wire2.close_calls
            assert not second.source_cancel_commands
            assert set(source2.rows) == set(history.source_facts[0])
            assert second.reload_stores()[0].intents() == second.store.intents()
            if maker:
                reloaded_owner = MakerStateStore(
                    second.strategy._config.store_path_prefix,
                    str(second.source_instrument.id), str(second.hedge_instrument.id),
                )
                assert reloaded_owner._to_payload() == second.strategy._state_store._to_payload()
                assert not reloaded_owner.cycle_freeze_only
            if fault in {"old-format", "same-text-external", "invalid-cost",
                         "closed-session", "after-capture"}:
                assert owner.restart_pending and owner.last_failure is not None
                assert current.status is (
                    ObligationStatus.UNKNOWN if fault == "after-capture"
                    else ObligationStatus.BLOCKED
                )
                assert current.hedge_leg_index == old_intent.hedge_leg_index
                assert second.store.source_freeze_reason == old_freeze
                return
            assert current.status is ObligationStatus.COMPLETED
            assert current.hedge_leg_index == len(current.hedge_plan)
            assert current.hedge_client_order_id is None
            assert current.hedge_leg_filled_ounces == 0
            assert second.store.halt_reason is None and second.store.source_freeze_reason is None
            if maker:
                assert not second.strategy._state_store.cycle_freeze_only
                assert all(view.source_freeze_reason is None
                           for view in second.strategy._state_store.stores.values())
            if fault == "after-publish":
                # The Actor's existing second attempt must not certify the
                # published-looking business file after its durability failure.
                assert owner.restart_pending and owner.last_failure is not None
                assert publications == 1
                return
            assert not get_source_terminal_reconciler(second.node).restart_pending, (
                owner.last_failure
            )
            if fault == "before-publish":
                assert publications == 2
                monkeypatch.setattr(
                    store_module, "replace_and_sync_parent", replace_and_sync_parent,
                )
            next_cid = await _accepted_source(second, source2, wire2, 2)
            assert next_cid not in history.source_facts[0]
            source2.fill(next_cid, second.source_quantity)
            await _settle_cycle(second, source2, wire2, cid=next_cid, expected=2)
            assert not wire2.close_calls and len(wire2.submit_calls) == 1
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
