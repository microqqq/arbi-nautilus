from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveDataClientConfig, RoutingConfig
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId

import py000_nautilus.mt5_v1_observe as observe
from py000_nautilus.mt5_v1_data import (
    Mt5V1DataClient,
    Mt5V1DataClientConfig,
    Mt5V1LiveDataClientFactory,
)
from py000_nautilus.mt5_v1_observe import Mt5V1ShadowObserver, run_mt5_v1_shadow
from py000_nautilus.mt5_v1_protocol import Binding, Identity, JsonObject, RecoveryState
from py000_nautilus.mt5_v1_transport import Mt5V1TransportError

ROOT = Path(__file__).parents[1]
FIXTURE = cast(
    dict[str, object],
    json.loads((ROOT / "tests" / "fixtures" / "mt5_ea_v1_readonly.json").read_text()),
)


class _FakeTransport:
    def __init__(
        self,
        *,
        pub_kind: Literal["tick", "heartbeat"] = "tick",
        fail_open: bool = False,
        fail_after_pub: bool = False,
        silence_after_pub: bool = False,
    ) -> None:
        self.identity = Identity.from_wire(FIXTURE["identity"])
        self.pub_kind = pub_kind
        self.fail_open = fail_open
        self.fail_after_pub = fail_after_pub
        self.silence_after_pub = silence_after_pub
        self.pub_count = 0
        self.opened = False
        self.closed = False

    @property
    def topic(self) -> bytes:
        return b"XAUUSD"

    async def open(self) -> None:
        self.opened = True
        if self.fail_open:
            raise Mt5V1TransportError("synthetic open failure")

    async def close(self) -> None:
        self.closed = True

    async def hello(self) -> tuple[Identity, RecoveryState]:
        return self.identity, "ready"

    async def snapshot(self, binding: Binding) -> JsonObject:
        assert binding == self.identity.binding()
        return _fresh_snapshot(self.identity)

    async def recv_pub(self) -> tuple[bytes, JsonObject]:
        await asyncio.sleep(0.02)
        if self.silence_after_pub and self.pub_count:
            await asyncio.Event().wait()
        if self.fail_after_pub and self.pub_count:
            raise Mt5V1TransportError("synthetic PUB failure")
        self.pub_count += 1
        return b"XAUUSD", _fresh_pub(self.identity, self.pub_kind)


class _LifecycleNode:
    def __init__(
        self,
        run: Callable[[], Awaitable[None]],
        stop: Callable[[], Awaitable[None]],
        *,
        running: bool,
    ) -> None:
        self._run = run
        self._stop = stop
        self._running = running

    async def run_async(self) -> None:
        await self._run()

    async def stop_async(self) -> None:
        await self._stop()

    def is_running(self) -> bool:
        return self._running


def _config() -> Mt5V1DataClientConfig:
    identity = Identity.from_wire(FIXTURE["identity"])
    return Mt5V1DataClientConfig(
        pub_url="tcp://127.0.0.1:6101",
        rep_url="tcp://127.0.0.1:6102",
        instrument_id=InstrumentId.from_str(identity.binding().symbol + ".MT5"),
        expected_account_id=identity.account_id,
        expected_symbol=identity.symbol,
        expected_magic=identity.magic,
        expected_ea_build_id=identity.ea_build_id,
        expected_source_sha256=identity.declared_source_sha256,
        expected_server_timezone=identity.server_timezone,
        request_timeout_ms=100,
        snapshot_interval_ms=250,
        max_snapshot_age_ms=1_000,
        max_tick_age_ms=1_000,
        routing=RoutingConfig(default=False, venues=frozenset({"MT5"})),
    )


def _install_fake_factory(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeTransport,
) -> None:
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LiveDataClient:
        assert isinstance(config, Mt5V1DataClientConfig)
        return Mt5V1DataClient(
            loop=loop,
            name=name,
            config=config,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(config=config.instrument_provider),
            transport=fake,
        )

    monkeypatch.setattr(Mt5V1LiveDataClientFactory, "create", staticmethod(create))


