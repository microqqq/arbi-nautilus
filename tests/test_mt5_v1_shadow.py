from __future__ import annotations

import asyncio

import pytest
from nautilus_trader.config import RoutingConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Venue

from py000_nautilus.mt5_v1_data import Mt5V1DataClient, Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_shadow import build_mt5_v1_shadow_node
from py000_nautilus.mt5_v1_transport import Mt5V1Transport


def _config(
    *,
    routing: RoutingConfig,
    instrument_id: InstrumentId | None = None,
) -> Mt5V1DataClientConfig:
    return Mt5V1DataClientConfig(
        pub_url="tcp://127.0.0.1:6101",
        rep_url="tcp://127.0.0.1:6102",
        instrument_id=instrument_id or InstrumentId.from_str("XAUUSD.MT5"),
        expected_account_id="synthetic-account",
        expected_symbol="XAUUSD",
        expected_magic="900000001",
        expected_ea_build_id="py000-mt5-ea-v1-readonly",
        expected_source_sha256="a" * 64,
        routing=routing,
    )


def test_build_registers_only_read_only_mt5_data_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_open(_transport: Mt5V1Transport) -> None:
        raise AssertionError("TradingNode.build must not open the MT5 transport")

    monkeypatch.setattr(Mt5V1Transport, "open", unexpected_open)
    loop = asyncio.new_event_loop()
    node = build_mt5_v1_shadow_node(
        _config(routing=RoutingConfig(default=False, venues=frozenset({"MT5"}))),
        loop=loop,
    )
    try:
        data_engine = node.kernel.data_engine
        client = data_engine.routing_map[Venue("MT5")]

        assert node.is_built()
        assert not node.is_running()
        assert data_engine.registered_clients == [ClientId("MT5")]
        assert data_engine.default_client is None
        assert isinstance(client, Mt5V1DataClient)
        assert not client.is_connected
        assert node.kernel.exec_engine.registered_clients == []
        assert node.kernel.exec_engine.reconciliation is False
        assert node.trader.strategies() == []
    finally:
        node.dispose()

    assert loop.is_closed()


@pytest.mark.parametrize(
    "routing",
    [
        RoutingConfig(),
        RoutingConfig(default=True, venues=frozenset({"MT5"})),
        RoutingConfig(default=False, venues=frozenset({"BITFINEX"})),
    ],
)
def test_build_rejects_implicit_or_broad_routing(routing: RoutingConfig) -> None:
    with pytest.raises(ValueError, match="non-default and restricted to MT5"):
        build_mt5_v1_shadow_node(_config(routing=routing))


def test_build_failure_disposes_its_owned_loop() -> None:
    loop = asyncio.new_event_loop()
    with pytest.raises(ValueError, match="instrument venue must be MT5"):
        build_mt5_v1_shadow_node(
            _config(
                routing=RoutingConfig(default=False, venues=frozenset({"MT5"})),
                instrument_id=InstrumentId.from_str("XAUUSD.OTHER"),
            ),
            loop=loop,
        )

    assert loop.is_closed()


def test_build_rejects_nautilus_silent_client_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TradingNode, "add_data_client_factory", lambda *_args: None)
    loop = asyncio.new_event_loop()
    with pytest.raises(RuntimeError, match="exact read-only composition"):
        build_mt5_v1_shadow_node(
            _config(routing=RoutingConfig(default=False, venues=frozenset({"MT5"}))),
            loop=loop,
        )

    assert loop.is_closed()
