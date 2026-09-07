"""Strict, transport-free Bitfinex private WebSocket v2 protocol slice."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, cast

AUTH_CHANNEL = 0
REDUCE_ONLY_FLAG = 1024
POST_ONLY_FLAG = 4096
MAX_CID = 2**45 - 1
MAX_AUTH_NONCE = 9_007_199_254_740_991

type OrderOperation = Literal["on", "ou", "oc"]
type NotificationRequestType = Literal["on-req", "ou-req", "oc-req"]
type WalletMessageType = Literal["ws", "wu"]
type PositionMessageType = Literal["ps", "pn", "pu", "pc"]


class BitfinexV1ProtocolError(ValueError):
    """A private-wire frame is outside the deliberately small v1 grammar."""


@dataclass(frozen=True, slots=True)
class OrderState:
    venue_order_id: int
    group_id: int | None
    client_order_id: int | None
    symbol: str
    ts_created_ms: int
    ts_updated_ms: int
    remaining_qty: Decimal
    original_qty: Decimal
    order_type: str
    previous_order_type: str | None
    tif_expiry_ms: int | None
    flags: int
    status: str
    price: Decimal
    average_price: Decimal
    post_only_meta: bool | None = None

    @property
    def effective_flags(self) -> int:
        """META retains post-only intent when the venue clears its active flag."""
        return self.flags | (POST_ONLY_FLAG if self.post_only_meta else 0)


@dataclass(frozen=True, slots=True)
class OrderEvent:
    operation: OrderOperation
    order: OrderState


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    orders: tuple[OrderState, ...]


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    trade_id: int
    symbol: str
    ts_event_ms: int
    venue_order_id: int
    execution_qty: Decimal
    execution_price: Decimal
    order_type: str
    order_price: Decimal
    maker: bool
    fee: Decimal
    fee_currency: str
    client_order_id: int | None


@dataclass(frozen=True, slots=True)
class TradeExecution:
    """Fee-pending execution facts from Bitfinex's low-latency ``te`` event."""

    trade_id: int
    symbol: str
    ts_event_ms: int
    venue_order_id: int
    execution_qty: Decimal
    execution_price: Decimal
    order_type: str
    order_price: Decimal
    maker: bool
    client_order_id: int | None


@dataclass(frozen=True, slots=True)
class Notification:
    ts_event_ms: int
    request_type: NotificationRequestType
    operation: OrderOperation
    message_id: int | None
    venue_order_id: int | None
    client_order_id: int | None
    code: int | None
    status: Literal["SUCCESS", "ERROR", "FAILURE"]
    text: str


@dataclass(frozen=True, slots=True)
class WalletBalance:
    wallet_type: str
    currency: str
    balance: Decimal
    unsettled_interest: Decimal | None
    available_balance: Decimal | None


@dataclass(frozen=True, slots=True)
class WalletEvent:
    message_type: WalletMessageType
    wallets: tuple[WalletBalance, ...]


@dataclass(frozen=True, slots=True)
class PositionState:
    symbol: str
    status: str
    quantity: Decimal
    base_price: Decimal | None
    margin_funding: Decimal | None
    margin_funding_type: int | None
    profit_loss: Decimal | None
    profit_loss_pct: Decimal | None
    liquidation_price: Decimal | None
    leverage: Decimal | None
    position_id: int | None
    ts_created_ms: int | None = None
    ts_updated_ms: int | None = None
    position_type: int | None = None
    collateral: Decimal | None = None
    collateral_min: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PositionEvent:
    message_type: PositionMessageType
    positions: tuple[PositionState, ...]


type PrivateMessage = (
    OrderEvent | OrderSnapshot | TradeUpdate | Notification | WalletEvent | PositionEvent
)


def auth_message(api_key: str, api_secret: str, *, nonce: int) -> dict[str, object]:
    """Build the deterministic HMAC-SHA384 private-channel authentication frame."""
    key = _text(api_key, "api_key")
    secret = _text(api_secret, "api_secret")
    auth_nonce = _bounded_positive_int(nonce, "nonce", maximum=MAX_AUTH_NONCE)
    payload = f"AUTH{auth_nonce}"
    signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha384,
    ).hexdigest()
    return {
        "event": "auth",
        "apiKey": key,
        "authSig": signature,
        "authPayload": payload,
        "authNonce": str(auth_nonce),
    }


