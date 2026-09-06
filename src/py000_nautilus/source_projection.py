"""Project a provable source-fill suffix without dispatching or releasing HOLD.

Native cache route ownership and complete venue reconciliation are prerequisites,
not established here. Business state has no separate price/fee event ledger.
"""

from collections.abc import Sequence
from copy import deepcopy
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled, OrderInitialized
from nautilus_trader.model.identifiers import InstrumentId, StrategyId, TraderId
from nautilus_trader.model.orders import Order

from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import BusinessOrderSide, ObligationStatus
from py000_nautilus.store import JsonStateStore, SourceOrderRecord


def _key(fill: OrderFilled) -> str:
    return f"{fill.client_order_id.value}|{fill.venue_order_id.value}|{fill.trade_id.value}"


def _sign(side: BusinessOrderSide) -> int:
    return 1 if side is BusinessOrderSide.BUY else -1


def _checked_fills(
    order: Order, record: SourceOrderRecord, *, source_instrument_id: InstrumentId,
    trader_id: TraderId, strategy_id: StrategyId,
) -> tuple[OrderFilled, ...]:
    side = OrderSide.BUY if record.side is BusinessOrderSide.BUY else OrderSide.SELL
    events = order.events
    if (
        not events or not isinstance(events[0], OrderInitialized)
        or order.trader_id != trader_id or order.strategy_id != strategy_id
        or order.instrument_id != source_instrument_id
        or order.client_order_id.value != record.client_order_id or order.side != side
        or "|" in record.client_order_id
        or order.is_quote_quantity or not record.quantity_ounces.is_finite()
        or record.quantity_ounces <= 0 or not record.filled_ounces.is_finite()
        or not 0 <= record.filled_ounces <= record.quantity_ounces
        or order.quantity.as_decimal() != record.quantity_ounces
        or events[0].quantity.as_decimal() != record.quantity_ounces
        or (record.source_account_id is not None and order.account_id is not None
            and order.account_id.value != record.source_account_id)
    ):
        raise ValueError(f"source projection order facts differ: {record.client_order_id}")
    if any(
        event.trader_id != trader_id or event.strategy_id != strategy_id
        or event.instrument_id != source_instrument_id
        or event.client_order_id != order.client_order_id
        for event in events
    ):
        raise ValueError("source projection event ownership differs")
    fills = tuple(event for event in events if isinstance(event, OrderFilled))
    if any(
        fill.account_id != order.account_id or fill.venue_order_id != order.venue_order_id
        or "|" in fill.venue_order_id.value or "|" in fill.trade_id.value
        or fill.order_side != side or fill.order_type != order.order_type
        or not fill.last_qty.as_decimal().is_finite() or fill.last_qty.as_decimal() <= 0
        for fill in fills
    ):
        raise ValueError("source projection fill identity or quantity differs")
    total = sum((fill.last_qty.as_decimal() for fill in fills), Decimal(0))
    if (total != order.filled_qty.as_decimal() or total > record.quantity_ounces
            or order.trade_ids != [fill.trade_id for fill in fills]):
        raise ValueError("source projection native fill history is incomplete")
    return fills


def _check_recorded_facts(
    views: Sequence[JsonStateStore], fills: dict[str, OrderFilled],
) -> None:
    claimed: set[str] = set()
    for view in views:
        for intent_id, intent in view._state.hedge_intents.items():
            if intent_id != intent.intent_id or intent.fill_key in claimed:
                raise ValueError("source projection recorded intent identity is not unique")
            claimed.add(intent.fill_key)
            fill = fills.get(intent.fill_key)
            record = view.source_order(intent.source_client_order_id)
            if (
                fill is None or record is None
                or intent.fill_key not in view._state.seen_source_fills
                or intent.source_client_order_id != fill.client_order_id.value
                or intent.source_trade_id != fill.trade_id.value
                or intent.source_side is not record.side
                or intent.source_fill_ounces != fill.last_qty.as_decimal()
            ):
                raise ValueError("source projection recorded intent differs from fill")


def _check_taker_tail(store: JsonStateStore, candidate: str, orders: dict[str, Order]) -> None:
    records = store.source_orders()
    if len(records) == 1:
        return
    if candidate != store.active_source_order_id:
        raise ValueError("source projection cannot order historical Taker late fills")
    historical = [record for record in records if record.client_order_id != candidate]
    old_intents = [intent for intent in store.intents()
                   if intent.source_client_order_id != candidate]
    if any(
        orders[record.client_order_id].status.name != record.status
        or not ((record.status == "FILLED" and record.filled_ounces == record.quantity_ounces)
             or (record.status in {"DENIED", "REJECTED"} and record.filled_ounces == 0))
        for record in historical
    ) or any(
        intent.status is not ObligationStatus.COMPLETED
        or intent.hedge_filled_ounces != intent.hedge_quantity_ounces for intent in old_intents
    ):
        raise ValueError("source projection historical Taker orders or hedges are unresolved")
    residual = sum((record.filled_ounces * _sign(record.side) for record in historical), Decimal(0))
    residual -= sum((-_sign(intent.hedge_side) * intent.hedge_quantity_ounces
                     for intent in old_intents), Decimal(0))
    if residual != 0:
        raise ValueError("source projection historical Taker residual is not zero")


