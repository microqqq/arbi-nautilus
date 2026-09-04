"""Safe startup entry point for the offline-buildable PY000 Taker composition."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast

from msgspec.structs import replace as struct_replace
from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientId

from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, BitfinexV1DataClientConfig
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
)
from py000_nautilus.config import TakerStrategyConfig
from py000_nautilus.live_taker import (
    BITFINEX_CLIENT_ID,
    MT5_CLIENT_ID,
    build_live_taker_node,
)
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig, Mt5V1ExecutionClient
from py000_nautilus.strategies.taker import TakerStrategy

_ENV_NAMES = frozenset({"BFX_TEST_API_KEY", "BFX_TEST_API_SECRET", "BFX_TEST_USER_ID"})
_OFFLINE_API_KEY = "OFFLINE-VALIDATION-ONLY"
_OFFLINE_API_SECRET = "OFFLINE-VALIDATION-ONLY"


class LiveTakerEntryError(RuntimeError):
    """The requested startup mode could not establish its bounded result."""


class LiveTakerProfile(NautilusConfig, frozen=True):
    """One typed, credential-free profile for the four-client Taker composition."""

    bitfinex_data_config: BitfinexV1DataClientConfig
    bitfinex_exec_config: BitfinexV1ExecClientConfig
    mt5_data_config: Mt5V1DataClientConfig
    mt5_exec_config: Mt5V1ExecClientConfig
    strategy_config: TakerStrategyConfig
    connection_timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class LiveTakerEntryResult:
    outcome: Literal["VALIDATED", "REHEARSED", "PAPER_STOPPED"]
    reason: str


@dataclass(frozen=True, slots=True, repr=False)
class BitfinexTestCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    user_id: int


class LiveTakerNodeBuilder(Protocol):
    def __call__(
        self,
        *,
        bitfinex_data_config: BitfinexV1DataClientConfig,
        bitfinex_exec_config: BitfinexV1ExecClientConfig,
        mt5_data_config: Mt5V1DataClientConfig,
        mt5_exec_config: Mt5V1ExecClientConfig,
        strategy_config: TakerStrategyConfig,
        loop: asyncio.AbstractEventLoop | None,
        connection_timeout_seconds: float,
    ) -> tuple[TradingNode, TakerStrategy]: ...


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
    """Reject secrets and ambiguous local paths before constructing a node."""
    execution = profile.bitfinex_exec_config
    if execution.api_key != "" or execution.api_secret != "":
        raise ValueError("live Taker profile must not contain Bitfinex credentials")
    max_cost_age_ns = profile.strategy_config.max_cost_age_ns
    if type(max_cost_age_ns) is not int or max_cost_age_ns <= 0:
        raise ValueError("max_cost_age_ns must be a positive exact integer")
    timeout = profile.connection_timeout_seconds
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 60
    ):
        raise ValueError("connection_timeout_seconds must be finite and in (0, 60]")

    paths = {
        _required_path(execution.cid_store_path, "Bitfinex CID store"),
        _required_path(profile.strategy_config.store_path, "Taker state store"),
    }
    if len(paths) != 2:
        raise ValueError("Bitfinex CID and Taker state stores must use distinct paths")


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
    environment: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    node_builder: LiveTakerNodeBuilder = build_live_taker_node,
    rehearsal_runner: LiveTakerRehearsalRunner | None = None,
) -> LiveTakerEntryResult:
    """Validate offline, rehearse adapters, or run the paper-bound Taker."""
    validate_live_taker_profile(profile)
    if rehearse and run_paper:
        raise ValueError("rehearse and run_paper are mutually exclusive")
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
        node, strategy = node_builder(
            bitfinex_data_config=profile.bitfinex_data_config,
            bitfinex_exec_config=bitfinex_execution,
            mt5_data_config=profile.mt5_data_config,
            mt5_exec_config=profile.mt5_exec_config,
            strategy_config=profile.strategy_config,
            loop=loop,
            connection_timeout_seconds=float(profile.connection_timeout_seconds),
        )
        if not rehearse and not run_paper:
            return LiveTakerEntryResult("VALIDATED", "offline_composition_built")

        if run_paper:
            strategies = node.trader.strategies()
            if len(strategies) != 1 or strategies[0] is not strategy:
                raise LiveTakerEntryError("paper runner requires exactly the built Taker strategy")
            run_paper_node(node)
            if node.is_running():
                raise LiveTakerEntryError("paper runner returned while the node was running")
            return LiveTakerEntryResult("PAPER_STOPPED", "paper_strategy_stopped")

        # Starting Taker itself is not read-only: its recovery path can persist state and
        # its stop path can cancel an active source order. Rehearsal therefore starts only
        # the exact four clients and Nautilus reconciliation/portfolio lifecycle.
        node.trader.remove_strategy(strategy.id)
        if node.trader.strategies():
            raise LiveTakerEntryError("read-only rehearsal retained a trading strategy")
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


def _validate_paper_binding(profile: LiveTakerProfile) -> None:
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
    if node.trader.strategies():
        raise LiveTakerEntryError("read-only rehearsal cannot start a strategy")
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
    parser = argparse.ArgumentParser(
        description="Validate, rehearse, or run the paper-bound PY000 Taker",
    )
    parser.add_argument("--profile", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--rehearse",
        action="store_true",
        help="connect and run one bounded adapter reconciliation without a strategy",
    )
    mode.add_argument(
        "--run-paper",
        action="store_true",
        help="run the existing Taker strategy on its bound Bitfinex paper account",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        profile = load_live_taker_profile(args.profile)
        result = run_live_taker_entry(
            profile,
            rehearse=args.rehearse,
            run_paper=args.run_paper,
            environment=environment,
            env_file=args.env_file,
            node_builder=node_builder,
            rehearsal_runner=rehearsal_runner,
        )
    except Exception as exc:
        print(
            json.dumps({"outcome": "FAILED", "reason": type(exc).__name__}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"outcome": result.outcome, "reason": result.reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
