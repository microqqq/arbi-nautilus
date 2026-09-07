"""The shared four-client native node; strategies and business recovery stay outside."""

import asyncio
import math

from nautilus_trader.config import DatabaseConfig, TradingNodeConfig
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.model.identifiers import InstrumentId, TraderId

from py000_nautilus.bitfinex_v1_data import (
    BitfinexV1DataClientConfig,
    BitfinexV1LiveDataClientFactory,
)
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1LiveExecClientFactory,
)
from py000_nautilus.live_cache import native_cache_config
from py000_nautilus.live_lifecycle import DrainingTradingNode
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig, Mt5V1LiveDataClientFactory
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig, Mt5V1LiveExecClientFactory


def build_execution_node(
    *, trader_id: TraderId,
    bitfinex_data_config: BitfinexV1DataClientConfig,
    bitfinex_exec_config: BitfinexV1ExecClientConfig,
    mt5_data_config: Mt5V1DataClientConfig,
    mt5_exec_config: Mt5V1ExecClientConfig,
    cache_database: DatabaseConfig | None,
    loop: asyncio.AbstractEventLoop | None,
    connection_timeout_seconds: float,
    cold_reconciliation_instruments: list[InstrumentId] | None = None,
) -> DrainingTradingNode:
    if (isinstance(connection_timeout_seconds, bool)
            or not math.isfinite(connection_timeout_seconds)
            or connection_timeout_seconds <= 0):
        raise ValueError("live connection timeout must be finite and positive")
    node = DrainingTradingNode(config=TradingNodeConfig(
        trader_id=trader_id, cache=native_cache_config(cache_database),
        data_clients={"BITFINEX": bitfinex_data_config, "MT5": mt5_data_config},
        exec_clients={"BITFINEX": bitfinex_exec_config, "MT5": mt5_exec_config},
        exec_engine=LiveExecEngineConfig(
            reconciliation=True, reconciliation_lookback_mins=None,
            reconciliation_instrument_ids=cold_reconciliation_instruments,
            generate_missing_orders=cold_reconciliation_instruments is not None,
            inflight_check_interval_ms=0, open_check_interval_secs=None,
            position_check_interval_secs=None,
        ),
        timeout_connection=connection_timeout_seconds,
        timeout_reconciliation=connection_timeout_seconds,
        timeout_portfolio=connection_timeout_seconds,
        timeout_disconnection=connection_timeout_seconds,
        timeout_post_stop=0.1, timeout_shutdown=connection_timeout_seconds,
    ), loop=loop)
    try:
        node.add_data_client_factory("BITFINEX", BitfinexV1LiveDataClientFactory)
        node.add_data_client_factory("MT5", Mt5V1LiveDataClientFactory)
        node.add_exec_client_factory("BITFINEX", BitfinexV1LiveExecClientFactory)
        node.add_exec_client_factory("MT5", Mt5V1LiveExecClientFactory)
        node.build()
    except BaseException:
        try:
            if not node.kernel.loop.is_closed():
                node.kernel.cancel_all_tasks()
        finally:
            node.dispose()
        raise
    return node
