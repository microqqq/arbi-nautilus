from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal, TextIO, cast

from msgspec.structs import replace as struct_replace
from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.events import OrderAccepted, OrderCanceled, OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from nautilus_trader.model.orders import Order

from py000_nautilus.bitfinex_v1_cids import BitfinexV1CidStore
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, BitfinexV1DataClientConfig
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecClientConfig
from py000_nautilus.bitfinex_v1_paper_canary import (
    PRIVATE_WS_URL,
    PUBLIC_WS_URL,
    REST_URL,
    PaperCanaryError,
    PaperSnapshot,
    _lock_canary,
    _new_transcript,
    _write,
    read_owned_evidence,
    read_snapshot,
)
from py000_nautilus.bitfinex_v1_rest import BitfinexV1RestClient
from py000_nautilus.config import MakerStrategyConfig
from py000_nautilus.economics import expected_leverage
from py000_nautilus.live_maker import BITFINEX_CLIENT_ID, build_live_maker_node
from py000_nautilus.live_taker_entry import _dispose_node, load_bitfinex_test_credentials
from py000_nautilus.models import BookTop, MakerQuote, SourceDirection
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig
from py000_nautilus.strategies.maker import MakerStrategy, SourceTerminalQuery

D = Decimal
QUANTITY = D(2)
type Outcome = Literal["VALIDATED", "PASSED", "HOLD", "FAILED", "UNKNOWN", "FILLED_HOLD"]


class LiveMakerCanaryProfile(NautilusConfig, frozen=True):
    bitfinex_data_config: BitfinexV1DataClientConfig
    bitfinex_exec_config: BitfinexV1ExecClientConfig
    mt5_data_config: Mt5V1DataClientConfig
    mt5_exec_config: Mt5V1ExecClientConfig
    strategy_config: MakerStrategyConfig
    connection_timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class MakerCanaryResult:
    outcome: Outcome
    reason: str
    transcript_path: Path
    source_order_id: str | None = None