def submit_order_op(
    *,
    symbol: str,
    amount: Decimal,
    price: Decimal,
    cid: int,
    order_type: Literal["LIMIT", "IOC"],
    leverage: int | None = None,
    post_only: bool = False,
    reduce_only: bool = False,
) -> list[object]:
    """Build ``on`` for the two source-leg forms used by PY000."""
    raw_symbol = _text(symbol, "symbol")
    signed_amount = _decimal_argument(amount, "amount", nonzero=True)
    limit_price = _decimal_argument(price, "price", positive=True)
    client_order_id = _bounded_positive_int(cid, "cid", maximum=MAX_CID)
    if order_type not in {"LIMIT", "IOC"}:
        raise BitfinexV1ProtocolError("order_type must be LIMIT or IOC")
    if type(post_only) is not bool:
        raise BitfinexV1ProtocolError("post_only must be an exact bool")
    if type(reduce_only) is not bool:
        raise BitfinexV1ProtocolError("reduce_only must be an exact bool")
    if post_only and reduce_only:
        raise BitfinexV1ProtocolError("post-only and reduce-only are mutually exclusive")
    if post_only and order_type != "LIMIT":
        raise BitfinexV1ProtocolError("post-only is valid only for the LIMIT source order")
    if reduce_only and order_type != "IOC":
        raise BitfinexV1ProtocolError("reduce-only is valid only for an IOC order")

    payload: dict[str, object] = {
        "type": order_type,
        "symbol": raw_symbol,
        "amount": format(signed_amount, "f"),
        "price": format(limit_price, "f"),
        "cid": client_order_id,
    }
    if leverage is not None:
        payload["lev"] = _leverage(leverage)
    if post_only:
        payload["flags"] = POST_ONLY_FLAG
    elif reduce_only:
        payload["flags"] = REDUCE_ONLY_FLAG
    return [AUTH_CHANNEL, "on", None, payload]


def update_order_op(
    *,
    venue_order_id: int,
    leverage: int,
    price: Decimal | None = None,
    amount: Decimal | None = None,
) -> list[object]:
    """Build derivative ``ou`` with its mandatory per-order leverage."""
    if price is None and amount is None:
        raise BitfinexV1ProtocolError("update requires price or amount")
    payload: dict[str, object] = {
        "id": _positive_int(venue_order_id, "venue_order_id"),
        "lev": _leverage(leverage),
    }
    if price is not None:
        payload["price"] = format(_decimal_argument(price, "price", positive=True), "f")
    if amount is not None:
        payload["amount"] = format(_decimal_argument(amount, "amount", nonzero=True), "f")
    return [AUTH_CHANNEL, "ou", None, payload]


def cancel_order_op(*, venue_order_id: int) -> list[object]:
    """Build ``oc`` using the venue-native order identity."""
    return [
        AUTH_CHANNEL,
        "oc",
        None,
        {"id": _positive_int(venue_order_id, "venue_order_id")},
    ]


def parse_private_message(message: object) -> PrivateMessage:
    """Parse one supported authenticated channel-0 event without numeric coercion."""
    frame = _array(message, "private message", exact=3)
    if _exact_int(frame[0], "channel") != AUTH_CHANNEL:
        raise BitfinexV1ProtocolError("private message must use channel 0")
    message_type = _text(frame[1], "message type")
    payload = frame[2]

    if message_type in {"on", "ou", "oc"}:
        operation = cast(OrderOperation, message_type)
        return OrderEvent(operation, _parse_order(payload))
    if message_type == "os":
        rows = _array(payload, "order snapshot")
        return OrderSnapshot(tuple(_parse_order(row) for row in rows))
    if message_type == "tu":
        return _parse_trade(payload)
    if message_type == "n":
        return _parse_notification(payload)
    if message_type in {"ws", "wu"}:
        wallet_kind = cast(WalletMessageType, message_type)
        rows = _array(payload, "wallet snapshot") if wallet_kind == "ws" else [payload]
        return WalletEvent(wallet_kind, tuple(_parse_wallet(row) for row in rows))
    if message_type in {"ps", "pn", "pu", "pc"}:
        position_kind = cast(PositionMessageType, message_type)
        rows = _array(payload, "position snapshot") if position_kind == "ps" else [payload]
        return PositionEvent(position_kind, tuple(_parse_position(row) for row in rows))
    raise BitfinexV1ProtocolError(f"unsupported private message type {message_type!r}")


