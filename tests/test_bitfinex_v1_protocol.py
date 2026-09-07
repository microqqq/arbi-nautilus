from __future__ import annotations

import hashlib
import hmac
from copy import deepcopy
from decimal import Decimal

import pytest

from py000_nautilus.bitfinex_v1_protocol import (
    MAX_AUTH_NONCE,
    MAX_CID,
    POST_ONLY_FLAG,
    REDUCE_ONLY_FLAG,
    BitfinexV1ProtocolError,
    Notification,
    OrderEvent,
    OrderSnapshot,
    PositionEvent,
    TradeExecution,
    TradeUpdate,
    WalletEvent,
    auth_message,
    cancel_order_op,
    parse_interim_trade_message,
    parse_private_message,
    submit_order_op,
    update_order_op,
    validate_interim_trade_message,
)

SYMBOL = "tXAUTF0:USTF0"


def order_row(*, status: str = "ACTIVE") -> list[object]:
    return [
        219492782587,
        None,
        1557225713,
        SYMBOL,
        1_788_282_000_000,
        1_788_282_000_010,
        Decimal("1.5"),
        Decimal("4.0"),
        "LIMIT",
        None,
        None,
        None,
        POST_ONLY_FLAG,
        status,
        None,
        None,
        Decimal("3926.7"),
        Decimal("3926.6"),
    ]


def position_row() -> list[object]:
    return [
        SYMBOL,
        "ACTIVE",
        Decimal("-0.75"),
        Decimal("4050.1"),
        Decimal("0"),
        0,
        Decimal("1.25"),
        Decimal("0.0004"),
        Decimal("7000"),
        Decimal("10"),
        None,
        991,
    ]


@pytest.mark.parametrize("flags,meta,expected_meta,effective", [
    (0, None, None, 0), (POST_ONLY_FLAG, {}, None, POST_ONLY_FLAG),
    (0, {"$F7": 0}, False, 0), (0, {"_$F7": 0}, False, 0),
    (0, {"$F7": 1}, True, POST_ONLY_FLAG), (0, {"_$F7": 1}, True, POST_ONLY_FLAG),
    (POST_ONLY_FLAG, {"$F7": 1, "_$F7": 1}, True, POST_ONLY_FLAG),
    (0, {"$F7": 0, "_$F7": 0, "lev": 16}, False, 0),
    (REDUCE_ONLY_FLAG, {"_$F7": 1}, True, REDUCE_ONLY_FLAG | POST_ONLY_FLAG),
    (8192, {"$F7": 1}, True, 8192 | POST_ONLY_FLAG),
])
def test_order_metadata_preserves_raw_flags_and_explicit_post_only_semantics(
    flags: int, meta: object, expected_meta: bool | None, effective: int,
) -> None:
    row = order_row()
    row[12] = flags
    row.extend([None] * (31 - len(row)))
    row.append(meta)
    original = deepcopy(row)
    event = parse_private_message([0, "on", row])
    assert isinstance(event, OrderEvent)
    assert event.order.flags == flags and event.order.effective_flags == effective
    assert event.order.post_only_meta is expected_meta
    assert row == original


@pytest.mark.parametrize("meta", [
    [], "{}", False, 1, {"$F7": None}, {"_$F7": True}, {"$F7": False},
    {"$F7": "1"}, {"$F7": 1.0}, {"$F7": Decimal(1)}, {"$F7": -1}, {"$F7": 2},
    {"$F7": 0, "_$F7": 1}, {"$F7": 1, "_$F7": 0},
])
def test_order_metadata_rejects_malformed_or_conflicting_explicit_values(meta: object) -> None:
    row = order_row()
    row[12] = 0
    row.extend([None] * (31 - len(row)))
    row.append(meta)
    with pytest.raises(BitfinexV1ProtocolError, match="order.meta"):
        parse_private_message([0, "on", row])


@pytest.mark.parametrize("key", ["$F7", "_$F7"])
def test_explicit_post_only_denial_cannot_conflict_with_active_bit(key: str) -> None:
    row = order_row()
    row.extend([None] * (31 - len(row)))
    row.append({key: 0})
    with pytest.raises(BitfinexV1ProtocolError, match="conflicts with active flag"):
        parse_private_message([0, "on", row])


def test_auth_message_uses_hmac_sha384_and_does_not_expose_secret() -> None:
    nonce = 1_700_000_000_000_000
    message = auth_message("KEY", "SECRET", nonce=nonce)

    expected = hmac.new(
        b"SECRET",
        f"AUTH{nonce}".encode(),
        hashlib.sha384,
    ).hexdigest()
    assert message == {
        "event": "auth",
        "apiKey": "KEY",
        "authSig": expected,
        "authPayload": f"AUTH{nonce}",
        "authNonce": str(nonce),
    }
    assert "SECRET" not in repr(message)


