"""One native Actor owns bounded source-terminal reconciliation for a live node."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import cast

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.component import TimeEvent
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, VenueOrderId

from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionClient
from py000_nautilus.strategies._source_terminal import SourceTerminalResult

_TIMER = "source-terminal-observation"
_POLL_NS = 250_000_000
_WORKING_ORDER_CHECK_NS = 5_000_000_000


@dataclass
class _TerminalRequest:
    client_order_id: ClientOrderId
    venue_order_id: VenueOrderId
    callbacks: list[SourceTerminalResult] = field(default_factory=list)


class SourceTerminalReconciler(Actor):
    """Share native reconciliation; leave trade identity and business HOLD to their owners."""

    def __init__(
        self,
        *,
        source_client: BitfinexV1ExecutionClient,
        exec_engine: LiveExecutionEngine,
        source_instrument_id: InstrumentId,
        timeout_seconds: float,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("terminal reconciliation timeout must be finite and positive")
        super().__init__()
        self._source = source_client
        self._engine = exec_engine
        self._instrument_id = source_instrument_id
        self._timeout = timeout_seconds
        self._backoff = min(0.05, timeout_seconds)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active = False
        self._busy = False
        self._observed_required = False
        self._next_working_check_ns = 0
        self._mass_required = False
        self._root: asyncio.Task[bool] | None = None
        self._requests: dict[tuple[str, str], _TerminalRequest] = {}
        self._failed_requests: dict[tuple[str, str], _TerminalRequest] = {}
        self._last_failure: str | None = None
        self._phase = "idle"

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    @property
    def source_submission_ready(self) -> bool:
        return (
            self._active
            and not self._busy
            and self._last_failure is None
            and not self._source.terminal_reconciliation_required
        )

    @property
    def working_observation_in_progress(self) -> bool:
        return (
            self._active
            and self._busy
            and not self._mass_required
            and self._last_failure is None
            and not self._source.terminal_reconciliation_required
        )

    def on_start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._active = True
        self._observed_required = False
        self._next_working_check_ns = 0
        self.clock.set_timer_ns(
            _TIMER,
            _POLL_NS,
            start_time_ns=0,
            stop_time_ns=0,
            callback=self._dispatch_observation,
        )
        self._observe(None)

    def on_stop(self) -> None:
        self._active = False
        if _TIMER in self.clock.timer_names:
            self.clock.cancel_timer(_TIMER)
        self._requests.clear()
        self._failed_requests.clear()
        if self._root is not None and not self._root.done():
            self._root.cancel()

    def _dispatch_observation(self, event: TimeEvent) -> None:
        # Native LiveClock callbacks run on a Rust timer thread, not this loop.
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._observe, event)
        except RuntimeError:
            if not loop.is_closed():
                raise

    def _observe(self, event: TimeEvent | None) -> None:
        if not self._active:
            return
        required = self._source.terminal_reconciliation_required
        if required and not self._observed_required:
            self._observed_required = True
            self._ensure_root(retry_failed=True)
        elif not required:
            self._observed_required = False
        if (
            not self._busy
            and self._last_failure is None
            and self._source.has_working_orders
            and self.clock.timestamp_ns() >= self._next_working_check_ns
        ):
            self._next_working_check_ns = self.clock.timestamp_ns() + _WORKING_ORDER_CHECK_NS
            self._ensure_root(working_check=True)

    def query_source_terminal(
        self,
        client_order_id: ClientOrderId,
        venue_order_id: VenueOrderId,
        complete: SourceTerminalResult,
    ) -> None:
        if not self._active:
            return
        key = (client_order_id.value, venue_order_id.value)
        if key in self._failed_requests:
            # Replayed terminal events must not reset this order's attempt budget.
            callbacks = self._failed_requests[key].callbacks
            if complete not in callbacks:
                callbacks.append(complete)
            return
        request = self._requests.setdefault(
            key,
            _TerminalRequest(client_order_id, venue_order_id),
        )
        if complete not in request.callbacks:
            request.callbacks.append(complete)
        self._ensure_root()

    async def reconcile(self, *, retry_failed: bool = True) -> bool:
        """Join the root; optionally leave failed actions reserved for explicit recovery."""
        if not self._active:
            return False
        if (
            not retry_failed
            and self._last_failure is not None
            and (self._root is None or self._root.done())
        ):
            return False
        return await asyncio.shield(self._ensure_root(retry_failed=retry_failed))

    def _ensure_root(
        self, *, retry_failed: bool = False, working_check: bool = False
    ) -> asyncio.Task[bool]:
        if retry_failed:
            self._requests.update(self._failed_requests)
            self._failed_requests.clear()
        if self._root is None or self._root.done():
            # Gate before scheduling: native fill callbacks can synchronously create work.
            self._busy = True
            self._mass_required = not working_check
            self._observed_required = self._source.terminal_reconciliation_required
            self._root = self._source.create_task(
                self._run(),
                log_msg="source-terminal-reconciliation",
            )
            self._root.add_done_callback(self._root_finished)
        elif not working_check:
            # A terminal/explicit caller must not receive a healthy active-list result.
            self._mass_required = True
        return self._root

    def _root_finished(self, task: asyncio.Task[bool]) -> None:
        if self._root is task:
            self._busy = False
            if task.cancelled():
                self._last_failure = "terminal reconciliation canceled"
                # Also handles cancellation before _run entered its try/finally.
                self._retain_pending()

    def _retain_pending(self) -> None:
        pending, self._requests = self._requests, {}
        if self._active:
            # Facts obtained within this failed round are not a new timer episode.
            self._observed_required = self._source.terminal_reconciliation_required
            # No early None: the strategy must retain its one-shot completion until
            # an explicit retry/new episode supplies authority or Actor.stop expires it.
            self._failed_requests.update(pending)

    async def _run(self) -> bool:
        try:
            for attempt in range(2):
                if not self._active or not self._engine.check_connected():
                    self._last_failure = "execution clients are disconnected"
                    break
                try:
                    await asyncio.wait_for(self._round(), timeout=self._timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Do not log raw private reports or exception payloads.
                    self._last_failure = f"{self._phase}: {type(exc).__name__}"
                    if not self._engine.check_connected():
                        self._last_failure = "execution clients disconnected during reconciliation"
                        break
                    if attempt == 0:
                        await asyncio.sleep(self._backoff)
                else:
                    if self._failed_requests:
                        # A different order does not reset an earlier failed action.
                        return False
                    self._last_failure = None
                    self._observed_required = False
                    return True
            self._retain_pending()
            self.log.warning(f"Source terminal reconciliation held: {self._last_failure}")
            return False
        except asyncio.CancelledError:
            self._last_failure = "terminal reconciliation canceled"
            self._retain_pending()
            raise
        finally:
            self._busy = False

    async def _round(self) -> None:
        if not self._mass_required:
            self._phase = "source working order observation"
            changed = await self._source.check_working_orders()
            if not self._active:
                raise asyncio.CancelledError
            if not self._engine.check_connected():
                raise RuntimeError("execution clients disconnected during observation")
            self._mass_required |= changed or self._source.terminal_reconciliation_required
            if not self._mass_required:
                return
        self._phase = "native execution reconciliation"
        if not await self._engine.reconcile_execution_state(timeout_secs=self._timeout):
            raise RuntimeError("native execution reconciliation was not exact")
        self._phase = "adapter terminal confirmation"
        self._source.confirm_terminal_reconciliation()
        if self._source.terminal_reconciliation_required:
            raise RuntimeError("source adapter still requires terminal reconciliation")
        await self._drain_requests()

    async def _drain_requests(self) -> None:
        while self._requests:
            key, request = next(iter(self._requests.items()))
            self._phase = "source order status report"
            report = await self._source.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=self._instrument_id,
                    client_order_id=request.client_order_id,
                    venue_order_id=request.venue_order_id,
                    command_id=UUID4(),
                    ts_init=cast(int, self.clock.timestamp_ns()),
                ),
            )
            if report is None:
                raise RuntimeError("source order status report is unavailable")
            self._phase = "source business terminal callback"
            while request.callbacks:
                if not self._active:
                    raise asyncio.CancelledError
                if request.callbacks[0](report) is not True:
                    raise RuntimeError("source business terminal confirmation was not exact")
                request.callbacks.pop(0)
                await asyncio.sleep(0)
            self._requests.pop(key, None)
            # Even immediate fake reports/reentrant callbacks respect the round budget.
            await asyncio.sleep(0)
        if self._source.terminal_reconciliation_required:
            raise RuntimeError("another source terminal requires reconciliation")


def get_source_terminal_reconciler(node: TradingNode) -> SourceTerminalReconciler:
    actors = [
        actor for actor in node.trader.actors() if isinstance(actor, SourceTerminalReconciler)
    ]
    if len(actors) != 1:
        raise RuntimeError("live node must contain exactly one source terminal reconciler")
    return actors[0]