def project_source_fills(
    store: JsonStateStore | MakerStateStore, orders: Sequence[Order], *,
    source_instrument_id: InstrumentId, trader_id: TraderId, strategy_id: StrategyId,
    reason: str,
) -> int:
    """Validate all known source orders, then atomically append at most one suffix."""
    if not isinstance(reason, str) or not reason:
        raise ValueError("source projection requires a non-empty HOLD reason")
    maker = store if isinstance(store, MakerStateStore) else None
    if maker is not None:
        if maker.source_instrument_id != source_instrument_id.value:
            raise ValueError("source projection Maker instrument differs")
        maker._validate()
        views = tuple(maker.stores.values())
    elif type(store) is JsonStateStore:
        views = (store,)
    else:
        raise TypeError("source projection requires the whole Maker owner or a JsonStateStore")
    for view in views:
        if any(not key or key != record.client_order_id
               for key, record in view._state.source_orders.items()) or (
            view.active_source_order_id is not None
            and view.active_source_order_id not in view._state.source_orders
        ):
            raise ValueError("source projection source dictionary or active identity differs")
    records = {record.client_order_id: (view, record)
               for view in views for record in view.source_orders()}
    by_id = {order.client_order_id.value: order for order in orders}
    if (len(records) != sum(len(view.source_orders()) for view in views)
            or len(by_id) != len(orders) or set(records) != set(by_id)):
        raise ValueError("source projection requires the exact complete source order set")
    native_fills: dict[str, OrderFilled] = {}
    trades: set[tuple[str, str]] = set()
    suffixes: dict[str, tuple[OrderFilled, ...]] = {}
    prefixes: dict[str, tuple[str, ...]] = {}
    for cid, (view, record) in records.items():
        fills = _checked_fills(by_id[cid], record, source_instrument_id=source_instrument_id,
                               trader_id=trader_id, strategy_id=strategy_id)
        keys = tuple(_key(fill) for fill in fills)
        for key, fill in zip(keys, fills, strict=True):
            trade = (fill.account_id.value, fill.trade_id.value)
            if key in native_fills or trade in trades:
                raise ValueError("source projection native TradeId is duplicated")
            native_fills[key] = fill
            trades.add(trade)
        seen = {key for key in view._state.seen_source_fills if key.split("|")[0] == cid}
        count = len(seen)
        if seen != set(keys[:count]):
            raise ValueError("source projection seen fills are not a complete contiguous prefix")
        prefix_total = sum((fill.last_qty.as_decimal() for fill in fills[:count]), Decimal(0))
        if prefix_total != record.filled_ounces:
            raise ValueError("source projection seen prefix quantity differs")
        prefixes[cid] = keys[:count]
        if fills[count:]:
            suffixes[cid] = fills[count:]
    if any(not view._state.seen_source_fills <= native_fills.keys() for view in views):
        raise ValueError("source projection seen fill has no native order")
    _check_recorded_facts(views, native_fills)
    if maker is not None:
        checkpoint_keys = {key for item in maker._legacy_orders for key in item.fill_keys}
        for cid, prefix in prefixes.items():
            allocated_keys = tuple(item.fill_key for item in maker._allocations
                                   if item.fill_key.split("|")[0] == cid)
            if allocated_keys != tuple(key for key in prefix if key not in checkpoint_keys):
                raise ValueError("source projection Maker allocation order differs")
        for allocation in maker._allocations:
            fill = native_fills[allocation.fill_key]
            sign = 1 if fill.order_side is OrderSide.BUY else -1
            if allocation.signed_fill_ounces != sign * fill.last_qty.as_decimal():
                raise ValueError("source projection recorded Maker allocation quantity differs")
    else:
        view = views[0]
        residual = sum((record.filled_ounces * _sign(record.side)
                        for record in view.source_orders()), Decimal(0))
        residual -= sum((-_sign(intent.hedge_side) * intent.hedge_quantity_ounces
                         for intent in view.intents()), Decimal(0))
        if view.rounding_residual_ounces != residual:
            raise ValueError("source projection recorded Taker residual is inconsistent")
    if not suffixes:
        return 0
    if len(suffixes) != 1:
        raise ValueError("source projection cannot order multiple source suffixes")
    cid, suffix = next(iter(suffixes.items()))
    if maker is not None:
        maker._validate_source_projection_tail(cid, prefixes[cid])
    else:
        _check_taker_tail(views[0], cid, by_id)
    view = records[cid][0]
    previous = deepcopy(view._state)
    maker_previous = maker._snapshot() if maker is not None else None
    try:
        for target in views:
            if target._state.halt_reason is None:
                target._state.halt_reason = reason
            if target._state.source_freeze_reason is None:
                target._state.source_freeze_reason = reason
        for fill in suffix:
            view._reserve_source_fill(
                fill_key=_key(fill), client_order_id=cid, trade_id=fill.trade_id.value,
                source_side=records[cid][1].side, fill_ounces=fill.last_qty.as_decimal(),
                blocked_reason=reason,
            )
        view._persist()
    except ParentDirectorySyncError:
        raise  # The complete candidate was published; do not roll back only memory.
    except Exception:
        if maker is not None and maker_previous is not None:
            maker._restore(maker_previous)
        else:
            view._state = previous
        raise
    return len(suffix)
