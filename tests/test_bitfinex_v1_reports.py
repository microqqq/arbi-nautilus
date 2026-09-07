from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.config import RoutingConfig
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.identifiers import AccountId, ClientOrderId

import py000_nautilus.bitfinex_v1_reports as reports_module
from py000_nautilus.bitfinex_v1_cids import (
    BitfinexFeeMetadata,
    BitfinexFeeTrade,
    BitfinexNativeFill,
)
from py000_nautilus.bitfinex_v1_data import (
    INSTRUMENT_ID,
    PAPER_RAW_SYMBOL,
    RAW_SYMBOL,
    BitfinexV1DataClientConfig,
    instrument_from_config,
)
from py000_nautilus.bitfinex_v1_protocol import POST_ONLY_FLAG, REDUCE_ONLY_FLAG
from py000_nautilus.bitfinex_v1_reports import (
    BitfinexV1ReportError,
    map_fill_reports,
    map_order_status_reports,
    map_position_status_reports,
)

ACCOUNT_ID = AccountId("BITFINEX-001")
TS_INIT = 1_800_000_000_000_000_000


@pytest.fixture
def instrument():  # type: ignore[no-untyped-def]
    return _instrument()


def _instrument(raw_symbol: str = RAW_SYMBOL):  # type: ignore[no-untyped-def]
    return instrument_from_config(
        BitfinexV1DataClientConfig(
            url="wss://api-pub.bitfinex.com/ws/2",
            instrument_id=INSTRUMENT_ID,
            raw_symbol=raw_symbol,
            price_precision=2,
            size_precision=8,
            price_increment=Decimal("0.01"),
            size_increment=Decimal("0.00000001"),
            min_quantity=Decimal("0.00000001"),
            max_quantity=Decimal("100000"),
            margin_init=Decimal("0.1"),
            margin_maint=Decimal("0.05"),
            maker_fee=Decimal(0),
            taker_fee=Decimal("0.0002"),
            routing=RoutingConfig(default=False, venues=frozenset({"BITFINEX"})),
        ),
        ts_init=0,
    )


def _order_row(
    *,
    venue_id: int = 1001,
    cid: int | None = 101,
    remaining: str = "4",
    original: str = "4",
    status: str = "ACTIVE",
    order_type: str = "LIMIT",
    flags: int = 0,
    price: str = "3926.70",
    average: str | None = None,
    updated: int = 1_700_000_000_100,
) -> list[object]:
    if average is None:
        unfilled = Decimal(remaining).copy_abs() == Decimal(original).copy_abs()
        average = "0" if unfilled else "3926.75"
    return [
        venue_id,
        None,
        cid,
        RAW_SYMBOL,
        1_700_000_000_000,
        updated,
        Decimal(remaining),
        Decimal(original),
        order_type,
        None,
        None,
        None,
        flags,
        status,
        None,
        None,
        Decimal(price),
        Decimal(average),
    ]


def test_captured_paper_canceled_order_retains_explicit_post_only_metadata() -> None:
    # Actual 2026-09-07 closed row: active FLAGS cleared, META retained _$F7=1.
    row = [
        243538491952, None, 1788743501126, PAPER_RAW_SYMBOL,
        1788743501335, 1788743501901, Decimal(2), Decimal(2), "LIMIT",
        None, None, None, 0, "CANCELED", None, None, Decimal("4406.7"), Decimal(0),
        0, 0, None, None, None, 0, 0, None, None, None, "API>BFX", None, None,
        {"lev": 16, "ugrp_id": "0.3045599654553539", "_$F33": 16, "_$F7": 1},
    ]
    report, = map_order_status_reports(
        active_rows=[], history_rows=[row], instrument=_instrument(PAPER_RAW_SYMBOL),
        account_id=ACCOUNT_ID, cid_lookup=lambda _: ClientOrderId("CAPTURED-MAKER"),
        ts_init=TS_INIT,
    )
    assert report.order_status is OrderStatus.CANCELED and report.filled_qty.as_decimal() == 0
    assert report.post_only
    assert row[12] == 0 and row[31] == {
        "lev": 16, "ugrp_id": "0.3045599654553539", "_$F33": 16, "_$F7": 1,
    }