def parse_interim_trade_message(message: object) -> TradeExecution:
    """Parse the fee-pending execution facts carried by a channel-0 ``te`` event."""
    frame = _array(message, "interim trade message", exact=3)
    if _exact_int(frame[0], "channel") != AUTH_CHANNEL or frame[1] != "te":
        raise BitfinexV1ProtocolError("interim trade message must be channel-0 te")
    row = _array(frame[2], "interim trade", minimum=12)
    maker_value = _exact_int(row[8], "trade.maker")
    if maker_value not in {-1, 1}:
        raise BitfinexV1ProtocolError("trade.maker must be exact -1 or 1")
    if row[9] is not None or row[10] is not None:
        raise BitfinexV1ProtocolError("interim trade must not contain final fee facts")
    execution_qty = _decimal(row[4], "trade.exec_amount")
    execution_price = _decimal(row[5], "trade.exec_price")
    if execution_qty == 0 or execution_price <= 0:
        raise BitfinexV1ProtocolError(
            "trade quantity must be non-zero and execution price positive"
        )
    return TradeExecution(
        trade_id=_positive_int(row[0], "trade.id"),
        symbol=_text(row[1], "trade.symbol"),
        ts_event_ms=_nonnegative_int(row[2], "trade.mts_create"),
        venue_order_id=_positive_int(row[3], "trade.order_id"),
        execution_qty=execution_qty,
        execution_price=execution_price,
        order_type=_text(row[6], "trade.order_type"),
        order_price=_decimal(row[7], "trade.order_price"),
        maker=maker_value == 1,
        client_order_id=_optional_te_cid(row[11], "trade.cid"),
    )


def validate_interim_trade_message(message: object) -> None:
    """Validate a ``te`` frame for callers which do not consume its execution facts."""
    parse_interim_trade_message(message)


def _parse_order(value: object) -> OrderState:
    row = _array(value, "order row", minimum=18)
    flags = _nonnegative_int(row[12], "order.flags")
    return OrderState(
        venue_order_id=_positive_int(row[0], "order.id"),
        group_id=_optional_int(row[1], "order.gid"),
        client_order_id=_optional_cid(row[2], "order.cid"),
        symbol=_text(row[3], "order.symbol"),
        ts_created_ms=_nonnegative_int(row[4], "order.mts_create"),
        ts_updated_ms=_nonnegative_int(row[5], "order.mts_update"),
        remaining_qty=_decimal(row[6], "order.amount"),
        original_qty=_decimal(row[7], "order.amount_orig"),
        order_type=_text(row[8], "order.type"),
        previous_order_type=_optional_text(row[9], "order.type_prev"),
        tif_expiry_ms=_optional_nonnegative_int(row[10], "order.mts_tif"),
        flags=flags,
        status=_text(row[13], "order.status"),
        price=_decimal(row[16], "order.price"),
        average_price=_decimal(row[17], "order.price_avg"),
        post_only_meta=_order_post_only_meta(row, flags),
    )


def _order_post_only_meta(row: list[object], flags: int) -> bool | None:
    if len(row) <= 31 or row[31] is None:
        return None
    meta = row[31]
    if not isinstance(meta, dict):
        raise BitfinexV1ProtocolError("order.meta must be a JSON object or null")
    observed: int | None = None
    # $F7 is documented; _$F7 is the observed wire spelling. Other META is unrelated.
    for key in ("$F7", "_$F7"):
        if key not in meta:
            continue
        value = _exact_int(meta[key], f"order.meta.{key}")
        if value not in {0, 1}:
            raise BitfinexV1ProtocolError("order.meta post-only must be 0 or 1")
        if observed is not None and observed != value:
            raise BitfinexV1ProtocolError("order.meta post-only aliases conflict")
        observed = value
    if observed == 0 and flags & POST_ONLY_FLAG:
        raise BitfinexV1ProtocolError("order.meta post-only conflicts with active flag")
    return None if observed is None else bool(observed)


