"""Native Actor/client task ownership with offline, controlled reconciliation."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import uvloop
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import TestClock as NativeTestClock
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from test_bitfinex_v1_execution import _Harness
from test_live_taker import _build as _build_live_taker
from test_live_taker import _configs as _live_taker_configs
from test_margin import _account_event
from test_taker_events import (
    RecordingTakerStrategy,
    _event_engine,
    _process_source_terminal,
    _seed_terminal_source,
    _terminal_report,
)

from py000_nautilus import live_runtime
from py000_nautilus.app import _maker_strategy_config
from py000_nautilus.bitfinex_v1_data import INSTRUMENT_ID as SOURCE_ID
from py000_nautilus.live_runtime import SourceTerminalReconciler, get_source_terminal_reconciler
from py000_nautilus.margin import LiveAccountReader
from py000_nautilus.models import BookTop, SourceDirection
from py000_nautilus.strategies._source_terminal import SourceTerminalResult
from py000_nautilus.strategies.maker import MakerStrategy


def _account_reader_probe(tmp_path: Path, *, query_failure: bool = False) -> Any:
    """Real AccountState/mappers; only current eligibility, clock and IO are synthetic."""
    config = _live_taker_configs(tmp_path).strategy
    now = 10_000_000_000
    source, hedge = _account_event(bfx=True), _account_event(bfx=False)
    source.info["bitfinex_margin"]["instrument_id"] = config.source_instrument_id.value
    source.info["bitfinex_margin"]["wallet"]["observed_ns"] = now - 1_000_000_000
    source.info["bitfinex_margin"]["positions"]["observed_ns"] = now - 500_000_000
    hedge.info["mt5_account_observed_ns"] = now - 2_000_000_000

    def bind_identity(event: AccountState, account_id: Any) -> AccountState:
        return AccountState(
            account_id=account_id, account_type=event.account_type,
            base_currency=event.base_currency, balances=[], margins=[], reported=True,
            info=event.info, event_id=UUID4(), ts_event=now, ts_init=now,
        )

    source = bind_identity(source, config.source_accounts[0].account_id)
    hedge = bind_identity(hedge, config.hedge_account_id)
    clock = NativeTestClock()
    calls: list[tuple[int, Any, Any]] = []
    errors: list[str] = []
    captured: list[LiveAccountReader] = []
    clock.set_time(now)
    book = BookTop(Decimal(3999), Decimal(4000), Decimal(10), Decimal(10))
    source_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(last_event=source), is_connected=True,
        execution_hold_reason=None, account_budget_ready=False, account_budget_refresh_ready=True,
    )
    hedge_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(last_event=hedge), execution_admitted=True,
        sample_current=True,
    )
    hedge_client.account_capacity_ready = lambda _age: hedge_client.sample_current

    def query(account_id: Any, client_id: Any) -> None:
        calls.append((clock.timestamp_ns(), account_id, client_id))
        if query_failure:
            # Reentry and a synchronous failure must neither bypass the interval
            # nor escape an account notification into an enclosing fill callback.
            current = clock.timestamp_ns()
            captured[0](book, current, book, current, True)
            raise OSError("synthetic request failure")

    strategy = SimpleNamespace(
        clock=clock, log=SimpleNamespace(error=errors.append), query_account=query,
        bind_live_account_reader=captured.append,
    )
    live_runtime.bind_live_account_reader(
        cast(Any, strategy), config=config,
        source_data=cast(Any, SimpleNamespace(is_connected=True, book_is_actionable=True)),
        source_client=cast(Any, source_client),
        hedge_data=cast(Any, SimpleNamespace(is_connected=True, snapshot_refresh_healthy=True)),
        hedge_client=cast(Any, hedge_client), wallet_currency="USTF0",
        hedge_symbol="XAUUSD", hedge_stream_id="stream-1",
    )
    return SimpleNamespace(
        read=captured[0], clock=clock, calls=calls, errors=errors, book=book,
        source=source, hedge=hedge, source_client=source_client, hedge_client=hedge_client,
        config=config, now=now,
    )


@pytest.mark.parametrize("query_failure", [False, True])
def test_live_account_reader_production_query_interval_and_failure_are_bounded(
    tmp_path: Path, query_failure: bool,
) -> None:
    probe = _account_reader_probe(tmp_path, query_failure=query_failure)
    old = deepcopy((probe.source.info, probe.hedge.info))
    assert live_runtime._ACCOUNT_QUERY_INTERVAL_NS == 2_000_000_000
    for offset in (0, 1, 1_999_999_999):
        probe.clock.set_time(probe.now + offset)
        for _ in range(20):
            view = probe.read(probe.book, probe.now + offset, probe.book, probe.now + offset, True)
            assert view is not None and view[3] is False
            assert view[2] == probe.now + 3_000_000_001
    assert len(probe.calls) == 1
    probe.clock.set_time(probe.now + 2_000_000_000)
    probe.read(probe.book, probe.clock.timestamp_ns(), probe.book, probe.clock.timestamp_ns(), True)
    assert [entry[0] for entry in probe.calls] == [probe.now, probe.now + 2_000_000_000]
    route = probe.config.source_accounts[0]
    assert all(entry[1:] == (route.account_id, route.client_id) for entry in probe.calls)
    assert len(probe.errors) == (2 if query_failure else 0)
    assert (probe.source.info, probe.hedge.info) == old


def test_live_account_reader_keeps_maintenance_separate_from_new_budget_and_current_hedge(
    tmp_path: Path,
) -> None:
    probe = _account_reader_probe(tmp_path)
    probe.source_client.account_budget_refresh_ready = False
    view = probe.read(probe.book, probe.now, probe.book, probe.now, False)
    assert view is not None and view[3] is False and not probe.calls
    probe.hedge_client.sample_current = False
    assert probe.hedge.info["mt5_account_sample_valid"] is True
    assert probe.read(probe.book, probe.now, probe.book, probe.now, False) is None
    assert not probe.calls


class _Engine:
    def __init__(self) -> None:
        self.calls = 0
        self.confirmations = 0
        self.required = False
        self.connected = True
        self.success = True
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def check_connected(self) -> bool:
        return self.connected

    async def reconcile_execution_state(self, *, timeout_secs: float) -> bool:
        assert timeout_secs > 0
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return self.success

    def confirm(self) -> None:
        self.confirmations += 1
        self.required = False


def _record(values: list[Any]) -> SourceTerminalResult:
    def complete(report: OrderStatusReport | None) -> bool:
        values.append(report)
        return True

    return complete


@contextmanager
def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout: float = 0.03,
) -> Iterator[tuple[SourceTerminalReconciler, _Harness, _Engine]]:
    harness = _Harness(cid_store_path=tmp_path / "cid.json")
    engine = _Engine()
    monkeypatch.setattr(
        type(harness.client),
        "terminal_reconciliation_required",
        property(lambda _: engine.required),
    )
    monkeypatch.setattr(harness.client, "confirm_terminal_reconciliation", engine.confirm)
    runtime = SourceTerminalReconciler(
        source_client=harness.client,
        exec_engine=cast(Any, engine),
        source_instrument_id=SOURCE_ID,
        timeout_seconds=timeout,
    )
    runtime.register_base(
        portfolio=TestComponentStubs.portfolio(),
        msgbus=harness.msgbus,
        cache=harness.cache,
        clock=NativeTestClock(),
    )
    assert isinstance(runtime, Actor)
    assert not harness.client._tasks
    assert not runtime.clock.timer_names
    try:
        yield runtime, harness, engine
    finally:
        if runtime.is_running:
            runtime.stop()
        runtime.dispose()


def _report(harness: _Harness, cid: ClientOrderId, vid: VenueOrderId) -> OrderStatusReport:
    return OrderStatusReport(
        account_id=harness.client.account_id,
        instrument_id=SOURCE_ID,
        client_order_id=cid,
        venue_order_id=vid,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.CANCELED,
        quantity=Quantity.from_int(1),
        filled_qty=Quantity.from_int(0),
        price=Price.from_str("2400"),
        report_id=UUID4(),
        ts_accepted=1,
        ts_last=2,
        ts_init=2,
    )


def test_native_actor_shares_root_and_covers_report_and_callback_with_busy_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()
            engine.release.clear()
            report_started, release_report = asyncio.Event(), asyncio.Event()
            report_calls: list[Any] = []
            callback_states: list[tuple[bool, object]] = []

            async def report(command: Any) -> OrderStatusReport:
                report_calls.append(command)
                report_started.set()
                await release_report.wait()
                return _report(harness, command.client_order_id, command.venue_order_id)

            monkeypatch.setattr(harness.client, "generate_order_status_report", report)
            cid, vid = ClientOrderId("O-SHARED"), VenueOrderId("V-SHARED")

            def complete(value: Any) -> bool:
                callback_states.append((runtime.busy, value))
                return True

            runtime.query_source_terminal(cid, vid, complete)
            runtime.query_source_terminal(cid, vid, complete)
            assert bool(runtime.busy) and not bool(runtime.source_submission_ready)
            assert len(harness.client._tasks) == 1
            first = asyncio.create_task(runtime.reconcile())
            second = asyncio.create_task(runtime.reconcile())
            await engine.entered.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert bool(runtime.busy)
            engine.release.set()
            await report_started.wait()
            assert bool(runtime.busy) and not bool(runtime.source_submission_ready)
            release_report.set()
            assert await second
            assert engine.calls == engine.confirmations == len(report_calls) == 1
            assert len(callback_states) == 1 and callback_states[0][0]
            assert isinstance(callback_states[0][1], OrderStatusReport)
            assert not bool(runtime.busy) and bool(runtime.source_submission_ready)
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("stop_owner", ["actor", "client"])
@pytest.mark.parametrize("phase", ["native", "report"])
def test_native_stop_or_client_drain_cancels_root_without_late_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_owner: str,
    phase: str,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch, timeout=1) as (runtime, harness, engine):
            runtime.start()
            report_entered = asyncio.Event()

            async def hang_report(command: Any) -> None:
                report_entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(harness.client, "generate_order_status_report", hang_report)
            if phase == "native":
                engine.release.clear()
            completions: list[Any] = []
            runtime.query_source_terminal(
                ClientOrderId("O-STOP"), VenueOrderId("V-STOP"), _record(completions)
            )
            waiter = asyncio.create_task(runtime.reconcile())
            await (engine.entered if phase == "native" else report_entered).wait()
            if stop_owner == "actor":
                runtime.stop()
            await harness.client.cancel_pending_tasks(timeout_secs=0.2)
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not runtime.busy and all(task.done() for task in harness.client._tasks)
            assert completions == []
            assert not runtime.source_submission_ready
            if stop_owner == "actor":
                assert not await runtime.reconcile()
                assert not runtime.clock.timer_names
                assert all(task.done() for task in harness.client._tasks)

    asyncio.run(scenario())


def test_request_arriving_in_native_or_business_callback_joins_the_same_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()
            completed: list[Any] = []
            commands: list[Any] = []

            def complete(value: Any) -> bool:
                completed.append(value)
                assert runtime.busy and not runtime.source_submission_ready
                runtime.query_source_terminal(
                    ClientOrderId("O-SECOND"),
                    VenueOrderId("V-SECOND"),
                    _record(completed),
                )
                return True

            async def report(command: Any) -> OrderStatusReport:
                commands.append(command)
                return _report(harness, command.client_order_id, command.venue_order_id)

            monkeypatch.setattr(harness.client, "generate_order_status_report", report)
            original_native = engine.reconcile_execution_state

            async def native_with_callback(*, timeout_secs: float) -> bool:
                result = await original_native(timeout_secs=timeout_secs)
                runtime.query_source_terminal(
                    ClientOrderId("O-FIRST"),
                    VenueOrderId("V-FIRST"),
                    complete,
                )
                return result

            monkeypatch.setattr(engine, "reconcile_execution_state", native_with_callback)
            assert await runtime.reconcile()
            assert len(commands) == len(completed) == 2
            assert all(value is not None for value in completed)
            assert engine.calls == 1
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_stop_from_one_completion_suppresses_remaining_order_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()

            async def report(command: Any) -> OrderStatusReport:
                return _report(harness, command.client_order_id, command.venue_order_id)

            monkeypatch.setattr(harness.client, "generate_order_status_report", report)
            first: list[Any] = []
            late: list[Any] = []

            def stop(value: Any) -> bool:
                first.append(value)
                runtime.stop()
                return True

            cid, vid = ClientOrderId("O-CALLBACK-STOP"), VenueOrderId("V-CALLBACK-STOP")
            runtime.query_source_terminal(cid, vid, stop)
            runtime.query_source_terminal(cid, vid, _record(late))
            with pytest.raises(asyncio.CancelledError):
                await runtime.reconcile()
            assert len(first) == 1 and late == []
            assert engine.calls == 1
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_failed_order_duplicate_keeps_budget_but_explicit_reconcile_can_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()
            engine.success = False
            engine.required = True
            completed: list[Any] = []
            repeated: list[Any] = []
            cid, vid = ClientOrderId("O-FAILED"), VenueOrderId("V-FAILED")
            runtime.query_source_terminal(cid, vid, _record(completed))
            assert runtime._root is not None
            await asyncio.shield(runtime._root)
            assert engine.calls == 2 and completed == []
            runtime.query_source_terminal(cid, vid, _record(repeated))
            for handler in runtime.clock.advance_time(1_000_000_000):
                handler.handle()
            await asyncio.sleep(0)
            assert engine.calls == 2 and not runtime.busy
            assert repeated == [] and not bool(runtime.source_submission_ready)

            async def report(command: Any) -> OrderStatusReport:
                return _report(harness, command.client_order_id, command.venue_order_id)

            monkeypatch.setattr(harness.client, "generate_order_status_report", report)
            engine.success = True
            assert await runtime.reconcile()
            assert engine.calls == 3
            assert len(completed) == len(repeated) == 1 and completed[0] is not None
            assert runtime.source_submission_ready
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_another_orders_success_cannot_clear_retained_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()
            engine.success = False
            failed: list[Any] = []
            runtime.query_source_terminal(
                ClientOrderId("O-FAILED"),
                VenueOrderId("V-FAILED"),
                _record(failed),
            )
            assert runtime._root is not None
            assert not await asyncio.shield(runtime._root)
            engine.success = True

            async def report(command: Any) -> OrderStatusReport:
                return _report(harness, command.client_order_id, command.venue_order_id)

            monkeypatch.setattr(harness.client, "generate_order_status_report", report)
            completed: list[Any] = []
            runtime.query_source_terminal(
                ClientOrderId("O-OTHER"),
                VenueOrderId("V-OTHER"),
                _record(completed),
            )
            assert not await runtime.reconcile(retry_failed=False)
            assert engine.calls == 3 and len(completed) == 1
            assert failed == [] and runtime.last_failure
            assert not runtime.source_submission_ready
            assert await runtime.reconcile()
            assert len(failed) == 1 and failed[0] is not None
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_no_retry_caller_joins_busy_second_attempt_without_resetting_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            original = engine.reconcile_execution_state

            async def fail_once(*, timeout_secs: float) -> bool:
                await original(timeout_secs=timeout_secs)
                return engine.calls > 1

            monkeypatch.setattr(engine, "reconcile_execution_state", fail_once)
            runtime.start()
            owner = asyncio.create_task(runtime.reconcile())
            await engine.entered.wait()
            root = runtime._root
            assert bool(runtime.busy) and runtime.last_failure is not None
            assert await runtime.reconcile(retry_failed=False)
            assert await owner
            assert runtime._root is root and engine.calls == 2
            assert not bool(runtime.busy) and runtime.source_submission_ready
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_no_retry_caller_never_reopens_done_failure_but_can_start_healthy_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            assert not await runtime.reconcile(retry_failed=False)
            assert not harness.client._tasks and not runtime.clock.timer_names
            runtime.start()
            assert await runtime.reconcile(retry_failed=False)
            assert engine.calls == 1
            engine.success = False
            assert not await runtime.reconcile()
            assert engine.calls == 3
            root = runtime._root
            assert not await runtime.reconcile(retry_failed=False)
            assert runtime._root is root and engine.calls == 3
            assert runtime.last_failure is not None and not runtime.source_submission_ready
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_client_cancel_before_root_starts_retains_completion_for_explicit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()
            completed: list[Any] = []
            runtime.query_source_terminal(
                ClientOrderId("O-EARLY"),
                VenueOrderId("V-EARLY"),
                _record(completed),
            )
            await harness.client.cancel_pending_tasks(timeout_secs=0.2)
            assert engine.calls == 0
            assert completed == [] and not runtime.busy
            assert runtime.last_failure and not runtime.source_submission_ready

            async def report(command: Any) -> OrderStatusReport:
                return _report(harness, command.client_order_id, command.venue_order_id)

            monkeypatch.setattr(harness.client, "generate_order_status_report", report)
            assert await runtime.reconcile()
            assert len(completed) == 1 and completed[0] is not None
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("failure", ["timeout", "client_cancel"])
def test_real_strategy_retained_callback_recovers_after_explicit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    failure: str,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()
            strategy: Any = (
                MakerStrategy(
                    _maker_strategy_config(tmp_path / kind),
                    source_terminal_query=runtime.query_source_terminal,
                )
                if kind == "maker"
                else RecordingTakerStrategy(
                    tmp_path / kind,
                    source_terminal_query=runtime.query_source_terminal,
                )
            )
            with _event_engine(strategy) as native:
                native.trader.start()
                order = _seed_terminal_source(native, strategy, maker=kind == "maker")
                store = (
                    strategy._stores[SourceDirection.LONG]
                    if kind == "maker"
                    else strategy.state_store
                )
                engine.release.clear()
                _process_source_terminal(native, order)
                assert bool(runtime.busy) and not store.can_submit_source()
                if failure == "client_cancel":
                    await harness.client.cancel_pending_tasks(timeout_secs=0.2)
                else:
                    assert not await runtime.reconcile()
                assert not runtime.busy and runtime.last_failure
                assert order.client_order_id.value in strategy._source_terminal_inflight
                assert not store.can_submit_source()

                async def report(command: Any) -> OrderStatusReport:
                    assert command.client_order_id == order.client_order_id
                    assert command.venue_order_id == order.venue_order_id
                    return _terminal_report(order)

                monkeypatch.setattr(harness.client, "generate_order_status_report", report)
                engine.release.set()
                assert await runtime.reconcile()
                assert order.client_order_id.value not in strategy._source_terminal_inflight
                assert store.can_submit_source()
                await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("failure", ["identity", "persist"])
def test_real_strategy_business_confirmation_failure_remains_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    failure: str,
) -> None:
    async def scenario() -> None:
        # This verifies business recovery with real fsync, not a 30ms disk deadline.
        with _runtime(tmp_path, monkeypatch, timeout=0.3) as (runtime, harness, engine):
            runtime.start()
            strategy: Any = (
                MakerStrategy(
                    _maker_strategy_config(tmp_path / kind),
                    source_terminal_query=runtime.query_source_terminal,
                )
                if kind == "maker"
                else RecordingTakerStrategy(
                    tmp_path / kind,
                    source_terminal_query=runtime.query_source_terminal,
                )
            )
            with _event_engine(strategy) as native:
                native.trader.start()
                order = _seed_terminal_source(native, strategy, maker=kind == "maker")
                store = (
                    strategy._stores[SourceDirection.LONG]
                    if kind == "maker"
                    else strategy.state_store
                )
                _process_source_terminal(native, order)
                faulty = True
                persist = store._persist

                def injected_persist() -> None:
                    if faulty and failure == "persist":
                        raise OSError("synthetic terminal confirmation write failure")
                    persist()

                monkeypatch.setattr(store, "_persist", injected_persist)

                async def report(command: Any) -> OrderStatusReport:
                    assert command.client_order_id == order.client_order_id
                    return _terminal_report(
                        order,
                        **(
                            {"client_order_id": ClientOrderId("O-WRONG")}
                            if faulty and failure == "identity"
                            else {}
                        ),
                    )

                monkeypatch.setattr(harness.client, "generate_order_status_report", report)
                assert not await runtime.reconcile()
                assert engine.calls == 2 and runtime.last_failure
                assert order.client_order_id.value in strategy._source_terminal_inflight
                assert not store.can_submit_source()
                assert not await runtime.reconcile(retry_failed=False)
                assert engine.calls == 2
                faulty = False
                assert await runtime.reconcile()
                assert engine.calls == 3
                assert order.client_order_id.value not in strategy._source_terminal_inflight
                assert store.can_submit_source()
                await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_successful_callback_is_not_replayed_when_another_confirmation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()

            async def report(command: Any) -> OrderStatusReport:
                return _report(harness, command.client_order_id, command.venue_order_id)

            monkeypatch.setattr(harness.client, "generate_order_status_report", report)
            accepted: list[Any] = []
            failed: list[Any] = []
            accept = False

            def first(value: Any) -> bool:
                accepted.append(value)
                return True

            def second(value: Any) -> bool:
                failed.append(value)
                return accept

            cid, vid = ClientOrderId("O-ACK"), VenueOrderId("V-ACK")
            runtime.query_source_terminal(cid, vid, first)
            runtime.query_source_terminal(cid, vid, second)
            assert not await runtime.reconcile()
            assert engine.calls == 2 and len(accepted) == 1 and len(failed) == 2
            accept = True
            assert await runtime.reconcile()
            assert engine.calls == 3 and len(accepted) == 1 and len(failed) == 3
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_timeout_has_two_attempts_and_one_failed_episode_is_not_polled_forever(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            engine.required = True
            engine.release.clear()
            runtime.start()
            assert not await runtime.reconcile()
            assert engine.calls == 2 and engine.confirmations == 0
            assert bool(runtime.last_failure) and not bool(runtime.source_submission_ready)
            for handler in runtime.clock.advance_time(2_000_000_000):
                handler.handle()
            await asyncio.sleep(0)
            assert engine.calls == 2
            engine.required = False
            for handler in runtime.clock.advance_time(3_000_000_000):
                handler.handle()
            await asyncio.sleep(0)  # Apply this separate observation on the owning loop.
            engine.required = True
            engine.release.set()
            for handler in runtime.clock.advance_time(4_000_000_000):
                handler.handle()
            assert await runtime.reconcile()
            assert engine.calls == 3 and runtime.last_failure is None
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_disconnect_caused_by_timeout_prevents_blind_second_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):

            async def disconnect_on_cancel(*, timeout_secs: float) -> bool:
                engine.calls += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    engine.connected = False
                return True

            monkeypatch.setattr(engine, "reconcile_execution_state", disconnect_on_cancel)
            runtime.start()
            assert not await runtime.reconcile()
            assert engine.calls == 1 and runtime.last_failure
            assert not runtime.source_submission_ready
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_report_timeout_stays_bounded_and_does_not_clear_other_adapter_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            runtime.start()
            harness.client._fatal_failure = "unrelated UNKNOWN"
            report_calls = 0

            async def hang_report(command: Any) -> None:
                nonlocal report_calls
                report_calls += 1
                await asyncio.Event().wait()

            monkeypatch.setattr(harness.client, "generate_order_status_report", hang_report)
            completions: list[Any] = []
            runtime.query_source_terminal(
                ClientOrderId("O-HANG"), VenueOrderId("V-HANG"), _record(completions)
            )
            assert not await asyncio.wait_for(runtime.reconcile(), timeout=0.3)
            assert engine.calls == report_calls == 2
            assert completions == []
            assert runtime.last_failure and not runtime.source_submission_ready
            assert harness.client._fatal_failure == "unrelated UNKNOWN"
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvloop"])
@pytest.mark.parametrize("debug", [False, True])
def test_built_live_clock_wakes_idle_loop_and_keeps_all_state_on_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loop_kind: str,
    debug: bool,
) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        node, _strategy = _build_live_taker(_live_taker_configs(tmp_path), loop=loop)
        owner = get_source_terminal_reconciler(node)
        source = owner._source
        engine = _Engine()
        loop_thread = threading.get_ident()
        observations: list[tuple[int, object]] = []
        state_reads: list[int] = []
        creates: list[int] = []
        native_threads: list[int] = []
        failures: list[object] = []
        loop.set_exception_handler(lambda _loop, context: failures.append(context))
        assert isinstance(owner.clock, LiveClock)
        assert not source._tasks and not owner.clock.timer_names
        original_observe = owner._observe
        original_create = source.create_task

        def observe(event: Any) -> None:
            observations.append((threading.get_ident(), event))
            try:
                original_observe(event)
            except Exception as exc:
                failures.append(exc)

        def create(coro: Any, **kwargs: Any) -> Any:
            creates.append(threading.get_ident())
            try:
                return original_create(coro, **kwargs)
            except BaseException:
                coro.close()  # Dispose only an unstarted failed probe coroutine.
                raise

        def required(_client: Any) -> bool:
            state_reads.append(threading.get_ident())
            return engine.required

        async def native(*, timeout_secs: float) -> bool:
            native_threads.append(threading.get_ident())
            return await engine.reconcile_execution_state(timeout_secs=timeout_secs)

        monkeypatch.setattr(owner, "_observe", observe)
        monkeypatch.setattr(source, "create_task", create)
        monkeypatch.setattr(type(source), "terminal_reconciliation_required", property(required))
        monkeypatch.setattr(source, "confirm_terminal_reconciliation", engine.confirm)
        monkeypatch.setattr(node.kernel.exec_engine, "reconcile_execution_state", native)
        for client in node.kernel.exec_engine._clients.values():
            client._set_connected(True)
        try:
            owner.start()
            engine.required = True
            started = time.monotonic()
            # No market input, polling sleep, explicit reconcile or other wake-up source.
            await asyncio.wait_for(engine.entered.wait(), timeout=0.5)
            assert time.monotonic() - started < 0.5
            assert owner._root is not None and await asyncio.shield(owner._root)
            assert engine.calls == 1 and failures == []
            assert creates == native_threads == [loop_thread]
            assert all(thread == loop_thread for thread, _ in observations)
            assert set(state_reads) == {loop_thread}
            assert any(event is not None for _, event in observations)
            event = next(event for _, event in observations if event is not None)
            engine.required = True
            # Queue a real event from another thread, then stop before the loop handles it.
            worker = threading.Thread(target=owner._dispatch_observation, args=(event,))
            worker.start()
            worker.join(timeout=0.2)
            assert not worker.is_alive()
            owner.stop()
            await asyncio.sleep(0)
            assert engine.calls == 1 and len(creates) == 1
            assert not owner.busy and not owner.source_submission_ready
            assert failures == []
        finally:
            if owner.is_running:
                owner.stop()
            for client in node.kernel.exec_engine._clients.values():
                await client.cancel_pending_tasks(timeout_secs=0.2)
            assert all(task.done() for task in source._tasks)
            assert not owner.clock.timer_names
            node.kernel.dispose()  # The Runner, not node.dispose, owns this test loop.
            if node.kernel.executor is not None:
                node.kernel.executor.shutdown(wait=True, cancel_futures=True)

    loop_factory = uvloop.new_event_loop if loop_kind == "uvloop" else asyncio.SelectorEventLoop
    with asyncio.Runner(loop_factory=loop_factory, debug=debug) as runner:
        runner.run(scenario())


class _WorkingObservation:
    def __init__(self, harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        self.available = True
        self.changed = False
        self.fails = False
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        monkeypatch.setattr(
            type(harness.client),
            "has_working_orders",
            property(lambda _: self.available),
            raising=False,
        )
        monkeypatch.setattr(harness.client, "check_working_orders", self.check, raising=False)

    async def check(self) -> bool:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.fails:
            raise RuntimeError("synthetic active-list failure")
        return self.changed


async def _working_tick(runtime: SourceTerminalReconciler, timestamp_ns: int) -> None:
    for handler in runtime.clock.advance_time(timestamp_ns):
        handler.handle()
    await asyncio.sleep(0)


def test_working_order_checks_use_existing_timer_and_skip_healthy_mass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            working.available = False
            runtime.start()
            await _working_tick(runtime, 5_000_000_000)
            assert working.calls == engine.calls == 0 and not bool(runtime._root)
            working.available = True
            await _working_tick(runtime, 5_250_000_000)
            assert runtime._root is not None and runtime.busy
            first = runtime._root
            assert await asyncio.shield(first)
            assert working.calls == 1 and engine.calls == engine.confirmations == 0
            assert runtime.source_submission_ready and len(runtime.clock.timer_names) == 1
            await _working_tick(runtime, 10_000_000_000)
            assert runtime._root is first and working.calls == 1
            await _working_tick(runtime, 10_250_000_000)
            assert runtime._root is not first and runtime._root is not None
            assert await asyncio.shield(runtime._root)
            assert working.calls == 2 and engine.calls == 0
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_working_difference_promotes_the_same_root_to_native_mass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            working.changed = True
            working.release.clear()
            runtime.start()
            assert runtime._root is not None and runtime.busy
            root = runtime._root
            await asyncio.wait_for(working.entered.wait(), timeout=0.2)
            assert not bool(runtime.source_submission_ready) and len(harness.client._tasks) == 1
            working.release.set()
            assert await asyncio.shield(root)
            assert runtime._root is root and working.calls == engine.calls == 1
            assert engine.confirmations == 1 and runtime.source_submission_ready
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("arrival", ["during_query", "query_returned"])
@pytest.mark.parametrize("trigger", ["explicit", "terminal", "adapter"])
def test_request_joining_healthy_observation_cannot_skip_native_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrival: str, trigger: str
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch, timeout=0.2) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            working.release.clear()
            completed: list[Any] = []
            joiners: list[asyncio.Task[bool]] = []

            async def report(command: Any) -> OrderStatusReport:
                return _report(harness, command.client_order_id, command.venue_order_id)

            def join() -> None:
                if trigger == "explicit":
                    joiners.append(asyncio.create_task(runtime.reconcile(retry_failed=False)))
                elif trigger == "terminal":
                    runtime.query_source_terminal(
                        ClientOrderId("O-WORKING"), VenueOrderId("V-WORKING"), _record(completed)
                    )
                else:
                    engine.required = True
                    runtime._observe(None)

            async def check() -> bool:
                result = await working.check()
                if arrival == "query_returned":
                    # Completion can expose native terminal/explicit requests before return.
                    join()
                    await asyncio.sleep(0)
                return result

            monkeypatch.setattr(harness.client, "generate_order_status_report", report)
            monkeypatch.setattr(harness.client, "check_working_orders", check)
            runtime.start()
            assert runtime._root is not None
            root = runtime._root
            await asyncio.wait_for(working.entered.wait(), timeout=0.2)
            if arrival == "during_query":
                join()
                await asyncio.sleep(0)
            working.release.set()
            assert await asyncio.shield(root)
            assert all(await asyncio.gather(*joiners))
            assert runtime._root is root and working.calls == engine.calls == 1
            assert engine.confirmations == 1
            assert len(completed) == (1 if trigger == "terminal" else 0)
            assert runtime.source_submission_ready
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["query", "mass", "timeout"])
def test_failed_working_episode_is_bounded_and_timer_cannot_reopen_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            working.fails = failure == "query"
            working.changed = failure == "mass"
            engine.success = failure != "mass"
            if failure == "timeout":
                working.release.clear()
            runtime.start()
            assert runtime._root is not None
            root = runtime._root
            assert not await asyncio.shield(root)
            assert working.calls == (1 if failure == "mass" else 2)
            assert engine.calls == (2 if failure == "mass" else 0)
            calls = working.calls, engine.calls
            assert runtime.last_failure and not bool(runtime.source_submission_ready)
            working.fails = working.changed = False
            working.release.set()
            engine.success = True
            await _working_tick(runtime, 20_000_000_000)
            assert not await runtime.reconcile(retry_failed=False)
            assert runtime._root is root and (working.calls, engine.calls) == calls
            assert await runtime.reconcile()
            assert working.calls == calls[0] and engine.calls == calls[1] + 1
            assert runtime.source_submission_ready
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["actor", "client"])
def test_working_observation_stop_is_owned_by_existing_client_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch, timeout=1) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            working.changed = True
            working.release.clear()
            runtime.start()
            assert runtime._root is not None
            root = runtime._root
            await asyncio.wait_for(working.entered.wait(), timeout=0.2)
            if owner == "actor":
                runtime.stop()
            await harness.client.cancel_pending_tasks(timeout_secs=0.2)
            with pytest.raises(asyncio.CancelledError):
                await root
            working.release.set()
            await asyncio.sleep(0)
            assert engine.calls == engine.confirmations == 0
            assert not runtime.busy and not runtime.source_submission_ready
            assert all(task.done() for task in harness.client._tasks)

    asyncio.run(scenario())


def test_working_check_interval_is_configurable_without_another_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            monkeypatch.setattr(live_runtime, "_WORKING_ORDER_CHECK_NS", 50_000_000, raising=False)
            runtime.start()
            assert runtime._root is not None and await asyncio.shield(runtime._root)
            await _working_tick(runtime, 250_000_000)
            assert runtime._root is not None and await asyncio.shield(runtime._root)
            assert working.calls == 2 and engine.calls == 0
            assert len(runtime.clock.timer_names) == 1
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_working_observation_and_promoted_mass_share_one_round_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch, timeout=0.08) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            original_native = engine.reconcile_execution_state

            async def check() -> bool:
                await working.check()
                await asyncio.sleep(0.06)
                return True

            async def native(*, timeout_secs: float) -> bool:
                await original_native(timeout_secs=timeout_secs)
                await asyncio.sleep(0.04)
                return True

            monkeypatch.setattr(harness.client, "check_working_orders", check)
            monkeypatch.setattr(engine, "reconcile_execution_state", native)
            runtime.start()
            assert runtime._root is not None and await asyncio.shield(runtime._root)
            # The first mass times out on the observation's remaining budget. The
            # second attempt retains the discovered discrepancy and does not re-observe.
            assert working.calls == engine.confirmations == 1 and engine.calls == 2
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_disconnect_during_working_observation_cannot_be_reported_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)

            async def check() -> bool:
                await working.check()
                engine.connected = False
                return False

            monkeypatch.setattr(harness.client, "check_working_orders", check)
            runtime.start()
            assert runtime._root is not None and not await asyncio.shield(runtime._root)
            assert working.calls == 1 and engine.calls == engine.confirmations == 0
            assert runtime.last_failure and not runtime.source_submission_ready
            await _working_tick(runtime, 10_000_000_000)
            assert working.calls == 1
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["idle", "mass", "required", "failure", "stop"])
def test_working_observation_state_never_weakens_source_submission_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            working.release.clear()
            assert not bool(runtime.working_observation_in_progress)
            runtime.start()
            assert runtime._root is not None
            root = runtime._root
            assert bool(runtime.working_observation_in_progress)
            assert not bool(runtime.source_submission_ready)
            await asyncio.wait_for(working.entered.wait(), timeout=0.2)
            if transition == "mass":
                waiter = asyncio.create_task(runtime.reconcile())
                await asyncio.sleep(0)
                assert not bool(runtime.working_observation_in_progress)
            elif transition == "required":
                engine.required = True
                assert not bool(runtime.working_observation_in_progress)
            elif transition == "failure":
                working.fails = True
            elif transition == "stop":
                runtime.stop()
                assert not bool(runtime.working_observation_in_progress)
            working.release.set()
            if transition == "stop":
                with pytest.raises(asyncio.CancelledError):
                    await root
            else:
                assert await asyncio.shield(root) is (transition != "failure")
            if transition == "mass":
                assert await waiter
            assert not bool(runtime.working_observation_in_progress)
            assert bool(runtime.source_submission_ready) is (transition not in {"failure", "stop"})
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())


def test_required_state_raised_within_failed_mass_does_not_reopen_its_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        with _runtime(tmp_path, monkeypatch) as (runtime, harness, engine):
            working = _WorkingObservation(harness, monkeypatch)
            working.changed = True
            engine.success = False
            original_native = engine.reconcile_execution_state

            async def native(*, timeout_secs: float) -> bool:
                # Source can acquire a terminal report before another client fails.
                engine.required = True
                return await original_native(timeout_secs=timeout_secs)

            monkeypatch.setattr(engine, "reconcile_execution_state", native)
            runtime.start()
            assert runtime._root is not None
            root = runtime._root
            assert not await asyncio.shield(root)
            assert engine.calls == 2 and bool(engine.required)
            await _working_tick(runtime, 2_000_000_000)
            assert runtime._root is root and engine.calls == 2
            assert not await runtime.reconcile(retry_failed=False)
            # A later observed falling edge and genuinely new rising edge still
            # starts the existing new-episode path, without explicit recovery.
            engine.required = False
            await _working_tick(runtime, 3_000_000_000)
            engine.required = engine.success = True
            await _working_tick(runtime, 4_000_000_000)
            assert runtime._root is not None and runtime._root is not root
            assert await asyncio.shield(runtime._root)
            assert engine.calls == 3 and runtime.source_submission_ready
            await harness.client.cancel_pending_tasks()

    asyncio.run(scenario())
