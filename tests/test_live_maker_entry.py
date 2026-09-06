"""Shared ordinary entry checks; injected builders do not certify a Redis backend."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

import msgspec
import pytest
from msgspec.structs import replace
from nautilus_trader.config import DatabaseConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import AccountId
from test_live_maker import _configs
from test_live_taker_entry import USER_ID, _paper_profile, _UnreadableEnvironment

import py000_nautilus.live_taker_entry as entry
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport
from py000_nautilus.live_lifecycle import DrainResult
from py000_nautilus.live_maker import build_live_maker_node
from py000_nautilus.live_maker_entry import (
    LiveMakerProfile,
    parse_live_maker_profile,
)
from py000_nautilus.live_maker_entry import (
    main as maker_main,
)
from py000_nautilus.live_runtime import SourceTerminalReconciler
from py000_nautilus.live_taker import build_live_taker_node
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.mt5_v1_transport import Mt5V1Transport
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.maker import MakerStrategy
from py000_nautilus.strategies.taker import TakerStrategy

_CREDENTIALS = {
    "BFX_TEST_API_KEY": "PAPER-KEY", "BFX_TEST_API_SECRET": "PAPER-SECRET",
    "BFX_TEST_USER_ID": str(USER_ID),
}


def _maker_profile(tmp_path: Path) -> LiveMakerProfile:
    configs = _configs(tmp_path)
    account = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    route = replace(configs.strategy.source_accounts[0], account_id=account)
    return LiveMakerProfile(
        bitfinex_data_config=replace(configs.bitfinex_data, raw_symbol=PAPER_RAW_SYMBOL),
        bitfinex_exec_config=replace(
            configs.bitfinex_exec, api_key="", api_secret="", account_id=account,
            raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
        ),
        mt5_data_config=configs.mt5_data, mt5_exec_config=configs.mt5_exec,
        strategy_config=replace(configs.strategy, source_accounts=(route,)),
        connection_timeout_seconds=3.0, stop_timeout_seconds=4.5,
    )


def _case(kind: str, tmp_path: Path) -> tuple[Any, Any, Any]:
    if kind == "maker":
        return _maker_profile(tmp_path), entry.run_live_maker_entry, build_live_maker_node
    return _paper_profile(tmp_path), entry.run_live_taker_entry, build_live_taker_node


def test_maker_profile_is_typed_and_cli_is_registered(tmp_path: Path) -> None:
    profile = replace(
        _maker_profile(tmp_path), cache_database=DatabaseConfig(host="offline.invalid"),
    )
    parsed = parse_live_maker_profile(profile.json())
    assert parsed == profile
    assert isinstance(parsed.strategy_config, type(profile.strategy_config))
    assert isinstance(parsed.cache_database, DatabaseConfig)
    scripts = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"][
        "scripts"
    ]
    assert scripts["py000-maker-live"] == "py000_nautilus.live_maker_entry:main"
    assert scripts["py000-taker-live"] == "py000_nautilus.live_taker_entry:main"


@pytest.mark.parametrize("nested", [None, "strategy_config", "cache_database"])
def test_maker_profile_rejects_unknown_fields(tmp_path: Path, nested: str | None) -> None:
    values = json.loads(replace(_maker_profile(tmp_path), cache_database=DatabaseConfig()).json())
    target = values if nested is None else values[nested]
    target["unsupported"] = True
    with pytest.raises(msgspec.ValidationError, match="unknown field"):
        parse_live_maker_profile(json.dumps(values))


@pytest.mark.parametrize("kind", ["taker", "maker"])
@pytest.mark.parametrize("invalid", [0, -1, True, float("nan"), float("inf"), 61])
def test_stop_timeout_is_strictly_bounded_before_build(
    kind: str, invalid: object, tmp_path: Path,
) -> None:
    profile, run, _ = _case(kind, tmp_path)

    def forbidden(**_kwargs: Any) -> Any:
        pytest.fail("invalid stop timeout reached construction")

    with pytest.raises(ValueError, match="stop_timeout_seconds"):
        run(replace(profile, stop_timeout_seconds=invalid), node_builder=forbidden,
            environment=_UnreadableEnvironment())


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_validate_checks_database_but_builds_offline_without_it(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, run, builder = _case(kind, tmp_path)
    database = DatabaseConfig(host="must-not-connect.invalid", port=6399)
    profile = replace(profile, cache_database=database, stop_timeout_seconds=60.0)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("offline validation attempted connection")

    monkeypatch.setattr("nautilus_trader.system.kernel.CacheDatabaseAdapter", forbidden)
    monkeypatch.setattr(BitfinexV1Transport, "open", forbidden)
    monkeypatch.setattr(Mt5V1Transport, "open", forbidden)
    captured: dict[str, object] = {}

    def recording_builder(**kwargs: Any) -> Any:
        captured.update(kwargs)
        assert kwargs["cache_database"] is None
        return builder(**kwargs)

    result = run(profile, node_builder=recording_builder, environment=_UnreadableEnvironment(),
                 env_file=tmp_path / "unread.env")
    assert result.outcome == "VALIDATED"
    assert result.reason == "offline_composition_built_without_cache_database"
    assert captured["stop_timeout_seconds"] == 60.0
    assert profile.cache_database is database
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match="only redis"):
        run(replace(profile, cache_database=DatabaseConfig(type="sqlite")), node_builder=forbidden,
            environment=_UnreadableEnvironment())


@pytest.mark.parametrize("kind", ["taker", "maker"])
@pytest.mark.parametrize("rehearse", [False, True], ids=["run-paper", "rehearse"])
def test_network_modes_forward_database_and_stop_budget_without_backend_claim(
    kind: str, rehearse: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, run, builder = _case(kind, tmp_path)
    database = DatabaseConfig(host="offline.invalid", port=6399)
    profile = replace(profile, cache_database=database, stop_timeout_seconds=4.5)
    built: list[TradingNode] = []

    def recording_builder(**kwargs: Any) -> Any:
        assert kwargs["cache_database"] is database
        assert kwargs["stop_timeout_seconds"] == 4.5
        # Only test the entry boundary: never connect to a synthetic database address.
        kwargs["cache_database"] = None
        node, strategy = builder(**kwargs)
        assert type(strategy) is (MakerStrategy if kind == "maker" else TakerStrategy)
        assert len(node.trader.actors()) == 1
        built.append(node)
        return node, strategy

    def paper_runner(node: TradingNode) -> None:
        assert len(node.trader.strategies()) == 1
        cast(Any, node).drain_result = DrainResult(True, "obligations_drained", (), {kind: "0"})

    def rehearsal_runner(node: TradingNode, *, timeout_seconds: float) -> None:
        assert timeout_seconds == 3.0
        assert node.trader.strategies() == node.trader.actors() == []

    monkeypatch.setattr(entry, "run_paper_node", paper_runner)
    result = run(profile, run_paper=not rehearse, rehearse=rehearse, environment=_CREDENTIALS,
                 node_builder=recording_builder, rehearsal_runner=rehearsal_runner)
    assert result.outcome == ("REHEARSED" if rehearse else "PAPER_STOPPED")
    assert len(built) == 1 and built[0].kernel.loop.is_closed()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_rehearsal_retains_business_pause_bytes_and_never_starts_recovery(
    kind: str, tmp_path: Path,
) -> None:
    profile, run, builder = _case(kind, tmp_path)
    strategy = profile.strategy_config
    if kind == "maker":
        state: Any = MakerStateStore(
            strategy.store_path_prefix, str(strategy.source_instrument_id),
            str(strategy.hedge_instrument_id),
        )
        state.freeze_sources("operator pause")
    else:
        state = JsonStateStore(strategy.store_path)
        state.freeze_source_submissions("operator pause")
    previous = state.path.read_bytes()

    def recording_builder(**kwargs: Any) -> Any:
        node, strategy = builder(**kwargs)
        actor = node.trader.actors()[0]
        assert isinstance(actor, SourceTerminalReconciler) and actor.restart_pending
        return node, strategy

    def runner(node: TradingNode, *, timeout_seconds: float) -> None:
        assert node.trader.actors() == node.trader.strategies() == []

    result = run(profile, rehearse=True, environment=_CREDENTIALS,
                 node_builder=recording_builder, rehearsal_runner=runner)
    assert result.outcome == "REHEARSED"
    assert state.path.read_bytes() == previous


@pytest.mark.parametrize("kind", ["taker", "maker"])
@pytest.mark.parametrize("complete", [None, False, True], ids=["missing", "pending", "complete"])
def test_cli_requires_drain_and_preserves_diagnostics(
    kind: str, complete: bool | None, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    profile, _, builder = _case(kind, tmp_path)
    path = tmp_path / "profile.json"
    path.write_bytes(profile.json())
    expected = (None if complete is None else DrainResult(
        complete, "obligations_drained" if complete else "stop_timeout",
        () if complete else ("HEDGE-OLD:UNKNOWN",), {kind: "0.1"},
    ))

    def runner(node: TradingNode) -> None:
        cast(Any, node).drain_result = expected

    monkeypatch.setattr(entry, "run_paper_node", runner)
    cli: Any = maker_main if kind == "maker" else entry.main
    status = cli(["--profile", str(path), "--run-paper"],
                 environment=_CREDENTIALS, node_builder=builder)
    assert status == (0 if complete is True else 1)
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert output == {
        "outcome": "PAPER_STOPPED" if complete is True else "PAPER_INCOMPLETE",
        "reason": "drain_result_missing" if expected is None else expected.reason,
        "pending": [] if expected is None else list(expected.pending),
        "residuals": {} if expected is None else expected.residuals,
    }


@pytest.mark.parametrize("kind", ["taker", "maker"])
@pytest.mark.parametrize("complete", [False, True])
def test_runner_failure_keeps_drain_diagnostics_but_never_reports_success(
    kind: str, complete: bool, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    profile, _, builder = _case(kind, tmp_path)
    path = tmp_path / "profile.json"
    path.write_bytes(profile.json())

    def runner(node: TradingNode) -> None:
        cast(Any, node).drain_result = DrainResult(
            complete, "retained_drain_facts", ("HEDGE-OLD",), {kind: "0.1"},
        )
        raise RuntimeError("MUST-NOT-LEAK")

    monkeypatch.setattr(entry, "run_paper_node", runner)
    cli: Any = maker_main if kind == "maker" else entry.main
    assert cli(["--profile", str(path), "--run-paper"],
               environment=_CREDENTIALS, node_builder=builder) == 1
    captured = capsys.readouterr()
    assert "MUST-NOT-LEAK" not in captured.out + captured.err
    assert json.loads(captured.out.splitlines()[-1]) == {
        "outcome": "PAPER_INCOMPLETE",
        "reason": "paper_runner_error:RuntimeError; retained_drain_facts",
        "pending": ["HEDGE-OLD"], "residuals": {kind: "0.1"},
    }


def test_maker_binding_and_secrets_fail_before_credentials_or_build(tmp_path: Path) -> None:
    profile = _maker_profile(tmp_path)

    def forbidden(**_kwargs: Any) -> Any:
        pytest.fail("invalid Maker profile reached construction")

    for changes, match in (({"wallet_currency": "USTF0"}, "bound Bitfinex paper"),
                           ({"api_secret": "FORBIDDEN"}, "must not contain")):
        with pytest.raises(ValueError, match=match):
            entry.run_live_maker_entry(
                replace(profile, bitfinex_exec_config=replace(
                    profile.bitfinex_exec_config, **changes,
                )),
                run_paper=True, node_builder=forbidden, environment=_UnreadableEnvironment(),
            )