@pytest.mark.parametrize("flags,order_type", [
    (REDUCE_ONLY_FLAG, "IOC"), (8192, "LIMIT"), (0, "IOC"),
])
def test_metadata_does_not_hide_unsupported_flags_or_post_only_tif(
    flags: int, order_type: str,
) -> None:
    row = _order_row(flags=flags, order_type=order_type)
    row.extend([None] * (31 - len(row)))
    row.append({"$F7": 1})
    with pytest.raises(BitfinexV1ReportError, match="flags|post-only"):
        map_order_status_reports(
            active_rows=[row], history_rows=[], instrument=_instrument(), account_id=ACCOUNT_ID,
            cid_lookup=lambda _: ClientOrderId("META-CONFLICT"), ts_init=TS_INIT,
        )


def _trade_row(
    *,
    trade_id: int = 5001,
    venue_id: int = 1001,
    cid: int | None = 101,
    quantity: str = "-0.25",
    price: str = "3926.75",
    maker: int = 1,
    fee: str = "-0.10",
    currency: str = "USD",
    symbol: str = RAW_SYMBOL,
    timestamp: int = 1_700_000_000_120,
) -> list[object]:
    return [
        trade_id,
        symbol,
        timestamp,
        venue_id,
        Decimal(quantity),
        Decimal(price),
        "LIMIT",
        Decimal("3926.70"),
        maker,
        Decimal(fee),
        currency,
        cid,
    ]


def _position_row(
    *,
    symbol: str = RAW_SYMBOL,
    amount: str = "0.75",
    base_price: str = "4050.10",
    position_id: int = 9001,
    status: str = "ACTIVE",
    position_type: int = 1,
    created: int | None = 1_700_000_000_000,
    updated: int | None = 1_700_000_000_200,
) -> list[object]:
    return [
        symbol,
        status,
        Decimal(amount),
        Decimal(base_price),
        Decimal(0),
        0,
        Decimal(0),
        Decimal(0),
        Decimal("7000"),
        Decimal("10"),
        None,
        position_id,
        created,
        updated,
        None,
        position_type,
    ]


def _lookup(*cids: int) -> Callable[[int], ClientOrderId | None]:
    bindings = {cid: ClientOrderId(f"O-{cid}") for cid in cids}
    return bindings.get


def test_order_status_mapping_covers_active_partial_and_history_terminals(instrument) -> None:  # type: ignore[no-untyped-def]
    active_rows: list[object] = [
        _order_row(venue_id=1001, cid=101, updated=1_700_000_000_010),
        _order_row(
            venue_id=1002,
            cid=102,
            remaining="3",
            status="ACTIVE",
            average="3926.754321",
            updated=1_700_000_000_020,
        ),
        _order_row(
            venue_id=1003,
            cid=103,
            remaining="2",
            status="PARTIALLY FILLED @ 3926.75(2)",
            updated=1_700_000_000_030,
        ),
    ]
    history_rows: list[object] = [
        _order_row(
            venue_id=1004,
            cid=104,
            remaining="0",
            status="EXECUTED @ 3926.75(4)",
            updated=1_700_000_000_040,
        ),
        _order_row(
            venue_id=1005,
            cid=105,
            remaining="2",
            status="CANCELED was: PARTIALLY FILLED @ 3926.75(2)",
            updated=1_700_000_000_050,
        ),
        _order_row(
            venue_id=1006,
            cid=106,
            status="POSTONLY CANCELED",
            flags=POST_ONLY_FLAG,
            updated=1_700_000_000_060,
        ),
    ]

    reports = map_order_status_reports(
        active_rows=active_rows,
        history_rows=history_rows,
        instrument=instrument,
        account_id=ACCOUNT_ID,
        cid_lookup=_lookup(101, 102, 103, 104, 105, 106),
        ts_init=TS_INIT,
    )

    assert [report.order_status for report in reports] == [
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    ]
    assert reports[1].filled_qty.as_decimal() == Decimal("1")
    assert reports[1].avg_px == Decimal("3926.754321")
    assert reports[3].filled_qty.as_decimal() == Decimal("4")
    assert reports[5].post_only
    assert all(report.venue_position_id is None for report in reports)


def test_order_status_mapping_preserves_reduce_only_ioc(instrument) -> None:  # type: ignore[no-untyped-def]
    report = map_order_status_reports(
        active_rows=[],
        history_rows=[
            _order_row(
                cid=107,
                remaining="0",
                original="-2",
                status="EXECUTED @ 3926.75(-2)",
                order_type="IOC",
                flags=REDUCE_ONLY_FLAG,
            )
        ],
        instrument=instrument,
        account_id=ACCOUNT_ID,
        cid_lookup=_lookup(107),
        ts_init=TS_INIT,
    )[0]

    assert report.time_in_force == TimeInForce.IOC
    assert report.reduce_only
    assert not report.post_only