def test_auth_nonce_is_bounded_by_the_json_safe_integer_limit() -> None:
    auth_message("KEY", "SECRET", nonce=MAX_AUTH_NONCE)
    with pytest.raises(BitfinexV1ProtocolError, match="exceeds"):
        auth_message("KEY", "SECRET", nonce=MAX_AUTH_NONCE + 1)


def test_maker_submit_is_limit_gtc_shape_with_post_only_and_per_order_leverage() -> None:
    message = submit_order_op(
        symbol=SYMBOL,
        amount=Decimal("-2.700"),
        price=Decimal("4012.30"),
        cid=123456,
        order_type="LIMIT",
        leverage=17,
        post_only=True,
    )

    assert message == [
        0,
        "on",
        None,
        {
            "type": "LIMIT",
            "symbol": SYMBOL,
            "amount": "-2.700",
            "price": "4012.30",
            "cid": 123456,
            "lev": 17,
            "flags": 4096,
        },
    ]


def test_taker_submit_is_ioc_and_has_no_implicit_post_only_flag() -> None:
    message = submit_order_op(
        symbol=SYMBOL,
        amount=Decimal("2"),
        price=Decimal("4013.1"),
        cid=123457,
        order_type="IOC",
        leverage=9,
    )

    assert message[3] == {
        "type": "IOC",
        "symbol": SYMBOL,
        "amount": "2",
        "price": "4013.1",
        "cid": 123457,
        "lev": 9,
    }


def test_reduce_only_submit_is_exact_ioc_flag() -> None:
    message = submit_order_op(
        symbol=SYMBOL,
        amount=Decimal("-2"),
        price=Decimal("4012.3"),
        cid=123458,
        order_type="IOC",
        leverage=9,
        reduce_only=True,
    )

    assert message[3] == {
        "type": "IOC",
        "symbol": SYMBOL,
        "amount": "-2",
        "price": "4012.3",
        "cid": 123458,
        "lev": 9,
        "flags": REDUCE_ONLY_FLAG,
    }


def test_submit_rejects_float_zero_and_post_only_ioc() -> None:
    with pytest.raises(BitfinexV1ProtocolError, match="Decimal"):
        submit_order_op(
            symbol=SYMBOL,
            amount=1.0,  # type: ignore[arg-type]
            price=Decimal("1"),
            cid=1,
            order_type="LIMIT",
        )
    with pytest.raises(BitfinexV1ProtocolError, match="non-zero"):
        submit_order_op(
            symbol=SYMBOL,
            amount=Decimal("0"),
            price=Decimal("1"),
            cid=1,
            order_type="LIMIT",
        )
    with pytest.raises(BitfinexV1ProtocolError, match="post-only"):
        submit_order_op(
            symbol=SYMBOL,
            amount=Decimal("1"),
            price=Decimal("1"),
            cid=1,
            order_type="IOC",
            post_only=True,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"post_only": True, "reduce_only": True}, "mutually exclusive"),
        ({"reduce_only": True, "order_type": "LIMIT"}, "only for an IOC"),
        ({"reduce_only": 1}, "exact bool"),
    ],
)
def test_submit_rejects_invalid_reduce_only_semantics(
    kwargs: dict[str, object],
    message: str,
) -> None:
    arguments: dict[str, object] = {
        "symbol": SYMBOL,
        "amount": Decimal("-2"),
        "price": Decimal("4012.3"),
        "cid": 123459,
        "order_type": "IOC",
        **kwargs,
    }
    with pytest.raises(BitfinexV1ProtocolError, match=message):
        submit_order_op(**arguments)  # type: ignore[arg-type]


def test_submit_enforces_int45_cid_and_bitfinex_leverage_range() -> None:
    submit_order_op(
        symbol=SYMBOL,
        amount=Decimal("1"),
        price=Decimal("1"),
        cid=MAX_CID,
        order_type="IOC",
        leverage=100,
    )
    with pytest.raises(BitfinexV1ProtocolError, match="exceeds"):
        submit_order_op(
            symbol=SYMBOL,
            amount=Decimal("1"),
            price=Decimal("1"),
            cid=MAX_CID + 1,
            order_type="IOC",
        )
    with pytest.raises(BitfinexV1ProtocolError, match="between 1 and 100"):
        submit_order_op(
            symbol=SYMBOL,
            amount=Decimal("1"),
            price=Decimal("1"),
            cid=1,
            order_type="IOC",
            leverage=101,
        )


