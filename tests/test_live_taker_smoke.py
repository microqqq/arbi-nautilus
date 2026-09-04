from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from msgspec.structs import replace as struct_replace
from nautilus_trader.config import RoutingConfig
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId

import py000_nautilus.live_taker_smoke as smoke
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, BitfinexV1DataClientConfig
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecClientConfig
from py000_nautilus.config import (
    CarryConfig,
    FxConfig,
    RiskConfig,
    SourceAccountRoute,
    TakerEconomicsConfig,
    TakerStrategyConfig,
)
from py000_nautilus.live_taker_canary import TakerCanaryResult
from py000_nautilus.live_taker_entry import LiveTakerProfile
from py000_nautilus.models import SourceDirection
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig
from py000_nautilus.mt5_v1_protocol import JsonObject

D = Decimal
USER_ID = 269_312
SOURCE_ID = InstrumentId.from_str("XAUTUSDT-PERP.BITFINEX")
HEDGE_ID = InstrumentId.from_str("XAUUSD.MT5")


def _base_profile(tmp_path: Path) -> LiveTakerProfile:
    bitfinex_route = RoutingConfig(default=False, venues=frozenset({"BITFINEX"}))
    mt5_route = RoutingConfig(default=False, venues=frozenset({"MT5"}))
    source_account = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    bitfinex_data = BitfinexV1DataClientConfig(
        url="wss://offline.invalid/ws/2",
        instrument_id=SOURCE_ID,
        raw_symbol=PAPER_RAW_SYMBOL,
        price_precision=1,
        size_precision=8,
        price_increment=D("0.1"),
        size_increment=D("0.00000001"),
        min_quantity=D(2),
        max_quantity=D(10_000),
        margin_init=D("0.01"),
        margin_maint=D("0.005"),
        maker_fee=D(0),
        taker_fee=D("0.0002"),
        routing=bitfinex_route,
    )
    bitfinex_exec = BitfinexV1ExecClientConfig(
        url="wss://offline.invalid/ws/2",
        rest_url="https://offline.invalid",
        api_key="",
        api_secret="",
        user_id=USER_ID,
        account_id=source_account,
        instrument_id=SOURCE_ID,
        raw_symbol=PAPER_RAW_SYMBOL,
        wallet_currency="TESTUSDTF0",
        cid_store_path=str(tmp_path / "account-cids.state.json"),
        routing=bitfinex_route,
    )
    mt5_common: dict[str, object] = {
        "pub_url": "tcp://127.0.0.1:6001",
        "rep_url": "tcp://127.0.0.1:6002",
        "instrument_id": HEDGE_ID,
        "expected_account_id": "demo-account",
        "expected_symbol": "XAUUSD",
        "expected_magic": "900000001",
        "expected_ea_build_id": "py000-mt5-ea-v1-taker-paper",
        "expected_source_sha256": "a" * 64,
        "expected_server_timezone": "Europe/Athens",
        "routing": mt5_route,
    }
    mt5_data = Mt5V1DataClientConfig(
        **mt5_common,  # type: ignore[arg-type]
        expected_execution_enabled=True,
        max_tick_age_ms=15_000,
    )
    mt5_exec = Mt5V1ExecClientConfig(
        **mt5_common,  # type: ignore[arg-type]
        expected_max_order_lots=D("0.02"),
        expected_stream_id="stream-test",
    )
    strategy = TakerStrategyConfig(
        source_instrument_id=SOURCE_ID,
        hedge_instrument_id=HEDGE_ID,
        source_accounts=(
            SourceAccountRoute(
                account_id=source_account,
                max_long_ounces=D(0),
                max_short_ounces=D(0),
                client_id=ClientId("BITFINEX"),
                base_margin_level=D(100),
            ),
        ),
        hedge_account_id=AccountId("MT5-demo-account"),
        hedge_max_long_ounces=D(0),
        hedge_max_short_ounces=D(0),
        economics=TakerEconomicsConfig(
            base_book_quantity=D(2),
            open_quantity_long=D(2),
            open_quantity_short=D(2),
            threshold_long=D("0.0009"),
            threshold_short=D("0.0005"),
            margin_level=D(500),
            carry=CarryConfig(total_trade_fee=D("0.00065")),
            fx=FxConfig(),
            risk=RiskConfig(source_max_abs=D(0), hedge_max_abs=D(0)),
        ),
        store_path=str(tmp_path / "rehearsal.state.json"),
        hedge_client_id=ClientId("MT5"),
        max_quote_age_ns=1_000_000_000,
        max_cross_leg_skew_ns=500_000_000,
        max_cost_age_ns=60_000_000_000,
    )
    return LiveTakerProfile(
        bitfinex_data_config=bitfinex_data,
        bitfinex_exec_config=bitfinex_exec,
        mt5_data_config=mt5_data,
        mt5_exec_config=mt5_exec,
        strategy_config=strategy,
        connection_timeout_seconds=20,
    )