class MakerCanaryStrategy(MakerStrategy):
    def __init__(
        self,
        config: MakerStrategyConfig,
        *,
        live_submission_ready: Callable[[], bool],
        hedge_quantity_ready: Callable[[Decimal], bool],
        live_costs_from_adapters: bool,
        source_terminal_query: SourceTerminalQuery,
    ) -> None:
        super().__init__(
            config,
            live_submission_ready=live_submission_ready,
            hedge_quantity_ready=hedge_quantity_ready,
            live_costs_from_adapters=live_costs_from_adapters,
            source_terminal_query=source_terminal_query,
        )
        self.armed: bool = False
        self.claimed: bool = False
        self._cached_triggered: bool = False
        self.cancel_sent: bool = False
        self.hold_before_rest: bool = False
        self.filled = D()
        self.venue_order_id: VenueOrderId | None = None
        self.failure_reason: str | None = None
        self.cleanup_reason = "not_run"
        self.resumed, self.post_resume_quote, self.exposure = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

    @property
    def source_order_id(self) -> str | None:
        records = self._stores[SourceDirection.LONG].source_orders()
        return records[0].client_order_id if len(records) == 1 else None

    @property
    def source_order(self) -> Order | None:
        order_id = self.source_order_id
        return None if order_id is None else self.cache.order(ClientOrderId(order_id))

    @property
    def resume_ready(self) -> bool:
        return (
            self.claimed
            and self.hold_before_rest
            and self.filled == 0
            and not self._source_terminal_inflight
            and all(store.can_submit_source() for store in self._stores.values())
            and not self._global_obligation_block()
        )

    @property
    def owned_terminal_settled(self) -> bool:
        store = self._stores[SourceDirection.LONG]
        return (
            self.source_order_id is not None
            and store.active_source_order_id is None
            and not self._source_terminal_inflight
        )

    def planned_price(self) -> Decimal:
        tick = self.cache.quote_tick(self._config.source_instrument_id)
        instrument = self._required_source_instrument()
        if tick is None:
            raise PaperCanaryError("Maker canary source quote is unavailable")
        age = cast(int, self.clock.timestamp_ns()) - tick.ts_event
        if age < 0 or age > self._config.max_quote_age_ns:
            raise PaperCanaryError("Maker canary source quote is stale")
        increment = instrument.price_increment.as_decimal()
        scaled = tick.bid_price.as_decimal() * D("0.95") / increment
        price = cast(Decimal, scaled.to_integral_value(ROUND_FLOOR) * increment)
        if price <= 0:
            raise PaperCanaryError("Maker canary passive price is invalid")
        return price

    def arm(self) -> None:
        if self.armed or self.claimed or self._cached_triggered:
            raise PaperCanaryError("Maker canary can be armed only once")
        self.armed = True

    def trigger_cached_once(self) -> bool:
        if not self.armed or self.claimed or self._cached_triggered:
            raise PaperCanaryError("Maker canary cached trigger is not armed")
        self._cached_triggered = True
        try:
            cache = self.cache
            tick = None if cache is None else cache.quote_tick(self._config.source_instrument_id)
            if tick is None:
                raise PaperCanaryError("Maker canary source quote is unavailable")
            self.on_quote_tick(tick)
        finally:
            self._disarm_unclaimed()
        return self.claimed

    def _disarm_unclaimed(self) -> None:
        if not self.claimed:
            self.armed = False

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self.claimed:
            if tick.instrument_id == self._config.source_instrument_id and self.resume_ready:
                self.post_resume_quote.set()
            return
        if self.armed:
            super().on_quote_tick(tick)

    def _passive_quote(self, quote: MakerQuote | None, source_book: BookTop) -> MakerQuote | None:
        if quote is None:
            return None
        price = self.planned_price()
        if price <= 0 or price >= source_book.bid:
            raise PaperCanaryError("Maker canary passive price is invalid")
        return replace(quote, source_price_usdt=price)

    def _submit_source(self, quote: MakerQuote) -> None:
        if self.claimed or not self.armed or quote.direction is not SourceDirection.LONG:
            return
        if quote.quantity_ounces != QUANTITY:
            raise PaperCanaryError("Maker canary source quantity changed")
        self.claimed = True
        super()._submit_source(quote)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        owned = event.client_order_id.value == self.source_order_id
        if owned:
            self.venue_order_id = event.venue_order_id
        try:
            super().on_order_accepted(event)
        finally:
            if owned:
                with suppress(Exception):
                    self.cancel_owned_once()

    def cancel_owned_once(self) -> bool:
        order = self.source_order
        if (
            self.cancel_sent
            or order is None
            or order.is_closed
            or cast(bool, order.is_pending_cancel)
            or self.venue_order_id is None
            or order.venue_order_id != self.venue_order_id
        ):
            return False
        self.cancel_sent = True
        try:
            self.cancel_order(order, client_id=BITFINEX_CLIENT_ID)
        except Exception as exc:
            self._mark_source_unknown(
                order.client_order_id.value, f"Maker canary cancel raised {type(exc).__name__}"
            )
            self.failure_reason = "cancel_send_failed"
            raise
        return True

    def _cancel_working(
        self,
        direction: SourceDirection,
        *,
        expected_order_id: str | None = None,
        reason: str,
    ) -> None:
        if direction is not SourceDirection.LONG or (
            expected_order_id is not None and expected_order_id != self.source_order_id
        ):
            return
        with suppress(Exception):
            self.cancel_owned_once()

    def on_order_canceled(self, event: OrderCanceled) -> None:
        owned = event.client_order_id.value == self.source_order_id
        super().on_order_canceled(event)
        if owned:
            store = self._stores[SourceDirection.LONG]
            record = store.source_order(event.client_order_id.value)
            self.hold_before_rest = bool(
                record
                and record.status == "CANCELED"
                and store.active_source_order_id == event.client_order_id.value
                and store.halt_reason
            )
            if not self.hold_before_rest:
                self.failure_reason = "cancel_did_not_hold_before_rest"

    def _complete_source_terminal_query(
        self, direction: SourceDirection, event: OrderCanceled, report: OrderStatusReport | None
    ) -> None:
        super()._complete_source_terminal_query(direction, event, report)
        if direction is SourceDirection.LONG and self.resume_ready:
            self.resumed.set()

    def on_order_filled(self, event: OrderFilled) -> None:
        if (event.instrument_id, event.client_order_id.value) != (
            self._config.source_instrument_id,
            self.source_order_id,
        ):
            return
        if self._hedges[SourceDirection.LONG].has_seen_source_fill(event):
            return
        if self.venue_order_id is None:
            self.venue_order_id = event.venue_order_id
        elif event.venue_order_id != self.venue_order_id:
            self.failure_reason = "source_fill_venue_identity_mismatch"
        self.filled += event.last_qty.as_decimal()
        self.exposure.set()
        super().on_order_filled(event)

    def _submit_next_pending_hedge(self) -> None:
        pass

    def on_stop(self) -> None:
        # The bounded lifecycle owns the sole exact cancel; stopping must not retry it.
        pass


