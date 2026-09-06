"""One native node and one route owner for the ordinary Maker and Taker."""

import asyncio
from decimal import Decimal
from math import isclose
from pathlib import Path
from typing import cast

from msgspec.structs import replace
from nautilus_trader.config import DatabaseConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.orders import Order

from py000_nautilus.bitfinex_v1_data import BitfinexV1DataClient, BitfinexV1DataClientConfig
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecClientConfig,
    BitfinexV1ExecutionClient,
)
from py000_nautilus.config import MakerStrategyConfig, TakerStrategyConfig
from py000_nautilus.live_cache import validate_native_cache
from py000_nautilus.live_lifecycle import validate_stop_timeout
from py000_nautilus.live_maker import (
    BITFINEX_CLIENT_ID,
    BITFINEX_VENUE,
    MT5_CLIENT_ID,
    MT5_VENUE,
    _data_client,
    _dispose,
    _exec_client,
    _required_path,
    _verify_built_composition,
)
from py000_nautilus.live_maker import (
    _validate_composition as validate_maker,
)
from py000_nautilus.live_node import build_execution_node
from py000_nautilus.live_runtime import SourceTerminalReconciler, bind_live_account_reader
from py000_nautilus.live_taker import _validate_composition as validate_taker
from py000_nautilus.maker_store import (
    MakerStateStore,
    maker_legacy_paths,
    maker_state_path,
    shared_state_path,
)
from py000_nautilus.models import HedgeIntent
from py000_nautilus.mt5_v1_data import Mt5V1DataClient, Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig, Mt5V1ExecutionClient
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.restart_recovery import (
    StartupRecoveryOptions,
    capture_startup_receipt,
    check_rejected_retry_execution,
    has_business_history,
    reconcile_startup,
    shared_business_owners,
)
from py000_nautilus.shared_admission import SharedSourceAdmission
from py000_nautilus.strategies.maker import MakerStrategy
from py000_nautilus.strategies.taker import TakerStrategy

LIVE_BOTH_TRADER_ID = TraderId("PY000-BOTH-LIVE-001")


def validate_shared_policy(maker: MakerStrategyConfig, taker: TakerStrategyConfig) -> None:
    if len(maker.hedge_accounts) != 1:
        raise ValueError("both requires exactly one shared hedge route")
    route = maker.hedge_accounts[0]
    if (maker.source_instrument_id != taker.source_instrument_id
            or maker.hedge_instrument_id != taker.hedge_instrument_id
            or len(maker.source_accounts) != 1 or maker.source_accounts != taker.source_accounts
            or (route.account_id, route.client_id, route.max_long_ounces, route.max_short_ounces)
            != (taker.hedge_account_id, taker.hedge_client_id,
                taker.hedge_max_long_ounces, taker.hedge_max_short_ounces)
            or any(getattr(maker.economics, field) != getattr(taker.economics, field)
                   for field in ("risk", "margin_level", "fx"))
            or maker.external_order_claims or taker.external_order_claims):
        raise ValueError("both strategies require one explicit shared route, risk, margin and FX")
    shared_strategy_ids(maker, taker)


def shared_strategy_ids(maker: MakerStrategyConfig, taker: TakerStrategyConfig) -> tuple[str, str]:
    maker_tag, taker_tag = maker.order_id_tag or "M", taker.order_id_tag or "T"
    ids = (f"{maker.strategy_id or 'MakerStrategy'}-{maker_tag}",
           f"{taker.strategy_id or 'TakerStrategy'}-{taker_tag}")
    if ids[0] == ids[1] or maker_tag == taker_tag:
        raise ValueError("both requires distinct StrategyId and order ID tags")
    return ids


