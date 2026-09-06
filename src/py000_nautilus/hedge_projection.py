"""Project complete known hedge fills without advancing legs or releasing HOLD.

Cache route/position indexes and venue/journal reconciliation are prerequisites.
This checks the quantities and identities retained by business state, not a second
price/fee ledger or permission to dispatch the next order.
"""

from collections.abc import Sequence
from copy import deepcopy
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from nautilus_trader.model.events import OrderFilled, OrderInitialized
from nautilus_trader.model.identifiers import InstrumentId, StrategyId, TraderId
from nautilus_trader.model.orders import Order

from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import BusinessOrderSide, HedgeIntent, ObligationStatus
from py000_nautilus.store import JsonStateStore, SourceOrderRecord


def _key(fill: OrderFilled) -> str:
    return f"{fill.client_order_id.value}|{fill.trade_id.value}"


def _order_ids(intent: HedgeIntent, record: SourceOrderRecord) -> tuple[str, ...]:
    ids, current = intent.hedge_order_ids, intent.hedge_client_order_id
    binding = (intent.hedge_position_id, intent.hedge_position_quantity_ounces)
    if binding != (record.hedge_position_id, record.hedge_position_quantity_ounces):
        raise ValueError("hedge projection source/intent ticket binding differs")
    if intent.hedge_plan:
        index = intent.hedge_leg_index
        if len(ids) != index + (current is not None) or (
            current is not None and (index == len(intent.hedge_plan) or current != ids[index])
        ):
            raise ValueError("hedge projection plan order history differs from its current leg")
        if binding[0] is not None and (
            len(intent.hedge_plan) != 1 or intent.hedge_plan[0].position_id != binding[0]
            or (binding[1] is not None
                and intent.hedge_plan[0].expected_position_quantity_ounces != binding[1])
        ):
            raise ValueError("hedge projection plan differs from the bound exit ticket")
    else:
        if not ids and current is not None:
            ids = (current,)  # Schema-1 single-CID history is still unambiguous.
        if len(ids) > 1 or (ids and current != ids[0]):
            raise ValueError("hedge projection unplanned history has no unique current order")
        if binding[1] is not None and intent.hedge_quantity_ounces > binding[1]:
            raise ValueError("hedge projection unplanned close exceeds its bound ticket")
    if len(set(ids)) != len(ids) or any(not cid or "|" in cid for cid in ids):
        raise ValueError("hedge projection order history identity is not unique")
    return ids


def _checked_fills(
    order: Order, record: SourceOrderRecord, intent: HedgeIntent, index: int, *,
    hedge_instrument_id: InstrumentId, trader_id: TraderId, strategy_id: StrategyId,
) -> tuple[OrderFilled, ...]:
    leg = intent.hedge_plan[index] if intent.hedge_plan else None
    quantity = leg.quantity_ounces if leg is not None else intent.hedge_quantity_ounces
    position = leg.position_id if leg is not None else intent.hedge_position_id
    side = OrderSide.BUY if intent.hedge_side is BusinessOrderSide.BUY else OrderSide.SELL
    events = order.events
    if not events or not isinstance(events[0], OrderInitialized):
        raise ValueError("hedge projection requires original native initialization")
    initial = events[0]
    if (
        order.trader_id != trader_id or order.strategy_id != strategy_id
        or order.instrument_id != hedge_instrument_id or order.side != side
        or order.order_type != OrderType.MARKET or order.time_in_force != TimeInForce.FOK
        or order.is_quote_quantity or order.is_reduce_only != (position is not None)
        or order.quantity.as_decimal() != quantity or initial.quantity.as_decimal() != quantity
        or initial.side != side or initial.order_type != OrderType.MARKET
        or initial.time_in_force != TimeInForce.FOK or initial.quote_quantity
        or initial.reduce_only != (position is not None)
        or (record.hedge_account_id is not None and order.account_id is not None
            and record.hedge_account_id != order.account_id.value)
        or (position is not None and order.position_id is not None
            and position != order.position_id.value)
    ):
        raise ValueError(f"hedge projection order or ticket facts differ: {order.client_order_id}")
    if any(
        event.trader_id != trader_id or event.strategy_id != strategy_id
        or event.instrument_id != hedge_instrument_id
        or event.client_order_id != order.client_order_id
        or (getattr(event, "account_id", None) is not None
            and event.account_id != order.account_id)
        for event in events
    ):
        raise ValueError("hedge projection event ownership differs")
    fills = tuple(event for event in events if isinstance(event, OrderFilled))
    if any(
        fill.account_id != order.account_id or fill.venue_order_id != order.venue_order_id
        or "|" in fill.trade_id.value or fill.order_side != side
        or fill.order_type != OrderType.MARKET or fill.position_id is None
        or fill.position_id != order.position_id
        or not fill.last_qty.as_decimal().is_finite() or fill.last_qty.as_decimal() <= 0
        for fill in fills
    ):
        raise ValueError("hedge projection fill identity, ticket or quantity differs")
    total = sum((fill.last_qty.as_decimal() for fill in fills), Decimal(0))
    if (total != order.filled_qty.as_decimal() or total > quantity
            or order.trade_ids != [fill.trade_id for fill in fills]
            or (order.status == OrderStatus.FILLED and total != quantity)):
        raise ValueError("hedge projection native fill history is incomplete")
    return fills