def parse_maker_canary_profile(raw: bytes | str) -> LiveMakerCanaryProfile:
    profile = cast(LiveMakerCanaryProfile, LiveMakerCanaryProfile.parse(raw))
    validate_maker_canary_profile(profile)
    return profile


def validate_maker_canary_profile(profile: LiveMakerCanaryProfile) -> None:
    execution, strategy = profile.bitfinex_exec_config, profile.strategy_config
    data, risk = profile.bitfinex_data_config, strategy.economics.risk
    if execution.api_key or execution.api_secret:
        raise ValueError("Maker canary profile must not contain credentials")
    paper = (
        execution.raw_symbol == data.raw_symbol == PAPER_RAW_SYMBOL
        and data.url == PUBLIC_WS_URL
        and execution.url == PRIVATE_WS_URL
        and execution.rest_url == REST_URL
        and execution.wallet_currency == "TESTUSDTF0"
        and execution.account_id.value == f"BITFINEX-PAPER-{execution.user_id}"
    )
    if not paper:
        raise ValueError("Maker canary requires the bound Bitfinex paper account and symbol")
    if len(strategy.source_accounts) != 1 or len(strategy.hedge_accounts) != 1:
        raise ValueError("Maker canary requires one source and one hedge account")
    source, hedge = strategy.source_accounts[0], strategy.hedge_accounts[0]
    limits = (
        strategy.economics.bid.open_quantity_ounces,
        source.max_long_ounces,
        source.max_short_ounces,
        hedge.max_long_ounces,
        hedge.max_short_ounces,
        risk.source_max_abs,
        risk.hedge_max_abs,
    )
    if any(value != QUANTITY for value in limits):
        raise ValueError("Maker canary quantity and risk limits must equal exactly 2oz")
    if not data.min_quantity <= QUANTITY <= data.max_quantity:
        raise ValueError("Maker canary instrument cannot express 2oz")
    if strategy.economics.ask.open_quantity_ounces != 0:
        raise ValueError("Maker canary ask side must be disabled")
    neutral = not (risk.source_min_keep_abs or risk.hedge_min_keep_abs or risk.only_long)
    leverage = expected_leverage(strategy.economics.margin_level, source.base_margin_level)
    if not neutral or leverage != 1 or profile.mt5_exec_config.expected_max_order_lots != D("0.02"):
        raise ValueError("Maker canary requires neutral 2oz risk and exactly 1x leverage")
    timeout = profile.connection_timeout_seconds
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or not 1 <= timeout <= 60
    ):
        raise ValueError("Maker canary connection timeout must be in [1, 60]")
    paths = _state_paths(profile)
    if len({path.resolve(strict=False) for path in paths}) != 3:
        raise ValueError("Maker canary state paths must be distinct")


