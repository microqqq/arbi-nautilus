"""Actual native Order/Position math with synthetic retained venue fee facts.

No adapter connects. These checks do not certify broker cash-flow completeness.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.cache.cache import Cache
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import LiquiditySide, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    PositionId,
    StrategyId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Cfd, Instrument
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import LimitOrder, MarketOrder, Order
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs
from restart_replay_worker import _engine
from test_mt5_v1_execution import _cache_report_fill, _connected_report_harness

from py000_nautilus.accounting_report import RunAccountingReport, build_run_accounting_report
from py000_nautilus.app import _hedge_instrument, _source_instrument
from py000_nautilus.bitfinex_v1_cids import (
    BitfinexFeeMetadata,
    BitfinexFeeTrade,
    BitfinexV1CidStore,
)
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL
from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecutionClient,
    _native_fill_evidence,
)
from py000_nautilus.bitfinex_v1_reports import BitfinexFeeSummary, usd_commission
from py000_nautilus.config import FxConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionClient, Mt5V1ExecutionError
from py000_nautilus.mt5_v1_protocol import JsonObject, _validate_event

D = Decimal
OWNER = StrategyId("ACCOUNTING-001")
FX = FxConfig(usd_usdt_bid=D("1.01"), usd_usdt_ask=D("1.02"))


class _Source:
    """Reuse the real CID owner and fee-summary methods, with no transport."""

    def __init__(self, cache: Cache, path: Path) -> None:
        self._cache = cache
        self.account_id = AccountId("BITFINEX-001")
        self._bfx_config = SimpleNamespace(
            instrument_id=_source_instrument().id, raw_symbol=PAPER_RAW_SYMBOL,
        )
        self._cid_store = BitfinexV1CidStore(path, account_id=self.account_id.value)

    @property
    def accounting_incomplete(self) -> bool:
        return self._cid_store.accounting_conflict or not self._cid_store.fees_durable

    def _cached_zero_fill_terminal(self, cid: str) -> bool:
        return BitfinexV1ExecutionClient._cached_zero_fill_terminal(
            cast(BitfinexV1ExecutionClient, self), cid,
        )

    def fee_summary(self, cid: ClientOrderId | None = None) -> BitfinexFeeSummary:
        return BitfinexV1ExecutionClient.fee_summary(cast(BitfinexV1ExecutionClient, self), cid)


class _Hedge:
    """A journal projection fixture, not a second implementation of fee extraction."""

    def __init__(self, cache: Cache) -> None:
        self._cache = cache
        self.id = ClientId("MT5")
        self.account_id = AccountId("MT5-001")
        self._mt5_config = SimpleNamespace(instrument_id=_hedge_instrument().id)
        self._reservations: dict[str, JsonObject] = {}
        self._terminal_events: dict[str, JsonObject] = {}
        self._pending: dict[str, object] = {}

    def raw_commission_cashflows(self) -> dict[tuple[ClientOrderId, TradeId], Decimal]:
        return Mt5V1ExecutionClient.raw_commission_cashflows(cast(Mt5V1ExecutionClient, self))

    def _validate_cached_terminal(
        self, request_id: str, event: JsonObject, *, cached_instrument: Instrument | None = None,
    ) -> None:
        Mt5V1ExecutionClient._validate_cached_terminal(
            cast(Mt5V1ExecutionClient, self), request_id, event,
            cached_instrument=cached_instrument,
        )

    _order_side = staticmethod(Mt5V1ExecutionClient._order_side)
    _event_ts_ns = staticmethod(Mt5V1ExecutionClient._event_ts_ns)


class _History:
    def __init__(self, engine: BacktestEngine, path: Path) -> None:
        self.engine = engine
        values = Cfd.to_dict(_hedge_instrument())
        values["lot_size"] = "100"  # Match the live canonical 100 oz / MT5 lot contract.
        engine.add_instrument(Cfd.from_dict(values))
        self.source = _Source(engine.cache, path)
        self.hedge = _Hedge(engine.cache)
        self._position_ids: dict[StrategyId, PositionId] = {}

    def fill(
        self, source: bool, side: OrderSide, *, quantity: int = 1, price: int = 100,
        fee: str = "-1.25", provisional: bool = False, pending: bool = False,
        owner: StrategyId = OWNER, total_quantity: int | None = None,
    ) -> Order:
        cache = self.engine.cache
        instrument = cache.instrument((_source_instrument() if source else _hedge_instrument()).id)
        assert instrument is not None
        number = len(cache.orders()) + 1
        cid = ClientOrderId(f"A07-{number}")
        account = self.source.account_id if source else self.hedge.account_id
        original = instrument.make_qty(D(quantity if total_quantity is None else total_quantity))
        position_id = None if source else self._position_ids.setdefault(
            owner, PositionId(str(900000 + len(self._position_ids))),
        )
        position = None if position_id is None else cache.position(position_id)
        close = (position is not None and not position.is_closed
                 and (position.signed_decimal_qty() > 0) != (side == OrderSide.BUY))
        order: Order
        if source:
            order = LimitOrder(
                trader_id=TestIdStubs.trader_id(), strategy_id=owner, instrument_id=instrument.id,
                client_order_id=cid, order_side=side, quantity=original,
                price=instrument.make_price(D(price)), time_in_force=TimeInForce.GTC,
                init_id=UUID4(), ts_init=number,
            )
        else:
            order = MarketOrder(
                trader_id=TestIdStubs.trader_id(), strategy_id=owner, instrument_id=instrument.id,
                client_order_id=cid, order_side=side, quantity=original,
                time_in_force=TimeInForce.FOK, reduce_only=close,
                init_id=UUID4(), ts_init=number,
            )
        venue = VenueOrderId(str(700000 + number))
        trade = TradeId(str(800000 + number))
        cache.add_order(order, client_id=ClientId(instrument.id.venue.value),
                        position_id=position_id if close else None)
        process = self.engine.kernel.exec_engine.process
        process(TestEventStubs.order_submitted(order, account, ts_event=number * 1_000_000))
        process(TestEventStubs.order_accepted(order, account, venue, number * 1_000_000))
        event = TestEventStubs.order_filled(
            order, instrument, account_id=account, venue_order_id=venue, trade_id=trade,
            position_id=position_id,
            last_qty=instrument.make_qty(D(quantity)), last_px=instrument.make_price(D(price)),
            commission=Money(0, USD) if provisional else usd_commission(D(fee)),
            liquidity_side=LiquiditySide.TAKER if source else LiquiditySide.NO_LIQUIDITY_SIDE,
            ts_event=number * 1_000_000,
        )
        process(event)
        assert order.filled_qty.as_decimal() == quantity
        fill = cast(OrderFilled, order.events[-1])
        if source:
            binding = self.source._cid_store.allocate(cid.value, epoch_ms=number)
            scope = BitfinexFeeMetadata(binding.cid, int(venue.value), instrument.id.value,
                                       PAPER_RAW_SYMBOL)
            self.source._cid_store.record_native_fill(
                scope, _native_fill_evidence(fill, "te_paper" if provisional else "tu"),
            )
            self.source._cid_store.record_venue_trade(scope, BitfinexFeeTrade(
                int(trade.value), number, D(quantity) * (1 if side == OrderSide.BUY else -1),
                D(price), "LIMIT", D(price), False,
                None if pending else D(fee), None if pending else "USD",
            ))
        else:
            lots = str(D(quantity) / Decimal(str(instrument.lot_size)))
            reserved: JsonObject = {
                "client_request_id": cid.value, "quantity_lots": lots,
                "side": "buy" if side == OrderSide.BUY else "sell",
            }
            if close:
                reserved.update(position_identifier=str(position_id),
                                position_ticket=str(position_id))
            header: JsonObject = {
                "boot_id": "a07-boot", "stream_id": "a07-stream", "event_time_ms": str(number),
            }
            reservation = dict(header, event_seq=str(2 * number),
                               event_type="submission_reserved", payload=reserved)
            terminal = dict(header, event_seq=str(2 * number + 1), event_type="order_filled",
                            payload=dict(reserved, venue_deal_id=trade.value,
                                         filled_quantity_lots=lots, commission=fee,
                                         fill_price=str(price), venue_order_id=venue.value,
                                         venue_position_id=str(position_id),
                                         broker_retcode="10009"))
            _validate_event(reservation, stream_id="a07-stream")
            _validate_event(terminal, stream_id="a07-stream")
            self.hedge._reservations[cid.value] = reservation
            self.hedge._terminal_events[cid.value] = terminal
        return order

    def report(self, *, cache: Cache | None = None, fx: FxConfig = FX) -> RunAccountingReport:
        return build_run_accounting_report(
            self.engine.cache if cache is None else cache,
            cast(BitfinexV1ExecutionClient, self.source),
            cast(Mt5V1ExecutionClient, self.hedge),
            trader_id=TestIdStubs.trader_id(), strategy_id=OWNER, fx=fx,
        )


def test_real_native_currency_domains_disprove_both_naive_fee_subtractions(tmp_path: Path) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        for source in (True, False):
            history.fill(source, OrderSide.BUY)
            history.fill(source, OrderSide.SELL)
        native = {p.settlement_currency.code: p.realized_pnl.as_decimal()
                  for p in engine.cache.positions()}
        assert native == {"USD": D("-2.50"), "USDT": D(0)}
        # Existing native behavior: subtracting all fees again gives -7.50;
        # subtracting only provisional corrections misses the BFX USD fee entirely.
        assert history.source.fee_summary().currencies["USD"].provisional_correction == 0
        report = history.report()
        assert report.status == "FINAL"
        usd = report.currencies["USD"]
        assert (usd.native_booked_cost, usd.native_pnl_embedded_cost) == (D(5), D("2.5"))
        assert usd.venue_raw_cost == 5 and usd.provisional_correction == 0
        assert usd.final_realized_pnl == -5
        assert report.final_realized_pnl_usdt == D("-5.10")


@pytest.mark.parametrize("provisional", [False, True], ids=["tu-booked", "paper-te"])
@pytest.mark.parametrize("fee", ["-0.061668", "0.061668"], ids=["expense", "rebate"])
def test_final_raw_rounding_and_fx_are_separate_from_native_booking(
    tmp_path: Path, provisional: bool, fee: str,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        history.fill(True, OrderSide.BUY, fee=fee, provisional=provisional)
        history.fill(True, OrderSide.SELL, fee=fee, provisional=provisional)
        report = history.report()
        assert report.status == "FINAL"
        usd = report.currencies["USD"]
        raw, quantized = -2 * D(fee), 2 * usd_commission(D(fee)).as_decimal()
        assert usd.venue_raw_cost == raw and usd.venue_quantized_cost == quantized
        assert usd.native_booked_cost == (0 if provisional else quantized)
        assert usd.rounding_delta == raw - quantized
        assert usd.provisional_correction == (quantized if provisional else 0)
        assert usd.native_pnl_embedded_cost == 0
        expected = -raw * (FX.usd_usdt_ask if raw > 0 else FX.usd_usdt_bid)
        assert report.final_realized_pnl_usdt == expected


@pytest.mark.parametrize("mode", ["pending", "dirty", "conflict", "orphan", "missing-native"])
def test_incomplete_fee_evidence_is_pending_not_assumed_zero(tmp_path: Path, mode: str) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        order = history.fill(True, OrderSide.BUY, provisional=True, pending=mode == "pending")
        owner = history.source._cid_store
        if mode == "dirty":
            owner.fees_durable = False
        elif mode == "conflict":
            owner.mark_accounting_conflict()
        elif mode == "orphan":
            owner.allocate("LOST-ORDER", epoch_ms=100)
        elif mode == "missing-native":
            metadata = owner.fee_metadata[0]
            owner._fee_metadata[metadata.cid] = replace(metadata, native_fills=())
        if mode == "pending":
            assert not history.source.accounting_incomplete
        report = history.report()
        assert report.status == "PENDING" and report.final_realized_pnl_usdt is None
        assert report.pending_reasons
        assert all(value.final_realized_pnl is None for value in report.currencies.values())
        assert engine.cache.order(order.client_order_id) is order


@pytest.mark.parametrize("flip", [False, True], ids=["reopen", "flip"])
def test_real_netting_snapshots_cover_old_cycles_and_split_trades(
    tmp_path: Path, flip: bool,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        history.fill(True, OrderSide.BUY, price=100)
        history.fill(True, OrderSide.SELL, quantity=2 if flip else 1, price=110)
        if not flip:
            history.fill(True, OrderSide.BUY, price=105)
        assert len(engine.cache.position_snapshots()) == 1
        report = history.report()
        assert report.status == "FINAL"
        assert report.currencies["USDT"].native_observed_realized_pnl == 10
        assert report.final_realized_pnl_usdt == D(10) - D("1.25") * (2 if flip else 3) * D("1.02")
        # A new cache containing only current native Positions is deliberately
        # incomplete history, not a claim to have exercised Redis restoration.
        incomplete = Cache()
        for order in engine.cache.orders():
            incomplete.add_order(order)
        for position in engine.cache.positions():
            incomplete.add_position(position, OmsType.NETTING)
        pending = history.report(cache=incomplete)
        assert pending.status == "PENDING"
        assert "native_position_history_incomplete" in pending.pending_reasons
        assert pending.final_realized_pnl_usdt is None


def test_report_is_read_only_and_repeatable_and_does_not_mark_open_inventory(
    tmp_path: Path,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        history.fill(True, OrderSide.BUY, price=100)
        history.fill(False, OrderSide.SELL, price=120)
        before = ([OrderFilled.to_dict(o.events[-1]) for o in engine.cache.orders()],
                  [p.to_dict() for p in engine.cache.positions()],
                  history.source._cid_store.path.read_bytes(),
                  deepcopy(history.hedge._terminal_events))
        first, second = history.report(), history.report()
        assert asdict(first) == asdict(second)
        assert first.status == "FINAL" and first.final_realized_pnl_usdt == D("-2.55")
        assert "unrealized_pnl" in first.excluded_cashflows
        assert "funding" in first.excluded_cashflows and "swap" in first.excluded_cashflows
        assert ([OrderFilled.to_dict(o.events[-1]) for o in engine.cache.orders()],
                [p.to_dict() for p in engine.cache.positions()],
                history.source._cid_store.path.read_bytes(),
                history.hedge._terminal_events) == before


@pytest.mark.parametrize("mode", ["missing", "changed", "unresolved"])
def test_mt5_raw_fee_requires_matching_native_trade_and_settled_journal(
    tmp_path: Path, mode: str,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        order = history.fill(False, OrderSide.BUY, fee="-0.061668")
        key = order.client_order_id.value
        if mode == "missing":
            history.hedge._terminal_events.clear()
            history.hedge._reservations.clear()
        elif mode == "changed":
            cast(JsonObject, history.hedge._terminal_events[key]["payload"])["commission"] = "-1.25"
        else:
            history.hedge._pending[key] = object()
        report = history.report()
        assert report.status == "PENDING" and report.final_realized_pnl_usdt is None


def test_mt5_accessor_preserves_unrounded_sign_and_does_not_expose_mutable_owner(
    tmp_path: Path,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        order = history.fill(False, OrderSide.BUY, fee="-0.061668")
        before = deepcopy(history.hedge._terminal_events)
        fees = history.hedge.raw_commission_cashflows()
        assert fees == {(order.client_order_id, order.trade_ids[0]): D("-0.061668")}
        fees.clear()
        assert history.hedge._terminal_events == before
        report = history.report()
        assert report.status == "FINAL"
        assert report.currencies["USD"].rounding_delta == D("0.001668")
        assert report.final_realized_pnl_usdt == D("-0.06290136")
        history.hedge._terminal_events.clear()  # A dangling reservation remains unknown.
        with pytest.raises(Mt5V1ExecutionError, match="unresolved"):
            history.hedge.raw_commission_cashflows()


@pytest.mark.parametrize("bid,ask", [("0", "1"), ("1", "-1"), ("2", "1"), ("NaN", "1")])
def test_fx_must_be_explicit_finite_positive_and_not_crossed(
    tmp_path: Path, bid: str, ask: str,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        with pytest.raises(ValueError, match="FX"):
            history.report(fx=FxConfig(usd_usdt_bid=D(bid), usd_usdt_ask=D(ask)))


@pytest.mark.parametrize("field", ["instrument_id", "raw_symbol", "venue_order_id"])
def test_matching_trade_math_cannot_hide_foreign_fee_metadata_scope(
    tmp_path: Path, field: str,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        history.fill(True, OrderSide.BUY)
        owner = history.source._cid_store
        metadata = owner.fee_metadata[0]
        if field == "venue_order_id":
            changed = replace(metadata, venue_order_id=999999)
        elif field == "instrument_id":
            changed = replace(metadata, instrument_id="FOREIGN")
        else:
            changed = replace(metadata, raw_symbol="FOREIGN")
        owner._fee_metadata[metadata.cid] = changed
        # The original fee summary checks trade math, not native order scope.
        assert history.source.fee_summary().complete
        report = history.report()
        assert report.status == "PENDING" and report.final_realized_pnl_usdt is None
        assert any("fee_scope" in reason for reason in report.pending_reasons)


def test_native_partial_fill_stays_pending_even_when_observed_fees_are_final(
    tmp_path: Path,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        order = history.fill(True, OrderSide.BUY, total_quantity=2)
        assert not order.is_closed and history.source.fee_summary().complete
        report = history.report()
        assert report.status == "PENDING" and report.final_realized_pnl_usdt is None
        assert report.pending_reasons == (f"unresolved_order:{order.client_order_id}",)


def test_duplicate_native_snapshot_is_not_counted_as_additional_realized_profit(
    tmp_path: Path,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        history.fill(True, OrderSide.BUY)
        history.fill(True, OrderSide.SELL, price=110)
        position = engine.cache.positions()[0]
        assert history.report().status == "FINAL"
        engine.cache.snapshot_position(position)  # Current Position remains in the cache as well.
        report = history.report()
        assert report.status == "PENDING" and report.final_realized_pnl_usdt is None
        assert "native_position_history_incomplete" in report.pending_reasons


def test_per_owner_scope_and_usd_netting_precede_fx(tmp_path: Path) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        history.fill(False, OrderSide.BUY)
        history.fill(False, OrderSide.SELL, price=110)
        history.fill(True, OrderSide.BUY, owner=StrategyId("OTHER-001"))
        history.fill(False, OrderSide.BUY, owner=StrategyId("OTHER-001"))
        report = history.report()
        assert report.status == "FINAL"
        assert report.currencies["USD"].final_realized_pnl == D("7.5")
        # Converting gross profit on bid and expense on ask would give 7.55.
        assert report.final_realized_pnl_usdt == D("7.575")


@pytest.mark.parametrize("field", ["quantity", "venue_order_id", "fill_price",
                                   "venue_position_id", "event_time_ms"])
def test_same_mt5_fee_and_trade_id_cannot_hide_different_native_fill_facts(
    tmp_path: Path, field: str,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        order = history.fill(False, OrderSide.BUY)
        cid = order.client_order_id.value
        terminal = history.hedge._terminal_events[cid]
        reservation = history.hedge._reservations[cid]
        payload = cast(JsonObject, terminal["payload"])
        if field == "quantity":
            # Both raw lots agree with each other but contradict the 1 oz native fill.
            cast(JsonObject, reservation["payload"])["quantity_lots"] = "0.02"
            payload.update(quantity_lots="0.02", filled_quantity_lots="0.02")
        elif field == "event_time_ms":
            terminal[field] = "999"
        else:
            payload[field] = "101" if field == "fill_price" else "999999"
        _validate_event(reservation, stream_id="a07-stream")
        _validate_event(terminal, stream_id="a07-stream")
        before = [event.id for event in order.events]
        report = history.report()
        assert report.status == "PENDING" and report.final_realized_pnl_usdt is None
        assert "hedge_commission_history_unresolved" in report.pending_reasons
        assert [event.id for event in order.events] == before


@pytest.mark.parametrize("missing", ["instrument", "order"])
def test_mt5_raw_fees_without_native_correspondence_remain_pending(
    tmp_path: Path, missing: str,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "cid")
        order = history.fill(False, OrderSide.BUY)
        incomplete = Cache()
        if missing == "instrument":
            incomplete.add_order(order)
        else:
            instrument = engine.cache.instrument(order.instrument_id)
            assert instrument is not None
            incomplete.add_instrument(instrument)
        history.hedge._cache = incomplete
        report = history.report(cache=incomplete)
        assert report.status == "PENDING" and report.final_realized_pnl_usdt is None


def test_actual_mt5_disconnect_keeps_raw_fees_readable_without_snapshot_or_io() -> None:
    async def scenario() -> None:
        harness, order, _ = await _connected_report_harness(asyncio.get_running_loop())
        harness.cache.add_instrument(harness.instrument)
        _cache_report_fill(harness, order)
        await harness.client._disconnect()
        assert harness.fake.closed and harness.client._snapshot is None
        before = (list(order.events), list(harness.fake.event_calls),
                  list(harness.fake.snapshot_calls))
        expected = {(order.client_order_id, TradeId("800000002")): D("-1.25")}
        assert harness.client.raw_commission_cashflows() == expected
        assert harness.client.raw_commission_cashflows() == expected
        assert (list(order.events), harness.fake.event_calls, harness.fake.snapshot_calls) == before
        assert harness.fake.closed
        assert not harness.fake.submit_calls and not harness.fake.close_calls

    asyncio.run(scenario())
