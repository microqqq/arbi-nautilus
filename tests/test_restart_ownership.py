"""Q3 native ownership prerequisites and observed 1.231.0 reconciliation limits.

These in-memory cases do not certify production persistence, crash durability,
business-store recovery, or strategy release. Direct report cases isolate the
native engine; the Bitfinex adapter does not admit their incomplete trade sets.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import msgspec
import pytest
import test_bitfinex_v1_execution as bfx
import test_mt5_v1_execution as mt5
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.config import InvalidConfiguration
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import FillReport, OrderStatusReport
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.live.reconciliation import create_order_filled_event
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import LiquiditySide, OmsType, OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.events import OrderInitialized
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    PositionId,
    StrategyId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.serialization.serializer import MsgSpecSerializer
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionError
from py000_nautilus.mt5_v1_protocol import JsonObject

D = Decimal
_OWNERS = (StrategyId("Q3-MAKER"), StrategyId("Q3-TAKER"))
_TICKETS = (PositionId("900000101"), PositionId("900000102"))


def _mt5_history() -> tuple[mt5._Harness, list[Order]]:
    """Reuse the real adapter and existing synthetic EA journal/snapshot helpers."""
    identity = mt5._identity()
    snapshot = mt5._snapshot(identity)
    template = cast(list[JsonObject], snapshot["positions"])[0]
    positions: list[JsonObject] = []
    for index, position_id in enumerate(_TICKETS, start=101):
        position = deepcopy(template)
        position.update({
            "identifier": str(position_id), "ticket": f"700000{index}",
            "volume_lots": "1", "price_open": "2401.25",
        })
        positions.append(position)
    snapshot["positions"] = positions
    harness = mt5._Harness(
        asyncio.get_running_loop(), identity=identity, snapshot=snapshot, capture_events=False,
    )
    orders = [TestExecStubs.market_order(
        instrument=harness.instrument, strategy_id=owner,
        client_order_id=ClientOrderId(f"Q3-MT5-{index}"), time_in_force=TimeInForce.FOK,
    ) for index, owner in enumerate(_OWNERS)]
    events = [mt5._stream_started(identity)]
    for index, (order, position_id) in enumerate(zip(orders, _TICKETS, strict=True)):
        sequence = 2 + index * 2
        events.append(mt5._event(
            identity, sequence, "submission_reserved", mt5._submission_payload(order),
        ))
        fill = mt5._outcome(identity, order, "order_filled", sequence=sequence + 1)
        cast(JsonObject, fill["payload"]).update({
            "venue_deal_id": f"800000{101 + index}",
            "venue_order_id": f"700000{101 + index}", "venue_position_id": str(position_id),
        })
        events.append(fill)
    harness.fake.pages.append(mt5._page(identity, after_cursor="0", events=events))
    return harness, orders


@pytest.mark.parametrize(
    "seed_original_identity", [False, True], ids=["reports-only", "init-seeded"],
)
def test_mt5_reports_preserve_ticket_ids_but_need_original_order_ownership(
    seed_original_identity: bool,
) -> None:
    async def scenario() -> None:
        harness, originals = _mt5_history()
        engine = mt5._live_engine(harness, generate_missing_orders=False)
        assert harness.client.oms_type is OmsType.HEDGING
        if seed_original_identity:
            for original, ticket in zip(originals, _TICKETS, strict=True):
                restored = OrderUnpacker.from_init(original.events[0])
                harness.cache.add_order(restored, position_id=ticket, client_id=harness.client.id)
        await harness.connect()
        try:
            assert await engine.reconcile_execution_state(timeout_secs=1.0)
            for index, (original, ticket) in enumerate(zip(originals, _TICKETS, strict=True)):
                order = harness.cache.order(original.client_order_id)
                position = harness.cache.position(ticket)
                owner = original.strategy_id if seed_original_identity else StrategyId("EXTERNAL")
                assert order is not None and position is not None
                assert order.strategy_id == position.strategy_id == owner
                assert order.client_order_id == original.client_order_id
                assert order.status is OrderStatus.FILLED
                assert order.filled_qty.as_decimal() == position.quantity.as_decimal() == D(100)
                assert order.position_id == position.id == ticket
                assert order.trade_ids == [TradeId(f"800000{101 + index}")]
                assert harness.cache.position_id(order.client_order_id) == ticket
                if seed_original_identity:
                    assert harness.cache.client_id(order.client_order_id) == harness.client.id
            before = [(order.client_order_id, len(order.events), tuple(order.trade_ids))
                      for order in harness.cache.orders()]
            assert await engine.reconcile_execution_state(timeout_secs=1.0)
            assert [(order.client_order_id, len(order.events), tuple(order.trade_ids))
                    for order in harness.cache.orders()] == before
            assert len(harness.cache.positions_open()) == 2
            assert harness.fake.submit_calls == []
            assert harness.fake.close_calls == []
        finally:
            await harness.client._disconnect()
            engine.dispose()

    asyncio.run(scenario())


class _ClaimingStrategy(Strategy):
    pass


def test_one_instrument_cannot_be_claimed_by_two_strategy_ids() -> None:
    async def scenario() -> None:
        harness, _ = _mt5_history()
        engine = mt5._live_engine(harness, generate_missing_orders=False)
        maker, taker = (_ClaimingStrategy(StrategyConfig(
            order_id_tag=tag,
            external_order_claims=[mt5.INSTRUMENT_ID],
        )) for tag in ("MAKER", "TAKER"))
        try:
            assert maker.id != taker.id
            engine.register_external_order_claims(maker)
            with pytest.raises(InvalidConfiguration, match="already exists"):
                engine.register_external_order_claims(taker)
            assert engine.get_external_order_claim(mt5.INSTRUMENT_ID) == maker.id
        finally:
            engine.dispose()

    asyncio.run(scenario())


def test_exact_close_init_codec_does_not_contain_position_or_client_mapping() -> None:
    async def scenario() -> None:
        harness, _ = _mt5_history()
        closing = harness.market(
            order_side=OrderSide.SELL, reduce_only=True, client_order_id=ClientOrderId("Q3-CLOSE"),
        )
        harness.cache.add_order(closing, position_id=_TICKETS[0], client_id=harness.client.id)
        codec = MsgSpecSerializer(msgspec.msgpack, timestamps_as_str=True)
        initialized = cast(OrderInitialized, codec.deserialize(codec.serialize(closing.events[0])))
        fields = OrderInitialized.to_dict(initialized)
        assert "position_id" not in fields and "client_id" not in fields
        restored = OrderUnpacker.from_init(initialized)
        cache = Cache()
        cache.add_order(restored)
        assert restored.client_order_id == closing.client_order_id
        assert restored.strategy_id == closing.strategy_id
        assert restored.is_reduce_only and restored.filled_qty.as_decimal() == 0
        assert restored.position_id is None
        assert cache.position_id(restored.client_order_id) is None
        assert cache.client_id(restored.client_order_id) is None
        seeded = Cache()
        seeded.add_order(OrderUnpacker.from_init(initialized),
                         position_id=_TICKETS[0], client_id=harness.client.id)
        assert seeded.position_id(restored.client_order_id) == _TICKETS[0]
        assert seeded.client_id(restored.client_order_id) == harness.client.id

    asyncio.run(scenario())


def test_cached_order_position_mapping_overrides_a_conflicting_mt5_fill_ticket() -> None:
    async def scenario() -> None:
        harness, originals = _mt5_history()
        engine = mt5._live_engine(harness, generate_missing_orders=False)
        order = originals[0]
        harness.cache.add_order(order, position_id=_TICKETS[0], client_id=harness.client.id)
        report = FillReport(
            account_id=harness.client.account_id, instrument_id=mt5.INSTRUMENT_ID,
            venue_order_id=VenueOrderId("700000101"), trade_id=TradeId("800000101"),
            client_order_id=order.client_order_id, venue_position_id=_TICKETS[1],
            order_side=order.side, last_qty=order.quantity,
            last_px=harness.instrument.make_price(D("2401.25")),
            commission=Money(0, USD), liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(), ts_event=1, ts_init=2,
        )
        try:
            fill = create_order_filled_event(order, 2, report, harness.instrument)
            assert fill.position_id == _TICKETS[1]
            oms_type = engine._determine_oms_type(fill)
            assert oms_type is OmsType.HEDGING
            engine._determine_position_id(fill, oms_type, order)
            assert fill.position_id == _TICKETS[0]
            assert report.venue_position_id == _TICKETS[1]
            assert harness.cache.positions() == []  # This isolates ID selection, not execution.
        finally:
            engine.dispose()

    asyncio.run(scenario())


def _source_engine(harness: bfx._Harness) -> LiveExecutionEngine:
    harness.msgbus.deregister("ExecEngine.process", harness.events.append)
    harness.cache.add_instrument(harness.instrument)
    harness.cache.add_account(TestExecStubs.margin_account(account_id=harness.client.account_id))
    engine = LiveExecutionEngine(
        loop=asyncio.get_running_loop(), msgbus=harness.msgbus, cache=harness.cache,
        clock=harness.clock, config=LiveExecEngineConfig(
            load_cache=False, generate_missing_orders=False, inflight_check_interval_ms=0,
            open_check_interval_secs=None, position_check_interval_secs=None,
        ),
    )
    engine.register_client(harness.client)
    assert harness.client.oms_type is OmsType.NETTING
    return engine


def _source_reports(
    harness: bfx._Harness, order: Order, status: OrderStatus, filled: str, trade_id: str,
) -> tuple[OrderStatusReport, FillReport]:
    price = harness.instrument.make_price(D("3926.70"))
    quantity = harness.instrument.make_qty(D(filled))
    report = OrderStatusReport(
        account_id=harness.client.account_id, instrument_id=order.instrument_id,
        client_order_id=order.client_order_id, venue_order_id=VenueOrderId("Q3-SOURCE-VENUE"),
        order_side=order.side, order_type=order.order_type, time_in_force=order.time_in_force,
        order_status=status, quantity=order.quantity, filled_qty=quantity, price=price,
        avg_px=price.as_decimal(), report_id=UUID4(), ts_accepted=1, ts_last=3, ts_init=4,
    )
    trade = FillReport(
        account_id=report.account_id, instrument_id=report.instrument_id,
        client_order_id=report.client_order_id, venue_order_id=report.venue_order_id,
        trade_id=TradeId(trade_id), order_side=order.side, last_qty=quantity, last_px=price,
        commission=Money(0, USD), liquidity_side=LiquiditySide.MAKER,
        report_id=UUID4(), ts_event=2, ts_init=4,
    )
    return report, trade


@pytest.mark.parametrize("status", [OrderStatus.CANCELED, OrderStatus.EXPIRED])
@pytest.mark.parametrize(
    "complete_trades", [True, False], ids=["complete-trades", "missing-trades"],
)
def test_native_partial_terminal_success_bool_does_not_prove_filled_quantity(
    tmp_path: Path, status: OrderStatus, complete_trades: bool,
) -> None:
    async def scenario() -> None:
        harness = bfx._Harness(cid_store_path=tmp_path / "cids.json")
        engine = _source_engine(harness)
        order = harness.order(client_order_id=ClientOrderId("Q3-PARTIAL"))
        harness.cache.add_order(order, client_id=harness.client.id)
        report, trade = _source_reports(harness, order, status, "1.25", "Q3-TRADE-1")
        try:
            # Direct native reports only: the ordinary BFX adapter rejects missing partial trades.
            assert engine._reconcile_order_report(report, [trade] if complete_trades else [])
            assert order.status is status
            assert order.strategy_id == harness.order_factory.strategy_id
            positions = harness.cache.positions_open(instrument_id=order.instrument_id)
            if complete_trades:
                assert order.filled_qty == report.filled_qty
                assert order.trade_ids == [trade.trade_id]
                assert len(positions) == 1
                assert positions[0].signed_decimal_qty() == D("1.25")
                assert positions[0].strategy_id == order.strategy_id
                assert positions[0].id == PositionId(f"{order.instrument_id}-{order.strategy_id}")
            else:
                assert order.filled_qty.as_decimal() == 0 < report.filled_qty.as_decimal()
                assert order.trade_ids == [] and positions == []
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_native_already_canceled_order_skips_later_reported_trade_and_quantity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = bfx._Harness(cid_store_path=tmp_path / "cids.json")
        engine = _source_engine(harness)
        order = harness.order(client_order_id=ClientOrderId("Q3-CLOSED"))
        harness.cache.add_order(order, client_id=harness.client.id)
        report, first = _source_reports(harness, order, OrderStatus.CANCELED, "1", "Q3-TRADE-1")
        try:
            assert engine._reconcile_order_report(report, [first])
            later, second = _source_reports(harness, order, OrderStatus.CANCELED, "1", "Q3-TRADE-2")
            later.filled_qty = harness.instrument.make_qty(D(2))
            assert engine._reconcile_order_report(later, [first, second])
            assert order.status is OrderStatus.CANCELED
            assert order.filled_qty.as_decimal() == 1 < later.filled_qty.as_decimal()
            assert order.trade_ids == [first.trade_id]
            assert harness.cache.positions_open()[0].signed_decimal_qty() == 1
        finally:
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


def test_ordinary_bitfinex_still_rejects_nonzero_cold_position(tmp_path: Path) -> None:
    async def scenario() -> None:
        rest = bfx._FakeRest()
        rest.position_rows = [bfx._position_row(D(2), avg_px=D("3926.75"))]
        harness = bfx._Harness(cid_store_path=tmp_path / "cids.json", rest=rest)
        try:
            assert harness.cache.orders() == [] and harness.cache.positions() == []
            with pytest.raises(BitfinexV1ExecutionError, match="position differs"):
                await harness.client.generate_mass_status(lookback_mins=1)
            assert harness.cache.orders() == [] and harness.cache.positions() == []
            assert harness.fake.sent == []
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["replay", "orders-only", "no-indexes"])
def test_fresh_process_native_event_replay_requires_positions_and_cache_indexes(
    tmp_path: Path, mode: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "tests/restart_replay_worker.py")]
    environment = os.environ | {"PYTHONPATH": str(root / "src")}
    produced = subprocess.run(
        [*command, "produce"], cwd=root, env=environment, capture_output=True, timeout=30,
    )
    assert produced.returncode == 0, produced.stderr.decode()
    # Only synthetic native events/indexes cross a real process boundary. This
    # file is a test carrier, not a proposed production journal or durability test.
    carrier = tmp_path / "native-events.json"
    carrier.write_bytes(produced.stdout)
    original = cast(dict[str, Any], json.loads(carrier.read_bytes()))
    consumed = subprocess.run(
        [*command, mode], input=carrier.read_bytes(), cwd=root, env=environment,
        capture_output=True, timeout=30,
    )
    assert consumed.returncode == 0, consumed.stderr.decode()
    restored = cast(dict[str, Any], json.loads(consumed.stdout))
    assert len({os.getpid(), original["pid"], restored["pid"]}) == 3

    # Fixed expected facts prevent two identically wrong snapshots from passing.
    source = "XAUTUSDT-PERP.BITFINEX"
    expected_positions = {
        f"{source}-Maker-001": {
            "strategy": "Maker-001", "account": "BITFINEX-001", "instrument": source,
            "signed_qty": "0.5", "trades": ["800000100"], "opening_order": "Q3-M-SOURCE",
        },
        f"{source}-Taker-002": {
            "strategy": "Taker-002", "account": "BITFINEX-001", "instrument": source,
            "signed_qty": "-1.0", "trades": ["800000102"], "opening_order": "Q3-T-SOURCE",
        },
        "900000101": {
            "strategy": "Maker-001", "account": "MT5-001", "instrument": "XAUUSD.MT5",
            "signed_qty": "-1", "trades": ["800000101"], "opening_order": "Q3-M-HEDGE",
        },
        "900000102": {
            "strategy": "Taker-002", "account": "MT5-001", "instrument": "XAUUSD.MT5",
            "signed_qty": "1", "trades": ["800000103"], "opening_order": "Q3-T-HEDGE",
        },
    }
    snapshot = original["snapshot"]
    assert snapshot["positions"] == expected_positions
    expected_orders = (
        ("Q3-M-SOURCE", "Maker-001", "BUY", "2", "0.5", "PARTIALLY_FILLED", "800000100"),
        ("Q3-T-SOURCE", "Taker-002", "SELL", "2", "1", "PARTIALLY_FILLED", "800000102"),
        ("Q3-M-HEDGE", "Maker-001", "SELL", "1", "1", "FILLED", "800000101"),
        ("Q3-T-HEDGE", "Taker-002", "BUY", "1", "1", "FILLED", "800000103"),
        ("Q3-M-CLOSE", "Maker-001", "BUY", "1", "0", "ACCEPTED", None),
    )
    assert set(snapshot["orders"]) == {row[0] for row in expected_orders}
    for cid, owner, side, quantity, filled, status, trade in expected_orders:
        order = snapshot["orders"][cid]
        source_order = cid.endswith("SOURCE")
        assert (order["strategy"], order["side"], D(order["quantity"]), D(order["filled"]),
                order["status"]) == (owner, side, D(quantity), D(filled), status)
        assert order["client_id"] == ("BITFINEX" if source_order else "MT5")
        assert order["account"] == f"{order['client_id']}-001"
        assert order["instrument"] == (source if source_order else "XAUUSD.MT5")
        assert order["reduce_only"] == (cid == "Q3-M-CLOSE")
        assert order["trades"] == ([] if trade is None else [trade])
        assert len(order["event_ids"]) == (3 if trade is None else 4)
        assert order["commissions"] == ([] if trade is None else ["0.00 USD"])
        expected_position = f"{source}-{owner}" if source_order else (
            "900000102" if owner == "Taker-002" else "900000101"
        )
        assert order["position_id"] == expected_position
    event_ids = [event_id for order in snapshot["orders"].values()
                 for event_id in order["event_ids"]]
    assert len(event_ids) == len(set(event_ids))
    assert snapshot["orders"]["Q3-M-SOURCE"]["fill_info"] == [{
        "bitfinex_fill_source": "te_paper", "bitfinex_fee_status": "pending",
    }]

    expected = deepcopy(snapshot)
    if mode == "orders-only":
        expected["positions"] = {}  # Order.apply/cache.add_order do not rebuild native Positions.
    elif mode == "no-indexes":
        for order in expected["orders"].values():
            order["client_id"] = None
        # Existing fills recover their ticket, but an unfilled exact close cannot.
        expected["orders"]["Q3-M-CLOSE"]["position_id"] = None
    assert restored["snapshot"] == expected
    assert restored["after_duplicate"] == expected  # Replayed native TradeIds cannot fill twice.