def _state_paths(profile: LiveMakerCanaryProfile) -> tuple[Path, Path, Path]:
    prefix = profile.strategy_config.store_path_prefix
    if not prefix or prefix != prefix.strip():
        raise ValueError("Maker canary state prefix must be non-empty and trimmed")
    return (
        Path(profile.bitfinex_exec_config.cid_store_path),
        Path(f"{prefix}.bid.json"),
        Path(f"{prefix}.ask.json"),
    )


def run_maker_canary(
    profile: LiveMakerCanaryProfile,
    output: Path,
    *,
    execute: bool = False,
    signal_timeout_seconds: float = 60.0,
    environment: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    node_builder: Callable[..., tuple[TradingNode, MakerStrategy]] = build_live_maker_node,
) -> MakerCanaryResult:
    validate_maker_canary_profile(profile)
    if (
        isinstance(signal_timeout_seconds, bool)
        or not isinstance(signal_timeout_seconds, int | float)
        or not math.isfinite(signal_timeout_seconds)
        or not 1 <= signal_timeout_seconds <= 300
    ):
        raise ValueError("Maker canary signal timeout must be finite and in [1, 300]")
    resolved_output = output.resolve(strict=False)
    if resolved_output in {path.resolve(strict=False) for path in _state_paths(profile)}:
        raise ValueError("Maker canary transcript and state paths must be distinct")
    transcript, loop = _new_transcript(output), asyncio.new_event_loop()
    node: TradingNode | None = None
    strategy: MakerCanaryStrategy | None = None
    rest: BitfinexV1RestClient | None = None
    lock: TextIO | None = None
    evidence_attempted = False
    result = MakerCanaryResult("FAILED", "canary_not_started", output)
    started_ms = time.time_ns() // 1_000_000
    _write(
        transcript,
        {
            "kind": "started",
            "execute": execute,
            "symbol": PAPER_RAW_SYMBOL,
            "quantity": "2",
            "direction": "LONG",
            "ts_utc_ns": time.time_ns(),
        },
    )
    try:
        if execute:
            lock = _lock_canary(profile.bitfinex_exec_config.user_id)
            if any(path.exists() for path in _state_paths(profile)):
                raise PaperCanaryError("state_not_fresh")
            credentials = load_bitfinex_test_credentials(
                expected_user_id=profile.bitfinex_exec_config.user_id,
                environment=os.environ if environment is None else environment,
                env_file=env_file,
            )
            key, secret = credentials.api_key, credentials.api_secret
        else:
            key = secret = "OFFLINE-MAKER-CANARY"
        rest = BitfinexV1RestClient(
            api_key=key, api_secret=secret, timeout_secs=int(profile.connection_timeout_seconds)
        )
        if execute:
            first = loop.run_until_complete(read_snapshot(rest))
            _write(transcript, {"kind": "preflight", "phase": 1, **first.record()})
            if first.user_id != profile.bitfinex_exec_config.user_id:
                raise PaperCanaryError("paper_account_identity_mismatch")
            if first.holds:
                raise PaperCanaryError(first.holds[0])
        node, built = node_builder(
            bitfinex_data_config=profile.bitfinex_data_config,
            bitfinex_exec_config=struct_replace(
                profile.bitfinex_exec_config, api_key=key, api_secret=secret
            ),
            mt5_data_config=profile.mt5_data_config,
            mt5_exec_config=profile.mt5_exec_config,
            strategy_config=profile.strategy_config,
            loop=loop,
            connection_timeout_seconds=float(profile.connection_timeout_seconds),
            strategy_factory=MakerCanaryStrategy,
        )
        if not isinstance(built, MakerCanaryStrategy):
            raise RuntimeError("Maker canary builder returned the wrong strategy")
        strategy = built
        if not execute:
            result = MakerCanaryResult("VALIDATED", "one_long_2oz_maker_built_disarmed", output)
        else:
            snapshot, reason = loop.run_until_complete(
                _run_lifecycle(
                    node,
                    strategy,
                    rest,
                    profile.bitfinex_exec_config.user_id,
                    float(profile.connection_timeout_seconds),
                    signal_timeout_seconds,
                )
            )
            _write(transcript, {"kind": "preflight", "phase": 2, **snapshot.record()})
            if not strategy.claimed:
                result = MakerCanaryResult("HOLD", reason, output)
            else:
                evidence_attempted = True
                result = _qualify(
                    loop,
                    rest,
                    profile,
                    strategy,
                    output,
                    started_ms,
                    reason,
                    transcript,
                )
    except PaperCanaryError as exc:
        result = _exception_result(
            loop,
            rest,
            profile,
            strategy,
            output,
            started_ms,
            str(exc),
            transcript,
            evidence_attempted,
            unclaimed="HOLD",
        )
    except Exception as exc:
        _write(transcript, {"kind": "error", "error": type(exc).__name__})
        result = _exception_result(
            loop,
            rest,
            profile,
            strategy,
            output,
            started_ms,
            type(exc).__name__,
            transcript,
            evidence_attempted,
            unclaimed="FAILED",
        )
    finally:
        try:
            if node is not None:
                _dispose_node(node)
            elif not loop.is_closed():
                loop.close()
        except Exception:
            outcome: Outcome = (
                "FILLED_HOLD"
                if result.outcome == "FILLED_HOLD" or (strategy and strategy.filled)
                else ("UNKNOWN" if strategy and strategy.claimed else "FAILED")
            )
            result = MakerCanaryResult(outcome, "node_cleanup_failed", output, _order_id(strategy))
        if lock is not None:
            lock.close()
    _write(
        transcript,
        {
            "kind": "finished",
            "outcome": result.outcome,
            "reason": result.reason,
            "source_order_id": result.source_order_id,
            "ts_utc_ns": time.time_ns(),
        },
    )
    transcript.close()
    return result


