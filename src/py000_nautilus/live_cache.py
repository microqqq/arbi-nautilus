"""Optional native persistence and identity checks for one live composition."""

from __future__ import annotations

from collections.abc import Mapping

from nautilus_trader.cache.cache import Cache
from nautilus_trader.config import CacheConfig, DatabaseConfig
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    InstrumentId,
    StrategyId,
    TraderId,
)


def native_cache_config(database: DatabaseConfig | None) -> CacheConfig | None:
    """Use a stable native namespace without flushing historical events."""
    if database is None:
        return None
    if database.type != "redis":
        raise ValueError("live native cache supports only redis")
    return CacheConfig(
        database=database,
        use_instance_id=False,
        flush_on_start=False,
        persist_account_events=True,
    )


def validate_native_cache(
    cache: Cache,
    *,
    trader_id: TraderId,
    strategy_id: StrategyId,
    routes: Mapping[InstrumentId, tuple[AccountId, ClientId]],
) -> bool:
    """Reject foreign/incomplete loads; return whether restart reconciliation is pending.

    This read-only check is not venue reconciliation or permission to release a
    business-store HOLD. An unsubmitted order may have no account yet; its native
    client index must still identify the configured execution route.
    """
    if not cache.check_integrity():
        raise ValueError("native cache integrity check failed")
    account_ids = {account_id for account_id, _ in routes.values()}
    if any(account.id not in account_ids for account in cache.accounts()):
        raise ValueError("native cache contains an account outside this live composition")
    if any(instrument.id not in routes for instrument in cache.instruments()):
        raise ValueError("native cache contains an instrument outside this live composition")

    orders, positions = cache.orders(), cache.positions()
    for order in orders:
        route = routes.get(order.instrument_id)
        if (
            route is None
            or order.trader_id != trader_id
            or order.strategy_id != strategy_id
            or cache.instrument(order.instrument_id) is None
        ):
            raise ValueError(f"native cache order identity mismatch: {order.client_order_id}")
        account_id, client_id = route
        if order.account_id is not None and (
            order.account_id != account_id or cache.account(account_id) is None
        ):
            raise ValueError(f"native cache order account mismatch: {order.client_order_id}")
        if cache.client_id(order.client_order_id) != client_id:
            raise ValueError(f"native cache order client index mismatch: {order.client_order_id}")
        position_id = cache.position_id(order.client_order_id)
        if client_id.value == "MT5" and order.is_reduce_only and position_id is None:
            raise ValueError(
                "native cache MT5 reduce-only order requires a position index: "
                f"{order.client_order_id}",
            )
        if order.position_id is not None and position_id != order.position_id:
            raise ValueError(f"native cache order position index mismatch: {order.client_order_id}")
        if position_id is not None:
            position = cache.position(position_id)
            if position is None or position.instrument_id != order.instrument_id:
                raise ValueError(
                    f"native cache order position missing or mismatched: {position_id}",
                )

    for position in positions:
        route = routes.get(position.instrument_id)
        if (
            route is None
            or position.trader_id != trader_id
            or position.strategy_id != strategy_id
            or position.account_id != route[0]
            or cache.account(position.account_id) is None
            or cache.instrument(position.instrument_id) is None
        ):
            raise ValueError(f"native cache position identity mismatch: {position.id}")
    return bool(orders or positions)