def _authority(
    source: str = "0",
    hedge: str = "0",
    *,
    ready: bool = True,
) -> smoke.AuthoritySnapshot:
    return smoke.AuthoritySnapshot(
        source_user_id=USER_ID,
        source_paper_enabled=True,
        source_permissions_ok=True,
        source_withdrawal_disabled=True,
        source_active_orders=0,
        source_position_ounces=D(source),
        hedge_position_count=0 if D(hedge) == 0 else 1,
        hedge_signed_ounces=D(hedge),
        hedge_execution_ready=ready,
        hedge_trade_ready=ready,
        hedge_session_ready=ready,
    )


def _mt5_snapshot(*, positions: list[JsonObject] | None = None) -> JsonObject:
    return {
        "positions": [] if positions is None else positions,
        "symbol_spec": {
            "contract_size": "100",
            "filling_mode": 3,
            "trade_mode": smoke.MT5_SYMBOL_TRADE_MODE_FULL,
        },
        "authority_flags": {
            "terminal_connected": True,
            "terminal_trade_allowed": True,
            "account_trade_allowed": True,
            "account_trade_expert": True,
            "mql_trade_allowed": True,
            "symbol_trade_mode": smoke.MT5_SYMBOL_TRADE_MODE_FULL,
        },
        "session": {
            "session_schedule_available": True,
            "scheduled_open": True,
            "session_open": True,
            "freshness": "fresh",
            "freshness_age_ms": "1000",
        },
    }


class RecordingRunner:
    def __init__(self, outcomes: list[TakerCanaryResult] | None = None) -> None:
        self.outcomes = outcomes or []
        self.calls: list[tuple[LiveTakerProfile, SourceDirection, bool, bool]] = []

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
    ) -> TakerCanaryResult:
        assert signal_timeout_seconds == 60
        assert environment == {}
        assert env_file == Path("unused.env")
        self.calls.append((profile, direction, execute, close_existing))
        if self.outcomes:
            return self.outcomes.pop(0)
        if not execute:
            return TakerCanaryResult("VALIDATED", "built_disarmed")
        return TakerCanaryResult(
            "PASSED_FLAT" if close_existing else "PASSED_PAIRED",
            "closed" if close_existing else "paired",
            f"order-{len(self.calls)}",
        )


