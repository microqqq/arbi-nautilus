"""Shared terminal exposure checks; native reconciliation owns execution recovery."""

from collections.abc import Callable
from decimal import Decimal
from math import isclose
from typing import cast

from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from nautilus_trader.model.events import OrderCanceled, OrderExpired
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, VenueOrderId
from nautilus_trader.model.orders import Order

from py000_nautilus.config import SourceAccountRoute
from py000_nautilus.models import BusinessOrderSide
from py000_nautilus.store import SourceOrderRecord

type SourceTerminalResult = Callable[[OrderStatusReport | None], bool]
type SourceTerminalQuery = Callable[
    [ClientOrderId, VenueOrderId, SourceTerminalResult], None,
]


def source_cancel_report_is_exact(
    *,
    record: SourceOrderRecord | None,
    order: Order | None,
    event: OrderCanceled | OrderExpired,
    report: OrderStatusReport,
    source_instrument_id: InstrumentId,
    source_accounts: tuple[SourceAccountRoute, ...],
    maker: bool,
) -> bool:
    client_order_id, venue_order_id = event.client_order_id, event.venue_order_id
    if record is None or order is None or venue_order_id is None:
        return False
    route = next((row for row in source_accounts
                  if row.account_id.value == record.source_account_id), None)
    if route is None:
        return False
    expected_client = route.client_id.value if route.client_id is not None else None
    expected_side = OrderSide.BUY if record.side is BusinessOrderSide.BUY else OrderSide.SELL
    order_avg = None if order.avg_px is None else Decimal(str(order.avg_px))
    report_avg = None if report.avg_px is None else Decimal(str(report.avg_px))
    average_is_exact = report_avg is None if record.filled_ounces == 0 else (
        order_avg is not None and report_avg is not None
        and isclose(float(order_avg), float(report_avg))
    )
    terminal_name, terminal_status = (
        ("CANCELED", OrderStatus.CANCELED) if isinstance(event, OrderCanceled)
        else ("EXPIRED", OrderStatus.EXPIRED)
    )
    ioc_shape = order.time_in_force == TimeInForce.IOC and not cast(bool, order.is_post_only)
    order_shape_is_exact = (
        (order.time_in_force == TimeInForce.GTC and cast(bool, order.is_post_only)
         and not cast(bool, order.is_reduce_only))
        or (ioc_shape and cast(bool, order.is_reduce_only))
    ) if maker else ioc_shape
    return (
        record.client_order_id == client_order_id.value and record.status == terminal_name
        and record.source_client_id == expected_client
        and event.instrument_id == source_instrument_id and event.account_id == route.account_id
        and report.instrument_id == source_instrument_id and report.account_id == route.account_id
        and order.instrument_id == source_instrument_id and order.account_id == route.account_id
        and report.client_order_id == client_order_id and order.client_order_id == client_order_id
        and report.venue_order_id == venue_order_id and order.venue_order_id == venue_order_id
        and report.order_status == terminal_status and order.status == terminal_status
        and order.is_closed and report.order_side == expected_side and order.side == expected_side
        and report.order_type == OrderType.LIMIT and order.order_type == OrderType.LIMIT
        and report.time_in_force == order.time_in_force and order_shape_is_exact
        # This confirms terminal exposure, not venue enforcement of post-only.
        # Keep Maker's existing support for an omitted Paper terminal bit.
        and (cast(bool, order.is_post_only) or not report.post_only)
        and report.reduce_only == cast(bool, order.is_reduce_only)
        and report.quantity.as_decimal() == record.quantity_ounces
        and order.quantity.as_decimal() == record.quantity_ounces
        and report.filled_qty.as_decimal() == record.filled_ounces
        and order.filled_qty.as_decimal() == record.filled_ounces
        and report.price == order.price and average_is_exact
    )
