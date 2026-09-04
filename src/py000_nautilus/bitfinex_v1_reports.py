"""Pure, fail-closed Bitfinex REST row to Nautilus report mapping."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from itertools import chain

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money, Price, Quantity

from py000_nautilus.bitfinex_v1_protocol import (
    POST_ONLY_FLAG,
    REDUCE_ONLY_FLAG,
    OrderSnapshot,
    OrderState,
    TradeUpdate,
    parse_private_message,
)

type CidLookup = Callable[[int], ClientOrderId | None]


class BitfinexV1ReportError(ValueError):
    """REST facts cannot be represented by the deliberately small v1 adapter."""


def map_order_status_reports(
    *,
    active_rows: list[object],
    history_rows: list[object],
    instrument: Instrument,
    account_id: AccountId,
    cid_lookup: CidLookup,
    ts_init: int,
) -> list[OrderStatusReport]:
    """Map active and historical order rows, rejecting ambiguous owned facts."""
    _timestamp(ts_init, "ts_init")
    active = _order_snapshot(active_rows)
    history = _order_snapshot(history_rows)
    by_venue: dict[int, tuple[OrderState, bool]] = {}
    venue_by_cid: dict[int, int] = {}
    sourced = chain(((row, True) for row in active), ((row, False) for row in history))
    for state, is_active in sourced:
        previous = by_venue.get(state.venue_order_id)
        if previous is not None and previous[0] != state:
            raise BitfinexV1ReportError("duplicate Bitfinex order ID changed its facts")
        by_venue[state.venue_order_id] = (state, is_active or (previous[1] if previous else False))
        if state.client_order_id is not None:
            prior_venue = venue_by_cid.setdefault(state.client_order_id, state.venue_order_id)
            if prior_venue != state.venue_order_id:
                raise BitfinexV1ReportError("duplicate Bitfinex CID names multiple orders")

    reports: list[OrderStatusReport] = []
    for state, is_active in sorted(
        by_venue.values(), key=lambda item: (item[0].ts_updated_ms, item[0].venue_order_id)
    ):
        client_order_id = _owned_client_id(state.client_order_id, cid_lookup)
        if client_order_id is None:
            if is_active:
                raise BitfinexV1ReportError("active Bitfinex order has no owned CID binding")
            continue
        reports.append(
            _order_report(
                state,
                is_active=is_active,
                instrument=instrument,
                account_id=account_id,
                client_order_id=client_order_id,
                ts_init=ts_init,
            )
        )
    return reports


def map_fill_reports(
    *,
    rows: list[object],
    instrument: Instrument,
    account_id: AccountId,
    cid_lookup: CidLookup,
    fee_currency: str,
    ts_init: int,
) -> list[FillReport]:
    """Map final ``tu``-equivalent trade rows, ignoring unowned history."""
    _timestamp(ts_init, "ts_init")
    expected_fee_currency = _text(fee_currency, "fee_currency")
    if expected_fee_currency != USD.code:
        raise BitfinexV1ReportError("Bitfinex XAUT trade fee currency must be USD")
    by_trade: dict[int, TradeUpdate] = {}
    for row in rows:
        trade = parse_private_message([0, "tu", row])
        if not isinstance(trade, TradeUpdate):  # pragma: no cover - parser grammar guard
            raise BitfinexV1ReportError("Bitfinex trade parser returned the wrong event")
        previous = by_trade.get(trade.trade_id)
        if previous is not None and previous != trade:
            raise BitfinexV1ReportError("duplicate Bitfinex trade ID changed its facts")
        by_trade[trade.trade_id] = trade

    reports: list[FillReport] = []
    venue_by_cid: dict[int, int] = {}
    cid_by_venue: dict[int, int] = {}
    for trade in sorted(by_trade.values(), key=lambda item: (item.ts_event_ms, item.trade_id)):
        client_order_id = _owned_client_id(trade.client_order_id, cid_lookup)
        if client_order_id is None:
            continue
        assert trade.client_order_id is not None
        prior_venue = venue_by_cid.setdefault(trade.client_order_id, trade.venue_order_id)
        prior_cid = cid_by_venue.setdefault(trade.venue_order_id, trade.client_order_id)
        if prior_venue != trade.venue_order_id or prior_cid != trade.client_order_id:
            raise BitfinexV1ReportError("Bitfinex trade history changes CID/order identity")
        _exact_symbol(trade.symbol, instrument)
        if trade.order_type not in {"LIMIT", "IOC"}:
            raise BitfinexV1ReportError(f"unsupported Bitfinex trade type {trade.order_type!r}")
        if trade.order_price <= 0:
            raise BitfinexV1ReportError("Bitfinex trade order price must be positive")
        _price(instrument, trade.order_price, "trade order price")
        last_qty = _quantity(instrument, abs(trade.execution_qty), "trade quantity")
        last_px = _price(instrument, trade.execution_price, "trade price")
        if trade.fee_currency != expected_fee_currency:
            raise BitfinexV1ReportError(
                f"Bitfinex trade fee currency must be {expected_fee_currency}"
            )
        try:
            commission = Money(-trade.fee, USD)
        except ValueError as exc:
            raise BitfinexV1ReportError("Bitfinex fee loses USD precision") from exc
        if commission.as_decimal() != -trade.fee:
            raise BitfinexV1ReportError("Bitfinex fee loses USD precision")
        reports.append(
            FillReport(
                account_id=account_id,
                instrument_id=instrument.id,
                venue_order_id=VenueOrderId(str(trade.venue_order_id)),
                trade_id=TradeId(str(trade.trade_id)),
                client_order_id=client_order_id,
                venue_position_id=None,
                order_side=OrderSide.BUY if trade.execution_qty > 0 else OrderSide.SELL,
                last_qty=last_qty,
                last_px=last_px,
                avg_px=trade.execution_price,
                commission=commission,
                liquidity_side=LiquiditySide.MAKER if trade.maker else LiquiditySide.TAKER,
                report_id=UUID4(),
                ts_event=trade.ts_event_ms * 1_000_000,
                ts_init=ts_init,
            )
        )
    return reports


def map_position_status_reports(
    *,
    rows: list[object],
    instrument: Instrument,
    account_id: AccountId,
    ts_init: int,
) -> list[PositionStatusReport]:
    """Map the one supported NETTING derivative position, including explicit flat."""
    _timestamp(ts_init, "ts_init")
    by_position: dict[int, list[object]] = {}
    for value in rows:
        row = _row(value, "position", minimum=1)
        if _text(row[0], "position.symbol") != instrument.raw_symbol.value:
            continue
        row = _row(row, "position", minimum=16)
        position_id = _positive_int(row[11], "position.id")
        previous = by_position.get(position_id)
        if previous is not None and previous != row:
            raise BitfinexV1ReportError("duplicate Bitfinex position ID changed its facts")
        by_position[position_id] = row
    if not by_position:
        return [
            PositionStatusReport(
                account_id=account_id,
                instrument_id=instrument.id,
                position_side=PositionSide.FLAT,
                quantity=_quantity(instrument, Decimal(0), "flat position quantity"),
                venue_position_id=None,
                report_id=UUID4(),
                ts_last=ts_init,
                ts_init=ts_init,
            )
        ]
    if len(by_position) != 1:
        raise BitfinexV1ReportError("Bitfinex v1 requires one NETTING position per symbol")

    row = next(iter(by_position.values()))
    symbol = _text(row[0], "position.symbol")
    _exact_symbol(symbol, instrument)
    if _text(row[1], "position.status") != "ACTIVE":
        raise BitfinexV1ReportError("Bitfinex position must be ACTIVE")
    if _exact_int(row[15], "position.type") != 1:
        raise BitfinexV1ReportError("Bitfinex position must be derivative type 1")
    amount = _decimal(row[2], "position.amount")
    if amount == 0:
        raise BitfinexV1ReportError("a zero Bitfinex position must be represented by an empty list")
    base_price = _decimal(row[3], "position.base_price")
    if base_price <= 0:
        raise BitfinexV1ReportError("Bitfinex position base price must be positive")
    venue_timestamp = row[13] if row[13] is not None else row[12]
    ts_last = (
        ts_init
        if venue_timestamp is None
        else _timestamp(venue_timestamp, "position timestamp") * 1_000_000
    )
    return [
        PositionStatusReport(
            account_id=account_id,
            instrument_id=instrument.id,
            position_side=PositionSide.LONG if amount > 0 else PositionSide.SHORT,
            quantity=_quantity(instrument, abs(amount), "position quantity"),
            venue_position_id=None,
            avg_px_open=base_price,
            report_id=UUID4(),
            ts_last=ts_last,
            ts_init=ts_init,
        )
    ]


def _order_snapshot(rows: list[object]) -> tuple[OrderState, ...]:
    snapshot = parse_private_message([0, "os", rows])
    if not isinstance(snapshot, OrderSnapshot):  # pragma: no cover - parser grammar guard
        raise BitfinexV1ReportError("Bitfinex order parser returned the wrong event")
    return snapshot.orders


def _owned_client_id(cid: int | None, lookup: CidLookup) -> ClientOrderId | None:
    if cid is None:
        return None
    result = lookup(cid)
    if result is not None and not isinstance(result, ClientOrderId):
        raise BitfinexV1ReportError("CID lookup must return ClientOrderId or None")
    return result


def _order_report(
    state: OrderState,
    *,
    is_active: bool,
    instrument: Instrument,
    account_id: AccountId,
    client_order_id: ClientOrderId,
    ts_init: int,
) -> OrderStatusReport:
    _exact_symbol(state.symbol, instrument)
    if state.flags not in {0, POST_ONLY_FLAG, REDUCE_ONLY_FLAG}:
        raise BitfinexV1ReportError(f"unsupported Bitfinex order flags {state.flags}")
    if state.order_type == "LIMIT":
        time_in_force = TimeInForce.GTC
    elif state.order_type == "IOC":
        time_in_force = TimeInForce.IOC
    else:
        raise BitfinexV1ReportError(f"unsupported Bitfinex order type {state.order_type!r}")
    if state.flags == POST_ONLY_FLAG and time_in_force != TimeInForce.GTC:
        raise BitfinexV1ReportError("post-only Bitfinex order must be LIMIT/GTC")
    if state.flags == REDUCE_ONLY_FLAG and time_in_force != TimeInForce.IOC:
        raise BitfinexV1ReportError("reduce-only Bitfinex order must be LIMIT/IOC")
    if state.tif_expiry_ms is not None:
        raise BitfinexV1ReportError("Bitfinex v1 does not support expiring orders")
    if state.original_qty == 0:
        raise BitfinexV1ReportError("Bitfinex original order quantity must be non-zero")
    if state.remaining_qty != 0 and (state.remaining_qty > 0) != (state.original_qty > 0):
        raise BitfinexV1ReportError("Bitfinex remaining quantity changed order side")
    quantity_value = abs(state.original_qty)
    remaining_value = abs(state.remaining_qty)
    if remaining_value > quantity_value:
        raise BitfinexV1ReportError("Bitfinex remaining quantity exceeds original quantity")
    filled_value = quantity_value - remaining_value
    order_status = _order_status(
        state.status,
        is_active=is_active,
        filled=filled_value,
        total=quantity_value,
    )
    quantity = _quantity(instrument, quantity_value, "order quantity")
    filled_qty = _quantity(instrument, filled_value, "order filled quantity")
    price = _price(instrument, state.price, "order price")
    if filled_value == 0:
        if state.average_price != 0:
            raise BitfinexV1ReportError("unfilled Bitfinex order has a non-zero average price")
        avg_px = None
    else:
        if state.average_price <= 0:
            raise BitfinexV1ReportError("filled Bitfinex order has no positive average price")
        avg_px = state.average_price
    ts_accepted = _timestamp(state.ts_created_ms, "order.mts_create") * 1_000_000
    ts_last = _timestamp(state.ts_updated_ms, "order.mts_update") * 1_000_000
    if ts_last < ts_accepted:
        raise BitfinexV1ReportError("Bitfinex order update predates creation")
    return OrderStatusReport(
        account_id=account_id,
        instrument_id=instrument.id,
        venue_order_id=VenueOrderId(str(state.venue_order_id)),
        client_order_id=client_order_id,
        venue_position_id=None,
        order_side=OrderSide.BUY if state.original_qty > 0 else OrderSide.SELL,
        order_type=OrderType.LIMIT,
        time_in_force=time_in_force,
        order_status=order_status,
        quantity=quantity,
        filled_qty=filled_qty,
        price=price,
        avg_px=avg_px,
        post_only=state.flags == POST_ONLY_FLAG,
        reduce_only=state.flags == REDUCE_ONLY_FLAG,
        cancel_reason=(
            state.status if order_status in {OrderStatus.CANCELED, OrderStatus.REJECTED} else None
        ),
        report_id=UUID4(),
        ts_accepted=ts_accepted,
        ts_last=ts_last,
        ts_init=ts_init,
    )


def _order_status(status: str, *, is_active: bool, filled: Decimal, total: Decimal) -> OrderStatus:
    upper = status.upper()
    if is_active:
        if upper == "ACTIVE":
            if filled == 0:
                return OrderStatus.ACCEPTED
            if filled < total:
                return OrderStatus.PARTIALLY_FILLED
        if upper.startswith("PARTIALLY FILLED") and Decimal(0) < filled < total:
            return OrderStatus.PARTIALLY_FILLED
        raise BitfinexV1ReportError(f"inconsistent active Bitfinex status {status!r}")
    if upper.startswith(("EXECUTED", "FORCED EXECUTED")):
        if filled == total:
            return OrderStatus.FILLED
        raise BitfinexV1ReportError("executed Bitfinex order is not fully filled")
    canceled = upper.startswith(
        ("CANCELED", "IOC CANCELED", "FOK CANCELED", "PARTIALLY FILLED", "RSN")
    )
    rejected = upper.startswith(
        ("POSTONLY CANCELED", "INSUFFICIENT MARGIN", "INSUFFICIENT BALANCE")
    )
    if rejected and filled == 0:
        return OrderStatus.REJECTED
    if (canceled or rejected) and filled < total:
        return OrderStatus.CANCELED
    raise BitfinexV1ReportError(f"unsupported historical Bitfinex status {status!r}")


def _exact_symbol(symbol: str, instrument: Instrument) -> None:
    if symbol != instrument.raw_symbol.value:
        raise BitfinexV1ReportError(f"unexpected Bitfinex symbol {symbol!r}")


def _quantity(instrument: Instrument, value: Decimal, label: str) -> Quantity:
    try:
        quantity = instrument.make_qty(value)
    except ValueError as exc:
        raise BitfinexV1ReportError(f"{label} loses instrument precision") from exc
    if quantity.as_decimal() != value:
        raise BitfinexV1ReportError(f"{label} loses instrument precision")
    return quantity


def _price(instrument: Instrument, value: Decimal, label: str) -> Price:
    if value <= 0:
        raise BitfinexV1ReportError(f"{label} must be positive")
    try:
        price = instrument.make_price(value)
    except ValueError as exc:
        raise BitfinexV1ReportError(f"{label} loses instrument precision") from exc
    if price.as_decimal() != value:
        raise BitfinexV1ReportError(f"{label} loses instrument precision")
    return price


def _row(value: object, label: str, *, minimum: int) -> list[object]:
    if not isinstance(value, list) or len(value) < minimum:
        raise BitfinexV1ReportError(f"{label} row has too few fields")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BitfinexV1ReportError(f"{label} must be a non-empty string")
    return value


def _exact_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise BitfinexV1ReportError(f"{label} must be an exact integer")
    return value


def _positive_int(value: object, label: str) -> int:
    parsed = _exact_int(value, label)
    if parsed <= 0:
        raise BitfinexV1ReportError(f"{label} must be positive")
    return parsed


def _timestamp(value: object, label: str) -> int:
    parsed = _exact_int(value, label)
    if parsed < 0:
        raise BitfinexV1ReportError(f"{label} must be non-negative")
    return parsed


def _decimal(value: object, label: str) -> Decimal:
    if type(value) is int:
        parsed = Decimal(value)
    elif isinstance(value, Decimal):
        parsed = value
    else:
        raise BitfinexV1ReportError(f"{label} must be an exact JSON number")
    if not parsed.is_finite():
        raise BitfinexV1ReportError(f"{label} must be finite")
    return parsed


__all__ = [
    "BitfinexV1ReportError",
    "CidLookup",
    "map_fill_reports",
    "map_order_status_reports",
    "map_position_status_reports",
]
