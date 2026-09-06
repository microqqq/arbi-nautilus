"""Live Taker composition; building is offline unless a cache database is supplied."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import cast

from msgspec.structs import replace as struct_replace
from nautilus_trader.config import DatabaseConfig, TradingNodeConfig
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientId, TraderId, Venue

from py000_nautilus.bitfinex_v1_data import (
    BitfinexV1DataClient,
    BitfinexV1DataClientConfig,
    BitfinexV1LiveDataClientFactory,
)
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
    BitfinexV1LiveExecClientFactory,
)
from py000_nautilus.config import TakerStrategyConfig
from py000_nautilus.live_cache import native_cache_config, validate_native_cache
from py000_nautilus.live_runtime import SourceTerminalReconciler, bind_live_account_reader
from py000_nautilus.models import SourceDirection
from py000_nautilus.mt5_v1_data import (
    Mt5V1DataClient,
    Mt5V1DataClientConfig,
    Mt5V1LiveDataClientFactory,
)
from py000_nautilus.mt5_v1_execution import (
    Mt5V1ExecClientConfig,
    Mt5V1ExecutionClient,
    Mt5V1LiveExecClientFactory,
    mt5_v1_execution_account_id,
)
from py000_nautilus.restart_recovery import has_business_history, reconcile_startup
from py000_nautilus.strategies.taker import TakerStrategy

BITFINEX_CLIENT_NAME = "BITFINEX"
MT5_CLIENT_NAME = "MT5"
BITFINEX_CLIENT_ID = ClientId(BITFINEX_CLIENT_NAME)
MT5_CLIENT_ID = ClientId(MT5_CLIENT_NAME)
BITFINEX_VENUE = Venue(BITFINEX_CLIENT_NAME)
MT5_VENUE = Venue(MT5_CLIENT_NAME)
LIVE_TAKER_TRADER_ID = TraderId("PY000-TAKER-LIVE-001")


def build_live_taker_node(
    *,
    bitfinex_data_config: BitfinexV1DataClientConfig,
    bitfinex_exec_config: BitfinexV1ExecClientConfig,
    mt5_data_config: Mt5V1DataClientConfig,
    mt5_exec_config: Mt5V1ExecClientConfig,
    strategy_config: TakerStrategyConfig,
    cache_database: DatabaseConfig | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    connection_timeout_seconds: float = 10.0,
    one_shot: bool = False,
    allowed_source_direction: SourceDirection | None = None,
    one_shot_expected_source_position: Decimal = Decimal(0),
    one_shot_close_existing: bool = False,
) -> tuple[TradingNode, TakerStrategy]:
    """Build without trading connections; an explicit cache database connects on construction."""
    if type(one_shot_close_existing) is not bool:
        raise TypeError("one_shot_close_existing must be a bool")
    if one_shot_close_existing and not one_shot:
        raise ValueError("one_shot_close_existing requires one_shot execution")
    if one_shot and cache_database is not None:
        raise ValueError("one_shot execution cannot use a native cache database")
    _validate_composition(
        bitfinex_data_config,
        bitfinex_exec_config,
        mt5_data_config,
        mt5_exec_config,
        strategy_config,
    )
    if connection_timeout_seconds <= 0:
        raise ValueError("live Taker connection timeout must be positive")

    runtime_bitfinex_exec_config = struct_replace(
        bitfinex_exec_config,
        allow_cold_position_reconciliation=one_shot_close_existing,
    )
    runtime_strategy_config = struct_replace(
        strategy_config,
        external_order_claims=(
            [strategy_config.source_instrument_id] if one_shot_close_existing else None
        ),
    )

    node = TradingNode(
        config=TradingNodeConfig(
            trader_id=LIVE_TAKER_TRADER_ID,
            cache=native_cache_config(cache_database),
            data_clients={
                BITFINEX_CLIENT_NAME: bitfinex_data_config,
                MT5_CLIENT_NAME: mt5_data_config,
            },
            exec_clients={
                BITFINEX_CLIENT_NAME: runtime_bitfinex_exec_config,
                MT5_CLIENT_NAME: mt5_exec_config,
            },
            exec_engine=LiveExecEngineConfig(
                reconciliation=True,
                reconciliation_lookback_mins=None,
                reconciliation_instrument_ids=(
                    [strategy_config.source_instrument_id, strategy_config.hedge_instrument_id]
                    if one_shot_close_existing
                    else None
                ),
                # A close canary starts with venue positions and a deliberately empty
                # local cache. Let Nautilus materialize only those two reconciled
                # positions as internal events; this never submits a venue order.
                generate_missing_orders=one_shot_close_existing,
                inflight_check_interval_ms=0,
                open_check_interval_secs=None,
                position_check_interval_secs=None,
            ),
            timeout_connection=connection_timeout_seconds,
            timeout_reconciliation=connection_timeout_seconds,
            timeout_portfolio=connection_timeout_seconds,
            timeout_disconnection=connection_timeout_seconds,
            timeout_post_stop=0.1,
            timeout_shutdown=connection_timeout_seconds,
        ),
        loop=loop,
    )
    try:
        node.add_data_client_factory(BITFINEX_CLIENT_NAME, BitfinexV1LiveDataClientFactory)
        node.add_data_client_factory(MT5_CLIENT_NAME, Mt5V1LiveDataClientFactory)
        node.add_exec_client_factory(BITFINEX_CLIENT_NAME, BitfinexV1LiveExecClientFactory)
        node.add_exec_client_factory(MT5_CLIENT_NAME, Mt5V1LiveExecClientFactory)
        node.build()

        bitfinex_data = _data_client(node, BITFINEX_VENUE, BitfinexV1DataClient)
        mt5_data = _data_client(node, MT5_VENUE, Mt5V1DataClient)
        bitfinex_exec = _exec_client(node, BITFINEX_CLIENT_ID, BitfinexV1ExecutionClient)
        mt5_exec = _exec_client(node, MT5_CLIENT_ID, Mt5V1ExecutionClient)
        reconciler = SourceTerminalReconciler(
            source_client=bitfinex_exec,
            exec_engine=node.kernel.exec_engine,
            source_instrument_id=strategy_config.source_instrument_id,
            timeout_seconds=connection_timeout_seconds,
        )
        node.trader.add_actor(reconciler)
        strategy = TakerStrategy(
            runtime_strategy_config,
            live_submission_ready=lambda: (
                not reconciler.restart_pending
                and bitfinex_data.is_connected
                and bitfinex_data.book_is_actionable
                and bitfinex_exec.execution_hold_reason is None
                and (one_shot_close_existing or bitfinex_exec.accounting_ready)
                and reconciler.source_submission_ready
                and bitfinex_exec.get_account() is not None
                and mt5_data.is_connected
                and mt5_data.snapshot_refresh_healthy
                and mt5_exec.execution_admitted
                and mt5_exec.get_account() is not None
                and (
                    not one_shot
                    or _one_shot_accounts_match(
                        node,
                        runtime_strategy_config,
                        mt5_exec,
                        expected_source_position=one_shot_expected_source_position,
                    )
                )
            ),
            hedge_quantity_ready=mt5_exec.can_execute_quantity,
            live_costs_from_adapters=True,
            source_terminal_query=reconciler.query_source_terminal,
            one_shot=one_shot,
            allowed_source_direction=allowed_source_direction,
            hedge_must_reduce_only=one_shot_close_existing,
        )
        node.trader.add_strategy(strategy)
        strategy.bind_restart_gate(lambda: reconciler.restart_pending)
        native_history = False
        if cache_database is not None:
            native_history = validate_native_cache(
                node.cache,
                trader_id=LIVE_TAKER_TRADER_ID,
                strategy_id=strategy.id,
                routes={
                    strategy_config.source_instrument_id: (
                        strategy_config.source_accounts[0].account_id, BITFINEX_CLIENT_ID,
                    ),
                    strategy_config.hedge_instrument_id: (
                        strategy_config.hedge_account_id, MT5_CLIENT_ID,
                    ),
                },
            )
        if not one_shot and (
            native_history or node.cache.orders() or node.cache.positions()
            or bitfinex_exec._cid_store.bindings
            or has_business_history(strategy.state_store)
        ):
            async def recover_startup() -> None:
                await reconcile_startup(
                    node.cache, strategy.state_store,
                    trader_id=LIVE_TAKER_TRADER_ID, strategy_id=strategy.id,
                    source=bitfinex_exec, hedge=mt5_exec,
                    source_instrument_id=strategy_config.source_instrument_id,
                    hedge_instrument_id=strategy_config.hedge_instrument_id,
                )

            reconciler.bind_restart_recovery(recover_startup)
            node.kernel.logger.warning("native or business history; restart reconciliation pending")
        bind_live_account_reader(
            strategy, config=runtime_strategy_config,
            source_data=bitfinex_data, source_client=bitfinex_exec,
            hedge_data=mt5_data, hedge_client=mt5_exec,
            wallet_currency=runtime_bitfinex_exec_config.wallet_currency,
            hedge_symbol=mt5_exec_config.expected_symbol,
            hedge_stream_id=mt5_exec_config.expected_stream_id,
        )
        _verify_built_composition(
            node,
            strategy,
            bitfinex_data,
            bitfinex_exec,
            mt5_data,
            mt5_exec,
            one_shot_close_existing=one_shot_close_existing,
        )
    except BaseException:
        _dispose(node)
        raise
    return node, strategy


def _one_shot_accounts_are_flat(
    node: TradingNode,
    strategy: TakerStrategyConfig,
    mt5_exec: Mt5V1ExecutionClient,
) -> bool:
    """Recheck the reconciled two-leg boundary in the same callback as source admission."""
    return _one_shot_accounts_match(
        node,
        strategy,
        mt5_exec,
        expected_source_position=Decimal(0),
    )


def _one_shot_accounts_match(
    node: TradingNode,
    strategy: TakerStrategyConfig,
    mt5_exec: Mt5V1ExecutionClient,
    *,
    expected_source_position: Decimal,
) -> bool:
    """Require the exact reconciled starting shape for one open or close attempt."""
    if type(expected_source_position) is not Decimal or not expected_source_position.is_finite():
        return False
    source_account = strategy.source_accounts[0].account_id
    if mt5_exec.pending_client_order_ids:
        return False
    for instrument_id, account_id, expected in (
        (strategy.source_instrument_id, source_account, expected_source_position),
        (
            strategy.hedge_instrument_id,
            strategy.hedge_account_id,
            -expected_source_position,
        ),
    ):
        if node.cache.orders_open(instrument_id=instrument_id, account_id=account_id):
            return False
        positions = node.cache.positions_open(
            instrument_id=instrument_id,
            account_id=account_id,
        )
        expected_count = 0 if expected == 0 else 1
        if len(positions) != expected_count:
            return False
        signed = [position.signed_decimal_qty() for position in positions]
        if (
            sum(signed, Decimal()) != expected
            or sum((abs(value) for value in signed), Decimal()) != abs(expected)
            or node.portfolio.net_position(instrument_id, account_id) != expected
        ):
            return False
    return True


def _validate_composition(
    bitfinex_data: BitfinexV1DataClientConfig,
    bitfinex_exec: BitfinexV1ExecClientConfig,
    mt5_data: Mt5V1DataClientConfig,
    mt5_exec: Mt5V1ExecClientConfig,
    strategy: TakerStrategyConfig,
) -> None:
    for label, config, client_name in (
        ("Bitfinex data", bitfinex_data, BITFINEX_CLIENT_NAME),
        ("Bitfinex execution", bitfinex_exec, BITFINEX_CLIENT_NAME),
        ("MT5 data", mt5_data, MT5_CLIENT_NAME),
        ("MT5 execution", mt5_exec, MT5_CLIENT_NAME),
    ):
        routing = config.routing
        if routing.default or routing.venues != frozenset({client_name}):
            raise ValueError(f"{label} routing must be non-default and restricted to {client_name}")

    if (
        bitfinex_data.instrument_id != bitfinex_exec.instrument_id
        or bitfinex_data.raw_symbol != bitfinex_exec.raw_symbol
    ):
        raise ValueError("Bitfinex data and execution profiles must identify the same market")

    mt5_shared_fields = (
        "pub_url",
        "rep_url",
        "instrument_id",
        "expected_account_id",
        "expected_symbol",
        "expected_magic",
        "expected_ea_build_id",
        "expected_source_sha256",
        "expected_server_timezone",
    )
    if any(getattr(mt5_data, field) != getattr(mt5_exec, field) for field in mt5_shared_fields):
        raise ValueError("MT5 data and execution profiles must have the same bound identity")
    if not mt5_data.expected_execution_enabled:
        raise ValueError("live Taker MT5 data must require execution_enabled=true")

    if strategy.source_instrument_id != bitfinex_exec.instrument_id:
        raise ValueError("Taker source instrument must match the Bitfinex profile")
    if strategy.hedge_instrument_id != mt5_exec.instrument_id:
        raise ValueError("Taker hedge instrument must match the MT5 profile")
    if len(strategy.source_accounts) != 1:
        raise ValueError("live Taker requires exactly one Bitfinex source account route")
    source_route = strategy.source_accounts[0]
    if (
        source_route.account_id != bitfinex_exec.account_id
        or source_route.client_id != BITFINEX_CLIENT_ID
    ):
        raise ValueError("Taker source account must explicitly route to the Bitfinex client")
    expected_hedge_account = mt5_v1_execution_account_id(
        MT5_CLIENT_ID,
        mt5_exec.expected_account_id,
    )
    if (
        strategy.hedge_account_id != expected_hedge_account
        or strategy.hedge_client_id != MT5_CLIENT_ID
    ):
        raise ValueError("Taker hedge account must explicitly route to the MT5 client")
    if strategy.initial_hedge_session_open or strategy.initial_session_ts_ns != 0:
        raise ValueError("live Taker session must begin closed and without a synthetic timestamp")
    if strategy.initial_cost_ts_ns != 0:
        raise ValueError("live Taker costs must begin stale until an explicit runtime refresh")
    if strategy.external_order_claims:
        raise ValueError("live Taker external order claims are controlled by close mode")

    carry = strategy.economics.carry
    total_trade_fee = carry.total_trade_fee
    if (
        not isinstance(total_trade_fee, Decimal)
        or not total_trade_fee.is_finite()
        or not Decimal(0) <= total_trade_fee <= Decimal(1)
    ):
        raise ValueError("live Taker total trade fee must be finite and in the range 0..1")
    if any(
        value != 0
        for value in (
            carry.bitfinex_long,
            carry.bitfinex_short,
            carry.mt5_long_swap,
            carry.mt5_short_swap,
        )
    ):
        raise ValueError("live Taker adapter-derived carry placeholders must be zero")

    fx = strategy.economics.fx
    if any(
        not isinstance(value, Decimal) or not value.is_finite() or value <= 0
        for value in (fx.usd_usdt_bid, fx.usd_usdt_ask)
    ):
        raise ValueError("live Taker FX bid and ask must be finite and positive")
    if fx.usd_usdt_bid > fx.usd_usdt_ask:
        raise ValueError("live Taker FX bid must not exceed ask")

    cid_path = _required_path(bitfinex_exec.cid_store_path, "Bitfinex CID store")
    state_path = _required_path(strategy.store_path, "Taker state store")
    if cid_path == state_path:
        raise ValueError("Bitfinex CID and Taker state stores must use distinct paths")


def _required_path(value: str, label: str) -> Path:
    if not value or value != value.strip():
        raise ValueError(f"{label} path must be a non-empty trimmed path")
    return Path(value).resolve(strict=False)


def _data_client(
    node: TradingNode,
    venue: Venue,
    expected_type: type[BitfinexV1DataClient] | type[Mt5V1DataClient],
) -> BitfinexV1DataClient | Mt5V1DataClient:
    client = node.kernel.data_engine.routing_map.get(venue)
    if not isinstance(client, expected_type):
        raise RuntimeError("live Taker node did not build the expected data client")
    return cast(BitfinexV1DataClient | Mt5V1DataClient, client)


def _exec_client(
    node: TradingNode,
    client_id: ClientId,
    expected_type: type[BitfinexV1ExecutionClient] | type[Mt5V1ExecutionClient],
) -> BitfinexV1ExecutionClient | Mt5V1ExecutionClient:
    clients = cast(dict[ClientId, LiveExecutionClient], node.kernel.exec_engine._clients)
    client = clients.get(client_id)
    if not isinstance(client, expected_type):
        raise RuntimeError("live Taker node did not build the expected execution client")
    return cast(BitfinexV1ExecutionClient | Mt5V1ExecutionClient, client)


def _verify_built_composition(
    node: TradingNode,
    strategy: TakerStrategy,
    bitfinex_data: BitfinexV1DataClient,
    bitfinex_exec: BitfinexV1ExecutionClient,
    mt5_data: Mt5V1DataClient,
    mt5_exec: Mt5V1ExecutionClient,
    *,
    one_shot_close_existing: bool,
) -> None:
    data_engine = node.kernel.data_engine
    exec_engine = node.kernel.exec_engine
    if (
        not node.is_built()
        or node.is_running()
        or data_engine.registered_clients != [BITFINEX_CLIENT_ID, MT5_CLIENT_ID]
        or data_engine.default_client is not None
        or set(data_engine.routing_map) != {BITFINEX_VENUE, MT5_VENUE}
        or data_engine.routing_map[BITFINEX_VENUE] is not bitfinex_data
        or data_engine.routing_map[MT5_VENUE] is not mt5_data
        or bitfinex_data.is_connected
        or mt5_data.is_connected
        or exec_engine.registered_clients != [BITFINEX_CLIENT_ID, MT5_CLIENT_ID]
        or exec_engine.default_client is not None
        or _exec_client(node, BITFINEX_CLIENT_ID, BitfinexV1ExecutionClient) is not bitfinex_exec
        or _exec_client(node, MT5_CLIENT_ID, Mt5V1ExecutionClient) is not mt5_exec
        or bitfinex_exec.is_connected
        or mt5_exec.is_connected
        or bitfinex_exec._bfx_config.allow_cold_position_reconciliation
        is not one_shot_close_existing
        or not exec_engine.reconciliation
        or exec_engine.generate_missing_orders is not one_shot_close_existing
        or exec_engine.reconciliation_instrument_ids
        != (
            [strategy.config.source_instrument_id, strategy.config.hedge_instrument_id]
            if one_shot_close_existing
            else []
        )
        or exec_engine.inflight_check_interval_ms != 0
        or exec_engine.open_check_interval_secs is not None
        or exec_engine.position_check_interval_secs is not None
        or strategy.config.external_order_claims
        != ([strategy.config.source_instrument_id] if one_shot_close_existing else None)
        or node.trader.strategies() != [strategy]
    ):
        raise RuntimeError("live Taker node did not build the exact offline composition")


def _dispose(node: TradingNode) -> None:
    try:
        if not node.kernel.loop.is_closed():
            node.kernel.cancel_all_tasks()
    finally:
        node.dispose()
