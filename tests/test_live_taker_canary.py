from __future__ import annotations

import asyncio
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from msgspec.structs import replace as struct_replace
from nautilus_trader.config import RoutingConfig
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId

import py000_nautilus.live_taker_canary as canary
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
from py000_nautilus.live_taker import (
    _one_shot_accounts_are_flat,
    _one_shot_accounts_match,
    build_live_taker_node,
)
from py000_nautilus.live_taker_entry import LiveTakerProfile
from py000_nautilus.models import (
    BusinessOrderSide,
    HedgeLeg,
    ObligationStatus,
    SourceDirection,
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
D = Decimal


def _profile(tmp_path: Path) -> LiveTakerProfile:
    bitfinex_route = RoutingConfig(default=False, venues=frozenset({"BITFINEX"}))
    mt5_route = RoutingConfig(default=False, venues=frozenset({"MT5"}))
    account_id = AccountId(f"BITFINEX-PAPER-{USER_ID}")
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
        account_id=account_id,
        instrument_id=SOURCE_ID,
        raw_symbol=PAPER_RAW_SYMBOL,
        wallet_currency="TESTUSDTF0",
        cid_store_path=str(tmp_path / "bitfinex-cids.json"),
        routing=bitfinex_route,
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
        "routing": mt5_route,
    }
    mt5_data = Mt5V1DataClientConfig(
        **mt5_identity,  # type: ignore[arg-type]
        expected_execution_enabled=True,
    )
    mt5_exec = Mt5V1ExecClientConfig(
        **mt5_identity,  # type: ignore[arg-type]
        expected_max_order_lots=D("0.02"),
        expected_stream_id="stream-test",
    )
    strategy = TakerStrategyConfig(
        source_instrument_id=SOURCE_ID,
        hedge_instrument_id=HEDGE_ID,
        source_accounts=(
            SourceAccountRoute(
                account_id=account_id,
                max_long_ounces=D(2),
                max_short_ounces=D(2),
                client_id=BITFINEX_ID,
                base_margin_level=D(100),
            ),
        ),
        hedge_account_id=AccountId("MT5-12345678"),
        hedge_max_long_ounces=D(2),
        hedge_max_short_ounces=D(2),
        economics=TakerEconomicsConfig(
            base_book_quantity=D(2),
            open_quantity_long=D(2),
            open_quantity_short=D(2),
            threshold_long=D("0.001"),
            threshold_short=D("0.001"),
            margin_level=D(500),
            carry=CarryConfig(total_trade_fee=D("0.00065")),
            fx=FxConfig(),
            risk=RiskConfig(source_max_abs=D(2), hedge_max_abs=D(2)),
        ),
        store_path=str(tmp_path / "taker-state.json"),
        hedge_client_id=MT5_ID,
    )
    return LiveTakerProfile(
        bitfinex_data_config=bitfinex_data,
        bitfinex_exec_config=bitfinex_exec,
        mt5_data_config=mt5_data,
        mt5_exec_config=mt5_exec,
        strategy_config=strategy,
        connection_timeout_seconds=2.0,
    )