def _parse_trade(value: object) -> TradeUpdate:
    row = _array(value, "trade update", minimum=12)
    maker_value = _exact_int(row[8], "trade.maker")
    if maker_value not in {-1, 1}:
        raise BitfinexV1ProtocolError("trade.maker must be exact -1 or 1")
    execution_qty = _decimal(row[4], "trade.exec_amount")
    execution_price = _decimal(row[5], "trade.exec_price")
    if execution_qty == 0 or execution_price <= 0:
        raise BitfinexV1ProtocolError(
            "trade quantity must be non-zero and execution price positive"
        )
    return TradeUpdate(
        trade_id=_positive_int(row[0], "trade.id"),
        symbol=_text(row[1], "trade.symbol"),
        ts_event_ms=_nonnegative_int(row[2], "trade.mts_create"),
        venue_order_id=_positive_int(row[3], "trade.order_id"),
        execution_qty=execution_qty,
        execution_price=execution_price,
        order_type=_text(row[6], "trade.order_type"),
        order_price=_decimal(row[7], "trade.order_price"),
        maker=maker_value == 1,
        fee=_decimal(row[9], "trade.fee"),
        fee_currency=_text(row[10], "trade.fee_currency"),
        client_order_id=_optional_cid(row[11], "trade.cid"),
    )


def _parse_notification(value: object) -> Notification:
    row = _array(value, "notification", minimum=8)
    request_type_value = _text(row[1], "notification.type")
    operation_by_type: dict[str, OrderOperation] = {
        "on-req": "on",
        "ou-req": "ou",
        "oc-req": "oc",
    }
    try:
        operation = operation_by_type[request_type_value]
    except KeyError as exc:
        raise BitfinexV1ProtocolError(
            f"unsupported notification type {request_type_value!r}"
        ) from exc
    status_value = _text(row[6], "notification.status").upper()
    if status_value not in {"SUCCESS", "ERROR", "FAILURE"}:
        raise BitfinexV1ProtocolError("notification.status is not recognized")
    venue_order_id, client_order_id = _notification_ids(row[4])
    return Notification(
        ts_event_ms=_nonnegative_int(row[0], "notification.mts"),
        request_type=cast(NotificationRequestType, request_type_value),
        operation=operation,
        message_id=_optional_int(row[2], "notification.message_id"),
        venue_order_id=venue_order_id,
        client_order_id=client_order_id,
        code=_optional_int(row[5], "notification.code"),
        status=cast(Literal["SUCCESS", "ERROR", "FAILURE"], status_value),
        text=_text(row[7], "notification.text", allow_empty=True),
    )


def _notification_ids(value: object) -> tuple[int | None, int | None]:
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise BitfinexV1ProtocolError("notification.info keys must be strings")
        return (
            _optional_positive_int(value.get("id"), "notification.info.id"),
            _optional_cid(value.get("cid"), "notification.info.cid"),
        )
    row = _array(value, "notification.info", minimum=3)
    return (
        _optional_positive_int(row[0], "notification.info.id"),
        _optional_cid(row[2], "notification.info.cid"),
    )


def _parse_wallet(value: object) -> WalletBalance:
    row = _array(value, "wallet row", minimum=5)
    return WalletBalance(
        wallet_type=_text(row[0], "wallet.type"),
        currency=_text(row[1], "wallet.currency"),
        balance=_decimal(row[2], "wallet.balance"),
        unsettled_interest=_optional_decimal(row[3], "wallet.unsettled_interest"),
        available_balance=_optional_decimal(row[4], "wallet.available_balance"),
    )


def _parse_position(value: object) -> PositionState:
    row = _array(value, "position row", minimum=12)
    return PositionState(
        symbol=_text(row[0], "position.symbol"),
        status=_text(row[1], "position.status"),
        quantity=_decimal(row[2], "position.amount"),
        base_price=_optional_decimal(row[3], "position.base_price"),
        margin_funding=_optional_decimal(row[4], "position.margin_funding"),
        margin_funding_type=_optional_int(row[5], "position.margin_funding_type"),
        profit_loss=_optional_decimal(row[6], "position.profit_loss"),
        profit_loss_pct=_optional_decimal(row[7], "position.profit_loss_pct"),
        liquidation_price=_optional_decimal(row[8], "position.liquidation_price"),
        leverage=_optional_decimal(row[9], "position.leverage"),
        position_id=_optional_int(row[11], "position.id"),
        ts_created_ms=_margin_extension_int(row, 12),
        ts_updated_ms=_margin_extension_int(row, 13),
        position_type=_margin_extension_int(row, 15),
        collateral=_margin_extension_decimal(row, 17),
        collateral_min=_margin_extension_decimal(row, 18),
    )