def test_native_update_and_cancel_keep_the_venue_order_id() -> None:
    assert update_order_op(
        venue_order_id=219492782587,
        leverage=17,
        price=Decimal("3925.10"),
        amount=Decimal("-3.0"),
    ) == [
        0,
        "ou",
        None,
        {"id": 219492782587, "lev": 17, "price": "3925.10", "amount": "-3.0"},
    ]
    assert cancel_order_op(venue_order_id=219492782587) == [
        0,
        "oc",
        None,
        {"id": 219492782587},
    ]


def test_empty_update_is_rejected() -> None:
    with pytest.raises(BitfinexV1ProtocolError, match="requires"):
        update_order_op(venue_order_id=1, leverage=10)


def test_update_requires_a_valid_derivative_leverage() -> None:
    with pytest.raises(BitfinexV1ProtocolError, match="between 1 and 100"):
        update_order_op(venue_order_id=1, leverage=0, price=Decimal("1"))


@pytest.mark.parametrize("operation", ["on", "ou", "oc"])
def test_order_events_preserve_decimal_values_and_native_ids(operation: str) -> None:
    event = parse_private_message([0, operation, order_row()])

    assert isinstance(event, OrderEvent)
    assert event.operation == operation
    assert event.order.venue_order_id == 219492782587
    assert event.order.client_order_id == 1557225713
    assert event.order.remaining_qty == Decimal("1.5")
    assert event.order.original_qty == Decimal("4.0")
    assert event.order.price == Decimal("3926.7")
    assert event.order.average_price == Decimal("3926.6")
    assert event.order.flags == POST_ONLY_FLAG


def test_order_snapshot_accepts_explicit_empty_and_nonempty_snapshots() -> None:
    empty = parse_private_message([0, "os", []])
    populated = parse_private_message([0, "os", [order_row()]])

    assert empty == OrderSnapshot(())
    assert isinstance(populated, OrderSnapshot)
    assert populated.orders[0].venue_order_id == 219492782587


def test_tu_is_the_authoritative_trade_shape_with_real_trade_and_order_ids() -> None:
    event = parse_private_message(
        [
            0,
            "tu",
            [
                1234,
                SYMBOL,
                1_700_000_000_123,
                9876,
                Decimal("-0.25"),
                Decimal("4001.5"),
                "LIMIT",
                Decimal("4001.4"),
                1,
                Decimal("-0.1"),
                "USD",
                4567,
            ],
        ]
    )

    assert event == TradeUpdate(
        trade_id=1234,
        symbol=SYMBOL,
        ts_event_ms=1_700_000_000_123,
        venue_order_id=9876,
        execution_qty=Decimal("-0.25"),
        execution_price=Decimal("4001.5"),
        order_type="LIMIT",
        order_price=Decimal("4001.4"),
        maker=True,
        fee=Decimal("-0.1"),
        fee_currency="USD",
        client_order_id=4567,
    )


def test_tu_uses_exact_one_and_minus_one_for_maker_taker() -> None:
    row = [
        1234,
        SYMBOL,
        1_700_000_000_123,
        9876,
        Decimal("0.25"),
        Decimal("4001.5"),
        "LIMIT",
        Decimal("4001.4"),
        -1,
        Decimal("-0.1"),
        "USD",
        4567,
    ]
    event = parse_private_message([0, "tu", row])
    assert isinstance(event, TradeUpdate)
    assert not event.maker
    for invalid in (0, True):
        row[8] = invalid
        with pytest.raises(BitfinexV1ProtocolError, match=r"integer|-1 or 1"):
            parse_private_message([0, "tu", row])


def test_te_preserves_execution_facts_but_normalizes_zero_cid_to_absent() -> None:
    row = [
        1234,
        SYMBOL,
        1_700_000_000_123,
        9876,
        Decimal("0.25"),
        Decimal("4001.5"),
        "LIMIT",
        Decimal("4001.4"),
        -1,
        None,
        None,
        0,
    ]
    execution = parse_interim_trade_message([0, "te", row])
    assert execution == TradeExecution(
        trade_id=1234,
        symbol=SYMBOL,
        ts_event_ms=1_700_000_000_123,
        venue_order_id=9876,
        execution_qty=Decimal("0.25"),
        execution_price=Decimal("4001.5"),
        order_type="LIMIT",
        order_price=Decimal("4001.4"),
        maker=False,
        client_order_id=None,
    )
    validate_interim_trade_message([0, "te", row])
    with pytest.raises(BitfinexV1ProtocolError, match="fee facts"):
        validate_interim_trade_message([0, "te", [*row[:9], Decimal("-0.1"), "USD", 0]])
    with pytest.raises(BitfinexV1ProtocolError):
        parse_private_message([0, "te", row])