async def _run_lifecycle(
    node: TradingNode,
    strategy: MakerCanaryStrategy,
    rest: BitfinexV1RestClient,
    user_id: int,
    connection_timeout: float,
    signal_timeout: float,
) -> tuple[PaperSnapshot, str]:
    task = asyncio.create_task(node.run_async())
    reason = "lifecycle_timeout"
    try:
        await _wait_ready(node, strategy, task, connection_timeout * 4)
        snapshot = await asyncio.wait_for(read_snapshot(rest), connection_timeout)
        if snapshot.user_id != user_id or snapshot.holds or not _runtime_flat(node, strategy):
            return snapshot, "pre_arm_runtime_not_flat"
        await _wait_ready(node, strategy, task, connection_timeout * 4)
        price = strategy.planned_price()
        if snapshot.available < QUANTITY * price * D("1.01"):
            return snapshot, "available_balance_cannot_cover_1x_canary"
        strategy.arm()
        if not strategy.trigger_cached_once():
            return snapshot, "cached_quote_not_eligible"
        deadline = asyncio.get_running_loop().time() + signal_timeout
        while asyncio.get_running_loop().time() < deadline:
            if task.done():
                _raise_node_task(task, "Maker node stopped during canary")
            if strategy.exposure.is_set():
                reason = "source_fill_observed"
                break
            if strategy.failure_reason:
                reason = strategy.failure_reason
                break
            if strategy.resumed.is_set() and strategy.resume_ready:
                reason = "hold_rest_resume_observed"
                break
            await asyncio.sleep(0.01)
        return snapshot, reason
    finally:
        strategy._disarm_unclaimed()
        try:
            strategy.cleanup_reason = await _cleanup_owned(
                strategy, task, min(connection_timeout * 2, 12.0)
            )
        except Exception as exc:
            strategy.cleanup_reason = f"owned_cleanup_{type(exc).__name__}"
        await _shutdown(node, task, connection_timeout)