@pytest.mark.parametrize(
    ("flags", "order_type", "message"),
    [
        (REDUCE_ONLY_FLAG | POST_ONLY_FLAG, "IOC", "unsupported"),
        (2048, "IOC", "unsupported"),
        (REDUCE_ONLY_FLAG, "LIMIT", "reduce-only"),
        (POST_ONLY_FLAG, "IOC", "post-only"),
    ],
)
def test_order_status_mapping_rejects_nonclosed_flag_semantics(
    instrument: Any,
    flags: int,
    order_type: str,
    message: str,
) -> None:
    with pytest.raises(BitfinexV1ReportError, match=message):
        map_order_status_reports(
            active_rows=[_order_row(flags=flags, order_type=order_type)],
            history_rows=[],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(101),
            ts_init=TS_INIT,
        )


def test_order_mapping_is_exact_about_precision_ownership_and_duplicate_ids(instrument) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(BitfinexV1ReportError, match=r"active.*CID"):
        map_order_status_reports(
            active_rows=[_order_row()],
            history_rows=[],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(),
            ts_init=TS_INIT,
        )
    assert (
        map_order_status_reports(
            active_rows=[],
            history_rows=[_order_row(status="CANCELED")],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(),
            ts_init=TS_INIT,
        )
        == []
    )
    with pytest.raises(BitfinexV1ReportError, match="precision"):
        map_order_status_reports(
            active_rows=[_order_row(price="3926.701")],
            history_rows=[],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(101),
            ts_init=TS_INIT,
        )
    changed = _order_row(price="3926.71")
    with pytest.raises(BitfinexV1ReportError, match=r"duplicate.*changed"):
        map_order_status_reports(
            active_rows=[_order_row(), changed],
            history_rows=[],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(101),
            ts_init=TS_INIT,
        )


def test_fill_mapping_uses_tu_semantics_and_deduplicates_stably(instrument) -> None:  # type: ignore[no-untyped-def]
    later = _trade_row(trade_id=5002, timestamp=200, quantity="0.125", maker=-1)
    earlier = _trade_row(trade_id=5001, timestamp=100)
    reports = map_fill_reports(
        rows=[later, earlier, earlier.copy()],
        instrument=instrument,
        account_id=ACCOUNT_ID,
        cid_lookup=_lookup(101),
        fee_currency="USD",
        ts_init=TS_INIT,
    )

    assert [report.trade_id.value for report in reports] == ["5001", "5002"]
    assert reports[0].order_side == OrderSide.SELL
    assert reports[0].last_qty.as_decimal() == Decimal("0.25")
    assert reports[0].last_px.as_decimal() == Decimal("3926.75")
    assert reports[0].commission.as_decimal() == Decimal("0.10")
    assert reports[0].commission.currency.code == "USD"
    assert reports[0].liquidity_side == LiquiditySide.MAKER
    assert reports[0].venue_position_id is None
    assert reports[1].order_side == OrderSide.BUY
    assert reports[1].liquidity_side == LiquiditySide.TAKER


def test_paper_fill_currency_maps_to_canonical_usd_commission() -> None:
    report = map_fill_reports(
        rows=[_trade_row(symbol=PAPER_RAW_SYMBOL, currency="USD")],
        instrument=_instrument(PAPER_RAW_SYMBOL),
        account_id=ACCOUNT_ID,
        cid_lookup=_lookup(101),
        fee_currency="USD",
        ts_init=TS_INIT,
    )[0]

    assert report.commission.currency.code == "USD"
    assert report.commission.as_decimal() == Decimal("0.10")


def test_fill_mapping_ignores_unknown_cids_and_rejects_conflicting_trade_ids(instrument) -> None:  # type: ignore[no-untyped-def]
    assert (
        map_fill_reports(
            rows=[_trade_row()],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(),
            fee_currency="USD",
            ts_init=TS_INIT,
        )
        == []
    )
    changed = _trade_row(price="3926.76")
    with pytest.raises(BitfinexV1ReportError, match="trade ID changed"):
        map_fill_reports(
            rows=[_trade_row(), changed],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(101),
            fee_currency="USD",
            ts_init=TS_INIT,
        )
    with pytest.raises(BitfinexV1ReportError, match="USD"):
        map_fill_reports(
            rows=[_trade_row(currency="USTF0")],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(101),
            fee_currency="USD",
            ts_init=TS_INIT,
        )