def test_profile_requires_paper_binding_and_every_limit_exactly_two(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    canary.validate_taker_canary_profile(profile)

    wrong_wallet = struct_replace(
        profile,
        bitfinex_exec_config=struct_replace(
            profile.bitfinex_exec_config,
            wallet_currency="USDT",
        ),
    )
    with pytest.raises(ValueError, match="paper account and symbol"):
        canary.validate_taker_canary_profile(wrong_wallet)

    economics = struct_replace(profile.strategy_config.economics, base_book_quantity=D(3))
    wrong_quantity = struct_replace(
        profile,
        strategy_config=struct_replace(profile.strategy_config, economics=economics),
    )
    with pytest.raises(ValueError, match="all equal exactly 2oz"):
        canary.validate_taker_canary_profile(wrong_quantity)


def test_default_build_is_disarmed_offline_and_does_not_read_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_open(_transport: object) -> None:
        raise AssertionError("offline canary validation opened a transport")

    def unexpected_credentials(**_kwargs: object) -> object:
        raise AssertionError("offline canary validation read credentials")

    monkeypatch.setattr(BitfinexV1Transport, "open", unexpected_open)
    monkeypatch.setattr(Mt5V1Transport, "open", unexpected_open)
    monkeypatch.setattr(canary, "load_bitfinex_test_credentials", unexpected_credentials)
    observed: dict[str, object] = {}

    def builder(**kwargs: Any) -> tuple[Any, TakerStrategy]:
        observed["one_shot"] = kwargs["one_shot"]
        observed["direction"] = kwargs["allowed_source_direction"]
        observed["expected_source_position"] = kwargs["one_shot_expected_source_position"]
        node, strategy = build_live_taker_node(**kwargs)
        observed["strategy"] = strategy
        return node, strategy

    profile = _profile(tmp_path)
    result = canary.run_taker_canary(
        profile,
        direction=SourceDirection.LONG,
        node_builder=builder,
    )

    strategy = cast(TakerStrategy, observed["strategy"])
    assert result == canary.TakerCanaryResult(
        "VALIDATED",
        "exact_2oz_one_shot_built_disarmed",
    )
    assert observed["one_shot"] is True
    assert observed["direction"] is SourceDirection.LONG
    assert observed["expected_source_position"] == D(0)
    assert cast(Any, strategy)._hedge_must_reduce_only is False
    assert (strategy.one_shot_armed, strategy.one_shot_claimed) == (False, False)
    assert not Path(profile.bitfinex_exec_config.cid_store_path).exists()
    assert not Path(profile.strategy_config.store_path).exists()


def test_close_build_is_disarmed_and_binds_the_exact_existing_long_pair(
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def builder(**kwargs: Any) -> tuple[Any, Any]:
        observed.update(kwargs)
        return SimpleNamespace(kernel=SimpleNamespace(loop=kwargs["loop"])), object()

    def dispose(node: Any) -> None:
        node.kernel.loop.close()

    result = canary.run_taker_canary(
        _profile(tmp_path),
        direction=SourceDirection.SHORT,
        close_existing=True,
        node_builder=builder,
        disposer=dispose,
    )

    assert result == canary.TakerCanaryResult(
        "VALIDATED",
        "exact_2oz_one_shot_close_built_disarmed",
    )
    assert observed["one_shot"] is True
    assert observed["allowed_source_direction"] is SourceDirection.SHORT
    assert observed["one_shot_expected_source_position"] == D(2)
    assert observed["one_shot_close_existing"] is True


def test_source_admission_rechecks_flat_accounts_after_arm(tmp_path: Path) -> None:
    profile = _profile(tmp_path)

    class Cache:
        def __init__(self) -> None:
            self.positions: list[object] = []

        def orders_open(self, **_kwargs: object) -> list[object]:
            return []

        def positions_open(self, **_kwargs: object) -> list[object]:
            return self.positions

    cache = Cache()
    node = SimpleNamespace(
        cache=cache,
        portfolio=SimpleNamespace(net_position=lambda *_args: D(0)),
    )
    mt5 = SimpleNamespace(pending_client_order_ids=())

    assert _one_shot_accounts_are_flat(
        cast(Any, node),
        profile.strategy_config,
        cast(Any, mt5),
    )
    cache.positions = [object()]
    assert not _one_shot_accounts_are_flat(
        cast(Any, node),
        profile.strategy_config,
        cast(Any, mt5),
    )


def test_source_admission_can_require_one_exact_existing_pair(tmp_path: Path) -> None:
    profile = _profile(tmp_path)

    class Position:
        def __init__(self, quantity: Decimal) -> None:
            self.quantity = quantity

        def signed_decimal_qty(self) -> Decimal:
            return self.quantity

    positions = {
        SOURCE_ID: [Position(D(2))],
        HEDGE_ID: [Position(D(-2))],
    }
    node = SimpleNamespace(
        cache=SimpleNamespace(
            orders_open=lambda **_kwargs: [],
            positions_open=lambda *, instrument_id, **_kwargs: positions[instrument_id],
        ),
        portfolio=SimpleNamespace(
            net_position=lambda instrument_id, _account_id: sum(
                (position.quantity for position in positions[instrument_id]), D(0)
            )
        ),
    )

    assert _one_shot_accounts_match(
        cast(Any, node),
        profile.strategy_config,
        cast(Any, SimpleNamespace(pending_client_order_ids=())),
        expected_source_position=D(2),
    )
    positions[HEDGE_ID] = [Position(D("-0.5")), Position(D("-1.5"))]
    assert not _one_shot_accounts_match(
        cast(Any, node),
        profile.strategy_config,
        cast(Any, SimpleNamespace(pending_client_order_ids=())),
        expected_source_position=D(2),
    )
    positions[HEDGE_ID] = [Position(D("-1.99"))]
    assert not _one_shot_accounts_match(
        cast(Any, node),
        profile.strategy_config,
        cast(Any, SimpleNamespace(pending_client_order_ids=())),
        expected_source_position=D(2),
    )


def test_close_preflight_requires_the_exact_opposite_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path)
    strategy = SimpleNamespace(config=profile.strategy_config)

    class Position:
        def __init__(self, quantity: Decimal) -> None:
            self.quantity = quantity

        def signed_decimal_qty(self) -> Decimal:
            return self.quantity

    positions = {
        SOURCE_ID: [Position(D(2))],
        HEDGE_ID: [Position(D(-2))],
    }
    node = SimpleNamespace(
        is_running=lambda: True,
        cache=SimpleNamespace(
            orders_open=lambda **_kwargs: [],
            positions_open=lambda *, instrument_id, **_kwargs: positions[instrument_id],
        ),
        portfolio=SimpleNamespace(
            net_position=lambda instrument_id, _account_id: sum(
                (position.quantity for position in positions[instrument_id]), D(0)
            )
        ),
    )
    monkeypatch.setattr(
        canary,
        "_clients",
        lambda _node: (
            SimpleNamespace(is_connected=True, execution_hold_reason=None),
            SimpleNamespace(
                is_connected=True,
                execution_hold_reason=None,
                pending_client_order_ids=(),
                can_execute_quantity=lambda quantity: quantity == D(2),
            ),
        ),
    )
    monkeypatch.setattr(
        canary,
        "_data_clients",
        lambda _node: (
            SimpleNamespace(is_connected=True),
            SimpleNamespace(
                is_connected=True,
                snapshot_refresh_healthy=True,
                last_failure=None,
            ),
        ),
    )

    assert (
        canary._runtime_hold(
            cast(Any, node),
            cast(Any, strategy),
            expected_source_position=D(2),
        )
        is None
    )
    positions[HEDGE_ID] = [Position(D("-0.75")), Position(D("-1.25"))]
    assert (
        canary._runtime_hold(
            cast(Any, node),
            cast(Any, strategy),
            expected_source_position=D(2),
        )
        == "reconciled_positions_do_not_match_canary_start"
    )
    positions[HEDGE_ID] = [Position(D("-1.99"))]
    assert (
        canary._runtime_hold(
            cast(Any, node),
            cast(Any, strategy),
            expected_source_position=D(2),
        )
        == "reconciled_positions_do_not_match_canary_start"
    )


def test_existing_state_holds_before_credentials_or_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path)
    Path(profile.strategy_config.store_path).write_text("occupied", encoding="utf-8")
    lock = StringIO()
    monkeypatch.setattr(canary, "_lock_paper_account", lambda _user_id: lock)
    monkeypatch.setattr(
        canary,
        "load_bitfinex_test_credentials",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("credentials were read")),
    )

    def unexpected_builder(**_kwargs: object) -> tuple[Any, Any]:
        raise AssertionError("builder was called")

    result = canary.run_taker_canary(
        profile,
        direction=SourceDirection.SHORT,
        execute=True,
        node_builder=unexpected_builder,
    )

    assert result == canary.TakerCanaryResult(
        "HOLD",
        "canary_state_requires_operator_review",
    )
    assert lock.closed