@pytest.mark.parametrize("pub_kind", ["tick", "heartbeat"])
def test_bounded_observer_records_public_data_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pub_kind: Literal["tick", "heartbeat"],
) -> None:
    fake = _FakeTransport(pub_kind=pub_kind)
    _install_fake_factory(monkeypatch, fake)
    transcript = tmp_path / f"{pub_kind}.jsonl"

    result = run_mt5_v1_shadow(
        _config(),
        duration_seconds=0.35,
        startup_timeout_seconds=0.5,
        transcript_path=transcript,
    )

    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    header = records[0]
    summary = records[-1]
    identity = Identity.from_wire(FIXTURE["identity"])
    assert result.outcome == "OBSERVED"
    assert len(records) == 2
    assert header["schema"] == "py000.mt5.shadow-observation"
    assert header["version"] == 1
    assert header["max_pub_silence_ms"] == 3_000
    assert header["expected_identity"] == {
        "account_id": identity.account_id,
        "symbol": identity.symbol,
        "magic": identity.magic,
        "ea_build_id": identity.ea_build_id,
        "source_sha256": identity.declared_source_sha256,
        "server_timezone": identity.server_timezone,
    }
    assert summary["committed_snapshot_count"] >= 2
    assert summary["identity_matched_pub_count"] >= 1
    assert summary["identity_matched_pub_age_ms"] <= 3_000
    assert summary["observed_identity"] == identity.to_wire()
    assert summary["status_count"] >= 2
    assert len(summary["samples"]["quotes"]) <= 2
    assert len(summary["samples"]["statuses"]) <= 2
    if pub_kind == "tick":
        assert summary["quote_count"] >= 1
    else:
        assert summary["quote_count"] == 0
    assert records[0]["kind"] == "run_started"
    assert records[-1]["kind"] == "run_finished"
    assert all(record.get("source_admission_ready") is not True for record in records)
    assert fake.opened and fake.closed


def test_single_pub_then_silence_is_inconclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTransport(pub_kind="heartbeat", silence_after_pub=True)
    _install_fake_factory(monkeypatch, fake)
    transcript = tmp_path / "silent.jsonl"
    config = _config()

    result = run_mt5_v1_shadow(
        config,
        duration_seconds=0.35,
        startup_timeout_seconds=0.5,
        max_pub_silence_ms=250,
        transcript_path=transcript,
    )

    summary = json.loads(transcript.read_text().splitlines()[-1])
    assert result.outcome == "INCONCLUSIVE"
    assert result.reason == "identity_matched_pub_stale"
    assert summary["identity_matched_pub_count"] == 1
    assert summary["identity_matched_pub_age_ms"] > 250


def test_dispose_failure_cannot_leave_observed_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTransport(pub_kind="heartbeat")
    _install_fake_factory(monkeypatch, fake)
    transcript = tmp_path / "dispose-failed.jsonl"
    dispose = observe._dispose_node
    calls = 0

    def fail_first_dispose(node: TradingNode) -> None:
        nonlocal calls
        calls += 1
        dispose(node)
        if calls == 1:
            raise RuntimeError("synthetic dispose failure")

    monkeypatch.setattr(observe, "_dispose_node", fail_first_dispose)
    with pytest.raises(RuntimeError, match="synthetic dispose failure"):
        run_mt5_v1_shadow(
            _config(),
            duration_seconds=0.35,
            startup_timeout_seconds=0.5,
            transcript_path=transcript,
        )

    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert [record["kind"] for record in records] == ["run_started", "run_failed"]


def test_disconnect_fails_and_retains_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTransport(pub_kind="heartbeat", fail_after_pub=True)
    _install_fake_factory(monkeypatch, fake)
    transcript = tmp_path / "disconnect.jsonl"

    with pytest.raises(ConnectionError, match="disconnected"):
        run_mt5_v1_shadow(
            _config(),
            duration_seconds=0.5,
            startup_timeout_seconds=0.5,
            transcript_path=transcript,
        )

    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert records[-1]["kind"] == "run_failed"
    assert records[-1]["reason"] == "ConnectionError"
    assert records[-1]["data_client_failure"].startswith("Mt5V1TransportError:")
    assert fake.closed


def test_startup_timeout_and_existing_output_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTransport(fail_open=True)
    _install_fake_factory(monkeypatch, fake)
    transcript = tmp_path / "failed.jsonl"
    with pytest.raises(TimeoutError, match="first MT5 status"):
        run_mt5_v1_shadow(
            _config(),
            duration_seconds=0.1,
            startup_timeout_seconds=0.05,
            transcript_path=transcript,
        )
    assert json.loads(transcript.read_text().splitlines()[-1])["kind"] == "run_failed"
    assert fake.closed

    transcript.write_text("keep\n")
    with pytest.raises(FileExistsError):
        run_mt5_v1_shadow(
            _config(),
            duration_seconds=0.1,
            transcript_path=transcript,
        )
    assert transcript.read_text() == "keep\n"


