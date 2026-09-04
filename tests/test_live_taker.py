from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from msgspec.structs import replace as struct_replace
from nautilus_trader.config import RoutingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport, PositionStatusReport
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import OrderSide, OrderStatus, PositionSide, TimeInForce
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from py000_nautilus.app import _hedge_instrument
from py000_nautilus.bitfinex_v1_data import (
    RAW_SYMBOL,
    BitfinexV1DataClient,
    BitfinexV1DataClientConfig,
    instrument_from_config,
)
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
)
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
from py000_nautilus.models import SourceDirection
from py000_nautilus.mt5_v1_data import Mt5V1DataClient, Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig, Mt5V1ExecutionClient
from py000_nautilus.mt5_v1_transport import Mt5V1Transport

SOURCE_ID = InstrumentId.from_str("XAUTUSDT-PERP.BITFINEX")
HEDGE_ID = InstrumentId.from_str("XAUUSD.MT5")
BITFINEX_ID = ClientId("BITFINEX")
MT5_ID = ClientId("MT5")


@dataclass(frozen=True)
class _Configs:
    bitfinex_data: BitfinexV1DataClientConfig
    bitfinex_exec: BitfinexV1ExecClientConfig
    mt5_data: Mt5V1DataClientConfig
    mt5_exec: Mt5V1ExecClientConfig
    strategy: TakerStrategyConfig