def project_hedge_fills(
    store: JsonStateStore | MakerStateStore, orders: Sequence[Order], *,
    hedge_instrument_id: InstrumentId, trader_id: TraderId, strategy_id: StrategyId,
    reason: str,
) -> int:
    """Validate all bound hedge orders, then publish only held current-fill suffixes."""
    if not isinstance(reason, str) or not reason:
        raise ValueError("hedge projection requires a non-empty HOLD reason")
    maker = store if isinstance(store, MakerStateStore) else None
    if maker is not None:
        if maker.hedge_instrument_id != hedge_instrument_id.value:
            raise ValueError("hedge projection Maker instrument differs")
        maker._validate()
        views = tuple(maker.stores.values())
    elif type(store) is JsonStateStore:
        views = (store,)
    else:
        raise TypeError("hedge projection requires the whole Maker owner or a JsonStateStore")

    sources: set[str] = set()
    intents: set[str] = set()
    source_fills: set[str] = set()
    bindings: dict[str, tuple[JsonStateStore, HedgeIntent, SourceOrderRecord, int]] = {}
    for view in views:
        if (view.active_source_order_id is not None
                and view.active_source_order_id not in view._state.source_orders):
            raise ValueError("hedge projection active source identity differs")
        for cid, source_record in view._state.source_orders.items():
            if not cid or cid != source_record.client_order_id or cid in sources:
                raise ValueError("hedge projection source dictionary identity differs")
            sources.add(cid)
        for identity, intent in view._state.hedge_intents.items():
            record = view.source_order(intent.source_client_order_id)
            parts = intent.fill_key.split("|")
            if (
                not identity or identity != intent.intent_id or identity in intents
                or intent.fill_key in source_fills or record is None
                or intent.source_side is not record.side
                or intent.fill_key not in view._state.seen_source_fills
                or len(parts) != 3 or not all(parts)
                or parts[0] != intent.source_client_order_id or parts[2] != intent.source_trade_id
            ):
                raise ValueError("hedge projection intent source identity differs")
            intents.add(identity)
            source_fills.add(intent.fill_key)
            for index, cid in enumerate(_order_ids(intent, record)):
                if cid in bindings:
                    raise ValueError("hedge projection order belongs to multiple intents")
                bindings[cid] = (view, intent, record, index)
    by_id = {order.client_order_id.value: order for order in orders}
    if sources & bindings.keys() or len(by_id) != len(orders) or set(by_id) != set(bindings):
        raise ValueError("hedge projection requires the exact complete hedge order set")

    trades: set[tuple[str, str]] = set()
    totals: dict[str, Decimal] = dict.fromkeys(intents, Decimal(0))
    current_totals: dict[str, Decimal] = dict.fromkeys(intents, Decimal(0))
    keys_by_view: dict[int, set[str]] = {id(view): set() for view in views}
    missing: list[tuple[JsonStateStore, OrderFilled]] = []
    for cid, (view, intent, record, index) in bindings.items():
        order = by_id[cid]
        fills = _checked_fills(order, record, intent, index,
                               hedge_instrument_id=hedge_instrument_id,
                               trader_id=trader_id, strategy_id=strategy_id)
        keys = tuple(_key(fill) for fill in fills)
        for fill in fills:
            trade = (fill.account_id.value, fill.trade_id.value)
            if trade in trades:
                raise ValueError("hedge projection native TradeId is duplicated")
            trades.add(trade)
        keys_by_view[id(view)].update(keys)
        seen = {key for key in view._state.seen_hedge_fills if key.split("|")[0] == cid}
        count = len(seen)
        if seen != set(keys[:count]):
            raise ValueError("hedge projection seen fills are not a complete contiguous prefix")
        prefix = sum((fill.last_qty.as_decimal() for fill in fills[:count]), Decimal(0))
        totals[intent.intent_id] += prefix
        if cid == intent.hedge_client_order_id:
            current_totals[intent.intent_id] = prefix
        elif (order.status != OrderStatus.FILLED or count != len(fills)):
            raise ValueError("hedge projection advanced historical leg is incomplete")
        if fills[count:]:
            if (intent.status is ObligationStatus.COMPLETED
                    or cid != intent.hedge_client_order_id or order.status != OrderStatus.FILLED):
                raise ValueError("hedge projection missing fills require a complete current FOK")
            missing.extend((view, fill) for fill in fills[count:])
    for view in views:
        if not view._state.seen_hedge_fills <= keys_by_view[id(view)]:
            raise ValueError("hedge projection seen fill has no owned native order")
        for intent in view.intents():
            total = totals[intent.intent_id]
            if total != intent.hedge_filled_ounces or total > intent.hedge_quantity_ounces:
                raise ValueError("hedge projection seen prefix differs from aggregate fill")
            if intent.hedge_plan and (
                current_totals[intent.intent_id] != intent.hedge_leg_filled_ounces
                or total != sum((leg.quantity_ounces for leg in
                                 intent.hedge_plan[:intent.hedge_leg_index]),
                                intent.hedge_leg_filled_ounces)
            ):
                raise ValueError("hedge projection seen prefix differs from current leg progress")
            if (intent.status is ObligationStatus.COMPLETED
                    and total != intent.hedge_quantity_ounces):
                raise ValueError("hedge projection completed intent is not fully seen")
    if not missing:
        return 0

    previous = deepcopy(views[0]._state)
    maker_previous = maker._snapshot() if maker is not None else None
    try:
        for view in views:
            if view._state.halt_reason is None:
                view._state.halt_reason = reason
            if view._state.source_freeze_reason is None:
                view._state.source_freeze_reason = reason
        for view, fill in missing:
            view._apply_hedge_fill(
                client_order_id=fill.client_order_id.value, trade_id=fill.trade_id.value,
                fill_ounces=fill.last_qty.as_decimal(), blocked_reason=reason,
            )
        views[0]._persist()
    except ParentDirectorySyncError:
        raise  # Complete candidate is published; reload must observe the same facts.
    except Exception:
        if maker is not None and maker_previous is not None:
            maker._restore(maker_previous)
        else:
            views[0]._state = previous
        raise
    return len(missing)
