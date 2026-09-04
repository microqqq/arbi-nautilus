"""Repeatable paper/demo smoke for both bounded Taker directions."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from msgspec.structs import replace as struct_replace

from py000_nautilus.bitfinex_v1_paper_canary import read_snapshot
from py000_nautilus.bitfinex_v1_rest import BitfinexV1RestClient
from py000_nautilus.live_taker_canary import (
    CANARY_QUANTITY,
    TakerCanaryResult,
    run_taker_canary,
    validate_taker_canary_profile,
)
from py000_nautilus.live_taker_entry import (
    LiveTakerProfile,
    load_bitfinex_test_credentials,
    load_live_taker_profile,
    validate_live_taker_profile,
)
from py000_nautilus.models import SourceDirection
from py000_nautilus.mt5_v1_protocol import Identity, JsonObject
from py000_nautilus.mt5_v1_transport import Mt5V1Transport

TEST_TRIGGER_THRESHOLD = Decimal("-0.005")
CANARY_HEDGE_LOTS = Decimal("0.02")
SMOKE_MT5_REQUEST_TIMEOUT_MS = 5_000
MAX_FORMAL_QUOTE_AGE_NS = 5_000_000_000
MAX_FORMAL_CROSS_LEG_SKEW_NS = 2_000_000_000
MAX_FORMAL_MT5_TICK_AGE_MS = 15_000
MT5_SYMBOL_TRADE_MODE_FULL = 4
MT5_SYMBOL_FILLING_FOK = 1
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")

SmokeDirection = Literal["long", "short", "both"]
SmokeOutcome = Literal["VALIDATED", "PASSED", "HOLD", "UNKNOWN", "FAILED"]


class TakerSmokeError(RuntimeError):
    """The bounded smoke could not establish a trustworthy result."""


@dataclass(frozen=True, slots=True)
class AuthoritySnapshot:
    """Small credential-free view of both venue authorities."""

    source_user_id: int
    source_paper_enabled: bool
    source_permissions_ok: bool
    source_withdrawal_disabled: bool
    source_active_orders: int
    source_position_ounces: Decimal
    hedge_position_count: int
    hedge_signed_ounces: Decimal
    hedge_execution_ready: bool
    hedge_trade_ready: bool
    hedge_session_ready: bool

    def record(self) -> dict[str, object]:
        return {
            "source_user_id": self.source_user_id,
            "source_paper_enabled": self.source_paper_enabled,
            "source_permissions_ok": self.source_permissions_ok,
            "source_withdrawal_disabled": self.source_withdrawal_disabled,
            "source_active_orders": self.source_active_orders,
            "source_position_ounces": format(self.source_position_ounces, "f"),
            "hedge_position_count": self.hedge_position_count,
            "hedge_signed_ounces": format(self.hedge_signed_ounces, "f"),
            "hedge_execution_ready": self.hedge_execution_ready,
            "hedge_trade_ready": self.hedge_trade_ready,
            "hedge_session_ready": self.hedge_session_ready,
        }


@dataclass(frozen=True, slots=True)
class SmokeLegPlan:
    name: str
    direction: SourceDirection
    close_existing: bool
    profile: LiveTakerProfile
    profile_path: Path
    state_path: Path


@dataclass(frozen=True, slots=True)
class SmokeLegResult:
    name: str
    direction: str
    close_existing: bool
    profile_path: str
    state_path: str
    canary: TakerCanaryResult
    authority: AuthoritySnapshot | None

    def record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "direction": self.direction,
            "close_existing": self.close_existing,
            "profile_path": self.profile_path,
            "state_path": self.state_path,
            "canary": {
                "outcome": self.canary.outcome,
                "reason": self.canary.reason,
                "source_order_id": self.canary.source_order_id,
            },
            "authority": None if self.authority is None else self.authority.record(),
        }


@dataclass(frozen=True, slots=True)
class TakerSmokeResult:
    outcome: SmokeOutcome
    reason: str
    run_id: str
    legs: tuple[SmokeLegResult, ...]

    def record(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "run_id": self.run_id,
            "legs": [leg.record() for leg in self.legs],
        }


class CanaryRunner(Protocol):
    def __call__(
        self,
        profile: LiveTakerProfile,
        *,
        direction: SourceDirection,
        execute: bool,
        close_existing: bool,
        signal_timeout_seconds: float,
        environment: Mapping[str, str] | None,
        env_file: Path | None,
    ) -> TakerCanaryResult: ...


class AuthorityReader(Protocol):
    def __call__(
        self,
        profile: LiveTakerProfile,
        *,
        environment: Mapping[str, str] | None,
        env_file: Path | None,
    ) -> AuthoritySnapshot: ...


def prepare_taker_smoke(
    base: LiveTakerProfile,
    *,
    direction: SmokeDirection,
    runtime_dir: Path,
    run_id: str,
) -> tuple[SmokeLegPlan, ...]:
    """Derive fresh paper-canary legs while preserving formal base freshness."""
    validate_live_taker_profile(base)
    _validate_smoke_base(base)
    if direction not in {"long", "short", "both"}:
        raise ValueError("smoke direction must be long, short, or both")
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must be 1..80 path-safe characters")

    root = runtime_dir.resolve(strict=False)
    specs = _leg_specs(direction)
    plans: list[SmokeLegPlan] = []
    for index, (name, source_direction, close_existing) in enumerate(specs, start=1):
        stem = f"taker-smoke-{run_id}-{index:02d}-{name}"
        state_path = root / f"{stem}.state.json"
        profile_path = root / f"{stem}.profile.json"
        profile = _canary_profile(base, state_path)
        validate_taker_canary_profile(profile)
        plans.append(
            SmokeLegPlan(
                name=name,
                direction=source_direction,
                close_existing=close_existing,
                profile=profile,
                profile_path=profile_path,
                state_path=state_path,
            )
        )
    return tuple(plans)


def run_taker_smoke(
    base: LiveTakerProfile,
    *,
    direction: SmokeDirection,
    runtime_dir: Path,
    run_id: str | None = None,
    execute: bool = False,
    signal_timeout_seconds: float = 60.0,
    environment: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    canary_runner: CanaryRunner = run_taker_canary,
    authority_reader: AuthorityReader | None = None,
) -> TakerSmokeResult:
    """Validate or run fixed open/close legs, stopping on the first uncertainty."""
    resolved_run_id = _new_run_id() if run_id is None else run_id
    plans = prepare_taker_smoke(
        base,
        direction=direction,
        runtime_dir=runtime_dir,
        run_id=resolved_run_id,
    )
    if (
        isinstance(signal_timeout_seconds, bool)
        or not isinstance(signal_timeout_seconds, int | float)
        or not math.isfinite(signal_timeout_seconds)
        or not 1 <= signal_timeout_seconds <= 300
    ):
        raise ValueError("signal timeout must be finite and in [1, 300]")

    if not execute:
        results = tuple(
            _run_leg(
                plan,
                execute=False,
                signal_timeout_seconds=float(signal_timeout_seconds),
                environment=environment,
                env_file=env_file,
                canary_runner=canary_runner,
                authority=None,
            )
            for plan in plans
        )
        if any(leg.canary.outcome != "VALIDATED" for leg in results):
            return TakerSmokeResult(
                "FAILED",
                "offline_leg_validation_failed",
                resolved_run_id,
                results,
            )
        return TakerSmokeResult(
            "VALIDATED",
            "paper_demo_cycle_built_disarmed",
            resolved_run_id,
            results,
        )

    _write_new_profiles(plans)
    reader = read_taker_authority if authority_reader is None else authority_reader
    completed: list[SmokeLegResult] = []
    try:
        authority = reader(plans[0].profile, environment=environment, env_file=env_file)
    except Exception:
        return TakerSmokeResult("FAILED", "initial_authority_read_failed", resolved_run_id, ())
    mismatch = _authority_mismatch(
        authority,
        plans[0].profile,
        expected_source=Decimal(0),
        expected_hedge=Decimal(0),
        require_ready=True,
    )
    if mismatch is not None:
        return TakerSmokeResult("HOLD", f"initial_authority:{mismatch}", resolved_run_id, ())

    for index, plan in enumerate(plans):
        try:
            canary_result = canary_runner(
                plan.profile,
                direction=plan.direction,
                execute=True,
                close_existing=plan.close_existing,
                signal_timeout_seconds=float(signal_timeout_seconds),
                environment=environment,
                env_file=env_file,
            )
        except Exception as exc:
            canary_result = TakerCanaryResult(
                "UNKNOWN",
                f"canary_runner_raised:{type(exc).__name__}",
            )
        try:
            authority = reader(plan.profile, environment=environment, env_file=env_file)
        except Exception:
            completed.append(_leg_result(plan, canary_result, None))
            return TakerSmokeResult(
                "UNKNOWN",
                f"{plan.name}:authority_read_failed",
                resolved_run_id,
                tuple(completed),
            )
        completed.append(_leg_result(plan, canary_result, authority))

        expected_outcome = "PASSED_FLAT" if plan.close_existing else "PASSED_PAIRED"
        expected_source, expected_hedge = _expected_authority(plan, after=True)
        mismatch = _authority_mismatch(
            authority,
            plan.profile,
            expected_source=expected_source,
            expected_hedge=expected_hedge,
            require_ready=index < len(plans) - 1,
        )
        if canary_result.outcome == expected_outcome:
            if mismatch is not None:
                return TakerSmokeResult(
                    "UNKNOWN",
                    f"{plan.name}:authority_mismatch:{mismatch}",
                    resolved_run_id,
                    tuple(completed),
                )
            continue

        before_source, before_hedge = _expected_authority(plan, after=False)
        before_mismatch = _authority_mismatch(
            authority,
            plan.profile,
            expected_source=before_source,
            expected_hedge=before_hedge,
            require_ready=False,
        )
        if mismatch is None or before_mismatch is not None:
            return TakerSmokeResult(
                "UNKNOWN",
                f"{plan.name}:{canary_result.outcome}:authority_conflict",
                resolved_run_id,
                tuple(completed),
            )
        outcome = _smoke_outcome(canary_result)
        if plan.close_existing and outcome != "UNKNOWN":
            outcome = "HOLD"
        return TakerSmokeResult(
            outcome,
            f"{plan.name}:{canary_result.outcome}:{canary_result.reason}",
            resolved_run_id,
            tuple(completed),
        )

    return TakerSmokeResult(
        "PASSED",
        "paper_demo_cycle_finished_flat",
        resolved_run_id,
        tuple(completed),
    )


def read_taker_authority(
    profile: LiveTakerProfile,
    *,
    environment: Mapping[str, str] | None,
    env_file: Path | None,
) -> AuthoritySnapshot:
    """Read both test venues through fresh read-only connections."""
    credentials = load_bitfinex_test_credentials(
        expected_user_id=profile.bitfinex_exec_config.user_id,
        environment=os.environ if environment is None else environment,
        env_file=env_file,
    )
    return asyncio.run(_read_taker_authority(profile, credentials.api_key, credentials.api_secret))


async def _read_taker_authority(
    profile: LiveTakerProfile,
    api_key: str,
    api_secret: str,
) -> AuthoritySnapshot:
    timeout = max(1, math.ceil(float(profile.connection_timeout_seconds)))
    rest = BitfinexV1RestClient(
        api_key=api_key,
        api_secret=api_secret,
        base_url=profile.bitfinex_exec_config.rest_url,
        timeout_secs=timeout,
    )
    source = await read_snapshot(rest)

    config = profile.mt5_exec_config
    transport = Mt5V1Transport(
        pub_url=config.pub_url,
        rep_url=config.rep_url,
        topic=config.expected_symbol,
        request_timeout_ms=config.request_timeout_ms,
        mutation_timeout_ms=config.mutation_timeout_ms,
    )
    await transport.open()
    try:
        identity, recovery = await transport.hello()
        snapshot = await transport.snapshot(identity.binding())
    finally:
        await transport.close()
    _validate_mt5_authority(profile, identity, recovery, snapshot)

    position_count, signed_ounces, trade_ready, session_ready = _mt5_snapshot_state(
        profile,
        snapshot,
    )
    return AuthoritySnapshot(
        source_user_id=source.user_id,
        source_paper_enabled=source.paper_enabled,
        source_permissions_ok=source.permissions_ok,
        source_withdrawal_disabled=source.withdrawal_disabled,
        source_active_orders=source.active_orders,
        source_position_ounces=source.position_quantity,
        hedge_position_count=position_count,
        hedge_signed_ounces=signed_ounces,
        hedge_execution_ready=(identity.execution_enabled and recovery == "ready"),
        hedge_trade_ready=trade_ready,
        hedge_session_ready=session_ready,
    )


def _mt5_snapshot_state(
    profile: LiveTakerProfile,
    snapshot: JsonObject,
) -> tuple[int, Decimal, bool, bool]:
    config = profile.mt5_exec_config
    positions = cast(list[JsonObject], snapshot["positions"])
    spec = cast(JsonObject, snapshot["symbol_spec"])
    flags = cast(JsonObject, snapshot["authority_flags"])
    contract_size = Decimal(cast(str, spec["contract_size"]))
    signed_ounces = Decimal(0)
    for position in positions:
        if position["magic"] != config.expected_magic:
            raise TakerSmokeError("MT5 authority contains a foreign-magic position")
        quantity = Decimal(cast(str, position["volume_lots"])) * contract_size
        signed_ounces += quantity if position["side"] == "buy" else -quantity

    trade_ready = (
        flags["terminal_connected"] is True
        and flags["terminal_trade_allowed"] is True
        and flags["account_trade_allowed"] is True
        and flags["account_trade_expert"] is True
        and flags["mql_trade_allowed"] is True
        and flags["symbol_trade_mode"] == MT5_SYMBOL_TRADE_MODE_FULL
        and spec["trade_mode"] == MT5_SYMBOL_TRADE_MODE_FULL
        and cast(int, spec["filling_mode"]) & MT5_SYMBOL_FILLING_FOK != 0
    )
    session = cast(JsonObject, snapshot["session"])
    freshness_age = session["freshness_age_ms"]
    session_ready = (
        session["session_schedule_available"] is True
        and session["scheduled_open"] is True
        and session["session_open"] is True
        and session["freshness"] == "fresh"
        and freshness_age is not None
        and int(cast(str, freshness_age)) <= profile.mt5_data_config.max_tick_age_ms
    )
    return len(positions), signed_ounces, trade_ready, session_ready


def _validate_smoke_base(profile: LiveTakerProfile) -> None:
    strategy = profile.strategy_config
    economics = strategy.economics
    if len(strategy.source_accounts) != 1:
        raise ValueError("smoke base requires exactly one source account")
    if any(
        not isinstance(value, Decimal) or not value.is_finite() or value <= 0
        for value in (economics.threshold_long, economics.threshold_short)
    ):
        raise ValueError("smoke base requires positive formal strategy thresholds")
    if strategy.max_quote_age_ns > MAX_FORMAL_QUOTE_AGE_NS:
        raise ValueError("smoke base quote freshness exceeds the formal ceiling")
    if strategy.max_cross_leg_skew_ns > MAX_FORMAL_CROSS_LEG_SKEW_NS:
        raise ValueError("smoke base cross-leg skew exceeds the formal ceiling")
    if profile.mt5_data_config.max_tick_age_ms > MAX_FORMAL_MT5_TICK_AGE_MS:
        raise ValueError("smoke base MT5 tick freshness exceeds the formal ceiling")


def _canary_profile(base: LiveTakerProfile, state_path: Path) -> LiveTakerProfile:
    strategy = base.strategy_config
    economics = strategy.economics
    risk = economics.risk
    route = strategy.source_accounts[0]
    canary_economics = struct_replace(
        economics,
        base_book_quantity=CANARY_QUANTITY,
        open_quantity_long=CANARY_QUANTITY,
        open_quantity_short=CANARY_QUANTITY,
        threshold_long=TEST_TRIGGER_THRESHOLD,
        threshold_short=TEST_TRIGGER_THRESHOLD,
        risk=struct_replace(
            risk,
            source_max_abs=CANARY_QUANTITY,
            hedge_max_abs=CANARY_QUANTITY,
            source_min_keep_abs=Decimal(0),
            hedge_min_keep_abs=Decimal(0),
            only_long=False,
        ),
    )
    canary_strategy = struct_replace(
        strategy,
        source_accounts=(
            struct_replace(
                route,
                max_long_ounces=CANARY_QUANTITY,
                max_short_ounces=CANARY_QUANTITY,
            ),
        ),
        hedge_max_long_ounces=CANARY_QUANTITY,
        hedge_max_short_ounces=CANARY_QUANTITY,
        economics=canary_economics,
        store_path=str(state_path),
        max_quote_age_ns=MAX_FORMAL_QUOTE_AGE_NS,
        max_cross_leg_skew_ns=MAX_FORMAL_CROSS_LEG_SKEW_NS,
    )
    return struct_replace(
        base,
        mt5_data_config=struct_replace(
            base.mt5_data_config,
            request_timeout_ms=max(
                base.mt5_data_config.request_timeout_ms,
                SMOKE_MT5_REQUEST_TIMEOUT_MS,
            ),
        ),
        mt5_exec_config=struct_replace(
            base.mt5_exec_config,
            expected_max_order_lots=CANARY_HEDGE_LOTS,
            request_timeout_ms=max(
                base.mt5_exec_config.request_timeout_ms,
                SMOKE_MT5_REQUEST_TIMEOUT_MS,
            ),
        ),
        strategy_config=canary_strategy,
    )


def _leg_specs(
    direction: SmokeDirection,
) -> tuple[tuple[str, SourceDirection, bool], ...]:
    short_cycle = (
        ("short-open", SourceDirection.SHORT, False),
        ("long-close", SourceDirection.LONG, True),
    )
    long_cycle = (
        ("long-open", SourceDirection.LONG, False),
        ("short-close", SourceDirection.SHORT, True),
    )
    if direction == "short":
        return short_cycle
    if direction == "long":
        return long_cycle
    return (*short_cycle, *long_cycle)


def _expected_authority(
    plan: SmokeLegPlan,
    *,
    after: bool,
) -> tuple[Decimal, Decimal]:
    if after:
        if plan.close_existing:
            return Decimal(0), Decimal(0)
        source = (
            CANARY_QUANTITY
            if plan.direction is SourceDirection.LONG
            else -CANARY_QUANTITY
        )
    else:
        if not plan.close_existing:
            return Decimal(0), Decimal(0)
        # A close signal is opposite to the position already held.
        source = (
            -CANARY_QUANTITY
            if plan.direction is SourceDirection.LONG
            else CANARY_QUANTITY
        )
    return source, -source


def _write_new_profiles(plans: tuple[SmokeLegPlan, ...]) -> None:
    paths = tuple(path for plan in plans for path in (plan.profile_path, plan.state_path))
    if len(set(paths)) != len(paths) or any(path.exists() for path in paths):
        raise TakerSmokeError("smoke profile or state path already exists")
    parent_dirs = {plan.profile_path.parent for plan in plans}
    if len(parent_dirs) != 1:
        raise AssertionError("smoke profiles escaped their runtime directory")
    parent_dirs.pop().mkdir(parents=True, exist_ok=True)
    for plan in plans:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(plan.profile_path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(plan.profile.json())


def _authority_mismatch(
    snapshot: AuthoritySnapshot,
    profile: LiveTakerProfile,
    *,
    expected_source: Decimal,
    expected_hedge: Decimal,
    require_ready: bool,
) -> str | None:
    expected_positions = 0 if expected_hedge == 0 else 1
    checks = (
        (snapshot.source_user_id != profile.bitfinex_exec_config.user_id, "source_user"),
        (not snapshot.source_paper_enabled, "source_not_paper"),
        (not snapshot.source_permissions_ok, "source_permissions"),
        (not snapshot.source_withdrawal_disabled, "source_withdrawal_permission"),
        (snapshot.source_active_orders != 0, "source_active_orders"),
        (snapshot.source_position_ounces != expected_source, "source_position"),
        (snapshot.hedge_position_count != expected_positions, "hedge_position_count"),
        (snapshot.hedge_signed_ounces != expected_hedge, "hedge_position"),
        (not snapshot.hedge_execution_ready, "hedge_execution"),
        (not snapshot.hedge_trade_ready, "hedge_trade_authority"),
        (require_ready and not snapshot.hedge_session_ready, "hedge_session"),
    )
    return next((reason for failed, reason in checks if failed), None)


def _validate_mt5_authority(
    profile: LiveTakerProfile,
    identity: Identity,
    recovery: str,
    snapshot: JsonObject,
) -> None:
    config = profile.mt5_exec_config
    expected = (
        (identity.account_id, config.expected_account_id),
        (identity.symbol, config.expected_symbol),
        (identity.magic, config.expected_magic),
        (identity.ea_build_id, config.expected_ea_build_id),
        (identity.declared_source_sha256, config.expected_source_sha256),
        (identity.stream_id, config.expected_stream_id),
        (identity.server_timezone, config.expected_server_timezone),
    )
    limits = cast(JsonObject, snapshot["execution_limits"])
    if (
        any(actual != wanted for actual, wanted in expected)
        or not identity.execution_enabled
        or recovery != "ready"
        or Identity.from_wire(snapshot["identity"]) != identity
        or snapshot["execution_enabled"] is not True
        or snapshot["recovery_state"] != "ready"
        or Decimal(cast(str, limits["max_order_lots"])) != config.expected_max_order_lots
    ):
        raise TakerSmokeError("MT5 authority does not match the configured demo execution")


def _run_leg(
    plan: SmokeLegPlan,
    *,
    execute: bool,
    signal_timeout_seconds: float,
    environment: Mapping[str, str] | None,
    env_file: Path | None,
    canary_runner: CanaryRunner,
    authority: AuthoritySnapshot | None,
) -> SmokeLegResult:
    result = canary_runner(
        plan.profile,
        direction=plan.direction,
        execute=execute,
        close_existing=plan.close_existing,
        signal_timeout_seconds=signal_timeout_seconds,
        environment=environment,
        env_file=env_file,
    )
    return _leg_result(plan, result, authority)


def _leg_result(
    plan: SmokeLegPlan,
    result: TakerCanaryResult,
    authority: AuthoritySnapshot | None,
) -> SmokeLegResult:
    return SmokeLegResult(
        name=plan.name,
        direction="long" if plan.direction is SourceDirection.LONG else "short",
        close_existing=plan.close_existing,
        profile_path=str(plan.profile_path),
        state_path=str(plan.state_path),
        canary=result,
        authority=authority,
    )


def _smoke_outcome(result: TakerCanaryResult) -> SmokeOutcome:
    if result.outcome == "UNKNOWN":
        return "UNKNOWN"
    if result.outcome in {"HOLD", "PARTIAL_HOLD", "EXPOSURE_HOLD"}:
        return "HOLD"
    return "FAILED"


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Validate or run the bounded PY000 paper/demo Taker cycle",
    )
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--direction", required=True, choices=("long", "short", "both"))
    parser.add_argument("--run-id")
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--signal-timeout", type=float, default=60.0)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        result = run_taker_smoke(
            load_live_taker_profile(args.profile),
            direction=cast(SmokeDirection, args.direction),
            runtime_dir=args.runtime_dir,
            run_id=args.run_id,
            execute=args.execute,
            signal_timeout_seconds=args.signal_timeout,
            environment=environment,
            env_file=args.env_file,
        )
    except Exception as exc:
        print(json.dumps({"outcome": "FAILED", "reason": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result.record(), sort_keys=True))
    expected = "PASSED" if args.execute else "VALIDATED"
    return 0 if result.outcome == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
