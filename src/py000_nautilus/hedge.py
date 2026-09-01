"""Translate Nautilus source fill events into durable hedge obligations."""

from decimal import Decimal

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId

from py000_nautilus.models import BusinessOrderSide, HedgeIntent
from py000_nautilus.store import JsonStateStore


class HedgeCoordinator:
    """Exactly-once intent reservation keyed by Nautilus venue trade identity."""

    def __init__(self, source_instrument_id: InstrumentId, store: JsonStateStore) -> None:
        self._source_instrument_id = source_instrument_id
        self._store = store

    def on_source_filled(self, event: OrderFilled) -> HedgeIntent | None:
        if event.instrument_id != self._source_instrument_id:
            return None
        client_order_id = event.client_order_id.value
        if not self._store.knows_source_order(client_order_id):
            return None
        side = _business_side(event.order_side)
        fill_key = _fill_key(event)
        return self._store.reserve_source_fill(
            fill_key=fill_key,
            client_order_id=client_order_id,
            trade_id=event.trade_id.value,
            source_side=side,
            fill_ounces=Decimal(str(event.last_qty)),
        )

    def has_seen_source_fill(self, event: OrderFilled) -> bool:
        if event.instrument_id != self._source_instrument_id:
            return False
        client_order_id = event.client_order_id.value
        if not self._store.knows_source_order(client_order_id):
            return False
        return self._store.has_seen_source_fill(_fill_key(event))


def _business_side(side: OrderSide) -> BusinessOrderSide:
    if side is OrderSide.BUY:
        return BusinessOrderSide.BUY
    if side is OrderSide.SELL:
        return BusinessOrderSide.SELL
    raise ValueError(f"unsupported source fill side {side}")


def _fill_key(event: OrderFilled) -> str:
    return (
        f"{event.client_order_id.value}|{event.venue_order_id.value}|{event.trade_id.value}"
    )