def test_fill_mapping_rejects_non_usd_config(instrument) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(BitfinexV1ReportError, match="fee currency must be USD"):
        map_fill_reports(
            rows=[_trade_row()],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            cid_lookup=_lookup(101),
            fee_currency="USTF0",
            ts_init=TS_INIT,
        )


@pytest.mark.parametrize(
    ("fee", "commission"),
    [
        ("-0.061668", "0.06"),
        ("-0.001", "0.00"),
        ("-0.005", "0.00"),
        ("0.005", "0.00"),
        ("-0.015", "0.02"),
        ("0.015", "-0.02"),
        ("0", "0.00"),
    ],
)
def test_fill_mapping_rounds_usd_fee_half_even(fee: str, commission: str) -> None:
    row = _trade_row(fee=fee)
    report = map_fill_reports(
        rows=[row],
        instrument=_instrument(),
        account_id=ACCOUNT_ID,
        cid_lookup=_lookup(101),
        fee_currency="USD",
        ts_init=TS_INIT,
    )[0]
    assert report.commission.as_decimal() == Decimal(commission)
    assert report.commission.currency.code == "USD"
    assert row[9] == Decimal(fee)


def test_fill_mapping_rounds_each_fee_before_aggregation() -> None:
    rows = [_trade_row(trade_id=trade_id, fee="-0.0049") for trade_id in (5001, 5002)]
    reports = map_fill_reports(
        rows=[*rows, rows[0].copy()],
        instrument=_instrument(),
        account_id=ACCOUNT_ID,
        cid_lookup=_lookup(101),
        fee_currency="USD",
        ts_init=TS_INIT,
    )
    assert len(reports) == 2
    assert sum((report.commission.as_decimal() for report in reports), Decimal()) == 0
    assert all(row[9] == Decimal("-0.0049") for row in rows)


def test_position_mapping_covers_long_short_and_explicit_target_flat(instrument) -> None:  # type: ignore[no-untyped-def]
    unrelated = _position_row(symbol="tBTCF0:USTF0")
    flat = map_position_status_reports(
        rows=[unrelated], instrument=instrument, account_id=ACCOUNT_ID, ts_init=TS_INIT
    )[0]
    long = map_position_status_reports(
        rows=[unrelated, _position_row(amount="0.75", base_price="4050.123456")],
        instrument=instrument,
        account_id=ACCOUNT_ID,
        ts_init=TS_INIT,
    )[0]
    short = map_position_status_reports(
        rows=[_position_row(amount="-0.125")],
        instrument=instrument,
        account_id=ACCOUNT_ID,
        ts_init=TS_INIT,
    )[0]
    observed = map_position_status_reports(
        rows=[_position_row(created=None, updated=None)],
        instrument=instrument,
        account_id=ACCOUNT_ID,
        ts_init=TS_INIT,
    )[0]

    assert flat.position_side == PositionSide.FLAT
    assert flat.quantity.as_decimal() == 0
    assert flat.ts_last == TS_INIT
    assert long.position_side == PositionSide.LONG
    assert long.quantity.as_decimal() == Decimal("0.75")
    assert long.avg_px_open == Decimal("4050.123456")
    assert short.position_side == PositionSide.SHORT
    assert short.quantity.as_decimal() == Decimal("0.125")
    assert observed.ts_last == TS_INIT
    assert all(report.venue_position_id is None for report in (flat, long, short, observed))


def test_position_mapping_rejects_non_derivative_and_multiple_target_positions(instrument) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(BitfinexV1ReportError, match="derivative type"):
        map_position_status_reports(
            rows=[_position_row(position_type=0)],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            ts_init=TS_INIT,
        )
    with pytest.raises(BitfinexV1ReportError, match="one NETTING"):
        map_position_status_reports(
            rows=[_position_row(position_id=1), _position_row(position_id=2)],
            instrument=instrument,
            account_id=ACCOUNT_ID,
            ts_init=TS_INIT,
        )