@pytest.mark.parametrize("duration", [0.0, -1.0, 301.0, float("inf"), float("nan")])
def test_invalid_duration_is_rejected_before_creating_transcript(
    tmp_path: Path,
    duration: float,
) -> None:
    transcript = tmp_path / "invalid.jsonl"
    with pytest.raises(ValueError, match="duration_seconds"):
        run_mt5_v1_shadow(
            _config(),
            duration_seconds=duration,
            transcript_path=transcript,
        )
    assert not transcript.exists()


def test_simultaneous_node_stop_does_not_hide_observation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def done() -> None:
        return

    async def disconnected(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("specific disconnect")

    monkeypatch.setattr(observe, "_bounded_observation", disconnected)
    node = _LifecycleNode(done, done, running=False)
    with pytest.raises(ConnectionError, match="specific disconnect"):
        _run_lifecycle_probe(node)


def test_shutdown_timeout_is_a_note_not_a_replacement_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forever() -> None:
        await asyncio.Event().wait()

    async def disconnected(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("primary disconnect")

    monkeypatch.setattr(observe, "_bounded_observation", disconnected)
    monkeypatch.setattr(observe, "SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    node = _LifecycleNode(forever, forever, running=True)
    with pytest.raises(ConnectionError, match="primary disconnect") as caught:
        _run_lifecycle_probe(node)
    assert any("shutdown also failed" in note for note in caught.value.__notes__)


def test_node_failure_during_stop_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def fail_after_stop() -> None:
        await release.wait()
        raise RuntimeError("node failed while stopping")

    async def stop() -> None:
        release.set()
        await asyncio.sleep(0)

    async def observed(*_args: object, **_kwargs: object) -> None:
        return

    monkeypatch.setattr(observe, "_bounded_observation", observed)
    node = _LifecycleNode(fail_after_stop, stop, running=True)
    with pytest.raises(RuntimeError, match="node failed while stopping"):
        _run_lifecycle_probe(node)


def _run_lifecycle_probe(node: _LifecycleNode) -> None:
    observer = Mt5V1ShadowObserver(InstrumentId.from_str("XAUUSD.MT5"))
    asyncio.run(
        observe._observe_window(
            cast(TradingNode, node),
            observer,
            startup_timeout_seconds=0.1,
            duration_seconds=0.1,
        )
    )


def _fresh_snapshot(identity: Identity) -> JsonObject:
    snapshot = deepcopy(cast(JsonObject, FIXTURE["snapshot"]))
    now_ms = time.time_ns() // 1_000_000
    server_ms = _server_wall_ms(now_ms, identity.server_timezone)
    cast(JsonObject, snapshot["time"])["observed_utc_ms"] = str(now_ms)
    cast(JsonObject, snapshot["time"])["server_quote_time_ms"] = str(server_ms)
    cast(JsonObject, snapshot["session"])["sample_server_time_ms"] = str(server_ms)
    snapshot.update(
        identity=identity.to_wire(),
        recovery_state="ready",
        execution_enabled=False,
    )
    return snapshot


def _fresh_pub(identity: Identity, kind: Literal["tick", "heartbeat"]) -> JsonObject:
    index = 0 if kind == "tick" else 1
    message = deepcopy(cast(list[JsonObject], FIXTURE["pub"])[index])
    now_ms = time.time_ns() // 1_000_000
    server_ms = _server_wall_ms(now_ms, identity.server_timezone)
    message.update(identity=identity.to_wire(), protocol="py000.mt5", version=1)
    message["event_time_ms"] = str(server_ms if kind == "tick" else now_ms)
    if kind == "heartbeat":
        message["last_tick_time_ms"] = str(server_ms)
    return message


def _server_wall_ms(utc_ms: int, timezone_name: str) -> int:
    observed = datetime.fromtimestamp(utc_ms / 1_000, tz=UTC)
    server_wall = observed.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=UTC)
    return int(server_wall.timestamp() * 1_000)