def _route_positions_match(
    node: TradingNode, source: BitfinexV1ExecutionClient, hedge: Mt5V1ExecutionClient,
) -> bool:
    """Read only after both current account samples are qualified and no hedge is in flight."""
    source_net = sum((position.signed_decimal_qty() for position in node.cache.positions_open(
        instrument_id=source._bfx_config.instrument_id, account_id=source.account_id,
    )), Decimal(0))
    current = source._margin_position
    if source_net != (Decimal(0) if current is None else current.quantity):
        return False
    snapshot = hedge._require_snapshot()
    contract = Decimal(cast(str, cast(JsonObject, snapshot["symbol_spec"])["contract_size"]))
    tickets = {
        str(row["identifier"]): (
            Decimal(cast(str, row["volume_lots"])) * contract * (1 if row["side"] == "buy" else -1),
            Decimal(cast(str, row["price_open"])),
        ) for row in cast(list[JsonObject], snapshot["positions"])
    }
    positions = {str(position.id): position for position in node.cache.positions_open(
        instrument_id=hedge._mt5_config.instrument_id, account_id=hedge.account_id,
    )}
    return tickets.keys() == positions.keys() and all(
        qty == positions[pid].signed_decimal_qty()
        and isclose(float(price), positions[pid].avg_px_open)
        for pid, (qty, price) in tickets.items()
    )