def test_fee_summary_keeps_raw_quantized_booked_and_currency_domains_separate() -> None:
    native = BitfinexNativeFill(
        "1234", "te_paper", Decimal(2), Decimal("3926.75"), 123_000_000,
        "TAKER", Decimal(0), "USD",
    )
    trade = BitfinexFeeTrade(
        1234, 123, Decimal(2), Decimal("3926.75"), "IOC", Decimal(4000), False,
        Decimal("-0.061668"), "USD",
    )
    metadata = BitfinexFeeMetadata(
        100, 987, "XAUTUSDT.BITFINEX", "tTESTXAUTF0:TESTUSDTF0", (native,), (trade,),
    )
    summary = reports_module.summarize_fees((metadata,))
    assert summary.complete
    assert summary.pending_trades == summary.unknown_orders == 0
    usd = summary.currencies["USD"]
    assert usd.raw_cost == Decimal("0.061668")
    assert usd.quantized_cost == Decimal("0.06")
    assert usd.native_cost == Decimal(0)
    assert usd.rounding_delta == Decimal("0.001668")
    assert usd.provisional_correction == Decimal("0.06")

    inferred = replace(native, trade_id="inferred-uuid", native_fill_origin="inferred",
                       commission_currency="USDT", liquidity_side="NO_LIQUIDITY_SIDE")
    summary = reports_module.summarize_fees((replace(metadata, native_fills=(inferred,)),))
    assert summary.complete
    assert summary.currencies["USD"].quantized_cost == Decimal("0.06")
    assert summary.currencies["USDT"].native_cost == Decimal(0)
    assert set(summary.currencies) == {"USD", "USDT"}

    pending = replace(trade, raw_fee=None, fee_currency=None)
    incomplete = reports_module.summarize_fees((replace(metadata, venue_trades=(pending,)),))
    assert not incomplete.complete and incomplete.pending_trades == 1
    assert not reports_module.summarize_fees((replace(metadata, native_fills=()),)).complete
    assert not reports_module.summarize_fees((replace(metadata, venue_trades=()),)).complete


def test_inferred_fee_coverage_rejects_offsetting_opposite_order_sides() -> None:
    native = BitfinexNativeFill(
        "inferred-uuid", "inferred", Decimal(2), Decimal(100), 123_000_000,
        "NO_LIQUIDITY_SIDE", Decimal(0), "USDT",
    )
    buy = BitfinexFeeTrade(
        1234, 123, Decimal(3), Decimal(100), "IOC", Decimal(100), False,
        Decimal("-0.03"), "USD",
    )
    sell = replace(buy, trade_id=1235, execution_qty=Decimal(-1), raw_fee=Decimal("-0.01"))
    metadata = BitfinexFeeMetadata(
        100, 987, "XAUTUSDT.BITFINEX", "tTESTXAUTF0:TESTUSDTF0", (native,), (buy, sell),
    )
    assert not reports_module.summarize_fees((metadata,)).complete


def test_missing_native_fee_coverage_is_unknown_not_zero_booked() -> None:
    trade = BitfinexFeeTrade(
        1234, 123, Decimal(2), Decimal(100), "IOC", Decimal(100), False,
        Decimal("-0.061668"), "USD",
    )
    metadata = BitfinexFeeMetadata(
        100, 987, "XAUTUSDT.BITFINEX", "tTESTXAUTF0:TESTUSDTF0", (), (trade,),
    )
    summary = reports_module.summarize_fees((metadata,))
    assert not summary.complete
    assert summary.currencies["USD"].native_cost is None
    assert summary.currencies["USD"].provisional_correction is None


@pytest.mark.parametrize(
    "amount,currency,symbol",
    [("1", "USD", PAPER_RAW_SYMBOL), ("0", "USDT", PAPER_RAW_SYMBOL), ("0", "USD", RAW_SYMBOL)],
)
def test_provisional_label_alone_cannot_authorize_fee_correction(
    amount: str, currency: str, symbol: str,
) -> None:
    native = BitfinexNativeFill(
        "1234", "te_paper", Decimal(2), Decimal(100), 123_000_000,
        "TAKER", Decimal(amount), currency,
    )
    trade = BitfinexFeeTrade(
        1234, 123, Decimal(2), Decimal(100), "IOC", Decimal(100), False,
        Decimal("-0.061668"), "USD",
    )
    summary = reports_module.summarize_fees((BitfinexFeeMetadata(
        100, 987, "XAUTUSDT.BITFINEX", symbol, (native,), (trade,),
    ),))
    assert not summary.complete
    assert summary.currencies["USD"].provisional_correction is None