def _configs(tmp_path: Path) -> _Configs:
    bitfinex_routing = RoutingConfig(default=False, venues=frozenset({"BITFINEX"}))
    mt5_routing = RoutingConfig(default=False, venues=frozenset({"MT5"}))
    bitfinex_data = BitfinexV1DataClientConfig(
        url="wss://offline.invalid/ws/2",
        instrument_id=SOURCE_ID,
        raw_symbol=RAW_SYMBOL,
        price_precision=1,
        size_precision=8,
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.00000001"),
        min_quantity=Decimal("0.0001"),
        max_quantity=Decimal("10000"),
        margin_init=Decimal("0.01"),
        margin_maint=Decimal("0.005"),
        maker_fee=Decimal(0),
        taker_fee=Decimal("0.0002"),
        routing=bitfinex_routing,
    )
    bitfinex_exec = BitfinexV1ExecClientConfig(
        url="wss://offline.invalid/ws/2",
        rest_url="https://offline.invalid",
        api_key="NOT-A-REAL-KEY",
        api_secret="NOT-A-REAL-SECRET",
        user_id=269_312,
        account_id=AccountId("BITFINEX-269312"),
        instrument_id=SOURCE_ID,
        raw_symbol=RAW_SYMBOL,
        cid_store_path=str(tmp_path / "bitfinex-cids.json"),
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
    mt5_exec = Mt5V1ExecClientConfig(
        **mt5_identity,  # type: ignore[arg-type]
        expected_max_order_lots=Decimal("0.02"),
        expected_stream_id="stream-offline-1",
    )
    strategy = TakerStrategyConfig(
        source_instrument_id=SOURCE_ID,
        hedge_instrument_id=HEDGE_ID,
        source_accounts=(
            SourceAccountRoute(
                account_id=bitfinex_exec.account_id,
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
            base_book_quantity=Decimal(1),
            open_quantity_long=Decimal(1),
            open_quantity_short=Decimal(1),
            threshold_long=Decimal("0.001"),
            threshold_short=Decimal("0.001"),
            margin_level=Decimal(500),
            carry=CarryConfig(total_trade_fee=Decimal("0.0002")),
            fx=FxConfig(),
            risk=RiskConfig(source_max_abs=Decimal(10), hedge_max_abs=Decimal(10)),
        ),
        store_path=str(tmp_path / "taker-state.json"),
        hedge_client_id=MT5_ID,
        initial_hedge_session_open=False,
        initial_session_ts_ns=0,
    )
    return _Configs(bitfinex_data, bitfinex_exec, mt5_data, mt5_exec, strategy)


def _build(configs: _Configs, *, loop: asyncio.AbstractEventLoop | None = None) -> Any:
    return build_live_taker_node(
        bitfinex_data_config=configs.bitfinex_data,
        bitfinex_exec_config=configs.bitfinex_exec,
        mt5_data_config=configs.mt5_data,
        mt5_exec_config=configs.mt5_exec,
        strategy_config=configs.strategy,
        loop=loop,
        connection_timeout_seconds=3.0,
    )


def test_builds_exact_offline_taker_composition_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_bitfinex_open(_transport: BitfinexV1Transport) -> None:
        raise AssertionError("TradingNode.build must not open a Bitfinex transport")

    async def unexpected_mt5_open(_transport: Mt5V1Transport) -> None:
        raise AssertionError("TradingNode.build must not open an MT5 transport")

    monkeypatch.setattr(BitfinexV1Transport, "open", unexpected_bitfinex_open)
    monkeypatch.setattr(Mt5V1Transport, "open", unexpected_mt5_open)
    configs = _configs(tmp_path)
    loop = asyncio.new_event_loop()
    node, strategy = _build(configs, loop=loop)
    try:
        data_engine = node.kernel.data_engine
        exec_engine = node.kernel.exec_engine
        exec_clients = cast(dict[ClientId, object], exec_engine._clients)
        bitfinex_data = cast(
            BitfinexV1DataClient,
            data_engine.routing_map[Venue("BITFINEX")],
        )
        mt5_data = cast(Mt5V1DataClient, data_engine.routing_map[Venue("MT5")])
        bitfinex_exec = cast(BitfinexV1ExecutionClient, exec_clients[BITFINEX_ID])
        mt5_exec = cast(Mt5V1ExecutionClient, exec_clients[MT5_ID])

        assert node.is_built()
        assert not node.is_running()
        assert data_engine.registered_clients == [BITFINEX_ID, MT5_ID]
        assert data_engine.default_client is None
        assert set(data_engine.routing_map) == {Venue("BITFINEX"), Venue("MT5")}
        assert isinstance(bitfinex_data, BitfinexV1DataClient)
        assert isinstance(mt5_data, Mt5V1DataClient)
        assert exec_engine.registered_clients == [BITFINEX_ID, MT5_ID]
        assert exec_engine.default_client is None
        assert isinstance(bitfinex_exec, BitfinexV1ExecutionClient)
        assert isinstance(mt5_exec, Mt5V1ExecutionClient)
        assert isinstance(mt5_data._transport, Mt5V1Transport)
        assert isinstance(mt5_exec._transport, Mt5V1Transport)
        assert mt5_data._transport.rep_coordinator is mt5_exec._transport.rep_coordinator
        assert exec_engine.reconciliation is True
        assert exec_engine.generate_missing_orders is False
        assert exec_engine.inflight_check_interval_ms == 0
        assert exec_engine.open_check_interval_secs is None
        assert exec_engine.position_check_interval_secs is None
        assert node.trader.strategies() == [strategy]
        readiness = cast(Any, strategy)._live_submission_ready
        assert readiness is not None and readiness() is False
        with monkeypatch.context() as readiness_patch:
            readiness_patch.setattr(
                BitfinexV1DataClient,
                "is_connected",
                property(lambda _client: True),
            )
            readiness_patch.setattr(
                BitfinexV1DataClient,
                "book_is_actionable",
                property(lambda _client: True),
            )
            readiness_patch.setattr(
                BitfinexV1ExecutionClient,
                "execution_hold_reason",
                property(lambda _client: None),
            )
            readiness_patch.setattr(
                BitfinexV1ExecutionClient,
                "get_account",
                lambda _client: object(),
            )
            readiness_patch.setattr(
                Mt5V1DataClient,
                "is_connected",
                property(lambda _client: True),
            )
            readiness_patch.setattr(
                Mt5V1ExecutionClient,
                "execution_admitted",
                property(lambda _client: True),
            )
            readiness_patch.setattr(
                Mt5V1ExecutionClient,
                "get_account",
                lambda _client: object(),
            )

            mt5_data._snapshot_refresh_healthy = False
            assert readiness() is False
            mt5_data._snapshot_refresh_healthy = True
            assert readiness() is True
            mt5_data._snapshot_refresh_healthy = False
            assert readiness() is False
        assert cast(Any, strategy)._live_costs_from_adapters is True
        assert not Path(configs.bitfinex_exec.cid_store_path).exists()
        assert not Path(configs.strategy.store_path).exists()
    finally:
        node.dispose()

    assert loop.is_closed()
    assert list(tmp_path.iterdir()) == []


def test_build_injects_mt5_hedge_quantity_capability(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    node, strategy = _build(_configs(tmp_path), loop=loop)
    try:
        exec_clients = cast(dict[ClientId, object], node.kernel.exec_engine._clients)
        mt5_exec = exec_clients[MT5_ID]
        hedge_quantity_ready = cast(Any, strategy)._hedge_quantity_ready

        assert callable(hedge_quantity_ready)
        assert hedge_quantity_ready.__self__ is mt5_exec
        assert hedge_quantity_ready.__func__ is Mt5V1ExecutionClient.can_execute_quantity
    finally:
        node.dispose()

    assert loop.is_closed()


def test_close_build_scopes_nautilus_cold_position_reconciliation_to_both_legs(
    tmp_path: Path,
) -> None:
    configs = _configs(tmp_path)
    loop = asyncio.new_event_loop()
    node, strategy = build_live_taker_node(
        bitfinex_data_config=configs.bitfinex_data,
        bitfinex_exec_config=configs.bitfinex_exec,
        mt5_data_config=configs.mt5_data,
        mt5_exec_config=configs.mt5_exec,
        strategy_config=configs.strategy,
        loop=loop,
        connection_timeout_seconds=3.0,
        one_shot=True,
        allowed_source_direction=SourceDirection.SHORT,
        one_shot_expected_source_position=Decimal(2),
        one_shot_close_existing=True,
    )
    try:
        exec_engine = node.kernel.exec_engine
        assert exec_engine.generate_missing_orders is True
        assert exec_engine.reconciliation_instrument_ids == [
            configs.strategy.source_instrument_id,
            configs.strategy.hedge_instrument_id,
        ]
        exec_clients = cast(dict[ClientId, object], exec_engine._clients)
        bitfinex = cast(BitfinexV1ExecutionClient, exec_clients[BITFINEX_ID])
        assert bitfinex._bfx_config.allow_cold_position_reconciliation is True
        assert strategy.config.external_order_claims == [configs.strategy.source_instrument_id]
        assert cast(Any, strategy)._hedge_must_reduce_only is True
        assert node.cache.positions_open() == []
        assert node.cache.orders() == []
    finally:
        node.dispose()

    assert loop.is_closed()


def test_close_build_cold_reconciliation_assigns_source_ownership_and_keeps_mt5_ticket(
    tmp_path: Path,
) -> None:
    configs = _configs(tmp_path)
    loop = asyncio.new_event_loop()
    node, strategy = build_live_taker_node(
        bitfinex_data_config=configs.bitfinex_data,
        bitfinex_exec_config=configs.bitfinex_exec,
        mt5_data_config=configs.mt5_data,
        mt5_exec_config=configs.mt5_exec,
        strategy_config=configs.strategy,
        loop=loop,
        connection_timeout_seconds=3.0,
        one_shot=True,
        allowed_source_direction=SourceDirection.SHORT,
        one_shot_expected_source_position=Decimal(2),
        one_shot_close_existing=True,
    )
    try:
        source = instrument_from_config(configs.bitfinex_data, ts_init=0)
        hedge = _hedge_instrument()
        node.cache.add_instrument(source)
        node.cache.add_instrument(hedge)
        node.cache.add_account(
            TestExecStubs.margin_account(account_id=configs.bitfinex_exec.account_id)
        )
        node.cache.add_account(
            TestExecStubs.margin_account(account_id=configs.strategy.hedge_account_id)
        )
        source_report = PositionStatusReport(
            account_id=configs.bitfinex_exec.account_id,
            instrument_id=source.id,
            position_side=PositionSide.LONG,
            quantity=source.make_qty(Decimal(2)),
            avg_px_open=Decimal("4492.1"),
            report_id=UUID4(),
            ts_last=1,
            ts_init=1,
        )
        hedge_position_id = PositionId("10349046774")
        hedge_report = PositionStatusReport(
            account_id=configs.strategy.hedge_account_id,
            instrument_id=hedge.id,
            venue_position_id=hedge_position_id,
            position_side=PositionSide.SHORT,
            quantity=hedge.make_qty(Decimal(2)),
            avg_px_open=Decimal("4493.74"),
            report_id=UUID4(),
            ts_last=1,
            ts_init=1,
        )

        assert node.kernel.exec_engine._reconcile_position_report(source_report)
        assert node.kernel.exec_engine._reconcile_position_report(hedge_report)

        source_positions = node.cache.positions_open(
            instrument_id=source.id,
            account_id=configs.bitfinex_exec.account_id,
        )
        assert len(source_positions) == 1
        assert source_positions[0].strategy_id == strategy.id
        assert source_positions[0].signed_decimal_qty() == Decimal(2)
        hedge_position = node.cache.position(hedge_position_id)
        assert hedge_position is not None
        assert hedge_position.signed_decimal_qty() == Decimal(-2)
        assert not Path(configs.strategy.store_path).exists()

        close = strategy.order_factory.limit(
            instrument_id=source.id,
            order_side=OrderSide.SELL,
            quantity=source.make_qty(Decimal(2)),
            price=source.make_price(Decimal("4492.0")),
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            client_order_id=ClientOrderId("EXIT-CLOSE-1"),
        )
        node.cache.add_order(close)
        close_report = OrderStatusReport(
            account_id=configs.bitfinex_exec.account_id,
            instrument_id=source.id,
            client_order_id=close.client_order_id,
            venue_order_id=VenueOrderId("243269180099"),
            order_side=OrderSide.SELL,
            order_type=close.order_type,
            time_in_force=TimeInForce.IOC,
            order_status=OrderStatus.FILLED,
            price=close.price,
            quantity=close.quantity,
            filled_qty=close.quantity,
            avg_px=Decimal("4492.0"),
            reduce_only=True,
            report_id=UUID4(),
            ts_accepted=2,
            ts_last=2,
            ts_init=2,
        )
        assert node.kernel.exec_engine._reconcile_order_report(
            close_report,
            trades=[],
            is_external=False,
        )
        assert (
            node.cache.positions_open(
                instrument_id=source.id,
                account_id=configs.bitfinex_exec.account_id,
            )
            == []
        )
    finally:
        node.dispose()

    assert loop.is_closed()


def test_rejects_inconsistent_adapter_identities_and_implicit_routes(tmp_path: Path) -> None:
    configs = _configs(tmp_path)

    with pytest.raises(ValueError, match="Bitfinex data and execution profiles"):
        _build(
            _Configs(
                struct_replace(configs.bitfinex_data, raw_symbol="tTESTXAUTF0:TESTUSDTF0"),
                configs.bitfinex_exec,
                configs.mt5_data,
                configs.mt5_exec,
                configs.strategy,
            )
        )

    with pytest.raises(ValueError, match="same bound identity"):
        _build(
            _Configs(
                configs.bitfinex_data,
                configs.bitfinex_exec,
                struct_replace(configs.mt5_data, expected_magic="DIFFERENT"),
                configs.mt5_exec,
                configs.strategy,
            )
        )

    with pytest.raises(ValueError, match="execution_enabled=true"):
        _build(
            _Configs(
                configs.bitfinex_data,
                configs.bitfinex_exec,
                struct_replace(configs.mt5_data, expected_execution_enabled=False),
                configs.mt5_exec,
                configs.strategy,
            )
        )

    with pytest.raises(ValueError, match="non-default and restricted to BITFINEX"):
        _build(
            _Configs(
                struct_replace(configs.bitfinex_data, routing=RoutingConfig()),
                configs.bitfinex_exec,
                configs.mt5_data,
                configs.mt5_exec,
                configs.strategy,
            )
        )


def test_rejects_ambiguous_strategy_routes_session_and_state_paths(tmp_path: Path) -> None:
    configs = _configs(tmp_path)
    route = struct_replace(configs.strategy.source_accounts[0], client_id=None)
    with pytest.raises(ValueError, match="explicitly route to the Bitfinex"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, source_accounts=(route,)),
            )
        )

    with pytest.raises(ValueError, match="explicitly route to the MT5"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, hedge_client_id=None),
            )
        )

    with pytest.raises(ValueError, match="begin closed"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(
                    configs.strategy,
                    initial_hedge_session_open=True,
                    initial_session_ts_ns=1,
                ),
            )
        )

    with pytest.raises(ValueError, match="costs must begin stale"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, initial_cost_ts_ns=1),
            )
        )

    with pytest.raises(ValueError, match="distinct paths"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(
                    configs.strategy,
                    store_path=configs.bitfinex_exec.cid_store_path,
                ),
            )
        )