def _margin_extension_int(row: list[object], index: int) -> int | None:
    value = row[index] if len(row) > index else None
    return value if type(value) is int and value >= 0 else None


def _margin_extension_decimal(row: list[object], index: int) -> Decimal | None:
    # Auxiliary capacity metadata must not break delivery of known execution facts.
    try:
        return _optional_decimal(row[index], "position.margin") if len(row) > index else None
    except BitfinexV1ProtocolError:
        return None


def _array(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
    exact: int | None = None,
) -> list[object]:
    if not isinstance(value, list):
        raise BitfinexV1ProtocolError(f"{label} must be an array")
    if exact is not None and len(value) != exact:
        raise BitfinexV1ProtocolError(f"{label} must contain exactly {exact} fields")
    if minimum is not None and len(value) < minimum:
        raise BitfinexV1ProtocolError(f"{label} has too few fields")
    return cast(list[object], value)


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise BitfinexV1ProtocolError(f"{label} must be a string")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _exact_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise BitfinexV1ProtocolError(f"{label} must be an exact integer")
    return value


def _positive_int(value: object, label: str) -> int:
    parsed = _exact_int(value, label)
    if parsed <= 0:
        raise BitfinexV1ProtocolError(f"{label} must be positive")
    return parsed


def _bounded_positive_int(value: object, label: str, *, maximum: int) -> int:
    parsed = _positive_int(value, label)
    if parsed > maximum:
        raise BitfinexV1ProtocolError(f"{label} exceeds {maximum}")
    return parsed


def _leverage(value: object) -> int:
    parsed = _exact_int(value, "leverage")
    if not 1 <= parsed <= 100:
        raise BitfinexV1ProtocolError("leverage must be between 1 and 100")
    return parsed


def _nonnegative_int(value: object, label: str) -> int:
    parsed = _exact_int(value, label)
    if parsed < 0:
        raise BitfinexV1ProtocolError(f"{label} must be non-negative")
    return parsed


def _optional_int(value: object, label: str) -> int | None:
    return None if value is None else _exact_int(value, label)


def _optional_positive_int(value: object, label: str) -> int | None:
    return None if value is None else _positive_int(value, label)


def _optional_cid(value: object, label: str) -> int | None:
    return None if value is None else _bounded_positive_int(value, label, maximum=MAX_CID)


def _optional_te_cid(value: object, label: str) -> int | None:
    if value is None:
        return None
    parsed = _nonnegative_int(value, label)
    if parsed > MAX_CID:
        raise BitfinexV1ProtocolError(f"{label} exceeds maximum {MAX_CID}")
    return None if parsed == 0 else parsed


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    return None if value is None else _nonnegative_int(value, label)


def _decimal(value: object, label: str) -> Decimal:
    if type(value) is int:
        parsed = Decimal(value)
    elif isinstance(value, Decimal):
        parsed = value
    else:
        raise BitfinexV1ProtocolError(f"{label} must be an exact JSON number")
    if not parsed.is_finite():
        raise BitfinexV1ProtocolError(f"{label} must be finite")
    return parsed


def _optional_decimal(value: object, label: str) -> Decimal | None:
    return None if value is None else _decimal(value, label)


def _decimal_argument(
    value: object,
    label: str,
    *,
    positive: bool = False,
    nonzero: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BitfinexV1ProtocolError(f"{label} must be a finite Decimal")
    if positive and value <= 0:
        raise BitfinexV1ProtocolError(f"{label} must be positive")
    if nonzero and value == 0:
        raise BitfinexV1ProtocolError(f"{label} must be non-zero")
    return value


__all__ = [
    "AUTH_CHANNEL",
    "MAX_AUTH_NONCE",
    "MAX_CID",
    "POST_ONLY_FLAG",
    "REDUCE_ONLY_FLAG",
    "BitfinexV1ProtocolError",
    "Notification",
    "OrderEvent",
    "OrderSnapshot",
    "OrderState",
    "PositionEvent",
    "PositionState",
    "PrivateMessage",
    "TradeExecution",
    "TradeUpdate",
    "WalletBalance",
    "WalletEvent",
    "auth_message",
    "cancel_order_op",
    "parse_interim_trade_message",
    "parse_private_message",
    "submit_order_op",
    "update_order_op",
    "validate_interim_trade_message",
]