def build_live_both_node(
    *, bitfinex_data_config: BitfinexV1DataClientConfig,
    bitfinex_exec_config: BitfinexV1ExecClientConfig,
    mt5_data_config: Mt5V1DataClientConfig, mt5_exec_config: Mt5V1ExecClientConfig,
    maker_config: MakerStrategyConfig, taker_config: TakerStrategyConfig,
    shared_store_prefix: str,
    cache_database: DatabaseConfig | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    connection_timeout_seconds: float = 10.0, stop_timeout_seconds: float = 10.0,
    startup_recovery: StartupRecoveryOptions | None = None,
) -> tuple[TradingNode, tuple[MakerStrategy, TakerStrategy]]:
    validate_stop_timeout(stop_timeout_seconds)
    validate_maker(bitfinex_data_config, bitfinex_exec_config,
                   mt5_data_config, mt5_exec_config, maker_config)
    validate_taker(bitfinex_data_config, bitfinex_exec_config,
                   mt5_data_config, mt5_exec_config, taker_config)
    validate_shared_policy(maker_config, taker_config)
    route = maker_config.hedge_accounts[0]
    prefix = _required_path(shared_store_prefix, "shared state prefix")
    path = shared_state_path(prefix).resolve(strict=False)
    if path == Path(bitfinex_exec_config.cid_store_path).resolve(strict=False):
        raise ValueError("shared state and Bitfinex CID stores must be distinct")
    old_paths = (Path(taker_config.store_path), maker_state_path(maker_config.store_path_prefix),
                 *maker_legacy_paths(maker_config.store_path_prefix))
    if any(old.resolve(strict=False) != path and old.exists() for old in old_paths):
        raise ValueError("both does not import or ignore existing standalone strategy state")
    maker_config = replace(maker_config, store_path_prefix=str(prefix),
                           order_id_tag=maker_config.order_id_tag or "M")
    taker_config = replace(taker_config, store_path=str(path),
                           order_id_tag=taker_config.order_id_tag or "T")
    ids = shared_strategy_ids(maker_config, taker_config)
    source_route = maker_config.source_accounts[0]
    carry_route = None
    if maker_config.residual_mode == "bounded-carry":
        carry_route = (str(source_route.account_id), str(BITFINEX_CLIENT_ID),
                       str(route.account_id), str(MT5_CLIENT_ID))
    owner = MakerStateStore(
        prefix, str(maker_config.source_instrument_id), str(maker_config.hedge_instrument_id),
        residual_limit_ounces=maker_config.residual_limit_ounces, carry_route=carry_route,
        shared_strategy_ids=ids,
    )
    if owner.taker_store is None:
        raise RuntimeError("shared state owner did not construct its Taker view")
    node = build_execution_node(
        trader_id=LIVE_BOTH_TRADER_ID, bitfinex_data_config=bitfinex_data_config,
        bitfinex_exec_config=bitfinex_exec_config, mt5_data_config=mt5_data_config,
        mt5_exec_config=mt5_exec_config, cache_database=cache_database,
        loop=loop, connection_timeout_seconds=connection_timeout_seconds,
    )
    try:
        source_data = cast(BitfinexV1DataClient,
                           _data_client(node, BITFINEX_VENUE, BitfinexV1DataClient))
        hedge_data = cast(Mt5V1DataClient, _data_client(node, MT5_VENUE, Mt5V1DataClient))
        source = cast(BitfinexV1ExecutionClient,
                      _exec_client(node, BITFINEX_CLIENT_ID, BitfinexV1ExecutionClient))
        hedge = cast(Mt5V1ExecutionClient, _exec_client(node, MT5_CLIENT_ID, Mt5V1ExecutionClient))
        reconciler = SourceTerminalReconciler(
            source_client=source, exec_engine=node.kernel.exec_engine,
            source_instrument_id=maker_config.source_instrument_id,
            timeout_seconds=connection_timeout_seconds,
        )
        node.trader.add_actor(reconciler)
        admission = SharedSourceAdmission(node.cache, owner, maker_config)
        admission.positions_ready = lambda: _route_positions_match(node, source, hedge)
        admission.on_position_mismatch = reconciler.request_reconciliation

        def ready(*, maker: bool = False) -> bool:
            return (
                not reconciler.restart_pending and source_data.is_connected
                and source_data.book_is_actionable and source.execution_hold_reason is None
                and source.accounting_ready and source.get_account() is not None
                and (reconciler.source_submission_ready
                     or (maker and reconciler.working_observation_in_progress))
                and hedge_data.is_connected and hedge_data.snapshot_refresh_healthy
                and hedge.execution_admitted and hedge.get_account() is not None
            )

        maker = MakerStrategy(
            maker_config, state_store=owner, source_admission=admission,
            live_submission_ready=lambda: ready(maker=True),
            hedge_quantity_ready=hedge.can_execute_quantity, live_costs_from_adapters=True,
            source_terminal_query=reconciler.query_source_terminal,
            source_quote_refresh_paused=lambda: reconciler.busy,
        )
        taker = TakerStrategy(
            taker_config, state_store=owner.taker_store, source_admission=admission,
            live_submission_ready=ready, hedge_quantity_ready=hedge.can_execute_quantity,
            live_costs_from_adapters=True, source_terminal_query=reconciler.query_source_terminal,
        )
        if (str(maker.id), str(taker.id)) != ids:
            raise ValueError("native strategy identities differ from shared state")
        for strategy in (maker, taker):
            node.trader.add_strategy(strategy)
            strategy.bind_restart_gate(lambda: reconciler.restart_pending)
            reader = bind_live_account_reader(
                strategy, config=strategy.config, source_data=source_data, source_client=source,
                hedge_data=hedge_data, hedge_client=hedge,
                wallet_currency=bitfinex_exec_config.wallet_currency,
                hedge_symbol=mt5_exec_config.expected_symbol,
                hedge_stream_id=mt5_exec_config.expected_stream_id,
            )
            if strategy is maker:
                admission.reader = reader
        node.bind_strategies_drain((maker, taker), timeout_seconds=stop_timeout_seconds)
        if cache_database is not None:
            validate_native_cache(
                node.cache, trader_id=LIVE_BOTH_TRADER_ID, strategy_id=maker.id,
                business_owners=shared_business_owners(owner), routes={
                    maker_config.source_instrument_id: (
                        source_route.account_id, BITFINEX_CLIENT_ID,
                    ),
                    maker_config.hedge_instrument_id: (route.account_id, MT5_CLIENT_ID),
                },
            )
        receipt = capture_startup_receipt(owner, startup_recovery)

        def retry_check(intent: HedgeIntent, order: Order) -> None:
            config = (taker_config if any(intent is known
                                         for known in taker.state_store.intents())
                      else maker_config)
            check_rejected_retry_execution(node.cache, hedge, intent, order,
                                            config=config, data=hedge_data)

        async def recover() -> None:
            await reconcile_startup(
                node.cache, owner, trader_id=LIVE_BOTH_TRADER_ID, strategy_id=maker.id,
                source=source, hedge=hedge,
                source_instrument_id=maker_config.source_instrument_id,
                hedge_instrument_id=maker_config.hedge_instrument_id,
                receipt=receipt, rejected_retry_check=retry_check,
            )

        reconciler.bind_restart_recovery(recover, history_present=lambda: bool(
            startup_recovery is not None or has_business_history(owner)
            or node.cache.orders() or node.cache.positions() or source._cid_store.bindings
        ))
        _verify_built_composition(node, maker, source_data, source, hedge_data, hedge,
                                  other_strategies=(taker,))
    except BaseException:
        _dispose(node)
        raise
    return node, (maker, taker)