def test_credential_failure_releases_account_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = StringIO()
    monkeypatch.setattr(canary, "_lock_paper_account", lambda _user_id: lock)
    monkeypatch.setattr(
        canary,
        "load_bitfinex_test_credentials",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("missing credentials")),
    )

    with pytest.raises(RuntimeError, match="missing credentials"):
        canary.run_taker_canary(
            _profile(tmp_path),
            direction=SourceDirection.LONG,
            execute=True,
        )

    assert lock.closed


def test_execute_wires_one_shot_direction_and_calls_fake_lifecycle_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path)
    lock = StringIO()
    monkeypatch.setattr(canary, "_lock_paper_account", lambda _user_id: lock)
    observed: dict[str, object] = {"builder": 0, "lifecycle": 0, "dispose": 0}
    strategy = object()

    def builder(**kwargs: Any) -> tuple[Any, Any]:
        observed["builder"] = cast(int, observed["builder"]) + 1
        observed["builder_kwargs"] = kwargs
        return SimpleNamespace(kernel=SimpleNamespace(loop=kwargs["loop"])), strategy

    def lifecycle(node: object, received: object, **kwargs: object) -> canary.TakerCanaryResult:
        observed["lifecycle"] = cast(int, observed["lifecycle"]) + 1
        observed["lifecycle_args"] = (node, received, kwargs)
        return canary.TakerCanaryResult("NO_ATTEMPT", "test_lifecycle")

    def dispose(node: Any) -> None:
        observed["dispose"] = cast(int, observed["dispose"]) + 1
        node.kernel.loop.close()

    result = canary.run_taker_canary(
        profile,
        direction=SourceDirection.SHORT,
        execute=True,
        signal_timeout_seconds=17,
        environment={
            "BFX_TEST_API_KEY": "TEST-KEY",
            "BFX_TEST_API_SECRET": "TEST-SECRET",
            "BFX_TEST_USER_ID": str(USER_ID),
        },
        env_file=tmp_path / "missing.env",
        node_builder=builder,
        lifecycle_runner=lifecycle,
        disposer=dispose,
    )

    kwargs = cast(dict[str, Any], observed["builder_kwargs"])
    _, received, lifecycle_kwargs = cast(
        tuple[object, object, dict[str, object]],
        observed["lifecycle_args"],
    )
    assert result == canary.TakerCanaryResult("NO_ATTEMPT", "test_lifecycle")
    assert (observed["builder"], observed["lifecycle"], observed["dispose"]) == (1, 1, 1)
    assert kwargs["one_shot"] is True
    assert kwargs["allowed_source_direction"] is SourceDirection.SHORT
    assert kwargs["one_shot_expected_source_position"] == D(0)
    assert kwargs["one_shot_close_existing"] is False
    assert kwargs["bitfinex_exec_config"].api_key == "TEST-KEY"
    assert received is strategy
    assert lifecycle_kwargs == {
        "direction": SourceDirection.SHORT,
        "close_existing": False,
        "signal_timeout_seconds": 17.0,
        "connection_timeout_seconds": 2.0,
    }
    assert lock.closed


def _claimed_strategy(
    tmp_path: Path,
    name: str,
    *,
    source_side: BusinessOrderSide = BusinessOrderSide.BUY,
) -> TakerStrategy:
    profile = _profile(tmp_path)
    config = struct_replace(profile.strategy_config, store_path=str(tmp_path / f"{name}.json"))
    direction = (
        SourceDirection.LONG if source_side is BusinessOrderSide.BUY else SourceDirection.SHORT
    )
    strategy = TakerStrategy(config, one_shot=True, allowed_source_direction=direction)
    strategy.arm_one_shot()
    assert strategy._claim_one_shot()
    route = config.source_accounts[0]
    strategy.state_store.begin_source(
        f"S-{name}",
        source_side,
        D(2),
        source_account_id=route.account_id.value,
        source_client_id=cast(ClientId, route.client_id).value,
        hedge_account_id=config.hedge_account_id.value,
        hedge_client_id=cast(ClientId, config.hedge_client_id).value,
        source_freeze_reason="one-shot source attempt claimed",
    )
    return strategy


