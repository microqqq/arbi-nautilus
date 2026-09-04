from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import msgspec
import pytest
from msgspec.structs import replace as struct_replace
from nautilus_trader.config import RoutingConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId

import py000_nautilus.live_taker_entry as live_taker_entry
from py000_nautilus.bitfinex_v1_data import (
    PAPER_RAW_SYMBOL,
    BitfinexV1DataClientConfig,
)
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecClientConfig
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport
from py000_nautilus.config import (
    CarryConfig,
    FxConfig,
    RiskConfig,
    SourceAccountRoute,
    TakerEconomicsConfig,
    TakerStrategyConfig,
)
from py000_nautilus.live_taker import build_live_taker_node
from py000_nautilus.live_taker_entry import (
    LiveTakerEntryError,
    LiveTakerProfile,
    load_bitfinex_test_credentials,
    main,
    parse_live_taker_profile,
    run_bounded_rehearsal,
    run_live_taker_entry,
    validate_live_taker_profile,
)
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig
from py000_nautilus.mt5_v1_transport import Mt5V1Transport
from py000_nautilus.strategies.taker import TakerStrategy

SOURCE_ID = InstrumentId.from_str("XAUTUSDT-PERP.BITFINEX")
HEDGE_ID = InstrumentId.from_str("XAUUSD.MT5")
BITFINEX_ID = ClientId("BITFINEX")
MT5_ID = ClientId("MT5")
USER_ID = 269_312


def _profile(tmp_path: Path) -> LiveTakerProfile:
    bitfinex_routing = RoutingConfig(default=False, venues=frozenset({"BITFINEX"}))
    mt5_routing = RoutingConfig(default=False, venues=frozenset({"MT5"}))
    bitfinex_data = BitfinexV1DataClientConfig(
        url="wss://offline.invalid/ws/2",
        instrument_id=SOURCE_ID,
        raw_symbol=PAPER_RAW_SYMBOL,
        price_precision=1,
        size_precision=8,
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.00000001"),
        min_quantity=Decimal(2),
        max_quantity=Decimal(10_000),
        margin_init=Decimal("0.01"),
        margin_maint=Decimal("0.005"),
        maker_fee=Decimal(0),
        taker_fee=Decimal("0.0002"),
        routing=bitfinex_routing,
    )
    bitfinex_execution = BitfinexV1ExecClientConfig(
        url="wss://offline.invalid/ws/2",
        rest_url="https://offline.invalid",
        api_key="",
        api_secret="",
        user_id=USER_ID,
        account_id=AccountId(f"BITFINEX-{USER_ID}"),
        instrument_id=SOURCE_ID,
        raw_symbol=PAPER_RAW_SYMBOL,
        wallet_currency="TESTUSDTF0",
        cid_store_path=str(tmp_path / "bitfinex-cids.state.json"),
        routing=bitfinex_routing,
    )
    mt5_identity: dict[str, object] = {
        "pub_url": "tcp://127.0.0.1:6001",
        "rep_url": "tcp://127.0.0.1:6002",
        "instrument_id": HEDGE_ID,
        "expected_account_id": "12345678",
        "expected_symbol": "XAUUSD",
        "expected_magic": "900000001",
        "expected_ea_build_id": "py000-mt5-ea-v1",
        "expected_source_sha256": "a" * 64,
        "expected_server_timezone": "Europe/Athens",
        "routing": mt5_routing,
    }
    mt5_data = Mt5V1DataClientConfig(
        **mt5_identity,  # type: ignore[arg-type]
        expected_execution_enabled=True,
    )
    mt5_execution = Mt5V1ExecClientConfig(
        **mt5_identity,  # type: ignore[arg-type]
        expected_max_order_lots=Decimal("0.02"),
        expected_stream_id="stream-offline-1",
    )
    strategy = TakerStrategyConfig(
        source_instrument_id=SOURCE_ID,
        hedge_instrument_id=HEDGE_ID,
        source_accounts=(
            SourceAccountRoute(
                account_id=bitfinex_execution.account_id,
                max_long_ounces=Decimal(10),
                max_short_ounces=Decimal(10),
                client_id=BITFINEX_ID,
                base_margin_level=Decimal(100),
            ),
        ),
        hedge_account_id=AccountId("MT5-12345678"),
        hedge_max_long_ounces=Decimal(10),
        hedge_max_short_ounces=Decimal(10),
        economics=TakerEconomicsConfig(
            base_book_quantity=Decimal(2),
            open_quantity_long=Decimal(2),
            open_quantity_short=Decimal(2),
            threshold_long=Decimal("0.001"),
            threshold_short=Decimal("0.001"),
            margin_level=Decimal(500),
            carry=CarryConfig(total_trade_fee=Decimal("0.0002")),
            fx=FxConfig(),
            risk=RiskConfig(source_max_abs=Decimal(10), hedge_max_abs=Decimal(10)),
        ),
        store_path=str(tmp_path / "taker.state.json"),
        hedge_client_id=MT5_ID,
    )
    return LiveTakerProfile(
        bitfinex_data_config=bitfinex_data,
        bitfinex_exec_config=bitfinex_execution,
        mt5_data_config=mt5_data,
        mt5_exec_config=mt5_execution,
        strategy_config=strategy,
        connection_timeout_seconds=3.0,
    )


