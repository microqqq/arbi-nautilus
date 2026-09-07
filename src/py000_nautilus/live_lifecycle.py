"""Drain existing strategy obligations before the native node closes its clients."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientOrderId

from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import ObligationStatus
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.maker import MakerStrategy
from py000_nautilus.strategies.taker import TakerStrategy

type LiveStrategy = MakerStrategy | TakerStrategy


@dataclass(frozen=True)
class DrainResult:
    complete: bool
    reason: str
    pending: tuple[str, ...]
    residuals: dict[str, str]


def validate_stop_timeout(timeout_seconds: float) -> None:
    if (isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60):
        raise ValueError("stop timeout must be finite and in (0, 60]")


def _stores(strategy: LiveStrategy) -> tuple[JsonStateStore, ...]:
    return (tuple(strategy._state_store.stores.values())
            if isinstance(strategy, MakerStrategy) else (strategy.state_store,))


def _snapshot(node: TradingNode, strategy: LiveStrategy) -> DrainResult:
    """Describe retained facts, not a second order book or permission to clear HOLD."""
    pending: list[str] = []
    residuals: dict[str, str] = {}
    reconciler = get_source_terminal_reconciler(node)
    if not reconciler.is_running:
        pending.append("terminal_reconciler_not_running")
    if not node.kernel.exec_engine.check_connected():
        pending.append("execution_disconnected")
    if reconciler.restart_pending:
        pending.append("startup_reconciliation_pending")
    if reconciler.busy or reconciler.last_failure is not None:
        pending.append("terminal_reconciliation_pending")
    if not strategy.is_running:
        pending.append("strategy_not_running")
    for store in _stores(strategy):
        if store.halt_reason is not None:
            pending.append(f"hold:{store.halt_reason}")
        if store.source_freeze_reason is not None:
            pending.append(f"freeze:{store.source_freeze_reason}")
        for record in store.source_orders():
            order = node.cache.order(ClientOrderId(record.client_order_id))
            if (store.active_source_order_id == record.client_order_id or order is None
                    or not order.is_closed or order.status.name != record.status
                    or order.filled_qty.as_decimal() != record.filled_ounces):
                pending.append(f"source:{record.client_order_id}")
        for intent in store.intents():
            if (intent.status is not ObligationStatus.COMPLETED
                    or intent.hedge_filled_ounces != intent.hedge_quantity_ounces):
                pending.append(f"hedge:{intent.intent_id}:{intent.status.value}")
    allocation_owner = (strategy._state_store if isinstance(strategy, MakerStrategy)
                        else getattr(strategy.state_store, "_owner", None))
    if isinstance(allocation_owner, MakerStateStore):
        if allocation_owner._freeze_publication_failed:
            pending.append("maker_pause_publication_failed")
        # Input freshness pauses new source admission, not a completed
        # drain. Retain the flag; stopping must not grant permission to quote.
        settled = all(
            view.cycle_evidence_complete() and view.source_freeze_reason is None
            for view in allocation_owner.all_views()
        )
        if isinstance(strategy, MakerStrategy) and strategy._source_hold and not settled:
            pending.append("maker_source_hold")
        for route, quantity in allocation_owner.residuals().items():
            residuals["|".join(value or "" for value in route)] = str(quantity)
        if not allocation_owner.source_balance_is_admissible():
            pending.append("residual_outside_configured_budget")
    elif isinstance(strategy, TakerStrategy) and (
        strategy.state_store.rounding_residual_ounces != Decimal(0)
    ):
        residuals["taker"] = str(strategy.state_store.rounding_residual_ounces)
        pending.append("unallocated_residual")
    return DrainResult(
        complete=not pending,
        reason="obligations_settled" if not pending else "obligations_pending",
        pending=tuple(dict.fromkeys(pending)), residuals=residuals,
    )


async def drain_strategy(
    node: TradingNode, strategy: LiveStrategy, *, timeout_seconds: float,
) -> DrainResult:
    return await drain_strategies(node, (strategy,), timeout_seconds=timeout_seconds)


async def drain_strategies(
    node: TradingNode, strategies: tuple[LiveStrategy, ...], *, timeout_seconds: float,
) -> DrainResult:
    """Keep callbacks online; cancel each known source at most once, never an uncertain hedge."""
    validate_stop_timeout(timeout_seconds)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    canceled: set[str] = set()
    if not strategies or len({strategy.id for strategy in strategies}) != len(strategies):
        raise ValueError("drain requires distinct participating strategies")
    for strategy in strategies:
        strategy.begin_drain()
    while True:
        reconciler = get_source_terminal_reconciler(node)
        for strategy in strategies:
            if reconciler.restart_pending or not strategy.is_running:
                continue
            for store in _stores(strategy):
                for record in store.source_orders():
                    order = node.cache.order(ClientOrderId(record.client_order_id))
                    if (order is None or order.is_closed or order.is_pending_cancel
                            or order.venue_order_id is None
                            or record.client_order_id in canceled):
                        continue
                    # Reserve before queuing. A reject or timeout must not create a cancel loop.
                    canceled.add(record.client_order_id)
                    strategy.cancel_order(order)
            strategy.continue_drain()
        # Allow already queued fill/terminal callbacks to reach the durable owner first.
        await asyncio.sleep(0)
        snapshots = [_snapshot(node, strategy) for strategy in strategies]
        pending = tuple(dict.fromkeys(item for snapshot in snapshots for item in snapshot.pending))
        result = DrainResult(
            not pending, "obligations_settled" if not pending else "obligations_pending",
            pending, {route: qty for snapshot in snapshots
                      for route, qty in snapshot.residuals.items()},
        )
        if result.complete:
            return result
        remaining = deadline - loop.time()
        if remaining <= 0:
            return DrainResult(False, "drain_timeout", result.pending, result.residuals)
        await asyncio.sleep(min(0.05, remaining))


class DrainingTradingNode(TradingNode):
    """One stop hook around Nautilus; its engines, signals and shutdown remain native."""

    drain_result: DrainResult | None = None
    _drain_strategy: LiveStrategy | None = None
    _drain_strategies: tuple[LiveStrategy, ...] = ()
    _drain_timeout: float = 10.0
    _drain_stop_task: asyncio.Task[None] | None = None

    def bind_strategy_drain(self, strategy: LiveStrategy, *, timeout_seconds: float) -> None:
        self.bind_strategies_drain((strategy,), timeout_seconds=timeout_seconds)

    def bind_strategies_drain(
        self, strategies: tuple[LiveStrategy, ...], *, timeout_seconds: float,
    ) -> None:
        validate_stop_timeout(timeout_seconds)
        if self.is_running() or self._drain_strategies:
            raise RuntimeError("strategy drain must be bound once before node start")
        if not strategies or len({strategy.id for strategy in strategies}) != len(strategies):
            raise ValueError("drain requires distinct participating strategies")
        self._drain_strategy = strategies[0]
        self._drain_strategies = strategies
        self._drain_timeout = timeout_seconds
        self.drain_result = None
        self._drain_stop_task = None

    async def stop_async(self) -> None:
        if not self.is_running() and (
            self._drain_stop_task is None or self._drain_stop_task.done()
        ):
            # A pre-start no-op must not consume the later real stop request.
            await super().stop_async()
            return
        if self._drain_stop_task is not None and self._drain_stop_task.done():
            self._drain_stop_task = None
            self.drain_result = None
        if self._drain_stop_task is None:
            self._drain_stop_task = asyncio.create_task(self._stop_after_drain())
        # A second signal or canceled caller cannot cancel the single owned stop sequence.
        await asyncio.shield(self._drain_stop_task)

    async def _stop_after_drain(self) -> None:
        strategies = self._drain_strategies
        try:
            # Rehearsal removes the strategy and recovery Actor before running the node.
            if (strategies and self.is_running()
                    and all(strategy in self.trader.strategies() for strategy in strategies)):
                self.drain_result = await drain_strategies(
                    self, strategies, timeout_seconds=self._drain_timeout,
                )
        except Exception as exc:
            self.drain_result = DrainResult(
                False, f"drain_error:{type(exc).__name__}", ("inspect_retained_state",), {},
            )
            self.kernel.logger.error(f"Strategy drain failed: {type(exc).__name__}")
        finally:
            try:
                await super().stop_async()
            except BaseException as exc:
                previous = self.drain_result
                self.drain_result = DrainResult(
                    False, f"native_stop_error:{type(exc).__name__}",
                    ("native_stop_incomplete",) + (() if previous is None else previous.pending),
                    {} if previous is None else previous.residuals,
                )
                raise