def test_terminal_classifies_reject_partial_and_unknown_hedge(tmp_path: Path) -> None:
    rejected = _claimed_strategy(tmp_path, "reject")
    rejected.state_store.update_source_status("S-reject", "REJECTED")
    assert canary._terminal(rejected) == canary.TakerCanaryResult(
        "NO_FILL",
        "source_was_definitively_rejected",
        "S-reject",
    )

    partial = _claimed_strategy(tmp_path, "partial")
    partial.state_store.reserve_source_fill(
        fill_key="S-partial|V-1|T-1",
        client_order_id="S-partial",
        trade_id="T-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D("0.5"),
    )
    partial.state_store.update_source_status("S-partial", "CANCELED")
    assert canary._terminal(partial) == canary.TakerCanaryResult(
        "PARTIAL_HOLD",
        "source_ioc_was_partially_filled",
        "S-partial",
    )

    canceled = _claimed_strategy(tmp_path, "canceled")
    canceled.state_store.update_source_status("S-canceled", "CANCELED")
    assert canary._terminal(canceled) == canary.TakerCanaryResult(
        "UNKNOWN",
        "source_ioc_terminal_requires_reconciliation",
        "S-canceled",
    )
    assert canceled.state_store.active_source_order_id == "S-canceled"

    unknown = _claimed_strategy(tmp_path, "unknown")
    intent = unknown.state_store.reserve_source_fill(
        fill_key="S-unknown|V-1|T-1",
        client_order_id="S-unknown",
        trade_id="T-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    unknown.state_store.bind_hedge_order(intent.intent_id, "H-unknown")
    unknown.state_store.update_hedge_status("H-unknown", ObligationStatus.UNKNOWN)
    assert canary._terminal(unknown) == canary.TakerCanaryResult(
        "UNKNOWN",
        "hedge_outcome_is_unknown",
        "S-unknown",
    )


def test_active_hedge_is_allowed_to_finish_before_pending_sibling_is_held(
    tmp_path: Path,
) -> None:
    strategy = _claimed_strategy(tmp_path, "single-flight")
    first = strategy.state_store.reserve_source_fill(
        fill_key="S-single-flight|V-1|T-1",
        client_order_id="S-single-flight",
        trade_id="T-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    assert first is not None
    strategy.state_store.bind_hedge_order(first.intent_id, "H-1")
    second = strategy.state_store.reserve_source_fill(
        fill_key="S-single-flight|V-1|T-2",
        client_order_id="S-single-flight",
        trade_id="T-2",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )

    assert second is not None
    assert second.status is ObligationStatus.PENDING
    assert canary._terminal(strategy) is None


def _complete_strategy(
    tmp_path: Path,
    name: str,
    *,
    source_side: BusinessOrderSide = BusinessOrderSide.BUY,
    planned_close: bool = False,
) -> TakerStrategy:
    strategy = _claimed_strategy(tmp_path, name, source_side=source_side)
    source_id = f"S-{name}"
    intent = strategy.state_store.reserve_source_fill(
        fill_key=f"{source_id}|V-1|T-1",
        client_order_id=source_id,
        trade_id="T-1",
        source_side=source_side,
        fill_ounces=D(2),
    )
    assert intent is not None
    if planned_close:
        strategy.state_store.bind_hedge_plan(
            intent.intent_id,
            (
                HedgeLeg(
                    side=intent.hedge_side,
                    quantity_ounces=D(2),
                    position_id=f"P-{name}",
                    expected_position_side=source_side,
                    expected_position_quantity_ounces=D(2),
                ),
            ),
        )
    strategy.state_store.bind_hedge_order(intent.intent_id, f"H-{name}")
    assert strategy.state_store.apply_hedge_fill(
        client_order_id=f"H-{name}",
        trade_id="HT-1",
        fill_ounces=D(2),
    )
    return strategy


@pytest.mark.parametrize(
    (
        "engine_connected",
        "bitfinex_connected",
        "mt5_connected",
        "snapshot_refresh_healthy",
        "expected",
    ),
    [
        (True, True, True, True, None),
        (False, True, True, True, "data_plane_not_clean_after_reconciliation"),
        (True, False, True, True, "data_plane_not_clean_after_reconciliation"),
        (True, True, False, True, "data_plane_not_clean_after_reconciliation"),
        (True, True, True, False, "data_plane_not_clean_after_reconciliation"),
    ],
)
def test_final_data_plane_requires_connected_clients_and_healthy_snapshot_refresh(
    monkeypatch: pytest.MonkeyPatch,
    engine_connected: bool,
    bitfinex_connected: bool,
    mt5_connected: bool,
    snapshot_refresh_healthy: bool,
    expected: str | None,
) -> None:
    node = SimpleNamespace(
        kernel=SimpleNamespace(
            data_engine=SimpleNamespace(check_connected=lambda: engine_connected),
        ),
    )
    monkeypatch.setattr(
        canary,
        "_data_clients",
        lambda _node: (
            SimpleNamespace(is_connected=bitfinex_connected),
            SimpleNamespace(
                is_connected=mt5_connected,
                snapshot_refresh_healthy=snapshot_refresh_healthy,
            ),
        ),
    )

    assert canary._final_data_plane_mismatch(cast(Any, node)) == expected


@pytest.mark.parametrize(
    (
        "source_position",
        "source_tif",
        "snapshot_refresh_healthy",
        "outcome",
        "reason",
    ),
    [
        (D(2), TimeInForce.IOC, True, "PASSED_PAIRED", "exact_2oz_pair_reconciled"),
        (
            D(2),
            TimeInForce.IOC,
            False,
            "UNKNOWN",
            "data_plane_not_clean_after_reconciliation",
        ),
        (
            D(1),
            TimeInForce.IOC,
            True,
            "UNKNOWN",
            "paired_positions_are_not_exact_and_opposite",
        ),
        (
            D(2),
            TimeInForce.GTC,
            True,
            "UNKNOWN",
            "order_evidence_is_not_exact_ioc_fok_2oz",
        ),
    ],
)
def test_exact_success_and_position_near_miss_are_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_position: Decimal,
    source_tif: TimeInForce,
    snapshot_refresh_healthy: bool,
    outcome: canary.CanaryOutcome,
    reason: str,
) -> None:
    strategy = _complete_strategy(tmp_path, str(source_position))
    assert canary._success_state(strategy)

    class ExecEngine:
        async def reconcile_execution_state(self, *, timeout_secs: float) -> bool:
            assert timeout_secs == 0.1
            return True

    class Portfolio:
        def net_position(self, instrument_id: InstrumentId, account_id: AccountId) -> Decimal:
            del account_id
            return source_position if instrument_id == SOURCE_ID else D(-2)

    def positions_open(*, instrument_id: InstrumentId, **_kwargs: object) -> list[object]:
        quantity = source_position if instrument_id == SOURCE_ID else D(-2)
        return [SimpleNamespace(signed_decimal_qty=lambda: quantity)]

    quantity = SimpleNamespace(as_decimal=lambda: D(2))
    source_order = SimpleNamespace(
        order_type=OrderType.LIMIT,
        time_in_force=source_tif,
        side=OrderSide.BUY,
        quantity=quantity,
        filled_qty=quantity,
        is_reduce_only=False,
        is_closed=True,
    )
    hedge_order = SimpleNamespace(
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.FOK,
        side=OrderSide.SELL,
        quantity=quantity,
        filled_qty=quantity,
        is_reduce_only=False,
        is_closed=True,
    )
    orders = {
        f"S-{source_position}": source_order,
        f"H-{source_position}": hedge_order,
    }

    node = SimpleNamespace(
        kernel=SimpleNamespace(
            data_engine=SimpleNamespace(check_connected=lambda: True),
            exec_engine=ExecEngine(),
        ),
        cache=SimpleNamespace(
            orders_open=lambda **_kwargs: [],
            positions_open=positions_open,
            order=lambda client_order_id: orders.get(client_order_id.value),
        ),
        portfolio=Portfolio(),
    )
    monkeypatch.setattr(
        canary,
        "_clients",
        lambda _node: (
            SimpleNamespace(
                execution_hold_reason=None,
                terminal_reconciliation_required=False,
            ),
            SimpleNamespace(execution_hold_reason=None, pending_client_order_ids=()),
        ),
    )
    monkeypatch.setattr(
        canary,
        "_data_clients",
        lambda _node: (
            SimpleNamespace(is_connected=True),
            SimpleNamespace(
                is_connected=True,
                snapshot_refresh_healthy=snapshot_refresh_healthy,
            ),
        ),
    )

    async def scenario() -> canary.TakerCanaryResult:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        try:
            return await canary._wait_terminal(
                cast(Any, node),
                strategy,
                task,
                SourceDirection.LONG,
                0.1,
                0.1,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    result = asyncio.run(scenario())
    assert result.outcome == outcome
    assert result.reason == reason


def test_source_terminal_recovery_and_final_reconciliation_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = SimpleNamespace(client_order_id="S-recovered")
    strategy = SimpleNamespace(
        one_shot_claimed=True,
        state_store=SimpleNamespace(source_orders=lambda: (record,)),
    )
    state = SimpleNamespace(success=False)

    class Bitfinex:
        terminal_reconciliation_required = True
        confirm_calls = 0

        def confirm_terminal_reconciliation(self) -> None:
            self.confirm_calls += 1
            self.terminal_reconciliation_required = False
            asyncio.get_running_loop().call_soon(setattr, state, "success", True)

    class ExecEngine:
        def __init__(self) -> None:
            self.calls: list[float] = []

        async def reconcile_execution_state(self, *, timeout_secs: float) -> bool:
            self.calls.append(timeout_secs)
            return True

    bitfinex = Bitfinex()
    engine = ExecEngine()
    node = SimpleNamespace(kernel=SimpleNamespace(exec_engine=engine))
    monkeypatch.setattr(
        canary,
        "_clients",
        lambda _node: (bitfinex, SimpleNamespace()),
    )
    monkeypatch.setattr(canary, "_terminal", lambda _strategy: None)
    monkeypatch.setattr(canary, "_success_state", lambda _strategy: state.success)
    monkeypatch.setattr(canary, "_final_mismatch", lambda *_args, **_kwargs: None)

    async def scenario() -> canary.TakerCanaryResult:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        try:
            return await canary._wait_terminal(
                cast(Any, node),
                cast(Any, strategy),
                task,
                SourceDirection.LONG,
                0.1,
                0.05,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    result = asyncio.run(scenario())

    assert result == canary.TakerCanaryResult(
        "PASSED_PAIRED",
        "exact_2oz_pair_reconciled",
        "S-recovered",
    )
    assert engine.calls == [0.05, 0.05]
    assert bitfinex.confirm_calls == 1


def test_silent_rest_terminal_reconciliation_drives_source_fill_and_hedge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _claimed_strategy(
        tmp_path,
        "silent-rest-close",
        source_side=BusinessOrderSide.SELL,
    )

    class Bitfinex:
        terminal_reconciliation_required = True
        confirm_calls = 0

        def confirm_terminal_reconciliation(self) -> None:
            self.confirm_calls += 1
            assert canary._success_state(strategy)
            self.terminal_reconciliation_required = False

    class ExecEngine:
        def __init__(self) -> None:
            self.calls: list[float] = []

        async def reconcile_execution_state(self, *, timeout_secs: float) -> bool:
            self.calls.append(timeout_secs)
            if len(self.calls) == 1:
                intent = strategy.state_store.reserve_source_fill(
                    fill_key="S-silent-rest-close|243271104376|1967947277",
                    client_order_id="S-silent-rest-close",
                    trade_id="1967947277",
                    source_side=BusinessOrderSide.SELL,
                    fill_ounces=D(2),
                )
                assert intent is not None
                strategy.state_store.bind_hedge_order(intent.intent_id, "H-silent-rest-close")
                assert strategy.state_store.apply_hedge_fill(
                    client_order_id="H-silent-rest-close",
                    trade_id="MT5-DEAL-1",
                    fill_ounces=D(2),
                )
            return True

    bitfinex = Bitfinex()
    engine = ExecEngine()
    node = SimpleNamespace(kernel=SimpleNamespace(exec_engine=engine))
    monkeypatch.setattr(
        canary,
        "_clients",
        lambda _node: (bitfinex, SimpleNamespace()),
    )
    monkeypatch.setattr(canary, "_final_mismatch", lambda *_args, **_kwargs: None)

    async def scenario() -> canary.TakerCanaryResult:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        try:
            return await canary._wait_terminal(
                cast(Any, node),
                strategy,
                task,
                SourceDirection.SHORT,
                0.1,
                0.05,
                close_existing=True,
                expected_source_position=D(2),
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert asyncio.run(scenario()) == canary.TakerCanaryResult(
        "PASSED_FLAT",
        "exact_2oz_pair_closed",
        "S-silent-rest-close",
    )
    assert engine.calls == [0.05, 0.05]
    assert bitfinex.confirm_calls == 1
    assert strategy.state_store.intents()[0].status is ObligationStatus.COMPLETED


def test_silent_terminal_reconciliation_is_bounded_when_rest_only_reports_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = SimpleNamespace(client_order_id="S-still-open")
    strategy = SimpleNamespace(
        one_shot_claimed=True,
        state_store=SimpleNamespace(source_orders=lambda: (record,)),
    )

    class Bitfinex:
        terminal_reconciliation_required = True
        confirm_calls = 0

        def confirm_terminal_reconciliation(self) -> None:
            self.confirm_calls += 1

    class ExecEngine:
        calls = 0

        async def reconcile_execution_state(self, *, timeout_secs: float) -> bool:
            assert timeout_secs == 0.01
            self.calls += 1
            return True

    bitfinex = Bitfinex()
    engine = ExecEngine()
    node = SimpleNamespace(kernel=SimpleNamespace(exec_engine=engine))
    monkeypatch.setattr(canary, "_clients", lambda _node: (bitfinex, SimpleNamespace()))
    monkeypatch.setattr(canary, "_terminal", lambda _strategy: None)
    monkeypatch.setattr(canary, "_success_state", lambda _strategy: False)

    async def scenario() -> canary.TakerCanaryResult:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        try:
            return await canary._wait_terminal(
                cast(Any, node),
                cast(Any, strategy),
                task,
                SourceDirection.SHORT,
                0.02,
                0.01,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert asyncio.run(scenario()) == canary.TakerCanaryResult(
        "UNKNOWN",
        "source_terminal_reconciliation_failed",
        "S-still-open",
    )
    assert engine.calls == 2
    assert bitfinex.confirm_calls == 2


@pytest.mark.parametrize(
    ("snapshot_refresh_healthy", "expected_outcome", "expected_reason"),
    [
        (True, "PASSED_FLAT", "exact_2oz_pair_closed"),
        (False, "UNKNOWN", "data_plane_not_clean_after_reconciliation"),
    ],
)
def test_exact_existing_pair_closes_to_flat_only_with_reduce_only_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_refresh_healthy: bool,
    expected_outcome: canary.CanaryOutcome,
    expected_reason: str,
) -> None:
    strategy = _complete_strategy(
        tmp_path,
        "close",
        source_side=BusinessOrderSide.BUY,
        planned_close=True,
    )
    completed = strategy.state_store.intents()[0]
    assert completed.hedge_client_order_id is None
    assert completed.hedge_order_ids == ("H-close",)

    class ExecEngine:
        async def reconcile_execution_state(self, *, timeout_secs: float) -> bool:
            assert timeout_secs == 0.1
            return True

    quantity = SimpleNamespace(as_decimal=lambda: D(2))
    orders = {
        "S-close": SimpleNamespace(
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            side=OrderSide.BUY,
            quantity=quantity,
            filled_qty=quantity,
            is_reduce_only=True,
            is_closed=True,
        ),
        "H-close": SimpleNamespace(
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.FOK,
            side=OrderSide.SELL,
            quantity=quantity,
            filled_qty=quantity,
            is_reduce_only=True,
            is_closed=True,
        ),
    }
    node = SimpleNamespace(
        kernel=SimpleNamespace(
            data_engine=SimpleNamespace(check_connected=lambda: True),
            exec_engine=ExecEngine(),
        ),
        cache=SimpleNamespace(
            orders_open=lambda **_kwargs: [],
            positions_open=lambda **_kwargs: [],
            order=lambda client_order_id: orders.get(client_order_id.value),
        ),
        portfolio=SimpleNamespace(net_position=lambda *_args: D(0)),
    )
    monkeypatch.setattr(
        canary,
        "_clients",
        lambda _node: (
            SimpleNamespace(
                execution_hold_reason=None,
                terminal_reconciliation_required=False,
            ),
            SimpleNamespace(execution_hold_reason=None, pending_client_order_ids=()),
        ),
    )
    monkeypatch.setattr(
        canary,
        "_data_clients",
        lambda _node: (
            SimpleNamespace(is_connected=True),
            SimpleNamespace(
                is_connected=True,
                snapshot_refresh_healthy=snapshot_refresh_healthy,
            ),
        ),
    )

    async def scenario() -> canary.TakerCanaryResult:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        try:
            return await canary._wait_terminal(
                cast(Any, node),
                strategy,
                task,
                SourceDirection.LONG,
                0.1,
                0.1,
                close_existing=True,
                expected_source_position=D(-2),
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert asyncio.run(scenario()) == canary.TakerCanaryResult(
        expected_outcome,
        expected_reason,
        "S-close",
    )


@pytest.mark.parametrize("failure", [False, RuntimeError("offline recovery failed")])
def test_source_terminal_recovery_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    failure: bool | RuntimeError,
) -> None:
    record = SimpleNamespace(client_order_id="S-unreconciled")
    strategy = SimpleNamespace(
        one_shot_claimed=True,
        state_store=SimpleNamespace(source_orders=lambda: (record,)),
    )

    class Bitfinex:
        terminal_reconciliation_required = True
        confirm_calls = 0

        def confirm_terminal_reconciliation(self) -> None:
            self.confirm_calls += 1

    class ExecEngine:
        async def reconcile_execution_state(self, *, timeout_secs: float) -> bool:
            assert timeout_secs == 0.05
            if isinstance(failure, RuntimeError):
                raise failure
            return failure

    bitfinex = Bitfinex()
    node = SimpleNamespace(kernel=SimpleNamespace(exec_engine=ExecEngine()))
    monkeypatch.setattr(
        canary,
        "_clients",
        lambda _node: (bitfinex, SimpleNamespace()),
    )

    async def scenario() -> canary.TakerCanaryResult:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        try:
            return await canary._wait_terminal(
                cast(Any, node),
                cast(Any, strategy),
                task,
                SourceDirection.LONG,
                0.1,
                0.05,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    result = asyncio.run(scenario())

    assert result == canary.TakerCanaryResult(
        "UNKNOWN",
        "source_terminal_reconciliation_failed",
        "S-unreconciled",
    )
    assert bitfinex.confirm_calls == 0


def test_bounded_no_attempt_always_stops_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.new_event_loop()

    class Node:
        def __init__(self) -> None:
            self.kernel = SimpleNamespace(loop=loop)
            self.running = False
            self.finished = asyncio.Event()
            self.stop_calls = 0

        async def run_async(self) -> None:
            self.running = True
            await self.finished.wait()

        async def stop_async(self) -> None:
            self.stop_calls += 1
            self.running = False
            self.finished.set()

        def is_running(self) -> bool:
            return self.running

    class Strategy:
        def __init__(self) -> None:
            self.one_shot_armed = False
            self.one_shot_claimed = False
            self.source_book_callback_count = 0
            self.last_decision_gate = "awaiting_source_book"
            self.arm_calls = 0
            self.disarm_calls = 0
            self.state_store = SimpleNamespace(
                source_orders=lambda: (),
                intents=lambda: (),
            )

        def arm_one_shot(self) -> None:
            self.arm_calls += 1
            self.one_shot_armed = True

        def disarm_one_shot(self) -> None:
            self.disarm_calls += 1
            self.one_shot_armed = False

    async def ready(_node: object, _strategy: object, _task: object) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(canary, "_wait_ready", ready)
    monkeypatch.setattr(
        canary,
        "_preflight_hold",
        lambda _node, _strategy, **_kwargs: None,
    )
    monkeypatch.setattr(
        canary,
        "_runtime_hold",
        lambda _node, _strategy, **_kwargs: None,
    )
    node, strategy = Node(), Strategy()
    try:
        result = canary.run_bounded_taker_canary(
            cast(Any, node),
            cast(Any, strategy),
            direction=SourceDirection.LONG,
            signal_timeout_seconds=0.01,
            connection_timeout_seconds=0.1,
        )
    finally:
        loop.close()

    assert result == canary.TakerCanaryResult(
        "NO_ATTEMPT",
        "no_actionable_source_book_before_deadline",
    )
    assert strategy.arm_calls == 1
    assert strategy.disarm_calls == 1
    assert node.stop_calls == 1
    assert not node.running


def test_readiness_timeout_reason_reports_only_safe_gate_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ns = 20_000_000_000
    strategy = SimpleNamespace(
        clock=SimpleNamespace(timestamp_ns=lambda: now_ns),
        config=SimpleNamespace(
            hedge_instrument_id=HEDGE_ID,
            max_quote_age_ns=5_000_000_000,
            max_cost_age_ns=5_000_000_000,
            max_session_age_ns=5_000_000_000,
        ),
        cache=SimpleNamespace(
            quote_tick=lambda _instrument_id: SimpleNamespace(ts_event=18_000_000_000),
        ),
        is_running=True,
        _cost_ts_ns=13_000_000_000,
        _session_ts_ns=21_000_000_000,
        _cost_snapshot_valid=True,
        _hedge_session_open=False,
        source_independent_inputs_ready=lambda: False,
        source_book_callback_count=0,
    )
    node = SimpleNamespace(
        is_running=lambda: True,
        trader=SimpleNamespace(is_running=False),
        kernel=SimpleNamespace(
            data_engine=SimpleNamespace(check_connected=lambda: False),
            exec_engine=SimpleNamespace(check_connected=lambda: True),
        ),
    )
    bitfinex = SimpleNamespace(
        execution_hold_reason="SECRET order=123 price=456 balance=789",
        get_account=lambda: None,
    )
    mt5 = SimpleNamespace(execution_admitted=True, get_account=lambda: object())
    mt5_data = SimpleNamespace(
        identity_matched_pub_age_ms=17,
        committed_snapshot_count=3,
        identity_matched_pub_count=9,
        snapshot_refresh_healthy=True,
    )
    monkeypatch.setattr(canary, "_clients", lambda _node: (bitfinex, mt5))
    monkeypatch.setattr(
        canary,
        "_data_clients",
        lambda _node: (SimpleNamespace(), mt5_data),
    )

    reason = canary._readiness_timeout_reason(cast(Any, node), cast(Any, strategy))

    assert reason == (
        "readiness_timeout:missing=trader_running,data_engine_connected,"
        "bitfinex_execution_clear,bitfinex_account_ready,hedge_session_open,"
        "cost_fresh,session_fresh;"
        "ages_ms=hedge_tick:2000,cost:7000,session:-1000,mt5_pub:17;"
        "callbacks=source_book:0,mt5_snapshot:3,mt5_pub:9"
    )
    assert all(marker not in reason for marker in ("SECRET", "123", "456", "789"))

    monkeypatch.setattr(canary, "_readiness_timeout_snapshot", lambda *_args: "x" * 1_000)
    assert len(canary._readiness_timeout_reason(cast(Any, node), cast(Any, strategy))) == 768

    def failed_snapshot(*_args: object) -> str:
        raise RuntimeError("SECRET diagnostic detail")

    monkeypatch.setattr(canary, "_readiness_timeout_snapshot", failed_snapshot)
    fallback = canary._readiness_timeout_reason(cast(Any, node), cast(Any, strategy))
    assert "diagnostic_unavailable" in fallback
    assert "SECRET" not in fallback


def test_readiness_timeout_does_not_arm_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.new_event_loop()
    ready_calls = 0

    class Node:
        def __init__(self) -> None:
            self.kernel = SimpleNamespace(loop=loop)
            self.running = False
            self.finished = asyncio.Event()
            self.stop_calls = 0

        async def run_async(self) -> None:
            self.running = True
            await self.finished.wait()

        async def stop_async(self) -> None:
            self.stop_calls += 1
            self.running = False
            self.finished.set()

        def is_running(self) -> bool:
            return self.running

    class Strategy:
        one_shot_armed = False
        one_shot_claimed = False
        arm_calls = 0
        disarm_calls = 0

        def arm_one_shot(self) -> None:
            self.arm_calls += 1

        def disarm_one_shot(self) -> None:
            self.disarm_calls += 1

    async def never_ready(_node: object, _strategy: object, _task: object) -> None:
        nonlocal ready_calls
        ready_calls += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(canary, "_wait_ready", never_ready)
    monkeypatch.setattr(
        canary,
        "_readiness_timeout_reason",
        lambda _node, _strategy: "readiness_timeout:missing=cost_fresh",
    )
    node, strategy = Node(), Strategy()
    try:
        result = canary.run_bounded_taker_canary(
            cast(Any, node),
            cast(Any, strategy),
            direction=SourceDirection.LONG,
            signal_timeout_seconds=0.01,
            connection_timeout_seconds=0.01,
        )
    finally:
        loop.close()

    assert result == canary.TakerCanaryResult(
        "FAILED",
        "readiness_timeout:missing=cost_fresh",
    )
    assert ready_calls == 1
    assert strategy.arm_calls == 0
    assert strategy.disarm_calls == 0
    assert node.stop_calls == 1


def test_node_timeout_is_not_misreported_as_readiness_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.new_event_loop()

    class Node:
        def __init__(self) -> None:
            self.kernel = SimpleNamespace(loop=loop)

        async def run_async(self) -> None:
            await asyncio.sleep(0)
            raise TimeoutError("SECRET transport timeout detail")

    class Strategy:
        def __init__(self) -> None:
            self.one_shot_armed = False
            self.one_shot_claimed = False
            self.state_store = SimpleNamespace(
                source_orders=lambda: (),
            )

    async def clean_shutdown(*_args: object) -> None:
        return None

    def unexpected_readiness_diagnostic(_node: object, _strategy: object) -> str:
        raise AssertionError("node timeout used readiness deadline diagnostics")

    monkeypatch.setattr(canary, "_ready", lambda *_args: False)
    monkeypatch.setattr(canary, "_shutdown", clean_shutdown)
    monkeypatch.setattr(
        canary,
        "_readiness_timeout_reason",
        unexpected_readiness_diagnostic,
    )
    node, strategy = Node(), Strategy()
    try:
        result = canary.run_bounded_taker_canary(
            cast(Any, node),
            cast(Any, strategy),
            direction=SourceDirection.LONG,
            signal_timeout_seconds=0.01,
            connection_timeout_seconds=0.01,
        )
    finally:
        loop.close()

    assert result == canary.TakerCanaryResult("FAILED", "TimeoutError")


def test_no_attempt_reason_reports_the_last_decision_gate() -> None:
    strategy = SimpleNamespace(
        source_book_callback_count=2,
        last_decision_gate="inputs_not_fresh",
    )

    assert canary._no_attempt_reason(cast(Any, strategy)) == (
        "source_decision_blocked:inputs_not_fresh"
    )


def test_execute_exit_code_is_green_only_for_a_paired_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canary, "load_live_taker_profile", lambda _path: _profile(tmp_path))
    monkeypatch.setattr(
        canary,
        "run_taker_canary",
        lambda *_args, **_kwargs: canary.TakerCanaryResult(
            "NO_ATTEMPT",
            "no_qualifying_signal_before_deadline",
        ),
    )

    assert canary.main(["--profile", "unused.json", "--direction", "long", "--execute"]) == 1

    monkeypatch.setattr(
        canary,
        "run_taker_canary",
        lambda *_args, **_kwargs: canary.TakerCanaryResult(
            "PASSED_PAIRED",
            "exact_2oz_pair_reconciled",
        ),
    )
    assert canary.main(["--profile", "unused.json", "--direction", "long", "--execute"]) == 0

    monkeypatch.setattr(
        canary,
        "run_taker_canary",
        lambda *_args, **_kwargs: canary.TakerCanaryResult(
            "PASSED_FLAT",
            "exact_2oz_pair_closed",
        ),
    )
    assert (
        canary.main(
            [
                "--profile",
                "unused.json",
                "--direction",
                "short",
                "--execute",
                "--close-existing",
            ]
        )
        == 0
    )