@pytest.mark.parametrize(
    ("request_type", "operation"),
    [("on-req", "on"), ("ou-req", "ou"), ("oc-req", "oc")],
)
def test_notification_preserves_the_mutation_type(
    request_type: str,
    operation: str,
) -> None:
    event = parse_private_message(
        [
            0,
            "n",
            [
                1_700_000_000_124,
                request_type,
                77,
                None,
                {"id": 9876, "cid": 4567},
                10020,
                "ERROR",
                "request rejected",
            ],
        ]
    )

    assert isinstance(event, Notification)
    assert event.request_type == request_type
    assert event.operation == operation
    assert event.venue_order_id == 9876
    assert event.client_order_id == 4567
    assert event.code == 10020
    assert event.status == "ERROR"


def test_notification_extracts_ids_from_an_order_row() -> None:
    event = parse_private_message(
        [0, "n", [1, "on-req", None, None, order_row(), 0, "SUCCESS", ""]]
    )

    assert isinstance(event, Notification)
    assert event.venue_order_id == 219492782587
    assert event.client_order_id == 1557225713
    assert event.text == ""


def test_notification_rejects_nonpositive_venue_order_id() -> None:
    with pytest.raises(BitfinexV1ProtocolError, match="positive"):
        parse_private_message(
            [
                0,
                "n",
                [1, "oc-req", None, None, {"id": 0, "cid": 4567}, 0, "ERROR", ""],
            ]
        )


def test_wallet_snapshot_and_update_preserve_decimal_balances() -> None:
    row = ["margin", "USTF0", Decimal("1000.25"), Decimal("-0.5"), None]
    snapshot = parse_private_message([0, "ws", [row]])
    update = parse_private_message([0, "wu", row])

    assert isinstance(snapshot, WalletEvent)
    assert isinstance(update, WalletEvent)
    assert snapshot.message_type == "ws"
    assert update.message_type == "wu"
    assert snapshot.wallets == update.wallets
    assert snapshot.wallets[0].balance == Decimal("1000.25")
    assert snapshot.wallets[0].unsettled_interest == Decimal("-0.5")
    assert snapshot.wallets[0].available_balance is None


@pytest.mark.parametrize("message_type", ["pn", "pu", "pc"])
def test_position_updates_preserve_native_position_id(message_type: str) -> None:
    event = parse_private_message([0, message_type, position_row()])

    assert isinstance(event, PositionEvent)
    assert event.message_type == message_type
    assert event.positions[0].position_id == 991
    assert event.positions[0].quantity == Decimal("-0.75")
    assert event.positions[0].leverage == Decimal("10")


def test_position_snapshot_distinguishes_not_received_from_confirmed_empty() -> None:
    event = parse_private_message([0, "ps", []])

    assert event == PositionEvent("ps", ())


def test_position_margin_extension_preserves_raw_numbers_and_venue_time() -> None:
    row = [*position_row(), 100, 200, None, 1, None, Decimal("150.125"), Decimal("15.0125")]
    event = parse_private_message([0, "ps", [row]])
    assert isinstance(event, PositionEvent)
    position = event.positions[0]
    assert position.ts_created_ms == 100
    assert position.ts_updated_ms == 200
    assert position.position_type == 1
    assert position.collateral == Decimal("150.125")
    assert position.collateral_min == Decimal("15.0125")


@pytest.mark.parametrize("bad", [None, "bad", True, 1.25, Decimal("NaN")])
def test_bad_optional_position_margin_extension_does_not_break_private_codec(bad: object) -> None:
    row = [*position_row(), bad, bad, None, bad, None, bad, bad]
    event = parse_private_message([0, "pu", row])
    assert isinstance(event, PositionEvent)
    position = event.positions[0]
    assert position.ts_created_ms is None and position.ts_updated_ms is None
    assert position.position_type is None
    assert position.collateral is None and position.collateral_min is None
    row[2] = True
    with pytest.raises(BitfinexV1ProtocolError, match="position.amount"):
        parse_private_message([0, "pu", row])


@pytest.mark.parametrize(
    "message",
    [
        [1, "on", order_row()],
        [0, "te", []],
        [0, "on"],
        [0, "tu", [1, SYMBOL]],
        [0, "n", [1, "calc", None, None, {}, 0, "SUCCESS", ""]],
    ],
)
def test_out_of_scope_or_malformed_private_frames_fail_closed(message: object) -> None:
    with pytest.raises(BitfinexV1ProtocolError):
        parse_private_message(message)


def test_inbound_float_and_bool_are_not_silently_coerced() -> None:
    floating = order_row()
    floating[6] = 1.5
    boolean_id = order_row()
    boolean_id[0] = True

    with pytest.raises(BitfinexV1ProtocolError, match="exact JSON number"):
        parse_private_message([0, "on", floating])
    with pytest.raises(BitfinexV1ProtocolError, match="integer"):
        parse_private_message([0, "on", boolean_id])