async def _wait_ready(
    node: TradingNode, strategy: MakerCanaryStrategy, task: asyncio.Task[None], timeout: float
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if task.done():
            _raise_node_task(task, "Maker node stopped before readiness")
        source = strategy.cache.quote_tick(strategy._config.source_instrument_id)
        hedge = strategy.cache.quote_tick(strategy._config.hedge_instrument_id)
        ready = (
            node.is_running()
            and strategy.is_running
            and node.kernel.data_engine.check_connected()
            and node.kernel.exec_engine.check_connected()
            and source is not None
            and hedge is not None
        )
        if ready and strategy._inputs_are_fresh(
            source, hedge, cast(int, strategy.clock.timestamp_ns())
        ):
            return
        await asyncio.sleep(0.01)
    raise PaperCanaryError("Maker readiness timed out")


def _raise_node_task(task: asyncio.Task[None], reason: str) -> None:
    if task.cancelled():
        raise RuntimeError(f"{reason}: canceled")
    failure = task.exception()
    if failure is not None:
        raise failure
    raise RuntimeError(reason)


def _runtime_flat(node: TradingNode, strategy: MakerCanaryStrategy) -> bool:
    config = strategy._config
    routes = (
        (config.source_instrument_id, config.source_accounts[0].account_id),
        (config.hedge_instrument_id, config.hedge_accounts[0].account_id),
    )
    return all(
        not node.cache.orders_open(instrument_id=i, account_id=a)
        and not node.cache.positions_open(instrument_id=i, account_id=a)
        and node.portfolio.net_position(i, a) == 0
        for i, a in routes
    )


async def _cleanup_owned(
    strategy: MakerCanaryStrategy,
    task: asyncio.Task[None],
    timeout: float,
) -> str:
    if not strategy.claimed:
        return "not_claimed"
    if strategy.failure_reason == "cancel_send_failed":
        return "owned_cancel_send_failed"
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if strategy.owned_terminal_settled:
            return "owned_terminal_settled"
        if task.done():
            return "node_stopped_before_owned_terminal"
        try:
            strategy.cancel_owned_once()
        except Exception:
            return "owned_cancel_send_failed"
        await asyncio.sleep(0.01)
    return "owned_order_missing" if strategy.source_order is None else "owned_cleanup_timeout"


async def _shutdown(node: TradingNode, task: asyncio.Task[None], timeout: float) -> None:
    if node.is_running():
        await asyncio.wait_for(node.stop_async(), min(timeout, 6.0))
    if not task.done():
        await asyncio.sleep(0)
    if not task.done():
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), min(timeout, 6.0))