def _paper_profile(tmp_path: Path) -> LiveTakerProfile:
    profile = _profile(tmp_path)
    account_id = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    execution = struct_replace(profile.bitfinex_exec_config, account_id=account_id)
    route = struct_replace(profile.strategy_config.source_accounts[0], account_id=account_id)
    strategy = struct_replace(profile.strategy_config, source_accounts=(route,))
    return struct_replace(
        profile,
        bitfinex_exec_config=execution,
        strategy_config=strategy,
    )


class _UnreadableEnvironment(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"default validation read environment key {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("default validation iterated the environment")

    def __len__(self) -> int:
        raise AssertionError("default validation measured the environment")


def test_profile_json_is_recursive_and_rejects_unknown_fields(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    parsed = parse_live_taker_profile(profile.json())

    assert isinstance(parsed.bitfinex_data_config, BitfinexV1DataClientConfig)
    assert isinstance(parsed.bitfinex_exec_config, BitfinexV1ExecClientConfig)
    assert isinstance(parsed.mt5_data_config, Mt5V1DataClientConfig)
    assert isinstance(parsed.mt5_exec_config, Mt5V1ExecClientConfig)
    assert isinstance(parsed.strategy_config, TakerStrategyConfig)
    assert parsed.strategy_config.economics.threshold_long == Decimal("0.001")

    unknown = json.loads(profile.json())
    unknown["unexpected"] = True
    with pytest.raises(msgspec.ValidationError, match="unknown field"):
        parse_live_taker_profile(json.dumps(unknown))


@pytest.mark.parametrize("invalid", [0, -1, True])
def test_profile_rejects_invalid_cost_age(
    tmp_path: Path,
    invalid: int,
) -> None:
    profile = _profile(tmp_path)
    profile = struct_replace(
        profile,
        strategy_config=struct_replace(profile.strategy_config, max_cost_age_ns=invalid),
    )
    with pytest.raises(ValueError, match="max_cost_age_ns must be a positive exact integer"):
        validate_live_taker_profile(profile)


def test_profile_rejects_credentials_and_ambiguous_paths(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    with pytest.raises(ValueError, match="must not contain"):
        validate_live_taker_profile(
            struct_replace(
                profile,
                bitfinex_exec_config=struct_replace(
                    profile.bitfinex_exec_config,
                    api_key="SHOULD-NOT-BE-HERE",
                ),
            )
        )

    with pytest.raises(ValueError, match="distinct paths"):
        validate_live_taker_profile(
            struct_replace(
                profile,
                strategy_config=struct_replace(
                    profile.strategy_config,
                    store_path=profile.bitfinex_exec_config.cid_store_path,
                ),
            )
        )


def test_default_validation_is_offline_and_does_not_read_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_bitfinex_open(_transport: BitfinexV1Transport) -> None:
        raise AssertionError("default validation must not open Bitfinex")

    async def unexpected_mt5_open(_transport: Mt5V1Transport) -> None:
        raise AssertionError("default validation must not open MT5")

    monkeypatch.setattr(BitfinexV1Transport, "open", unexpected_bitfinex_open)
    monkeypatch.setattr(Mt5V1Transport, "open", unexpected_mt5_open)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BFX_TEST_API_KEY=first\nBFX_TEST_API_KEY=duplicate\n",
        encoding="utf-8",
    )
    profile = _profile(tmp_path)
    seen: dict[str, object] = {}

    def recording_builder(**kwargs: Any) -> tuple[TradingNode, TakerStrategy]:
        assert "cost_snapshot_provider" not in kwargs
        node, strategy = build_live_taker_node(**kwargs)
        seen["adapter_costs"] = cast(Any, strategy)._live_costs_from_adapters
        return node, strategy

    result = run_live_taker_entry(
        profile,
        environment=_UnreadableEnvironment(),
        env_file=env_file,
        node_builder=recording_builder,
    )

    assert result.outcome == "VALIDATED"
    assert result.reason == "offline_composition_built"
    assert seen == {"adapter_costs": True}
    assert not Path(profile.bitfinex_exec_config.cid_store_path).exists()
    assert not Path(profile.strategy_config.store_path).exists()


def test_rehearsal_injects_credentials_removes_strategy_and_delegates_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "PRIVATE-TEST-SECRET"
    seen: dict[str, object] = {"runner_calls": 0}

    async def unexpected_bitfinex_open(_transport: BitfinexV1Transport) -> None:
        raise AssertionError("the injected rehearsal runner must own network activity")

    async def unexpected_mt5_open(_transport: Mt5V1Transport) -> None:
        raise AssertionError("the injected rehearsal runner must own network activity")

    monkeypatch.setattr(BitfinexV1Transport, "open", unexpected_bitfinex_open)
    monkeypatch.setattr(Mt5V1Transport, "open", unexpected_mt5_open)

    def recording_builder(**kwargs: Any) -> tuple[TradingNode, TakerStrategy]:
        execution = cast(BitfinexV1ExecClientConfig, kwargs["bitfinex_exec_config"])
        seen["key"] = execution.api_key
        seen["secret"] = execution.api_secret
        return build_live_taker_node(**kwargs)

    def fake_runner(node: TradingNode, *, timeout_seconds: float) -> None:
        seen["runner_calls"] = cast(int, seen["runner_calls"]) + 1
        seen["timeout"] = timeout_seconds
        assert node.trader.strategies() == []
        assert not node.is_running()

    profile = _profile(tmp_path)
    result = run_live_taker_entry(
        profile,
        rehearse=True,
        environment={
            "BFX_TEST_API_KEY": "PRIVATE-TEST-KEY",
            "BFX_TEST_API_SECRET": secret,
            "BFX_TEST_USER_ID": str(USER_ID),
        },
        env_file=tmp_path / "missing.env",
        node_builder=recording_builder,
        rehearsal_runner=fake_runner,
    )

    assert result.outcome == "REHEARSED"
    assert result.reason == "adapter_startup_rehearsed"
    assert seen == {
        "runner_calls": 1,
        "key": "PRIVATE-TEST-KEY",
        "secret": secret,
        "timeout": 3.0,
    }
    assert not Path(profile.bitfinex_exec_config.cid_store_path).exists()
    assert not Path(profile.strategy_config.store_path).exists()


def test_run_paper_injects_credentials_keeps_one_strategy_and_runs_node_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {"run_calls": 0}

    def recording_builder(**kwargs: Any) -> tuple[TradingNode, TakerStrategy]:
        execution = cast(BitfinexV1ExecClientConfig, kwargs["bitfinex_exec_config"])
        seen["credentials"] = (execution.api_key, execution.api_secret)
        node, strategy = build_live_taker_node(**kwargs)
        seen["node"] = node
        return node, strategy

    def fake_run(node: TradingNode, raise_exception: bool = False) -> None:
        seen["run_calls"] = cast(int, seen["run_calls"]) + 1
        seen["raise_exception"] = raise_exception
        strategies = node.trader.strategies()
        assert len(strategies) == 1 and isinstance(strategies[0], TakerStrategy)

    monkeypatch.setattr(TradingNode, "run", fake_run)
    result = run_live_taker_entry(
        _paper_profile(tmp_path),
        run_paper=True,
        environment={
            "BFX_TEST_API_KEY": "PAPER-KEY",
            "BFX_TEST_API_SECRET": "PAPER-SECRET",
            "BFX_TEST_USER_ID": str(USER_ID),
        },
        node_builder=recording_builder,
    )

    node = cast(TradingNode, seen["node"])
    assert result.outcome == "PAPER_STOPPED"
    assert result.reason == "paper_strategy_stopped"
    assert seen["credentials"] == ("PAPER-KEY", "PAPER-SECRET")
    assert (seen["run_calls"], seen["raise_exception"]) == (1, True)
    assert node.kernel.loop.is_closed()


def test_run_paper_rejects_non_paper_bindings_before_credentials_or_build(
    tmp_path: Path,
) -> None:
    paper = _paper_profile(tmp_path)
    invalid = struct_replace(
        paper,
        bitfinex_data_config=struct_replace(
            paper.bitfinex_data_config, raw_symbol="tXAUTF0:USTF0"
        ),
        bitfinex_exec_config=struct_replace(
            paper.bitfinex_exec_config,
            raw_symbol="tXAUTF0:USTF0",
            wallet_currency="USTF0",
            account_id=AccountId(f"BITFINEX-{USER_ID}"),
        ),
    )

    def unexpected_builder(**_kwargs: Any) -> tuple[TradingNode, TakerStrategy]:
        raise AssertionError("invalid paper binding reached the builder")

    with pytest.raises(ValueError, match="bound Bitfinex paper"):
        run_live_taker_entry(
            invalid,
            run_paper=True,
            environment=_UnreadableEnvironment(),
            node_builder=unexpected_builder,
        )


def test_programmatic_and_cli_paper_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_live_taker_entry(
            _paper_profile(tmp_path),
            rehearse=True,
            run_paper=True,
            environment=_UnreadableEnvironment(),
        )

    with pytest.raises(SystemExit) as exc_info:
        main(["--profile", "unused.json", "--rehearse", "--run-paper"])
    assert exc_info.value.code == 2


def test_credentials_can_come_from_env_file_and_user_id_must_match(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "IGNORED=value\n"
        "BFX_TEST_API_KEY='FILE-KEY'\n"
        'BFX_TEST_API_SECRET="FILE-SECRET"\n'
        f"BFX_TEST_USER_ID={USER_ID}\n",
        encoding="utf-8",
    )
    credentials = load_bitfinex_test_credentials(
        expected_user_id=USER_ID,
        environment={},
        env_file=env_file,
    )

    assert credentials.api_key == "FILE-KEY"
    assert credentials.api_secret == "FILE-SECRET"
    assert credentials.user_id == USER_ID
    assert "FILE-KEY" not in repr(credentials)
    assert "FILE-SECRET" not in repr(credentials)

    with pytest.raises(LiveTakerEntryError, match="user IDs differ"):
        load_bitfinex_test_credentials(
            expected_user_id=USER_ID + 1,
            environment={},
            env_file=env_file,
        )


class _FakeTrader:
    def __init__(self) -> None:
        self.is_running = False

    def strategies(self) -> list[object]:
        return []


class _FakeNode:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.trader = _FakeTrader()
        self.kernel = type("Kernel", (), {"loop": loop})()
        self._running = False
        self._finished = asyncio.Event()
        self.stop_calls = 0

    async def run_async(self) -> None:
        self._running = True
        self.trader.is_running = True
        await self._finished.wait()

    async def stop_async(self) -> None:
        self.stop_calls += 1
        self.trader.is_running = False
        self._running = False
        self._finished.set()

    def is_running(self) -> bool:
        return self._running


class _EarlyExitNode(_FakeNode):
    async def run_async(self) -> None:
        self._running = True
        self.trader.is_running = True


class _DelayedCleanExitNode(_FakeNode):
    async def run_async(self) -> None:
        self._running = True
        self.trader.is_running = True
        await self._finished.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class _CancellationResistantNode(_FakeNode):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        release: threading.Event,
    ) -> None:
        super().__init__(loop)
        self._release = release
        self.run_cancelled = threading.Event()
        self.stop_cancelled = threading.Event()

    async def run_async(self) -> None:
        self._running = True
        self.trader.is_running = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.run_cancelled.set()
            while not self._release.is_set():
                await asyncio.sleep(0.005)

    async def stop_async(self) -> None:
        self.stop_calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stop_cancelled.set()
            await asyncio.sleep(0.02)
        finally:
            self.trader.is_running = False
            self._running = False


def test_default_rehearsal_readiness_requires_both_registered_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBitfinex:
        execution_hold_reason = None

        def __init__(self) -> None:
            self.account: object | None = None

        def get_account(self) -> object | None:
            return self.account

    class FakeMt5:
        execution_admitted = True

        def __init__(self) -> None:
            self.account: object | None = None

        def get_account(self) -> object | None:
            return self.account

    class ConnectedEngine:
        def check_connected(self) -> bool:
            return True

    bitfinex = FakeBitfinex()
    mt5 = FakeMt5()
    exec_engine = ConnectedEngine()
    exec_engine._clients = {BITFINEX_ID: bitfinex, MT5_ID: mt5}  # type: ignore[attr-defined]
    kernel = type(
        "Kernel",
        (),
        {"data_engine": ConnectedEngine(), "exec_engine": exec_engine},
    )()
    node = type(
        "Node",
        (),
        {
            "kernel": kernel,
            "trader": type("Trader", (), {"is_running": True})(),
            "is_running": lambda _self: True,
        },
    )()
    monkeypatch.setattr(live_taker_entry, "BitfinexV1ExecutionClient", FakeBitfinex)
    monkeypatch.setattr(live_taker_entry, "Mt5V1ExecutionClient", FakeMt5)

    assert live_taker_entry._rehearsal_ready(cast(TradingNode, node)) is False
    bitfinex.account = object()
    assert live_taker_entry._rehearsal_ready(cast(TradingNode, node)) is False
    mt5.account = object()
    assert live_taker_entry._rehearsal_ready(cast(TradingNode, node)) is True


def test_bounded_rehearsal_uses_injected_readiness_and_always_stops() -> None:
    loop = asyncio.new_event_loop()
    node = _FakeNode(loop)
    try:
        run_bounded_rehearsal(
            cast(TradingNode, node),
            timeout_seconds=0.1,
            readiness_probe=lambda current: cast(_FakeNode, current).trader.is_running,
        )
        assert node.stop_calls == 1
        assert not node.is_running()
    finally:
        loop.close()


def test_bounded_rehearsal_allows_bounded_clean_run_task_drain() -> None:
    loop = asyncio.new_event_loop()
    node = _DelayedCleanExitNode(loop)
    try:
        run_bounded_rehearsal(
            cast(TradingNode, node),
            timeout_seconds=0.1,
            readiness_probe=lambda current: cast(
                _DelayedCleanExitNode,
                current,
            ).trader.is_running,
        )
        assert node.stop_calls == 1
        assert not node.is_running()
    finally:
        loop.close()


def test_bounded_rehearsal_timeout_is_fail_closed() -> None:
    loop = asyncio.new_event_loop()
    node = _FakeNode(loop)
    try:
        with pytest.raises(LiveTakerEntryError, match="timed out"):
            run_bounded_rehearsal(
                cast(TradingNode, node),
                timeout_seconds=0.01,
                readiness_probe=lambda _node: False,
            )
        assert node.stop_calls == 1
        assert not node.is_running()
    finally:
        loop.close()


def test_bounded_rehearsal_rejects_ready_node_when_run_task_exits() -> None:
    loop = asyncio.new_event_loop()
    node = _EarlyExitNode(loop)
    try:
        with pytest.raises(LiveTakerEntryError):
            run_bounded_rehearsal(
                cast(TradingNode, node),
                timeout_seconds=0.1,
                readiness_probe=lambda current: cast(_EarlyExitNode, current).trader.is_running,
            )
    finally:
        loop.close()


def test_bounded_rehearsal_has_hard_deadline_when_tasks_resist_cancellation() -> None:
    release = threading.Event()
    completed = threading.Event()
    captured: list[BaseException] = []
    observed: dict[str, _CancellationResistantNode] = {}

    def run_in_owned_loop() -> None:
        loop = asyncio.new_event_loop()
        node = _CancellationResistantNode(loop, release)
        observed["node"] = node
        try:
            run_bounded_rehearsal(
                cast(TradingNode, node),
                timeout_seconds=0.01,
                readiness_probe=lambda _node: False,
            )
        except BaseException as exc:
            captured.append(exc)
        finally:
            release.set()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            completed.set()

    worker = threading.Thread(target=run_in_owned_loop, daemon=True)
    worker.start()
    completed_before_deadline = completed.wait(timeout=0.45)
    release.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive(), "test cleanup failed to stop rehearsal worker"
    node = observed["node"]
    assert node.run_cancelled.is_set()
    assert node.stop_cancelled.is_set()
    assert completed_before_deadline, "rehearsal exceeded its 0.5 second hard deadline"
    assert len(captured) == 1
    assert isinstance(captured[0], LiveTakerEntryError)


def test_run_paper_cli_redacts_runner_failure_disposes_and_has_no_execute_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "paper-profile.json"
    profile_path.write_bytes(_paper_profile(tmp_path).json())
    secret = "RUNNER-MUST-NOT-LEAK"
    seen: dict[str, TradingNode] = {}

    def recording_builder(**kwargs: Any) -> tuple[TradingNode, TakerStrategy]:
        node, strategy = build_live_taker_node(**kwargs)
        seen["node"] = node
        return node, strategy

    def failed_run(_node: TradingNode, raise_exception: bool = False) -> None:
        assert raise_exception
        raise RuntimeError(secret)

    monkeypatch.setattr(TradingNode, "run", failed_run)
    assert (
        main(
            ["--profile", str(profile_path), "--run-paper"],
            environment={
                "BFX_TEST_API_KEY": "PAPER-KEY",
                "BFX_TEST_API_SECRET": secret,
                "BFX_TEST_USER_ID": str(USER_ID),
            },
            node_builder=recording_builder,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err
    assert json.loads(captured.err) == {"outcome": "FAILED", "reason": "RuntimeError"}
    assert seen["node"].kernel.loop.is_closed()

    with pytest.raises(SystemExit) as exc_info:
        main(["--profile", str(profile_path), "--execute"])
    assert exc_info.value.code == 2
