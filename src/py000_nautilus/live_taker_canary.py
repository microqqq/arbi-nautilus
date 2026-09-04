"""Bounded exact-2oz canary for the existing live Taker chain."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, TextIO, cast

from msgspec.structs import replace as struct_replace
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import ClientId, ClientOrderId

from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, BitfinexV1DataClient
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionClient
from py000_nautilus.live_taker import (
    BITFINEX_CLIENT_ID,
    MT5_CLIENT_ID,
    build_live_taker_node,
)
from py000_nautilus.live_taker_entry import (
    LiveTakerProfile,
    _dispose_node,
    load_bitfinex_test_credentials,
    load_live_taker_profile,
    validate_live_taker_profile,
)
from py000_nautilus.models import BusinessOrderSide, ObligationStatus, SourceDirection
from py000_nautilus.mt5_v1_data import Mt5V1DataClient
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionClient
from py000_nautilus.strategies.taker import TakerStrategy

CANARY_QUANTITY = Decimal(2)
CanaryOutcome = Literal[
    "VALIDATED",
    "NO_ATTEMPT",
    "NO_FILL",
    "PASSED_PAIRED",
    "PASSED_FLAT",
    "HOLD",
    "PARTIAL_HOLD",
    "EXPOSURE_HOLD",
    "UNKNOWN",
    "FAILED",
]


@dataclass(frozen=True, slots=True)
class TakerCanaryResult:
    outcome: CanaryOutcome
    reason: str
    source_order_id: str | None = None


def validate_taker_canary_profile(profile: LiveTakerProfile) -> None:
    """Require one paper-only profile whose complete risk envelope is exactly 2oz."""
    validate_live_taker_profile(profile)
    execution = profile.bitfinex_exec_config
    if (
        execution.raw_symbol != PAPER_RAW_SYMBOL
        or profile.bitfinex_data_config.raw_symbol != PAPER_RAW_SYMBOL
        or execution.wallet_currency != "TESTUSDTF0"
        or execution.account_id.value != f"BITFINEX-PAPER-{execution.user_id}"
    ):
        raise ValueError("canary requires the bound Bitfinex paper account and symbol")
    if profile.mt5_exec_config.expected_max_order_lots != Decimal("0.02"):
        raise ValueError("canary requires the MT5 0.02-lot ceiling")

    strategy = profile.strategy_config
    if len(strategy.source_accounts) != 1:
        raise ValueError("canary requires exactly one Bitfinex source account")
    route = strategy.source_accounts[0]
    economics = strategy.economics
    exact_limits = (
        economics.base_book_quantity,
        economics.open_quantity_long,
        economics.open_quantity_short,
        route.max_long_ounces,
        route.max_short_ounces,
        strategy.hedge_max_long_ounces,
        strategy.hedge_max_short_ounces,
        economics.risk.source_max_abs,
        economics.risk.hedge_max_abs,
    )
    if any(value != CANARY_QUANTITY for value in exact_limits):
        raise ValueError("canary quantity and risk limits must all equal exactly 2oz")
    if (
        economics.risk.source_min_keep_abs != 0
        or economics.risk.hedge_min_keep_abs != 0
        or economics.risk.only_long
    ):
        raise ValueError("canary requires neutral zero-position risk settings")
    if not (
        profile.bitfinex_data_config.min_quantity
        <= CANARY_QUANTITY
        <= profile.bitfinex_data_config.max_quantity
    ):
        raise ValueError("Bitfinex instrument limits cannot express 2oz")


def run_taker_canary(
    profile: LiveTakerProfile,
    *,
    direction: SourceDirection,
    execute: bool = False,
    close_existing: bool = False,
    signal_timeout_seconds: float = 60.0,
    environment: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    node_builder: Callable[..., tuple[TradingNode, TakerStrategy]] = build_live_taker_node,
    lifecycle_runner: Callable[..., TakerCanaryResult] | None = None,
    disposer: Callable[[TradingNode], None] = _dispose_node,
) -> TakerCanaryResult:
    """Build disarmed by default; ``execute`` permits one bounded open or close attempt."""
    validate_taker_canary_profile(profile)
    if not isinstance(direction, SourceDirection):
        raise ValueError("direction must be LONG or SHORT")
    if (
        isinstance(signal_timeout_seconds, bool)
        or not isinstance(signal_timeout_seconds, int | float)
        or not math.isfinite(signal_timeout_seconds)
        or not 1 <= signal_timeout_seconds <= 300
    ):
        raise ValueError("signal timeout must be finite and in [1, 300]")

    lock: TextIO | None = None
    if execute:
        try:
            lock = _lock_paper_account(profile.bitfinex_exec_config.user_id)
        except BlockingIOError:
            return TakerCanaryResult("HOLD", "another_paper_canary_is_running")
        if Path(profile.strategy_config.store_path).exists():
            lock.close()
            return TakerCanaryResult("HOLD", "canary_state_requires_operator_review")
        try:
            credentials = load_bitfinex_test_credentials(
                expected_user_id=profile.bitfinex_exec_config.user_id,
                environment=os.environ if environment is None else environment,
                env_file=env_file,
            )
        except BaseException:
            lock.close()
            raise
        api_key, api_secret = credentials.api_key, credentials.api_secret
    else:
        api_key = api_secret = "OFFLINE-CANARY-VALIDATION-ONLY"

    loop = asyncio.new_event_loop()
    node: TradingNode | None = None
    strategy: TakerStrategy | None = None
    result: TakerCanaryResult | None = None
    expected_source_position = _initial_source_position(direction, close_existing)
    try:
        node, strategy = node_builder(
            bitfinex_data_config=profile.bitfinex_data_config,
            bitfinex_exec_config=struct_replace(
                profile.bitfinex_exec_config,
                api_key=api_key,
                api_secret=api_secret,
            ),
            mt5_data_config=profile.mt5_data_config,
            mt5_exec_config=profile.mt5_exec_config,
            strategy_config=profile.strategy_config,
            loop=loop,
            connection_timeout_seconds=float(profile.connection_timeout_seconds),
            one_shot=True,
            allowed_source_direction=direction,
            one_shot_expected_source_position=expected_source_position,
            one_shot_close_existing=close_existing,
        )
        result = (
            TakerCanaryResult(
                "VALIDATED",
                (
                    "exact_2oz_one_shot_close_built_disarmed"
                    if close_existing
                    else "exact_2oz_one_shot_built_disarmed"
                ),
            )
            if not execute
            else (lifecycle_runner or run_bounded_taker_canary)(
                node,
                strategy,
                direction=direction,
                close_existing=close_existing,
                signal_timeout_seconds=float(signal_timeout_seconds),
                connection_timeout_seconds=float(profile.connection_timeout_seconds),
            )
        )
    except Exception as exc:
        result = _failure(strategy, type(exc).__name__)
    finally:
        if node is not None:
            try:
                disposer(node)
            except Exception as exc:
                result = _failure(strategy, f"node_disposal_failed:{type(exc).__name__}")
        elif not loop.is_closed():
            loop.close()
        if lock is not None:
            lock.close()
    if result is None:
        raise AssertionError("canary completed without a result")
    return result


def run_bounded_taker_canary(
    node: TradingNode,
    strategy: TakerStrategy,
    *,
    direction: SourceDirection,
    close_existing: bool = False,
    signal_timeout_seconds: float,
    connection_timeout_seconds: float,
) -> TakerCanaryResult:
    """Run once, arm after reconciliation, inspect one terminal, and always stop."""
    loop = node.kernel.loop
    if loop.is_closed() or loop.is_running():
        raise RuntimeError("canary requires an owned, stopped event loop")
    return loop.run_until_complete(
        _run_bounded(
            node,
            strategy,
            direction,
            close_existing,
            signal_timeout_seconds,
            connection_timeout_seconds,
        )
    )


async def _run_bounded(
    node: TradingNode,
    strategy: TakerStrategy,
    direction: SourceDirection,
    close_existing: bool,
    signal_timeout: float,
    connection_timeout: float,
) -> TakerCanaryResult:
    task = asyncio.create_task(node.run_async())
    result: TakerCanaryResult
    try:
        readiness_task = asyncio.create_task(_wait_ready(node, strategy, task))
        ready, _ = await asyncio.wait(
            {readiness_task},
            timeout=connection_timeout * 4,
        )
        if readiness_task not in ready:
            readiness_task.cancel()
            await asyncio.gather(readiness_task, return_exceptions=True)
            result = TakerCanaryResult(
                "FAILED",
                _readiness_timeout_reason(node, strategy),
            )
        else:
            await readiness_task
            expected_source_position = _initial_source_position(direction, close_existing)
            hold = _preflight_hold(
                node,
                strategy,
                expected_source_position=expected_source_position,
            )
            if hold is not None:
                result = TakerCanaryResult("HOLD", hold)
            else:
                strategy.arm_one_shot()
                result = await _wait_terminal(
                    node,
                    strategy,
                    task,
                    direction,
                    signal_timeout,
                    connection_timeout,
                    close_existing=close_existing,
                    expected_source_position=expected_source_position,
                )
    except Exception as exc:
        result = _failure(strategy, type(exc).__name__)
    if strategy.one_shot_armed and not strategy.one_shot_claimed:
        strategy.disarm_one_shot()
    shutdown_error = await _shutdown(node, task, connection_timeout)
    return (
        _failure(strategy, f"node_shutdown_failed:{type(shutdown_error).__name__}")
        if shutdown_error
        else result
    )


async def _wait_ready(
    node: TradingNode,
    strategy: TakerStrategy,
    task: asyncio.Task[None],
) -> None:
    while not _ready(node, strategy):
        if task.done():
            raise task.exception() or RuntimeError("node stopped before canary readiness")
        await asyncio.sleep(0.01)


def _ready(node: TradingNode, strategy: TakerStrategy) -> bool:
    bitfinex, mt5 = _clients(node)
    return (
        node.is_running()
        and node.trader.is_running
        and strategy.is_running
        and node.kernel.data_engine.check_connected()
        and node.kernel.exec_engine.check_connected()
        and bitfinex.execution_hold_reason is None
        and bitfinex.get_account() is not None
        and mt5.execution_admitted
        and mt5.get_account() is not None
        and strategy.source_independent_inputs_ready()
    )


def _readiness_timeout_reason(node: TradingNode, strategy: TakerStrategy) -> str:
    """Return one bounded, value-free snapshot without masking the timeout."""
    try:
        return _readiness_timeout_snapshot(node, strategy)[:768]
    except Exception:
        return (
            "readiness_timeout:missing=diagnostic_unavailable;"
            "ages_ms=hedge_tick:unavailable,cost:unavailable,session:unavailable,"
            "mt5_pub:unavailable;callbacks=source_book:unavailable,"
            "mt5_snapshot:unavailable,mt5_pub:unavailable"
        )


def _readiness_timeout_snapshot(node: TradingNode, strategy: TakerStrategy) -> str:
    bitfinex, mt5 = _clients(node)
    _, mt5_data = _data_clients(node)
    now_ns = cast(int, strategy.clock.timestamp_ns())
    config = strategy.config
    hedge_tick = strategy.cache.quote_tick(config.hedge_instrument_id)
    hedge_ts_ns = cast(int, hedge_tick.ts_event) if hedge_tick is not None else None
    cost_ts_ns = strategy._cost_ts_ns
    session_ts_ns = strategy._session_ts_ns

    def age_ns(timestamp_ns: int | None) -> int | None:
        return None if timestamp_ns is None or timestamp_ns <= 0 else now_ns - timestamp_ns

    hedge_age_ns = age_ns(hedge_ts_ns)
    cost_age_ns = age_ns(cost_ts_ns)
    session_age_ns = age_ns(session_ts_ns)
    mt5_pub_age_ms = mt5_data.identity_matched_pub_age_ms
    source_inputs_ready = strategy.source_independent_inputs_ready()
    predicates = (
        ("node_running", node.is_running()),
        ("trader_running", node.trader.is_running),
        ("strategy_running", strategy.is_running),
        ("data_engine_connected", node.kernel.data_engine.check_connected()),
        ("exec_engine_connected", node.kernel.exec_engine.check_connected()),
        ("bitfinex_execution_clear", bitfinex.execution_hold_reason is None),
        ("bitfinex_account_ready", bitfinex.get_account() is not None),
        ("mt5_execution_admitted", mt5.execution_admitted),
        ("mt5_account_ready", mt5.get_account() is not None),
        ("mt5_snapshot_refresh_healthy", mt5_data.snapshot_refresh_healthy),
        ("hedge_tick_present", hedge_tick is not None),
        ("cost_snapshot_valid", strategy._cost_snapshot_valid),
        ("hedge_session_open", strategy._hedge_session_open),
        (
            "hedge_tick_fresh",
            hedge_age_ns is not None and 0 <= hedge_age_ns <= config.max_quote_age_ns,
        ),
        (
            "cost_fresh",
            cost_age_ns is not None and 0 <= cost_age_ns <= config.max_cost_age_ns,
        ),
        (
            "session_fresh",
            session_age_ns is not None and 0 <= session_age_ns <= config.max_session_age_ns,
        ),
    )
    missing = [name for name, ready in predicates if not ready]
    if not source_inputs_ready and not any(
        name
        in {
            "hedge_tick_present",
            "cost_snapshot_valid",
            "hedge_session_open",
            "hedge_tick_fresh",
            "cost_fresh",
            "session_fresh",
        }
        for name in missing
    ):
        missing.append("source_inputs_ready")

    def metric(value: int | None) -> str:
        if value is None:
            return "none"
        bounded = max(-999_999_999, min(value, 999_999_999))
        return str(bounded)

    missing_text = ",".join(missing) if missing else "none_at_timeout_snapshot"
    return (
        f"readiness_timeout:missing={missing_text};"
        "ages_ms="
        f"hedge_tick:{metric(None if hedge_age_ns is None else hedge_age_ns // 1_000_000)},"
        f"cost:{metric(None if cost_age_ns is None else cost_age_ns // 1_000_000)},"
        f"session:{metric(None if session_age_ns is None else session_age_ns // 1_000_000)},"
        f"mt5_pub:{metric(mt5_pub_age_ms)};"
        "callbacks="
        f"source_book:{metric(strategy.source_book_callback_count)},"
        f"mt5_snapshot:{metric(mt5_data.committed_snapshot_count)},"
        f"mt5_pub:{metric(mt5_data.identity_matched_pub_count)}"
    )


def _preflight_hold(
    node: TradingNode,
    strategy: TakerStrategy,
    *,
    expected_source_position: Decimal = Decimal(0),
) -> str | None:
    if strategy.one_shot_armed or strategy.one_shot_claimed:
        return "one_shot_gate_was_not_disarmed"
    if strategy.state_store.source_orders() or not strategy.state_store.can_submit_source():
        return "canary_state_is_not_fresh"
    return _runtime_hold(
        node,
        strategy,
        expected_source_position=expected_source_position,
    )


def _runtime_hold(
    node: TradingNode,
    strategy: TakerStrategy,
    *,
    expected_source_position: Decimal = Decimal(0),
) -> str | None:
    bitfinex, mt5 = _clients(node)
    bitfinex_data, mt5_data = _data_clients(node)
    config = strategy.config
    source_account = config.source_accounts[0].account_id
    if not node.is_running():
        return "live_node_stopped_before_source_claim"
    for label, client in (
        ("bitfinex_data", bitfinex_data),
        ("mt5_data", mt5_data),
        ("bitfinex_execution", bitfinex),
        ("mt5_execution", mt5),
    ):
        if not client.is_connected:
            failure = getattr(client, "last_failure", None)
            detail = str(failure).replace("\n", " ")[:180] if failure else "no_failure_detail"
            return f"{label}_disconnected_before_source_claim:{detail}"
    if not mt5_data.snapshot_refresh_healthy:
        detail = (
            str(mt5_data.last_failure).replace("\n", " ")[:180]
            if mt5_data.last_failure
            else "no_failure_detail"
        )
        return f"mt5_snapshot_refresh_unhealthy_before_source_claim:{detail}"
    if bitfinex.execution_hold_reason or mt5.execution_hold_reason:
        return "execution_adapter_is_on_hold"
    if mt5.pending_client_order_ids or not mt5.can_execute_quantity(CANARY_QUANTITY):
        return "mt5_execution_is_not_clean_for_2oz"
    for instrument_id, account_id, expected in (
        (config.source_instrument_id, source_account, expected_source_position),
        (
            config.hedge_instrument_id,
            config.hedge_account_id,
            -expected_source_position,
        ),
    ):
        if node.cache.orders_open(instrument_id=instrument_id, account_id=account_id):
            return "reconciled_cache_has_open_orders"
        positions = node.cache.positions_open(
            instrument_id=instrument_id,
            account_id=account_id,
        )
        expected_count = 0 if expected == 0 else 1
        if len(positions) != expected_count:
            return "reconciled_positions_do_not_match_canary_start"
        signed = [position.signed_decimal_qty() for position in positions]
        if (
            sum(signed, Decimal()) != expected
            or sum((abs(value) for value in signed), Decimal()) != abs(expected)
            or node.portfolio.net_position(instrument_id, account_id) != expected
        ):
            return "reconciled_positions_do_not_match_canary_start"
    return None


async def _wait_terminal(
    node: TradingNode,
    strategy: TakerStrategy,
    task: asyncio.Task[None],
    direction: SourceDirection,
    timeout: float,
    reconciliation_timeout: float,
    *,
    close_existing: bool = False,
    expected_source_position: Decimal = Decimal(0),
) -> TakerCanaryResult:
    deadline = asyncio.get_running_loop().time() + timeout
    source_reconciliation_attempts = 0
    final_reconciled = False
    while True:
        if task.done():
            raise task.exception() or RuntimeError("node stopped during canary")
        if not _claimed(strategy):
            hold = _runtime_hold(
                node,
                strategy,
                expected_source_position=expected_source_position,
            )
            if hold is not None:
                return TakerCanaryResult("HOLD", hold)
        records = strategy.state_store.source_orders()
        record = records[0] if len(records) == 1 else None
        bitfinex: BitfinexV1ExecutionClient | None = None
        needs_source_terminal_reconciliation = False
        if _claimed(strategy) and record is not None:
            bitfinex, _ = _clients(node)
            needs_source_terminal_reconciliation = bitfinex.terminal_reconciliation_required
        now = asyncio.get_running_loop().time()
        source_reconciliation_due = needs_source_terminal_reconciliation and (
            source_reconciliation_attempts == 0
            or (source_reconciliation_attempts == 1 and now >= deadline)
        )
        if source_reconciliation_due:
            assert record is not None
            assert bitfinex is not None
            source_reconciliation_attempts += 1
            try:
                clean = await asyncio.wait_for(
                    node.kernel.exec_engine.reconcile_execution_state(
                        timeout_secs=reconciliation_timeout,
                    ),
                    timeout=reconciliation_timeout,
                )
                if not clean:
                    raise RuntimeError("source terminal reconciliation was not clean")
                bitfinex.confirm_terminal_reconciliation()
            except Exception:
                return TakerCanaryResult(
                    "UNKNOWN",
                    "source_terminal_reconciliation_failed",
                    record.client_order_id,
                )
            if bitfinex.terminal_reconciliation_required and now >= deadline:
                return TakerCanaryResult(
                    "UNKNOWN",
                    "source_terminal_reconciliation_failed",
                    record.client_order_id,
                )
            await asyncio.sleep(0)
            continue
        terminal = _terminal(strategy)
        needs_reconcile = record is not None and _success_state(strategy)
        if needs_reconcile and not final_reconciled:
            assert record is not None
            final_reconciled = True
            clean = await asyncio.wait_for(
                node.kernel.exec_engine.reconcile_execution_state(
                    timeout_secs=reconciliation_timeout,
                ),
                timeout=reconciliation_timeout,
            )
            if not clean:
                return TakerCanaryResult(
                    "UNKNOWN",
                    "final_reconciliation_failed",
                    record.client_order_id,
                )
            terminal = _terminal(strategy)
        if terminal is not None:
            return terminal
        if _success_state(strategy):
            reason = _final_mismatch(
                node,
                strategy,
                direction,
                close_existing=close_existing,
            )
            passed_outcome: CanaryOutcome = (
                "PASSED_FLAT" if close_existing else "PASSED_PAIRED"
            )
            passed_reason = (
                "exact_2oz_pair_closed" if close_existing else "exact_2oz_pair_reconciled"
            )
            return TakerCanaryResult(
                "UNKNOWN" if reason else passed_outcome,
                reason or passed_reason,
                _source_order_id(strategy),
            )
        if now >= deadline:
            return (
                _failure(strategy, "post_claim_deadline_without_exact_terminal")
                if _claimed(strategy)
                else TakerCanaryResult("NO_ATTEMPT", _no_attempt_reason(strategy))
            )
        await asyncio.sleep(0.01)


def _no_attempt_reason(strategy: TakerStrategy) -> str:
    if strategy.source_book_callback_count == 0:
        return "no_actionable_source_book_before_deadline"
    return f"source_decision_blocked:{strategy.last_decision_gate}"


def _terminal(strategy: TakerStrategy) -> TakerCanaryResult | None:
    records = strategy.state_store.source_orders()
    if not records:
        return None
    if len(records) != 1:
        return TakerCanaryResult("UNKNOWN", "source_order_count_is_not_one")
    record = records[0]
    if record.quantity_ounces != CANARY_QUANTITY or record.status == "UNKNOWN":
        return TakerCanaryResult(
            "UNKNOWN",
            "source_order_evidence_is_invalid",
            record.client_order_id,
        )
    intents = strategy.state_store.intents()
    statuses = {intent.status for intent in intents}
    if ObligationStatus.UNKNOWN in statuses:
        return TakerCanaryResult("UNKNOWN", "hedge_outcome_is_unknown", record.client_order_id)
    if statuses & {ObligationStatus.BLOCKED, ObligationStatus.REJECTED}:
        return TakerCanaryResult("EXPOSURE_HOLD", "hedge_did_not_complete", record.client_order_id)
    if statuses & {
        ObligationStatus.SUBMITTING,
        ObligationStatus.SUBMITTED,
        ObligationStatus.ACCEPTED,
    }:
        return None
    if record.status in {"FILLED", "CANCELED", "EXPIRED"} and ObligationStatus.PENDING in statuses:
        return TakerCanaryResult("EXPOSURE_HOLD", "hedge_was_not_submitted", record.client_order_id)
    if record.status in {"DENIED", "REJECTED"}:
        outcome: CanaryOutcome = (
            "NO_FILL" if record.filled_ounces == 0 and not statuses else "UNKNOWN"
        )
        return TakerCanaryResult(
            outcome,
            "source_was_definitively_rejected",
            record.client_order_id,
        )
    if record.status in {"CANCELED", "EXPIRED"}:
        if record.filled_ounces == 0 and not statuses:
            return TakerCanaryResult(
                "UNKNOWN",
                "source_ioc_terminal_requires_reconciliation",
                record.client_order_id,
            )
        return TakerCanaryResult(
            "PARTIAL_HOLD",
            "source_ioc_was_partially_filled",
            record.client_order_id,
        )
    return None


def _success_state(strategy: TakerStrategy) -> bool:
    records = strategy.state_store.source_orders()
    intents = strategy.state_store.intents()
    if len(records) != 1:
        return False
    record = records[0]
    return (
        strategy.one_shot_claimed
        and strategy.state_store.source_freeze_reason is not None
        and record.quantity_ounces == record.filled_ounces == CANARY_QUANTITY
        and record.status == "FILLED"
        and strategy.state_store.active_source_order_id is None
        and bool(intents)
        and all(intent.status is ObligationStatus.COMPLETED for intent in intents)
        and sum((intent.hedge_quantity_ounces for intent in intents), Decimal()) == CANARY_QUANTITY
        and sum((intent.hedge_filled_ounces for intent in intents), Decimal()) == CANARY_QUANTITY
        and strategy.state_store.net_unhedged_ounces == 0
        and strategy.state_store.rounding_residual_ounces == 0
        and strategy.state_store.halt_reason is None
    )


def _final_mismatch(
    node: TradingNode,
    strategy: TakerStrategy,
    direction: SourceDirection,
    *,
    close_existing: bool = False,
) -> str | None:
    bitfinex, mt5 = _clients(node)
    config = strategy.config
    route = config.source_accounts[0]
    record = strategy.state_store.source_orders()[0]
    expected_side = (
        BusinessOrderSide.BUY if direction is SourceDirection.LONG else BusinessOrderSide.SELL
    )
    data_plane_mismatch = _final_data_plane_mismatch(node)
    if data_plane_mismatch is not None:
        return data_plane_mismatch
    if bitfinex.execution_hold_reason or mt5.execution_hold_reason or mt5.pending_client_order_ids:
        return "execution_adapter_not_clean_after_reconciliation"
    if (
        record.side is not expected_side
        or record.source_account_id != route.account_id.value
        or record.source_client_id != cast(ClientId, route.client_id).value
        or record.hedge_account_id != config.hedge_account_id.value
        or record.hedge_client_id != cast(ClientId, config.hedge_client_id).value
    ):
        return "persisted_route_does_not_match_canary"
    if not _orders_are_exact(
        node,
        strategy,
        direction,
        close_existing=close_existing,
    ):
        return "order_evidence_is_not_exact_ioc_fok_2oz"
    for instrument_id, account_id in (
        (config.source_instrument_id, route.account_id),
        (config.hedge_instrument_id, config.hedge_account_id),
    ):
        if node.cache.orders_open(instrument_id=instrument_id, account_id=account_id):
            return "open_order_remains_after_reconciliation"
    expected_source = (
        Decimal(0)
        if close_existing
        else (CANARY_QUANTITY if direction is SourceDirection.LONG else -CANARY_QUANTITY)
    )
    expected_abs = Decimal(0) if close_existing else CANARY_QUANTITY
    for instrument_id, account_id, expected in (
        (config.source_instrument_id, route.account_id, expected_source),
        (config.hedge_instrument_id, config.hedge_account_id, -expected_source),
    ):
        positions = node.cache.positions_open(
            instrument_id=instrument_id,
            account_id=account_id,
        )
        signed = [position.signed_decimal_qty() for position in positions]
        if (
            sum(signed, Decimal()) != expected
            or sum((abs(value) for value in signed), Decimal()) != expected_abs
            or node.portfolio.net_position(instrument_id, account_id) != expected
        ):
            return "paired_positions_are_not_exact_and_opposite"
    return None


def _final_data_plane_mismatch(node: TradingNode) -> str | None:
    bitfinex_data, mt5_data = _data_clients(node)
    if (
        not node.kernel.data_engine.check_connected()
        or not bitfinex_data.is_connected
        or not mt5_data.is_connected
        or not mt5_data.snapshot_refresh_healthy
    ):
        return "data_plane_not_clean_after_reconciliation"
    return None


def _orders_are_exact(
    node: TradingNode,
    strategy: TakerStrategy,
    direction: SourceDirection,
    *,
    close_existing: bool = False,
) -> bool:
    record = strategy.state_store.source_orders()[0]
    source = node.cache.order(ClientOrderId(record.client_order_id))
    source_side = OrderSide.BUY if direction is SourceDirection.LONG else OrderSide.SELL
    if (
        source is None
        or source.order_type != OrderType.LIMIT
        or source.time_in_force != TimeInForce.IOC
        or source.side != source_side
        or source.quantity.as_decimal() != CANARY_QUANTITY
        or source.filled_qty.as_decimal() != CANARY_QUANTITY
        or cast(bool, source.is_reduce_only) is not close_existing
        or not source.is_closed
    ):
        return False
    hedge_side = OrderSide.SELL if direction is SourceDirection.LONG else OrderSide.BUY
    hedge_ids = tuple(
        order_id
        for intent in strategy.state_store.intents()
        for order_id in (
            intent.hedge_order_ids
            or ((intent.hedge_client_order_id,) if intent.hedge_client_order_id else ())
        )
    )
    hedges = [node.cache.order(ClientOrderId(order_id)) for order_id in hedge_ids]
    return (
        all(
            order is not None
            and order.order_type == OrderType.MARKET
            and order.time_in_force == TimeInForce.FOK
            and order.side == hedge_side
            and cast(bool, order.is_reduce_only) is close_existing
            and order.is_closed
            for order in hedges
        )
        and sum((order.quantity.as_decimal() for order in hedges if order is not None), Decimal())
        == CANARY_QUANTITY
        and sum((order.filled_qty.as_decimal() for order in hedges if order is not None), Decimal())
        == CANARY_QUANTITY
    )

async def _shutdown(
    node: TradingNode,
    task: asyncio.Task[None],
    timeout: float,
) -> BaseException | None:
    error: BaseException | None = None
    if node.is_running():
        stop = asyncio.create_task(node.stop_async())
        done, _ = await asyncio.wait({stop}, timeout=timeout)
        if stop not in done:
            stop.cancel()
            error = RuntimeError("node stop timed out")
        elif stop.cancelled():
            error = RuntimeError("node stop was canceled")
        else:
            error = stop.exception()
    if not task.done() and error is None:
        await asyncio.wait({task}, timeout=timeout)
    if not task.done():
        task.cancel()
    await asyncio.sleep(0)
    if not task.done():
        return error or RuntimeError("node task resisted cancellation")
    return error or (None if task.cancelled() else task.exception())


def _clients(node: TradingNode) -> tuple[BitfinexV1ExecutionClient, Mt5V1ExecutionClient]:
    clients = cast(dict[ClientId, LiveExecutionClient], node.kernel.exec_engine._clients)
    bitfinex, mt5 = clients.get(BITFINEX_CLIENT_ID), clients.get(MT5_CLIENT_ID)
    if not isinstance(bitfinex, BitfinexV1ExecutionClient) or not isinstance(
        mt5,
        Mt5V1ExecutionClient,
    ):
        raise RuntimeError("canary execution clients are missing")
    return bitfinex, mt5


def _data_clients(node: TradingNode) -> tuple[BitfinexV1DataClient, Mt5V1DataClient]:
    clients = cast(dict[ClientId, object], node.kernel.data_engine._clients)
    bitfinex, mt5 = clients.get(BITFINEX_CLIENT_ID), clients.get(MT5_CLIENT_ID)
    if not isinstance(bitfinex, BitfinexV1DataClient) or not isinstance(mt5, Mt5V1DataClient):
        raise RuntimeError("canary data clients are missing")
    return bitfinex, mt5


def _claimed(strategy: TakerStrategy | None) -> bool:
    return strategy is not None and (
        strategy.one_shot_claimed or bool(strategy.state_store.source_orders())
    )


def _source_order_id(strategy: TakerStrategy | None) -> str | None:
    records = strategy.state_store.source_orders() if strategy is not None else ()
    return records[0].client_order_id if len(records) == 1 else None


def _failure(strategy: TakerStrategy | None, reason: str) -> TakerCanaryResult:
    return TakerCanaryResult(
        "UNKNOWN" if _claimed(strategy) else "FAILED",
        reason,
        _source_order_id(strategy),
    )


def _lock_paper_account(user_id: int) -> TextIO:
    path = Path(tempfile.gettempdir()) / f"py000-bitfinex-paper-{user_id}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    stream = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise
    return stream


def _direction(value: str) -> SourceDirection:
    return SourceDirection.LONG if value == "long" else SourceDirection.SHORT


def _initial_source_position(
    direction: SourceDirection,
    close_existing: bool,
) -> Decimal:
    if not close_existing:
        return Decimal(0)
    return -CANARY_QUANTITY if direction is SourceDirection.LONG else CANARY_QUANTITY


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Validate or run one paired 2oz Taker canary")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--direction", required=True, choices=("long", "short"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="permit one attempt; close mode requires one explicit close flag",
    )
    parser.add_argument(
        "--close-existing",
        action="store_true",
        help="require and close the exact opposite 2oz pair instead of opening a new pair",
    )
    parser.add_argument("--signal-timeout", type=float, default=60.0)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        result = run_taker_canary(
            load_live_taker_profile(args.profile),
            direction=_direction(args.direction),
            execute=args.execute,
            close_existing=args.close_existing,
            signal_timeout_seconds=args.signal_timeout,
            environment=environment,
            env_file=args.env_file,
        )
    except Exception as exc:
        print(json.dumps({"outcome": "FAILED", "reason": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    expected = (
        "PASSED_FLAT"
        if args.execute and args.close_existing
        else ("PASSED_PAIRED" if args.execute else "VALIDATED")
    )
    return 0 if result.outcome == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