def _qualify(
    loop: asyncio.AbstractEventLoop,
    rest: BitfinexV1RestClient,
    profile: LiveMakerCanaryProfile,
    strategy: MakerCanaryStrategy,
    output: Path,
    started_ms: int,
    reason: str,
    transcript: TextIO,
    *,
    allow_pass: bool = True,
) -> MakerCanaryResult:
    order, order_id = strategy.source_order, strategy.source_order_id
    cid_path = Path(profile.bitfinex_exec_config.cid_store_path)
    account_id = profile.bitfinex_exec_config.account_id.value
    binding = (
        BitfinexV1CidStore(cid_path, account_id=account_id).binding_for_client(order_id)
        if order_id and cid_path.exists()
        else None
    )
    final = loop.run_until_complete(read_snapshot(rest))
    evidence = None
    if order is not None and binding is not None:
        venue_id = int(strategy.venue_order_id.value) if strategy.venue_order_id else None
        evidence = loop.run_until_complete(
            read_owned_evidence(
                rest,
                cid=binding.cid,
                venue_order_id=venue_id,
                price=order.price.as_decimal(),
                start_ms=max(0, started_ms - 1_000),
                same_run_ownership_venue_order_id=venue_id if strategy.cancel_sent else None,
                post_only_intent_submitted=bool(order.is_post_only),
            )
        )
    _write(
        transcript,
        {
            "kind": "mutation",
            "client_order_id": order_id,
            "cid": binding.cid if binding else None,
            "venue_order_id": strategy.venue_order_id.value if strategy.venue_order_id else None,
            "filled": format(strategy.filled, "f"),
            "hold_before_rest": strategy.hold_before_rest,
            "resume_ready": strategy.resume_ready,
            "post_resume_quote": strategy.post_resume_quote.is_set(),
            "lifecycle_reason": reason,
            "cleanup_reason": strategy.cleanup_reason,
        },
    )
    _write(transcript, {"kind": "final", **final.record()})
    if evidence:
        _write(transcript, {"kind": "owned_order_evidence", **evidence.record()})
    exposure = evidence and (evidence.fill_indicated or evidence.trade_ids)
    if strategy.filled or final.position_quantity or exposure:
        return MakerCanaryResult(
            "FILLED_HOLD", "source_exposure_requires_manual_review", output, order_id
        )
    stores_clean = (
        len(strategy._stores[SourceDirection.LONG].source_orders()) == 1
        and not strategy._stores[SourceDirection.SHORT].source_orders()
        and all(store.can_submit_source() for store in strategy._stores.values())
    )
    passed = (
        allow_pass
        and reason == "hold_rest_resume_observed"
        and strategy.cleanup_reason == "owned_terminal_settled"
        and strategy.cancel_sent
        and strategy.resume_ready
        and stores_clean
        and binding is not None
        and final.user_id == profile.bitfinex_exec_config.user_id
        and not final.holds
        and evidence is not None
        and evidence.complete
        and not evidence.active_venue_ids
        and not evidence.trade_ids
        and evidence.terminal_lifecycle_exact
    )
    return MakerCanaryResult(
        "PASSED" if passed else "UNKNOWN",
        "maker_cancel_terminal_resume_qualified" if passed else "maker_final_evidence_not_clean",
        output,
        order_id,
    )


def _exception_result(
    loop: asyncio.AbstractEventLoop,
    rest: BitfinexV1RestClient | None,
    profile: LiveMakerCanaryProfile,
    strategy: MakerCanaryStrategy | None,
    output: Path,
    started_ms: int,
    reason: str,
    transcript: TextIO,
    evidence_attempted: bool,
    *,
    unclaimed: Literal["HOLD", "FAILED"],
) -> MakerCanaryResult:
    if strategy is None or not strategy.claimed:
        return MakerCanaryResult(unclaimed, reason, output, _order_id(strategy))
    fallback: Outcome = "FILLED_HOLD" if strategy.filled else "UNKNOWN"
    if rest is None or evidence_attempted or loop.is_closed():
        return MakerCanaryResult(fallback, reason, output, strategy.source_order_id)
    try:
        observed = _qualify(
            loop,
            rest,
            profile,
            strategy,
            output,
            started_ms,
            f"exception:{reason}",
            transcript,
            allow_pass=False,
        )
    except Exception as evidence_error:
        _write(
            transcript,
            {"kind": "exception_evidence_error", "error": type(evidence_error).__name__},
        )
        return MakerCanaryResult(fallback, reason, output, strategy.source_order_id)
    if observed.outcome == "FILLED_HOLD":
        return observed
    return MakerCanaryResult("UNKNOWN", reason, output, strategy.source_order_id)


def _order_id(strategy: MakerCanaryStrategy | None) -> str | None:
    return strategy.source_order_id if strategy else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or run one 2oz Maker cancel canary")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--signal-timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    try:
        result = run_maker_canary(
            parse_maker_canary_profile(args.profile.read_bytes()),
            args.output,
            execute=args.execute,
            signal_timeout_seconds=args.signal_timeout,
            env_file=args.env_file,
        )
    except Exception as exc:
        print(json.dumps({"outcome": "FAILED", "reason": type(exc).__name__}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "outcome": result.outcome,
                "reason": result.reason,
                "source_order_id": result.source_order_id,
            },
            sort_keys=True,
        )
    )
    codes = {"VALIDATED": 0, "PASSED": 0, "HOLD": 2, "FAILED": 1, "UNKNOWN": 3, "FILLED_HOLD": 4}
    return codes[result.outcome]


if __name__ == "__main__":
    raise SystemExit(main())
