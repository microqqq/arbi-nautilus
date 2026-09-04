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
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId, Venue

from py000_nautilus.bitfinex_v1_data import (
    RAW_SYMBOL,
    BitfinexV1DataClient,
    BitfinexV1DataClientConfig,
)
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
)
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport
from py000_nautilus.config import (
    CarryConfig,
    FxConfig,
    HedgeAccountRoute,
    MakerEconomicsConfig,
    MakerSideConfig,
    MakerStrategyConfig,
    RiskConfig,
    SourceAccountRoute,
)
from py000_nautilus.live_maker import build_live_maker_node
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
    strategy: MakerStrategyConfig


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
    strategy = MakerStrategyConfig(
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
        hedge_accounts=(
            HedgeAccountRoute(
                account_id=AccountId("MT5-12345678"),
                max_long_ounces=Decimal(10),
                max_short_ounces=Decimal(10),
                client_id=MT5_ID,
            ),
        ),
        economics=MakerEconomicsConfig(
            bid=MakerSideConfig(
                open_quantity_ounces=Decimal(1),
                open_spread=Decimal("0.001"),
                delta=Decimal("0.0001"),
            ),
            ask=MakerSideConfig(
                open_quantity_ounces=Decimal(1),
                open_spread=Decimal("0.001"),
                delta=Decimal("0.0001"),
            ),
            margin_level=Decimal(500),
            carry=CarryConfig(total_trade_fee=Decimal("0.0002")),
            fx=FxConfig(),
            risk=RiskConfig(source_max_abs=Decimal(10), hedge_max_abs=Decimal(10)),
        ),
        store_path_prefix=str(tmp_path / "maker-state"),
        initial_hedge_session_open=False,
        initial_session_ts_ns=0,
        initial_cost_ts_ns=0,
    )
    return _Configs(bitfinex_data, bitfinex_exec, mt5_data, mt5_exec, strategy)


def _build(configs: _Configs, *, loop: asyncio.AbstractEventLoop | None = None) -> Any:
    return build_live_maker_node(
        bitfinex_data_config=configs.bitfinex_data,
        bitfinex_exec_config=configs.bitfinex_exec,
        mt5_data_config=configs.mt5_data,
        mt5_exec_config=configs.mt5_exec,
        strategy_config=configs.strategy,
        loop=loop,
        connection_timeout_seconds=3.0,
    )


def test_builds_exact_offline_maker_composition_without_creating_state(
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
        assert bitfinex_exec._bfx_config.allow_cold_position_reconciliation is False
        assert (
            cast(Any, mt5_data._transport).rep_coordinator
            is cast(Any, mt5_exec._transport).rep_coordinator
        )
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
            readiness_patch.setattr(Mt5V1ExecutionClient, "get_account", lambda _client: object())

            mt5_data._snapshot_refresh_healthy = False
            assert readiness() is False
            mt5_data._snapshot_refresh_healthy = True
            assert readiness() is True

        hedge_quantity_ready = cast(Any, strategy)._hedge_quantity_ready
        assert hedge_quantity_ready.__self__ is mt5_exec
        assert hedge_quantity_ready.__func__ is Mt5V1ExecutionClient.can_execute_quantity
        assert cast(Any, strategy)._live_costs_from_adapters is True
        assert not Path(configs.bitfinex_exec.cid_store_path).exists()
        assert not Path(f"{configs.strategy.store_path_prefix}.bid.json").exists()
        assert not Path(f"{configs.strategy.store_path_prefix}.ask.json").exists()
    finally:
        node.dispose()

    assert loop.is_closed()
    assert list(tmp_path.iterdir()) == []


def test_rejects_wrong_identities_routes_and_synthetic_live_inputs(tmp_path: Path) -> None:
    configs = _configs(tmp_path)

    with pytest.raises(ValueError, match="Bitfinex data and execution profiles"):
        _build(
            dataclass_replace(
                configs,
                bitfinex_data=struct_replace(configs.bitfinex_data, raw_symbol="DIFFERENT"),
            )
        )
    with pytest.raises(ValueError, match="same bound identity"):
        _build(
            dataclass_replace(
                configs,
                mt5_data=struct_replace(configs.mt5_data, expected_magic="DIFFERENT"),
            )
        )
    with pytest.raises(ValueError, match="explicitly route to the Bitfinex"):
        source_route = struct_replace(configs.strategy.source_accounts[0], client_id=None)
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, source_accounts=(source_route,)),
            )
        )
    with pytest.raises(ValueError, match="explicitly route to the MT5"):
        hedge_route = struct_replace(configs.strategy.hedge_accounts[0], client_id=None)
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, hedge_accounts=(hedge_route,)),
            )
        )
    with pytest.raises(ValueError, match="session must begin closed"):
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
    with pytest.raises(ValueError, match="price-only requotes"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, fixed_amount=True),
            )
        )
    with pytest.raises(ValueError, match="does not allow cold Bitfinex"):
        _build(
            dataclass_replace(
                configs,
                bitfinex_exec=struct_replace(
                    configs.bitfinex_exec,
                    allow_cold_position_reconciliation=True,
                ),
            )
        )


def test_rejects_colliding_cid_and_maker_state_paths(tmp_path: Path) -> None:
    configs = _configs(tmp_path)
    colliding_prefix = str(tmp_path / "maker-state")
    execution = struct_replace(
        configs.bitfinex_exec,
        cid_store_path=f"{colliding_prefix}.bid.json",
    )

    with pytest.raises(ValueError, match="distinct paths"):
        _build(dataclass_replace(configs, bitfinex_exec=execution))


@pytest.mark.parametrize("prefix", ["", "   "])
def test_rejects_empty_or_untrimmed_maker_state_prefix(tmp_path: Path, prefix: str) -> None:
    configs = _configs(tmp_path)

    with pytest.raises(ValueError, match="Maker state store prefix"):
        _build(
            dataclass_replace(
                configs,
                strategy=struct_replace(configs.strategy, store_path_prefix=prefix),
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
