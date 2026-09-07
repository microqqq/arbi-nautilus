"""Shared ordinary Maker/Taker entry, retaining the original Taker CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, cast

from msgspec.structs import replace as struct_replace
from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.config import DatabaseConfig
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.trading.strategy import Strategy

from py000_nautilus.accounting_report import RunAccountingReport, build_run_accounting_report
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, BitfinexV1DataClientConfig
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
)
from py000_nautilus.config import MakerStrategyConfig, TakerStrategyConfig
from py000_nautilus.live_cache import native_cache_config
from py000_nautilus.live_maker import build_live_maker_node
from py000_nautilus.live_runtime import SourceTerminalReconciler
from py000_nautilus.live_taker import (
    BITFINEX_CLIENT_ID,
    MT5_CLIENT_ID,
    build_live_taker_node,
)
from py000_nautilus.maker_store import MakerStateStore, maker_legacy_paths, maker_state_path
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig, Mt5V1ExecutionClient
from py000_nautilus.restart_recovery import StartupRecoveryOptions, describe_business_recovery
from py000_nautilus.store import JsonStateStore

if TYPE_CHECKING:
    from py000_nautilus.live_both_entry import LiveBothNodeBuilder
    from py000_nautilus.live_lifecycle import DrainResult

_ENV_NAMES = frozenset({"BFX_TEST_API_KEY", "BFX_TEST_API_SECRET", "BFX_TEST_USER_ID"})
_OFFLINE_API_KEY = "OFFLINE-VALIDATION-ONLY"
_OFFLINE_API_SECRET = "OFFLINE-VALIDATION-ONLY"


class LiveTakerEntryError(RuntimeError):
    """The requested startup mode could not establish its bounded result."""


class _LiveProfile(NautilusConfig, frozen=True):
    """The common four-client configuration; Bitfinex credentials are loaded separately."""

    bitfinex_data_config: BitfinexV1DataClientConfig
    bitfinex_exec_config: BitfinexV1ExecClientConfig
    mt5_data_config: Mt5V1DataClientConfig
    mt5_exec_config: Mt5V1ExecClientConfig
    connection_timeout_seconds: float = 10.0
    stop_timeout_seconds: float = 10.0
    cache_database: DatabaseConfig | None = None


class LiveTakerProfile(_LiveProfile, frozen=True, kw_only=True):
    strategy_config: TakerStrategyConfig


class LiveMakerProfile(_LiveProfile, frozen=True, kw_only=True):
    strategy_config: MakerStrategyConfig


@dataclass(frozen=True, slots=True)
class LiveTakerEntryResult:
    outcome: Literal["VALIDATED", "REHEARSED", "PAPER_STOPPED", "PAPER_INCOMPLETE"]
    reason: str
    pending: tuple[str, ...] = ()
    residuals: dict[str, str] = field(default_factory=dict)
    drain_complete: bool | None = None
    accounting: RunAccountingReport | None = None


@dataclass(frozen=True, slots=True, repr=False)
class BitfinexTestCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    user_id: int


_ConfigT = TypeVar("_ConfigT", TakerStrategyConfig, MakerStrategyConfig)
_BuilderConfigT = TypeVar("_BuilderConfigT", contravariant=True)


class _LiveNodeBuilder(Protocol[_BuilderConfigT]):
    def __call__(
        self,
        *,
        bitfinex_data_config: BitfinexV1DataClientConfig,
        bitfinex_exec_config: BitfinexV1ExecClientConfig,
        mt5_data_config: Mt5V1DataClientConfig,
        mt5_exec_config: Mt5V1ExecClientConfig,
        strategy_config: _BuilderConfigT,
        cache_database: DatabaseConfig | None,
        loop: asyncio.AbstractEventLoop | None,
        connection_timeout_seconds: float,
        stop_timeout_seconds: float,
        startup_recovery: StartupRecoveryOptions | None = None,
    ) -> tuple[TradingNode, Strategy | tuple[Strategy, ...]]: ...


LiveTakerNodeBuilder = _LiveNodeBuilder[TakerStrategyConfig]
LiveMakerNodeBuilder = _LiveNodeBuilder[MakerStrategyConfig]


class LiveTakerRehearsalRunner(Protocol):
    def __call__(self, node: TradingNode, *, timeout_seconds: float) -> None: ...


def parse_live_taker_profile(raw: bytes | str) -> LiveTakerProfile:
    """Parse a strict recursive JSON profile, then validate entry-point-only fields."""
    profile = cast(LiveTakerProfile, LiveTakerProfile.parse(raw))
    validate_live_taker_profile(profile)
    return profile


def load_live_taker_profile(path: Path) -> LiveTakerProfile:
    """Load one profile without consulting the process environment."""
    return parse_live_taker_profile(path.read_bytes())


def validate_live_taker_profile(profile: LiveTakerProfile) -> None:
    _validate_live_profile(profile, profile.strategy_config)


def parse_live_maker_profile(raw: bytes | str) -> LiveMakerProfile:
    profile = cast(LiveMakerProfile, LiveMakerProfile.parse(raw))
    validate_live_maker_profile(profile)
    return profile


def load_live_maker_profile(path: Path) -> LiveMakerProfile:
    return parse_live_maker_profile(path.read_bytes())


def inspect_profile_recovery(profile: LiveTakerProfile | LiveMakerProfile) -> dict[str, object]:
    """Open only the existing business file, without building a node or reading secrets."""
    config = profile.strategy_config
    store: JsonStateStore | MakerStateStore
    if isinstance(config, TakerStrategyConfig):
        path = _required_path(config.store_path, "Taker state store")
        if not path.is_file():
            raise ValueError("no existing Taker state to inspect")
        store = JsonStateStore(path)
    else:
        prefix = _required_path(config.store_path_prefix, "Maker state store prefix")
        if not maker_state_path(prefix).is_file():
            raise ValueError("no existing Maker state to inspect")
        carry_route = None
        if config.residual_mode == "bounded-carry":
            source, hedge = config.source_accounts[0], config.hedge_accounts[0]
            carry_route = (source.account_id.value,
                           source.client_id.value if source.client_id is not None else None,
                           hedge.account_id.value,
                           hedge.client_id.value if hedge.client_id is not None else None)
        store = MakerStateStore(
            prefix, str(config.source_instrument_id), str(config.hedge_instrument_id),
            residual_limit_ounces=config.residual_limit_ounces, carry_route=carry_route,
        )
    return describe_business_recovery(store)


def validate_live_maker_profile(profile: LiveMakerProfile) -> None:
    _validate_live_profile(profile, profile.strategy_config)


def _validate_live_profile(
    profile: _LiveProfile, strategy: TakerStrategyConfig | MakerStrategyConfig,
) -> None:
    """Validate configuration without connecting its optional database or either venue."""
    execution = profile.bitfinex_exec_config
    if execution.api_key != "" or execution.api_secret != "":
        raise ValueError("live profile must not contain Bitfinex credentials")
    native_cache_config(profile.cache_database)
    max_cost_age_ns = strategy.max_cost_age_ns
    if type(max_cost_age_ns) is not int or max_cost_age_ns <= 0:
        raise ValueError("max_cost_age_ns must be a positive exact integer")
    for field_name in ("connection_timeout_seconds", "stop_timeout_seconds"):
        timeout = getattr(profile, field_name)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValueError(f"{field_name} must be finite and in (0, 60]")

    if isinstance(strategy, TakerStrategyConfig):
        state_paths = [_required_path(strategy.store_path, "Taker state store")]
    else:
        prefix = _required_path(strategy.store_path_prefix, "Maker state store prefix")
        state_paths = [
            path.resolve(strict=False)
            for path in (maker_state_path(prefix), *maker_legacy_paths(prefix))
        ]
    paths = [_required_path(execution.cid_store_path, "Bitfinex CID store"), *state_paths]
    if len(set(paths)) != len(paths):
        raise ValueError("Bitfinex CID and strategy state stores must use distinct paths")


def load_bitfinex_test_credentials(
    *,
    expected_user_id: int,
    environment: Mapping[str, str],
    env_file: Path | None,
) -> BitfinexTestCredentials:
    """Load only the three paper-account values, with environment taking precedence."""
    file_values = _read_env_file(env_file) if env_file is not None else {}

    def value(name: str) -> str:
        candidate = environment.get(name, file_values.get(name, ""))
        if not isinstance(candidate, str):
            raise LiveTakerEntryError(f"{name} must be text")
        return candidate

    key = value("BFX_TEST_API_KEY")
    secret = value("BFX_TEST_API_SECRET")
    user_id_text = value("BFX_TEST_USER_ID")
    if not key.strip() or not secret.strip():
        raise LiveTakerEntryError("Bitfinex test credentials are required")
    if not user_id_text.isdigit() or int(user_id_text) <= 0:
        raise LiveTakerEntryError("BFX_TEST_USER_ID must be a positive exact integer")
    user_id = int(user_id_text)
    if user_id != expected_user_id:
        raise LiveTakerEntryError("Bitfinex profile and environment user IDs differ")
    return BitfinexTestCredentials(key, secret, user_id)


def run_live_taker_entry(
    profile: LiveTakerProfile,
    *,
    rehearse: bool = False,
    run_paper: bool = False,
    resume_held: bool = False,
    retry_rejected_hedge: str | None = None,
    environment: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    node_builder: LiveTakerNodeBuilder = build_live_taker_node,
    rehearsal_runner: LiveTakerRehearsalRunner | None = None,
) -> LiveTakerEntryResult:
    """Validate offline, rehearse observations, or run the ordinary paper-bound Taker."""
    return _run_live_entry(
        profile, strategy_config=profile.strategy_config, node_builder=node_builder,
        rehearse=rehearse, run_paper=run_paper, environment=environment,
        env_file=env_file, rehearsal_runner=rehearsal_runner,
        resume_held=resume_held, retry_rejected_hedge=retry_rejected_hedge,
    )


def run_live_maker_entry(
    profile: LiveMakerProfile,
    *,
    rehearse: bool = False,
    run_paper: bool = False,
    resume_held: bool = False,
    retry_rejected_hedge: str | None = None,
    environment: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    node_builder: LiveMakerNodeBuilder = build_live_maker_node,
    rehearsal_runner: LiveTakerRehearsalRunner | None = None,
) -> LiveTakerEntryResult:
    """Use the same lifecycle with the ordinary Maker, never a canary strategy."""
    return _run_live_entry(
        profile, strategy_config=profile.strategy_config, node_builder=node_builder,
        rehearse=rehearse, run_paper=run_paper, environment=environment,
        env_file=env_file, rehearsal_runner=rehearsal_runner,
        resume_held=resume_held, retry_rejected_hedge=retry_rejected_hedge,
    )


def _run_live_entry(
    profile: _LiveProfile,
    *,
    strategy_config: _ConfigT,
    node_builder: _LiveNodeBuilder[_ConfigT],
    rehearse: bool,
    run_paper: bool,
    environment: Mapping[str, str] | None,
    env_file: Path | None,
    rehearsal_runner: LiveTakerRehearsalRunner | None,
    resume_held: bool,
    retry_rejected_hedge: str | None,
) -> LiveTakerEntryResult:
    _validate_live_profile(profile, strategy_config)
    if rehearse and run_paper:
        raise ValueError("rehearse and run_paper are mutually exclusive")
    recovery = StartupRecoveryOptions(resume_held, retry_rejected_hedge)
    if (resume_held or retry_rejected_hedge is not None) and not run_paper:
        raise ValueError("recovery choices require --run-paper")
    if run_paper:
        _validate_paper_binding(profile)
    if rehearsal_runner is None:
        rehearsal_runner = run_bounded_rehearsal

    if rehearse or run_paper:
        credentials = load_bitfinex_test_credentials(
            expected_user_id=profile.bitfinex_exec_config.user_id,
            environment=os.environ if environment is None else environment,
            env_file=env_file,
        )
        api_key = credentials.api_key
        api_secret = credentials.api_secret
    else:
        # These values satisfy adapter construction only. The node is never started.
        api_key = _OFFLINE_API_KEY
        api_secret = _OFFLINE_API_SECRET

    bitfinex_execution = struct_replace(
        profile.bitfinex_exec_config,
        api_key=api_key,
        api_secret=api_secret,
    )
    loop = asyncio.new_event_loop()
    node: TradingNode | None = None
    try:
        node, built = node_builder(
            bitfinex_data_config=profile.bitfinex_data_config,
            bitfinex_exec_config=bitfinex_execution,
            mt5_data_config=profile.mt5_data_config,
            mt5_exec_config=profile.mt5_exec_config,
            strategy_config=strategy_config,
            cache_database=profile.cache_database if rehearse or run_paper else None,
            loop=loop,
            connection_timeout_seconds=float(profile.connection_timeout_seconds),
            stop_timeout_seconds=float(profile.stop_timeout_seconds),
            startup_recovery=recovery if resume_held else None,
        )
        built_strategies = built if isinstance(built, tuple) else (built,)
        if not built_strategies:
            raise LiveTakerEntryError("runner requires the built strategy set")
        strategy = built_strategies[0]
        if not rehearse and not run_paper:
            reason = (
                "offline_composition_built" if profile.cache_database is None
                else "offline_composition_built_without_cache_database"
            )
            return LiveTakerEntryResult("VALIDATED", reason)

        if run_paper:
            strategies = node.trader.strategies()
            if strategies != list(built_strategies):
                raise LiveTakerEntryError("paper runner requires exactly the built strategy set")
            run_failure: str | None = None
            try:
                run_paper_node(node)
            except Exception as exc:
                if getattr(node, "drain_result", None) is None:
                    raise
                # Preserve the bounded drain evidence, never an exception's secret text.
                run_failure = f"paper_runner_error:{type(exc).__name__}"
            if node.is_running():
                raise LiveTakerEntryError("paper runner returned while the node was running")
            drain = cast("DrainResult | None", getattr(node, "drain_result", None))
            # Reports read retained facts while the native cache still exists. They
            # never reconnect, mutate Positions, or turn a pending drain into success.
            accounting = None
            accounting_failure = None
            try:
                clients = cast(
                    dict[ClientId, LiveExecutionClient], node.kernel.exec_engine._clients,
                )
                source = clients[BITFINEX_CLIENT_ID]
                hedge = clients[MT5_CLIENT_ID]
                if (not isinstance(source, BitfinexV1ExecutionClient)
                        or not isinstance(hedge, Mt5V1ExecutionClient)):
                    raise LiveTakerEntryError("accounting requires configured execution clients")
                accounting = build_run_accounting_report(
                    node.cache, source, hedge,
                    trader_id=node.trader.id, strategy_id=strategy.id,
                    fx=strategy_config.economics.fx,
                    strategy_ids=(tuple(item.id for item in built_strategies)
                                  if len(built_strategies) > 1 else None),
                )
            except Exception as exc:
                accounting_failure = f"accounting_report_error:{type(exc).__name__}"
            if drain is None:
                return LiveTakerEntryResult(
                    "PAPER_INCOMPLETE", "drain_result_missing", accounting=accounting,
                )
            reason = drain.reason if run_failure is None else f"{run_failure}; {drain.reason}"
            if accounting_failure is not None:
                reason += f"; {accounting_failure}"
            elif accounting is not None and accounting.status != "FINAL":
                reason += "; accounting_pending"
            return LiveTakerEntryResult(
                "PAPER_STOPPED" if drain.complete is True and run_failure is None
                and accounting is not None and accounting.status == "FINAL"
                else "PAPER_INCOMPLETE",
                reason, tuple(drain.pending), dict(drain.residuals),
                drain_complete=drain.complete, accounting=accounting,
            )

        # Strategy callbacks and the bound startup Actor can mutate business state.
        # Adapter observations (including CID fees/native persistence) remain permitted.
        for strategy in built_strategies:
            node.trader.remove_strategy(strategy.id)
        for actor in node.trader.actors():
            if isinstance(actor, SourceTerminalReconciler):
                node.trader.remove_actor(actor.id)
                actor.dispose()
        if node.trader.strategies() or node.trader.actors():
            raise LiveTakerEntryError("rehearsal retained a strategy or business Actor")
        rehearsal_runner(
            node,
            timeout_seconds=float(profile.connection_timeout_seconds),
        )
        if node.is_running():
            raise LiveTakerEntryError("read-only rehearsal runner did not stop the node")
        return LiveTakerEntryResult("REHEARSED", "adapter_startup_rehearsed")
    finally:
        if node is not None:
            _dispose_node(node)
        elif not loop.is_closed():
            loop.close()


def _validate_paper_binding(profile: _LiveProfile) -> None:
    execution = profile.bitfinex_exec_config
    if (
        profile.bitfinex_data_config.raw_symbol != PAPER_RAW_SYMBOL
        or execution.raw_symbol != PAPER_RAW_SYMBOL
        or execution.wallet_currency != "TESTUSDTF0"
        or execution.account_id.value != f"BITFINEX-PAPER-{execution.user_id}"
    ):
        raise ValueError("run_paper requires the bound Bitfinex paper market and account")


def run_paper_node(node: TradingNode) -> None:
    """Run the existing node until its normal signal or stop path returns."""
    node.run(raise_exception=True)


def run_bounded_rehearsal(
    node: TradingNode,
    *,
    timeout_seconds: float,
    readiness_probe: Callable[[TradingNode], bool] | None = None,
) -> None:
    """Connect, reconcile, initialize the portfolio, then immediately stop."""
    if not 0 < timeout_seconds <= 60 or not math.isfinite(timeout_seconds):
        raise ValueError("rehearsal timeout must be finite and in (0, 60]")
    if node.trader.strategies() or node.trader.actors():
        raise LiveTakerEntryError("rehearsal cannot start a strategy or business Actor")
    probe = _rehearsal_ready if readiness_probe is None else readiness_probe
    loop = node.kernel.loop
    if loop.is_closed() or loop.is_running():
        raise LiveTakerEntryError("rehearsal requires an owned, stopped event loop")
    loop.run_until_complete(
        _bounded_rehearsal(
            node,
            timeout_seconds=timeout_seconds,
            readiness_probe=probe,
        )
    )


async def _bounded_rehearsal(
    node: TradingNode,
    *,
    timeout_seconds: float,
    readiness_probe: Callable[[TradingNode], bool],
) -> None:
    run_task = asyncio.create_task(node.run_async())
    readiness_task = asyncio.create_task(_await_rehearsal_ready(node, run_task, readiness_probe))
    primary: BaseException | None = None
    # The node applies this timeout independently to connection, reconciliation, and
    # portfolio initialization, so the outer lifecycle bound covers all three stages.
    outer_timeout = timeout_seconds * 4
    done, _ = await asyncio.wait({readiness_task}, timeout=outer_timeout)
    if readiness_task not in done:
        primary = LiveTakerEntryError("read-only startup rehearsal timed out")
        readiness_task.cancel()
    else:
        try:
            readiness_task.result()
        except BaseException as exc:
            primary = exc
    if primary is None and run_task.done():
        primary = _premature_run_result(run_task)

    shutdown_error: BaseException | None = None
    stop_task: asyncio.Task[None] | None = None
    if node.is_running():
        stop_task = asyncio.create_task(node.stop_async())
        done, _ = await asyncio.wait({stop_task}, timeout=timeout_seconds)
        if stop_task not in done:
            shutdown_error = LiveTakerEntryError("read-only rehearsal shutdown timed out")
            stop_task.cancel()
        else:
            try:
                stop_task.result()
            except BaseException as exc:
                shutdown_error = exc

    if not readiness_task.done():
        readiness_task.cancel()
    if not run_task.done() and shutdown_error is None:
        # A clean TradingNode stop can finish its outer run task on a later loop turn.
        # Give it the same bounded shutdown allowance before forcing cancellation.
        await asyncio.wait({run_task}, timeout=timeout_seconds)
    if not run_task.done():
        run_task.cancel()
    # Deliver forced cancellation once, but never await a cancellation-resistant task.
    await asyncio.sleep(0)
    if not run_task.done():
        run_error: BaseException | None = LiveTakerEntryError(
            "TradingNode task did not terminate after shutdown",
        )
    elif run_task.cancelled():
        run_error = None
    else:
        run_error = run_task.exception()
    if run_error is not None:
        if primary is None:
            primary = run_error
        elif run_error is not primary:
            primary.add_note(f"TradingNode also failed with {type(run_error).__name__}")
    if shutdown_error is not None:
        if primary is None:
            primary = LiveTakerEntryError("read-only rehearsal shutdown failed")
            primary.__cause__ = shutdown_error
        else:
            primary.add_note(f"shutdown also failed with {type(shutdown_error).__name__}")
    if primary is not None:
        raise primary


async def _await_rehearsal_ready(
    node: TradingNode,
    run_task: asyncio.Task[None],
    readiness_probe: Callable[[TradingNode], bool],
) -> None:
    while True:
        if run_task.done():
            raise _premature_run_result(run_task)
        if readiness_probe(node):
            if run_task.done():
                raise _premature_run_result(run_task)
            return
        await asyncio.sleep(0.01)


def _premature_run_result(run_task: asyncio.Task[None]) -> BaseException:
    if run_task.cancelled():
        return LiveTakerEntryError("TradingNode stopped before rehearsal readiness")
    error = run_task.exception()
    if error is not None:
        return error
    return LiveTakerEntryError("TradingNode stopped before rehearsal readiness")


def _rehearsal_ready(node: TradingNode) -> bool:
    clients = cast(
        dict[ClientId, LiveExecutionClient],
        node.kernel.exec_engine._clients,
    )
    bitfinex = clients.get(BITFINEX_CLIENT_ID)
    mt5 = clients.get(MT5_CLIENT_ID)
    if not isinstance(bitfinex, BitfinexV1ExecutionClient):
        raise LiveTakerEntryError("rehearsal has no Bitfinex execution client")
    if not isinstance(mt5, Mt5V1ExecutionClient):
        raise LiveTakerEntryError("rehearsal has no MT5 execution client")
    return (
        node.is_running()
        and node.trader.is_running
        and node.kernel.data_engine.check_connected()
        and node.kernel.exec_engine.check_connected()
        and bitfinex.execution_hold_reason is None
        and bitfinex.get_account() is not None
        and mt5.execution_admitted
        and mt5.get_account() is not None
    )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise LiveTakerEntryError("environment path is not a regular file")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if name not in _ENV_NAMES:
            continue
        if name in values:
            raise LiveTakerEntryError(f"duplicate {name} in environment file")
        if len(value) > 1 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def _required_path(value: str, label: str) -> Path:
    if not value or value != value.strip():
        raise ValueError(f"{label} path must be a non-empty trimmed path")
    return Path(value).resolve(strict=False)


def _dispose_node(node: TradingNode) -> None:
    active_error = sys.exception()
    cleanup_error: BaseException | None = None
    try:
        if not node.kernel.loop.is_closed():
            node.kernel.cancel_all_tasks()
    except BaseException as exc:
        cleanup_error = exc
    try:
        node.dispose()
    except BaseException as exc:
        cleanup_error = cleanup_error or exc
    if not node.kernel.loop.is_closed():
        cleanup_error = cleanup_error or LiveTakerEntryError(
            "TradingNode disposal left its owned event loop open"
        )
    if cleanup_error is not None:
        if active_error is not None:
            active_error.add_note(
                f"TradingNode cleanup also failed with {type(cleanup_error).__name__}"
            )
            return
        raise LiveTakerEntryError("TradingNode cleanup failed") from cleanup_error


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    node_builder: LiveTakerNodeBuilder = build_live_taker_node,
    rehearsal_runner: LiveTakerRehearsalRunner | None = None,
) -> int:
    return _main(
        argv, mode="taker", environment=environment,
        node_builder=node_builder, rehearsal_runner=rehearsal_runner,
    )


def maker_main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    node_builder: LiveMakerNodeBuilder = build_live_maker_node,
    rehearsal_runner: LiveTakerRehearsalRunner | None = None,
) -> int:
    return _main(
        argv, mode="maker", environment=environment,
        node_builder=node_builder, rehearsal_runner=rehearsal_runner,
    )


def _main(
    argv: Sequence[str] | None,
    *,
    mode: Literal["taker", "maker", "both"],
    environment: Mapping[str, str] | None,
    node_builder: LiveTakerNodeBuilder | LiveMakerNodeBuilder | LiveBothNodeBuilder,
    rehearsal_runner: LiveTakerRehearsalRunner | None,
) -> int:
    parser = argparse.ArgumentParser(
        description=f"Validate, rehearse, or run the ordinary paper-bound PY000 {mode.title()}",
    )
    parser.add_argument("--profile", required=True, type=Path)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--rehearse",
        action="store_true",
        help="reconcile adapter observations without trading or business recovery",
    )
    operation.add_argument(
        "--run-paper",
        action="store_true",
        help=f"run the ordinary {mode.title()} on its bound Bitfinex paper account",
    )
    operation.add_argument("--inspect-recovery", action="store_true",
                           help="inspect existing local business state without connections")
    parser.add_argument("--resume-held", action="store_true",
                        help="review old pauses for this run; reconciliation still required")
    parser.add_argument("--retry-rejected-hedge", metavar="OLD_CLIENT_ORDER_ID",
                        help="with --resume-held, qualify one zero-fill rejection for a new ID")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        if mode == "both":
            from py000_nautilus.live_both_entry import (
                inspect_both_recovery,
                load_live_both_profile,
                run_live_both_entry,
            )
        if args.inspect_recovery:
            if args.resume_held or args.retry_rejected_hedge is not None:
                raise ValueError("offline inspection cannot consume recovery choices")
            if mode == "both":
                inspected = inspect_both_recovery(load_live_both_profile(args.profile))
            else:
                profile = (load_live_taker_profile(args.profile) if mode == "taker"
                           else load_live_maker_profile(args.profile))
                inspected = inspect_profile_recovery(profile)
            print(json.dumps(inspected, default=str))
            return 0
        if mode == "taker":
            result = run_live_taker_entry(
                load_live_taker_profile(args.profile),
                rehearse=args.rehearse, run_paper=args.run_paper,
                environment=environment, env_file=args.env_file,
                node_builder=cast(LiveTakerNodeBuilder, node_builder),
                rehearsal_runner=rehearsal_runner,
                resume_held=args.resume_held, retry_rejected_hedge=args.retry_rejected_hedge,
            )
        elif mode == "maker":
            result = run_live_maker_entry(
                load_live_maker_profile(args.profile),
                rehearse=args.rehearse, run_paper=args.run_paper,
                environment=environment, env_file=args.env_file,
                node_builder=cast(LiveMakerNodeBuilder, node_builder),
                rehearsal_runner=rehearsal_runner,
                resume_held=args.resume_held, retry_rejected_hedge=args.retry_rejected_hedge,
            )
        else:
            result = run_live_both_entry(
                load_live_both_profile(args.profile),
                rehearse=args.rehearse, run_paper=args.run_paper,
                environment=environment, env_file=args.env_file,
                node_builder=cast("LiveBothNodeBuilder", node_builder),
                rehearsal_runner=rehearsal_runner,
                resume_held=args.resume_held, retry_rejected_hedge=args.retry_rejected_hedge,
            )
    except Exception as exc:
        print(
            json.dumps({"outcome": "FAILED", "reason": type(exc).__name__}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(asdict(result), default=str))
    return 1 if result.outcome == "PAPER_INCOMPLETE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
