"""Offline-buildable Nautilus composition for the read-only MT5 v1 client."""

from __future__ import annotations

import asyncio

from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientId, TraderId, Venue

from py000_nautilus.mt5_v1_data import (
    Mt5V1DataClient,
    Mt5V1DataClientConfig,
    Mt5V1LiveDataClientFactory,
)

MT5_CLIENT_NAME = "MT5"
MT5_CLIENT_ID = ClientId(MT5_CLIENT_NAME)
MT5_VENUE = Venue(MT5_CLIENT_NAME)
MT5_SHADOW_TRADER_ID = TraderId("PY000-SHADOW-001")


def build_mt5_v1_shadow_node(
    data_config: Mt5V1DataClientConfig,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    connection_timeout_seconds: float = 10.0,
) -> TradingNode:
    """Build one MT5-only data node without connecting or granting order authority.

    The node owns the supplied event loop and closes it when disposed.
    """
    routing = data_config.routing
    if routing.default or routing.venues != frozenset({MT5_CLIENT_NAME}):
        raise ValueError("MT5 shadow routing must be non-default and restricted to MT5")
    if connection_timeout_seconds <= 0:
        raise ValueError("MT5 shadow connection timeout must be positive")

    node = TradingNode(
        config=TradingNodeConfig(
            trader_id=MT5_SHADOW_TRADER_ID,
            data_clients={MT5_CLIENT_NAME: data_config},
            exec_clients={},
            exec_engine=LiveExecEngineConfig(reconciliation=False),
            timeout_connection=connection_timeout_seconds,
            timeout_disconnection=5.0,
            timeout_post_stop=0.1,
            timeout_shutdown=5.0,
        ),
        loop=loop,
    )
    try:
        node.add_data_client_factory(MT5_CLIENT_NAME, Mt5V1LiveDataClientFactory)
        node.build()
        data_engine = node.kernel.data_engine
        client = data_engine.routing_map.get(MT5_VENUE)
        if (
            data_engine.registered_clients != [MT5_CLIENT_ID]
            or data_engine.default_client is not None
            or not isinstance(client, Mt5V1DataClient)
            or client.is_connected
            or node.kernel.exec_engine.registered_clients
            or node.trader.strategies()
        ):
            raise RuntimeError("MT5 shadow node did not build the exact read-only composition")
    except BaseException:
        try:
            if not node.kernel.loop.is_closed():
                node.kernel.cancel_all_tasks()
        finally:
            node.dispose()
        raise
    return node
