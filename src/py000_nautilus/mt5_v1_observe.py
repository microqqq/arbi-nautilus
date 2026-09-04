"""One-shot, data-only MT5 shadow observation on NautilusTrader."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.config import RoutingConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import InstrumentStatus, QuoteTick
from nautilus_trader.model.identifiers import ComponentId, InstrumentId

from py000_nautilus.mt5_v1_data import Mt5V1DataClient, Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_shadow import MT5_CLIENT_ID, MT5_VENUE, build_mt5_v1_shadow_node

DEFAULT_PUB_URL = "tcp://10.211.55.13:6001"
DEFAULT_REP_URL = "tcp://10.211.55.13:6002"
INSTRUMENT_ID = InstrumentId.from_str("XAUUSD.MT5")
STARTUP_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 6.0
MAX_PUB_SILENCE_MS = 3_000
type Record = dict[str, object]


class Mt5V1ShadowObserver(Actor):  # type: ignore[misc, unused-ignore]
    """Observe the public MT5 data surface; this actor has no order API."""

    def __init__(self, instrument_id: InstrumentId) -> None:
        super().__init__(ActorConfig(component_id=ComponentId("MT5-SHADOW-OBSERVER")))
        self._instrument_id = instrument_id
        self.first_status = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.quote_count = 0
        self.status_count = 0
        self.samples: dict[str, list[Record]] = {"quotes": [], "statuses": []}

    def on_start(self) -> None:
        self.subscribe_quote_ticks(self._instrument_id, client_id=MT5_CLIENT_ID)
        self.subscribe_instrument_status(self._instrument_id, client_id=MT5_CLIENT_ID)

    def on_stop(self) -> None:
        self.unsubscribe_quote_ticks(self._instrument_id, client_id=MT5_CLIENT_ID)
        self.unsubscribe_instrument_status(self._instrument_id, client_id=MT5_CLIENT_ID)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if tick.instrument_id != self._instrument_id:
            return
        self.quote_count += 1
        self._sample(
            "quotes",
            {
                "bid": str(tick.bid_price),
                "ask": str(tick.ask_price),
                "ts_event_ns": tick.ts_event,
                "ts_init_ns": tick.ts_init,
            },
        )

    def on_instrument_status(self, status: InstrumentStatus) -> None:
        if status.instrument_id != self._instrument_id:
            return
        self.status_count += 1
        self._sample(
            "statuses",
            {
                "action": status.action.name,
                "reason": status.reason,
                "is_trading": status.is_trading,
                "ts_event_ns": status.ts_event,
                "ts_init_ns": status.ts_init,
            },
        )
        self.first_status.set()
        if status.reason == "mt5_data_client_disconnected":
            self.disconnected.set()

    def _sample(self, kind: str, record: Record) -> None:
        samples = self.samples[kind]
        if len(samples) < 2:
            samples.append(record)
        else:
            samples[-1] = record


@dataclass(frozen=True, slots=True)
class Mt5V1ShadowResult:
    outcome: Literal["OBSERVED", "INCONCLUSIVE"]
    reason: str
    transcript_path: Path


class _Transcript:
    def __init__(self, path: Path) -> None:
        self._stream = path.open("x", encoding="utf-8")

    def write(self, record: Record) -> None:
        self._stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


async def _bounded_observation(
    observer: Mt5V1ShadowObserver,
    *,
    startup_timeout_seconds: float,
    duration_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(observer.first_status.wait(), startup_timeout_seconds)
    except TimeoutError as exc:
        raise TimeoutError("timed out waiting for the first MT5 status") from exc
    if observer.disconnected.is_set():
        raise ConnectionError("MT5 data client disconnected")
    try:
        await asyncio.wait_for(observer.disconnected.wait(), duration_seconds)
    except TimeoutError:
        if observer.disconnected.is_set():
            raise ConnectionError("MT5 data client disconnected") from None
        return
    raise ConnectionError("MT5 data client disconnected")


async def _observe_window(
    node: TradingNode,
    observer: Mt5V1ShadowObserver,
    *,
    startup_timeout_seconds: float,
    duration_seconds: float,
) -> None:
    run_task = asyncio.create_task(node.run_async())
    window_task = asyncio.create_task(
        _bounded_observation(
            observer,
            startup_timeout_seconds=startup_timeout_seconds,
            duration_seconds=duration_seconds,
        )
    )
    primary: BaseException | None = None
    try:
        done, _ = await asyncio.wait(
            {run_task, window_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if window_task in done:
            await window_task
            if run_task in done:
                await run_task
                raise RuntimeError("TradingNode stopped during MT5 observation")
        else:
            await run_task
            raise RuntimeError("TradingNode stopped during MT5 observation")
    except BaseException as exc:
        primary = exc

    shutdown_error: BaseException | None = None
    try:
        if node.is_running():
            await asyncio.wait_for(node.stop_async(), SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        shutdown_error = TimeoutError("timed out stopping TradingNode")
        shutdown_error.__cause__ = exc
    except BaseException as exc:
        shutdown_error = exc

    if not run_task.done():
        await asyncio.sleep(0)
    for task in (run_task, window_task):
        if not task.done():
            task.cancel()
    _, pending = await asyncio.wait(
        {run_task, window_task},
        timeout=SHUTDOWN_TIMEOUT_SECONDS,
    )
    if pending:
        drain_error = TimeoutError("timed out awaiting TradingNode shutdown")
        if shutdown_error is None:
            shutdown_error = drain_error
        else:
            shutdown_error.add_note(f"task drain also failed: {drain_error!r}")
    run_error = (
        run_task.exception() if run_task.done() and not run_task.cancelled() else None
    )
    if primary is not None:
        if shutdown_error is not None:
            primary.add_note(f"shutdown also failed: {shutdown_error!r}")
        if run_error is not None and run_error is not primary:
            primary.add_note(f"TradingNode also failed: {run_error!r}")
        raise primary
    if shutdown_error is not None:
        if run_error is not None and run_error is not shutdown_error:
            shutdown_error.add_note(f"TradingNode also failed: {run_error!r}")
        raise shutdown_error
    if run_error is not None:
        raise run_error


def run_mt5_v1_shadow(
    data_config: Mt5V1DataClientConfig,
    *,
    duration_seconds: float,
    transcript_path: Path,
    startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
    max_pub_silence_ms: int = MAX_PUB_SILENCE_MS,
) -> Mt5V1ShadowResult:
    """Run one bounded observation and retain a non-overwriting summary transcript."""
    if not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 300:
        raise ValueError("duration_seconds must be finite and in (0, 300]")
    if not math.isfinite(startup_timeout_seconds) or not 0 < startup_timeout_seconds <= 60:
        raise ValueError("startup_timeout_seconds must be finite and in (0, 60]")
    if not 250 <= max_pub_silence_ms <= 60_000:
        raise ValueError("max_pub_silence_ms must be in [250, 60000]")

    transcript = _Transcript(transcript_path)
    loop = asyncio.new_event_loop()
    node: TradingNode | None = None
    observer: Mt5V1ShadowObserver | None = None
    client: Mt5V1DataClient | None = None
    try:
        transcript.write(_run_started(data_config, duration_seconds, max_pub_silence_ms))
        node = build_mt5_v1_shadow_node(
            data_config,
            loop=loop,
            connection_timeout_seconds=startup_timeout_seconds,
        )
        routed_client = node.kernel.data_engine.routing_map[MT5_VENUE]
        if not isinstance(routed_client, Mt5V1DataClient):
            raise RuntimeError("MT5 shadow route is not the v1 data client")
        client = routed_client
        observer = Mt5V1ShadowObserver(data_config.instrument_id)
        node.trader.add_actor(observer)
        loop.run_until_complete(
            _observe_window(
                node,
                observer,
                startup_timeout_seconds=startup_timeout_seconds,
                duration_seconds=duration_seconds,
            )
        )

        reason = _outcome_reason(observer, client, max_pub_silence_ms)
        facts = _facts(observer, client)
        outcome: Literal["OBSERVED", "INCONCLUSIVE"] = (
            "OBSERVED" if reason == "bounded_read_only_observation_complete" else "INCONCLUSIVE"
        )
        owned_node = node
        node = None
        _dispose_node(owned_node)
        result = Mt5V1ShadowResult(
            outcome=outcome,
            reason=reason,
            transcript_path=transcript_path,
        )
        transcript.write(
            {
                "kind": "run_finished",
                "outcome": outcome,
                "reason": reason,
                **facts,
            }
        )
        return result
    except BaseException as exc:
        transcript.write(
            {
                "kind": "run_failed",
                "reason": type(exc).__name__,
                "message": str(exc)[:300],
                "notes": list(getattr(exc, "__notes__", ())),
                **_facts(observer, client),
            }
        )
        raise
    finally:
        try:
            if node is not None:
                _dispose_node(node)
            elif not loop.is_closed():
                loop.close()
        finally:
            transcript.close()


def _run_started(
    config: Mt5V1DataClientConfig,
    duration_seconds: float,
    max_pub_silence_ms: int,
) -> Record:
    return {
        "schema": "py000.mt5.shadow-observation",
        "version": 1,
        "kind": "run_started",
        "ts_utc_ns": time.time_ns(),
        "duration_seconds": duration_seconds,
        "max_pub_silence_ms": max_pub_silence_ms,
        "pub_url": config.pub_url,
        "rep_url": config.rep_url,
        "instrument_id": str(config.instrument_id),
        "expected_identity": {
            "account_id": config.expected_account_id,
            "symbol": config.expected_symbol,
            "magic": config.expected_magic,
            "ea_build_id": config.expected_ea_build_id,
            "source_sha256": config.expected_source_sha256,
            "server_timezone": config.expected_server_timezone,
        },
        "composition": {"data_clients": ["MT5"], "execution_clients": 0, "strategies": 0},
        "source_admission_ready": False,
    }


def _facts(
    observer: Mt5V1ShadowObserver | None,
    client: Mt5V1DataClient | None,
) -> Record:
    identity = client.observed_identity if client else None
    return {
        "quote_count": observer.quote_count if observer else 0,
        "status_count": observer.status_count if observer else 0,
        "committed_snapshot_count": client.committed_snapshot_count if client else 0,
        "identity_matched_pub_count": client.identity_matched_pub_count if client else 0,
        "identity_matched_pub_age_ms": client.identity_matched_pub_age_ms if client else None,
        "observed_identity": identity.to_wire() if identity else None,
        "data_client_failure": client.last_failure if client else None,
        "samples": observer.samples if observer else {"quotes": [], "statuses": []},
        "source_admission_ready": False,
        "ts_utc_ns": time.time_ns(),
    }


def _outcome_reason(
    observer: Mt5V1ShadowObserver,
    client: Mt5V1DataClient,
    max_pub_silence_ms: int,
) -> str:
    if client.last_failure is not None:
        return "data_client_failed"
    if observer.status_count < 2 or client.committed_snapshot_count < 2:
        return "insufficient_rep_snapshots"
    if client.identity_matched_pub_count < 1:
        return "no_identity_matched_pub"
    age_ms = client.identity_matched_pub_age_ms
    if age_ms is None or age_ms > max_pub_silence_ms:
        return "identity_matched_pub_stale"
    return "bounded_read_only_observation_complete"


def _dispose_node(node: TradingNode) -> None:
    try:
        if not node.kernel.loop.is_closed():
            node.kernel.cancel_all_tasks()
    finally:
        node.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded, read-only MT5 shadow observation")
    parser.add_argument("--pub-url", default=DEFAULT_PUB_URL)
    parser.add_argument("--rep-url", default=DEFAULT_REP_URL)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-seconds", default=30.0, type=float)
    parser.add_argument("--max-pub-silence-ms", default=MAX_PUB_SILENCE_MS, type=int)
    args = parser.parse_args()
    config = Mt5V1DataClientConfig(
        pub_url=args.pub_url,
        rep_url=args.rep_url,
        instrument_id=INSTRUMENT_ID,
        expected_account_id=args.expected_account_id,
        expected_symbol="XAUUSD",
        expected_magic="900000001",
        expected_ea_build_id="py000-mt5-ea-v1-readonly",
        expected_source_sha256=args.expected_source_sha256,
        expected_server_timezone="Europe/Athens",
        routing=RoutingConfig(default=False, venues=frozenset({"MT5"})),
    )
    try:
        result = run_mt5_v1_shadow(
            config,
            duration_seconds=args.duration_seconds,
            transcript_path=args.output,
            max_pub_silence_ms=args.max_pub_silence_ms,
        )
    except Exception as exc:
        print(json.dumps({"outcome": "FAILED", "reason": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({"outcome": result.outcome, "reason": result.reason}))
    return 0 if result.outcome == "OBSERVED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