class RecordingAuthority:
    def __init__(self, snapshots: list[smoke.AuthoritySnapshot]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def __call__(
        self,
        profile: LiveTakerProfile,
        *,
        environment: Mapping[str, str] | None,
        env_file: Path | None,
    ) -> smoke.AuthoritySnapshot:
        assert profile.bitfinex_exec_config.user_id == USER_ID
        assert profile.mt5_exec_config.expected_max_order_lots == D("0.02")
        assert environment == {}
        assert env_file == Path("unused.env")
        self.calls += 1
        return self.snapshots.pop(0)


def test_dry_run_builds_both_cycles_without_credentials_network_or_files(tmp_path: Path) -> None:
    runtime = tmp_path / "not-created"
    runner = RecordingRunner()
    base = _base_profile(tmp_path)

    result = smoke.run_taker_smoke(
        base,
        direction="both",
        runtime_dir=runtime,
        run_id="dry-run",
        environment={},
        env_file=Path("unused.env"),
        canary_runner=runner,
        authority_reader=lambda *_args, **_kwargs: pytest.fail("authority read in dry-run"),
    )

    assert result.outcome == "VALIDATED"
    assert not runtime.exists()
    assert base.strategy_config.economics.threshold_long == D("0.0009")
    assert base.strategy_config.economics.threshold_short == D("0.0005")
    assert base.strategy_config.max_quote_age_ns == 1_000_000_000
    assert base.strategy_config.max_cross_leg_skew_ns == 500_000_000
    assert [call[1] for call in runner.calls] == [
        SourceDirection.SHORT,
        SourceDirection.LONG,
        SourceDirection.LONG,
        SourceDirection.SHORT,
    ]
    assert [call[3] for call in runner.calls] == [False, True, False, True]
    assert all(call[2] is False for call in runner.calls)
    assert len({call[0].strategy_config.store_path for call in runner.calls}) == 4
    assert {
        call[0].bitfinex_exec_config.cid_store_path for call in runner.calls
    } == {str(tmp_path / "account-cids.state.json")}
    for profile, *_rest in runner.calls:
        assert profile.strategy_config.economics.threshold_long == D("-0.005")
        assert profile.strategy_config.economics.threshold_short == D("-0.005")
        assert profile.strategy_config.max_quote_age_ns == 5_000_000_000
        assert profile.strategy_config.max_cross_leg_skew_ns == 2_000_000_000
        assert profile.mt5_data_config.max_tick_age_ms == 15_000
        assert profile.mt5_data_config.request_timeout_ms == 5_000
        assert profile.mt5_exec_config.request_timeout_ms == 5_000


def test_execute_runs_four_legs_with_authority_after_every_leg_and_finishes_flat(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runner = RecordingRunner()
    authority = RecordingAuthority(
        [
            _authority(),
            _authority("-2", "2"),
            _authority(),
            _authority("2", "-2"),
            _authority(),
        ]
    )

    result = smoke.run_taker_smoke(
        _base_profile(tmp_path),
        direction="both",
        runtime_dir=runtime,
        run_id="full-cycle",
        execute=True,
        environment={},
        env_file=Path("unused.env"),
        canary_runner=runner,
        authority_reader=authority,
    )

    assert result.outcome == "PASSED"
    assert result.reason == "paper_demo_cycle_finished_flat"
    assert authority.calls == 5
    assert [leg.name for leg in result.legs] == [
        "short-open",
        "long-close",
        "long-open",
        "short-close",
    ]
    assert len(runner.calls) == 4
    profile_paths = [Path(leg.profile_path) for leg in result.legs]
    assert all(path.exists() for path in profile_paths)
    assert all(not Path(leg.state_path).exists() for leg in result.legs)
    for path in profile_paths:
        raw = json.loads(path.read_text())
        assert raw["bitfinex_exec_config"]["api_key"] == ""
        assert raw["bitfinex_exec_config"]["api_secret"] == ""


@pytest.mark.parametrize(
    "change",
    ["negative_threshold", "quote_age", "cross_leg_skew", "mt5_tick_age"],
)
def test_rejects_test_only_or_overwide_base_profiles(tmp_path: Path, change: str) -> None:
    base = _base_profile(tmp_path)
    strategy = base.strategy_config
    if change == "negative_threshold":
        economics = struct_replace(strategy.economics, threshold_long=D("-0.005"))
        base = struct_replace(base, strategy_config=struct_replace(strategy, economics=economics))
    elif change == "quote_age":
        base = struct_replace(
            base,
            strategy_config=struct_replace(strategy, max_quote_age_ns=240_000_000_000),
        )
    elif change == "cross_leg_skew":
        base = struct_replace(
            base,
            strategy_config=struct_replace(strategy, max_cross_leg_skew_ns=240_000_000_000),
        )
    else:
        base = struct_replace(
            base,
            mt5_data_config=struct_replace(base.mt5_data_config, max_tick_age_ms=240_000),
        )

    with pytest.raises(ValueError):
        smoke.prepare_taker_smoke(
            base,
            direction="both",
            runtime_dir=tmp_path / "runtime",
            run_id="rejected",
        )


def test_execute_rejects_existing_profile_or_state_before_authority_or_canary(
    tmp_path: Path,
) -> None:
    base = _base_profile(tmp_path)
    runtime = tmp_path / "runtime"
    plans = smoke.prepare_taker_smoke(
        base,
        direction="long",
        runtime_dir=runtime,
        run_id="collision",
    )
    runtime.mkdir()
    plans[1].state_path.write_text("already used")
    runner = RecordingRunner()
    authority = RecordingAuthority([_authority()])

    with pytest.raises(smoke.TakerSmokeError, match="already exists"):
        smoke.run_taker_smoke(
            base,
            direction="long",
            runtime_dir=runtime,
            run_id="collision",
            execute=True,
            environment={},
            env_file=Path("unused.env"),
            canary_runner=runner,
            authority_reader=authority,
        )

    assert runner.calls == []
    assert authority.calls == 0


def test_unknown_leg_is_read_back_once_then_stops_without_retry(tmp_path: Path) -> None:
    runner = RecordingRunner([TakerCanaryResult("UNKNOWN", "ambiguous_submit", "source-1")])
    authority = RecordingAuthority([_authority(), _authority("-2", "2")])

    result = smoke.run_taker_smoke(
        _base_profile(tmp_path),
        direction="both",
        runtime_dir=tmp_path / "runtime",
        run_id="unknown",
        execute=True,
        environment={},
        env_file=Path("unused.env"),
        canary_runner=runner,
        authority_reader=authority,
    )

    assert result.outcome == "UNKNOWN"
    assert len(runner.calls) == 1
    assert authority.calls == 2
    assert len(result.legs) == 1
    assert result.legs[0].authority == _authority("-2", "2")


def test_authority_mismatch_after_pass_stops_before_close(tmp_path: Path) -> None:
    runner = RecordingRunner()
    authority = RecordingAuthority([_authority(), _authority()])

    result = smoke.run_taker_smoke(
        _base_profile(tmp_path),
        direction="short",
        runtime_dir=tmp_path / "runtime",
        run_id="mismatch",
        execute=True,
        environment={},
        env_file=Path("unused.env"),
        canary_runner=runner,
        authority_reader=authority,
    )

    assert result.outcome == "UNKNOWN"
    assert result.reason == "short-open:authority_mismatch:source_position"
    assert len(runner.calls) == 1
    assert authority.calls == 2


def test_close_no_fill_with_pair_still_open_is_a_hold_without_retry(tmp_path: Path) -> None:
    runner = RecordingRunner(
        [
            TakerCanaryResult("PASSED_PAIRED", "paired", "source-open"),
            TakerCanaryResult("NO_FILL", "close_not_filled", "source-close"),
        ]
    )
    authority = RecordingAuthority(
        [_authority(), _authority("-2", "2"), _authority("-2", "2")]
    )

    result = smoke.run_taker_smoke(
        _base_profile(tmp_path),
        direction="short",
        runtime_dir=tmp_path / "runtime",
        run_id="close-no-fill",
        execute=True,
        environment={},
        env_file=Path("unused.env"),
        canary_runner=runner,
        authority_reader=authority,
    )

    assert result.outcome == "HOLD"
    assert result.reason == "long-close:NO_FILL:close_not_filled"
    assert len(runner.calls) == 2
    assert authority.calls == 3


def test_nonpass_with_active_source_order_is_unknown(tmp_path: Path) -> None:
    runner = RecordingRunner([TakerCanaryResult("NO_FILL", "not_filled", "source-open")])
    active = replace(_authority(), source_active_orders=1)
    authority = RecordingAuthority([_authority(), active])

    result = smoke.run_taker_smoke(
        _base_profile(tmp_path),
        direction="short",
        runtime_dir=tmp_path / "runtime",
        run_id="active-order",
        execute=True,
        environment={},
        env_file=Path("unused.env"),
        canary_runner=runner,
        authority_reader=authority,
    )

    assert result.outcome == "UNKNOWN"
    assert result.reason == "short-open:NO_FILL:authority_conflict"
    assert len(runner.calls) == 1
    assert authority.calls == 2


@pytest.mark.parametrize(
    "flag",
    [
        "terminal_connected",
        "terminal_trade_allowed",
        "account_trade_allowed",
        "account_trade_expert",
        "mql_trade_allowed",
    ],
)
def test_mt5_snapshot_requires_every_live_trade_authority_flag(
    tmp_path: Path,
    flag: str,
) -> None:
    snapshot = _mt5_snapshot()
    cast(JsonObject, snapshot["authority_flags"])[flag] = False

    _, _, trade_ready, _ = smoke._mt5_snapshot_state(_base_profile(tmp_path), snapshot)

    assert trade_ready is False


@pytest.mark.parametrize(
    ("trade_mode", "filling_mode"),
    [(0, 3), (smoke.MT5_SYMBOL_TRADE_MODE_FULL, 2)],
)
def test_mt5_snapshot_requires_full_trade_mode_and_fok(
    tmp_path: Path,
    trade_mode: int,
    filling_mode: int,
) -> None:
    snapshot = _mt5_snapshot()
    flags = cast(JsonObject, snapshot["authority_flags"])
    spec = cast(JsonObject, snapshot["symbol_spec"])
    flags["symbol_trade_mode"] = trade_mode
    spec["trade_mode"] = trade_mode
    spec["filling_mode"] = filling_mode

    _, _, trade_ready, _ = smoke._mt5_snapshot_state(_base_profile(tmp_path), snapshot)

    assert trade_ready is False


def test_mt5_snapshot_enforces_numeric_tick_age_ceiling(tmp_path: Path) -> None:
    snapshot = _mt5_snapshot()
    cast(JsonObject, snapshot["session"])["freshness_age_ms"] = "159000"

    _, _, _, session_ready = smoke._mt5_snapshot_state(_base_profile(tmp_path), snapshot)

    assert session_ready is False


def test_mt5_snapshot_maps_hedging_positions_without_netting_them_away(
    tmp_path: Path,
) -> None:
    positions: list[JsonObject] = [
        {"magic": "900000001", "volume_lots": "0.02", "side": "buy"},
        {"magic": "900000001", "volume_lots": "0.02", "side": "sell"},
    ]

    count, signed, trade_ready, session_ready = smoke._mt5_snapshot_state(
        _base_profile(tmp_path),
        _mt5_snapshot(positions=positions),
    )

    assert count == 2
    assert signed == 0
    assert trade_ready is True
    assert session_ready is True
    authority = replace(
        _authority(),
        hedge_position_count=count,
        hedge_signed_ounces=signed,
    )
    assert smoke._authority_mismatch(
        authority,
        _base_profile(tmp_path),
        expected_source=D(0),
        expected_hedge=D(0),
        require_ready=True,
    ) == "hedge_position_count"
