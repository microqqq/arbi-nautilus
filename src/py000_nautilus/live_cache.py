"""Optional native persistence and identity checks for one live composition."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from nautilus_trader.cache.cache import Cache
from nautilus_trader.config import CacheConfig, DatabaseConfig
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TraderId,
)
from nautilus_trader.model.orders import Order


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


def _filled_bitfinex_position_id(order: Order) -> PositionId:
    """Prove the native NETTING reference from this order, not the current net amount."""
    position_id = order.position_id
    if position_id is None or position_id.value != f"{order.instrument_id}-{order.strategy_id}":
        raise ValueError(
            f"native cache BITFINEX filled order position mismatch: {order.client_order_id}",
        )
    fills = [event for event in order.events if isinstance(event, OrderFilled)]
    trade_ids = [fill.trade_id for fill in fills]
    if (
        not fills
        or trade_ids != order.trade_ids
        or len(set(trade_ids)) != len(trade_ids)
        or any(
            fill.trader_id != order.trader_id
            or fill.strategy_id != order.strategy_id
            or fill.instrument_id != order.instrument_id
            or fill.client_order_id != order.client_order_id
            or fill.account_id != order.account_id
            or fill.venue_order_id != order.venue_order_id
            or fill.position_id != position_id
            or fill.order_side != order.side
            or fill.last_qty.as_decimal() <= 0
            for fill in fills
        )
        or sum((fill.last_qty.as_decimal() for fill in fills), Decimal(0))
        != order.filled_qty.as_decimal()
    ):
        raise ValueError(
            f"native cache BITFINEX filled order facts mismatch: {order.client_order_id}",
        )
    return position_id


def validate_native_cache(
    cache: Cache,
    *,
    trader_id: TraderId,
    strategy_id: StrategyId,
    routes: Mapping[InstrumentId, tuple[AccountId, ClientId]],
    business_owners: Mapping[str, StrategyId] | None = None,
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
    external_ids = _closed_external_mt5_history(cache, trader_id=trader_id, routes=routes)
    allowed = {strategy_id} if business_owners is None else set(business_owners.values())
    if business_owners is not None and set(business_owners) != {
        order.client_order_id.value for order in orders if order.client_order_id not in external_ids
    }:
        raise ValueError("native cache order set differs from shared business ownership")
    for order in orders:
        if order.client_order_id in external_ids:
            continue  # Validated closed venue history is not this strategy's business history.
        route = routes.get(order.instrument_id)
        if (
            route is None
            or order.trader_id != trader_id
            or order.strategy_id != (strategy_id if business_owners is None
                                     else business_owners[order.client_order_id.value])
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
        if client_id.value == "BITFINEX" and order.filled_qty.as_decimal() > 0:
            native_position_id = _filled_bitfinex_position_id(order)
            if position_id is not None and position_id != native_position_id:
                raise ValueError(
                    f"native cache order position index mismatch: {order.client_order_id}",
                )
            # NT 1.231.0 indexes only an opening CID during normal NETTING fills.
            # Other filled orders keep their canonical PID; never repair this index here.
            position_id = native_position_id
        elif order.position_id is not None and position_id != order.position_id:
            raise ValueError(f"native cache order position index mismatch: {order.client_order_id}")
        if position_id is not None:
            position = cache.position(position_id)
            if position is None or position.instrument_id != order.instrument_id:
                raise ValueError(
                    f"native cache order position missing or mismatched: {position_id}",
                )
            if (business_owners is not None and position.strategy_id != order.strategy_id
                    and not (client_id.value == "MT5" and order.is_reduce_only)):
                raise ValueError(f"native cache order position owner differs: {position_id}")

    for position in positions:
        if position.strategy_id.is_external():
            continue  # Its complete closed lifecycle was checked with the external orders.
        route = routes.get(position.instrument_id)
        if (
            route is None
            or position.trader_id != trader_id
            or position.strategy_id not in allowed
            or position.account_id != route[0]
            or cache.account(position.account_id) is None
            or cache.instrument(position.instrument_id) is None
        ):
            raise ValueError(f"native cache position identity mismatch: {position.id}")
        if business_owners is not None:
            opening = cache.order(position.opening_order_id)
            if opening is None or opening.strategy_id != position.strategy_id:
                raise ValueError(f"native cache position opening owner differs: {position.id}")
    return bool(orders or positions)


def _closed_external_mt5_history(
    cache: Cache, *, trader_id: TraderId,
    routes: Mapping[InstrumentId, tuple[AccountId, ClientId]],
) -> set[ClientOrderId]:
    """Read-only qualification of NT's unclaimed, completely closed MT5 history.

    Native imported orders may lack account/client/position indexes. The fills
    must still prove their actual route and exact closed Position, never just a
    zero aggregate net. Live EA reports remain a separate required check.
    """
    external = [order for order in cache.orders() if order.strategy_id.is_external()]
    expected: dict[PositionId, dict[object, OrderFilled]] = {}
    for order in external:
        route = routes.get(order.instrument_id)
        position = None if order.position_id is None else cache.position(order.position_id)
        fills = [event for event in order.events if isinstance(event, OrderFilled)]
        if (
            route is None or route[1].value != "MT5" or order.trader_id != trader_id
            or order.status != OrderStatus.FILLED or order.quantity.as_decimal() <= 0
            or order.filled_qty != order.quantity or len(fills) != 1
            or order.account_id not in {None, route[0]}
            or cache.account(route[0]) is None or cache.instrument(order.instrument_id) is None
            or cache.client_id(order.client_order_id) not in {None, route[1]}
            or cache.position_id(order.client_order_id) not in {None, order.position_id}
            or position is None or not position.is_closed or position.adjustments
            or not position.strategy_id.is_external() or position.trader_id != trader_id
            or position.instrument_id != order.instrument_id or position.account_id != route[0]
        ):
            raise ValueError(
                f"native external MT5 history is not closed and bound: {order.client_order_id}",
            )
        fill = fills[0]
        if (
            fill.trader_id != trader_id or fill.strategy_id != order.strategy_id
            or fill.account_id != route[0] or fill.instrument_id != order.instrument_id
            or fill.client_order_id != order.client_order_id
            or fill.position_id != position.id or fill.venue_order_id != order.venue_order_id
            or fill.order_side != order.side or fill.order_type != order.order_type
            or fill.last_qty != order.quantity or order.trade_ids != [fill.trade_id]
        ):
            raise ValueError(f"native external MT5 fill identity differs: {order.client_order_id}")
        expected.setdefault(position.id, {})[fill.id] = fill
    positions = [position for position in cache.positions() if position.strategy_id.is_external()]
    if {position.id for position in positions} != set(expected) or any(
        len(position.events) != len(expected[position.id])
        or {fill.id for fill in position.events} != set(expected[position.id])
        or any(OrderFilled.to_dict(fill) != OrderFilled.to_dict(expected[position.id][fill.id])
               for fill in position.events)
        for position in positions
    ):
        raise ValueError("native external MT5 position history is incomplete")
    return {order.client_order_id for order in external}