@pytest.mark.parametrize(
    "fee",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.0001"), Decimal("1.0001")],
)
def test_rejects_invalid_static_live_trade_fee(tmp_path: Path, fee: Decimal) -> None:
    configs = _configs(tmp_path)
    economics = struct_replace(
        configs.strategy.economics,
        carry=CarryConfig(total_trade_fee=fee),
    )

    with pytest.raises(ValueError, match="total trade fee must be finite"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, economics=economics),
            )
        )


@pytest.mark.parametrize(
    "carry",
    [
        CarryConfig(bitfinex_long=Decimal("0.0001")),
        CarryConfig(bitfinex_short=Decimal("0.0001")),
        CarryConfig(mt5_long_swap=Decimal("0.0001")),
        CarryConfig(mt5_short_swap=Decimal("0.0001")),
    ],
)
def test_rejects_configured_adapter_derived_live_carry(
    tmp_path: Path,
    carry: CarryConfig,
) -> None:
    configs = _configs(tmp_path)
    economics = struct_replace(configs.strategy.economics, carry=carry)

    with pytest.raises(ValueError, match="adapter-derived carry placeholders must be zero"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, economics=economics),
            )
        )


@pytest.mark.parametrize(
    "fx",
    [
        FxConfig(usd_usdt_bid=Decimal(0), usd_usdt_ask=Decimal(1)),
        FxConfig(usd_usdt_bid=Decimal("NaN"), usd_usdt_ask=Decimal(1)),
        FxConfig(usd_usdt_bid=Decimal(1), usd_usdt_ask=Decimal("Infinity")),
    ],
)
def test_rejects_nonpositive_or_nonfinite_live_fx(tmp_path: Path, fx: FxConfig) -> None:
    configs = _configs(tmp_path)
    economics = struct_replace(configs.strategy.economics, fx=fx)

    with pytest.raises(ValueError, match="FX bid and ask must be finite and positive"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, economics=economics),
            )
        )


def test_rejects_crossed_live_fx(tmp_path: Path) -> None:
    configs = _configs(tmp_path)
    economics = struct_replace(
        configs.strategy.economics,
        fx=FxConfig(usd_usdt_bid=Decimal("1.01"), usd_usdt_ask=Decimal("1.00")),
    )

    with pytest.raises(ValueError, match="FX bid must not exceed ask"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, economics=economics),
            )
        )


def test_build_failure_disposes_the_node_and_owned_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = TradingNode.add_exec_client_factory

    def omit_mt5_factory(node: TradingNode, name: str, factory: object) -> None:
        if name != "MT5":
            original(node, name, factory)  # type: ignore[arg-type]

    monkeypatch.setattr(TradingNode, "add_exec_client_factory", omit_mt5_factory)
    loop = asyncio.new_event_loop()
    with pytest.raises(RuntimeError, match="expected execution client"):
        _build(_configs(tmp_path), loop=loop)

    assert loop.is_closed()
    assert list(tmp_path.iterdir()) == []
