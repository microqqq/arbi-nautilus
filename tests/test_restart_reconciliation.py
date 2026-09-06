"""W6b native facts and held source projection, with synthetic execution history.

These tests do not certify process durability or automatic strategy recovery.
No strategy or trading transport starts and no request is submitted.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import test_bitfinex_v1_execution as bfx
import test_mt5_v1_execution as mt5
import test_restart_ownership as q3
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.events import OrderFilled, OrderInitialized
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    StrategyId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import LimitOrder, MarketOrder
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs
from restart_replay_worker import _engine, _summary

from py000_nautilus.app import _hedge_instrument, _source_instrument
from py000_nautilus.hedge import HedgeCoordinator
from py000_nautilus.live_cache import validate_native_cache
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import BusinessOrderSide, ObligationStatus, SourceDirection
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.maker import MakerStrategy
from py000_nautilus.strategies.taker import TakerStrategy


@pytest.mark.parametrize("swapped", [False, True], ids=["correct-targets", "swapped-targets"])
def test_complete_cache_close_targets_must_match_journal_before_native_reconciliation(
    swapped: bool,
) -> None:
    async def scenario() -> None:
        harness, originals = q3._mt5_history()
        engine = mt5._live_engine(harness, generate_missing_orders=False)
        owner = StrategyId("W6B-MT5")
        history = deepcopy(cast(
            list[JsonObject], cast(JsonObject, harness.fake.pages[0])["events"],
        ))
        for original, ticket in zip(originals, q3._TICKETS, strict=True):
            values = OrderInitialized.to_dict(original.events[0])
            values["strategy_id"] = owner.value
            order = OrderUnpacker.from_init(OrderInitialized.from_dict(values))
            order.apply(TestEventStubs.order_submitted(order, harness.client.account_id))
            harness.cache.add_order(order, position_id=ticket, client_id=harness.client.id)
            harness.cache.update_order(order)
        try:
            await harness.connect()
            assert await engine.reconcile_execution_state(timeout_secs=1)
            assert {position.quantity.as_decimal() for position in harness.cache.positions()} == {
                Decimal(100),
            }
            await harness.client._disconnect()

            closes = []
            for index, target in enumerate(q3._TICKETS):
                close = MarketOrder(
                    trader_id=originals[0].trader_id, strategy_id=owner,
                    instrument_id=harness.instrument.id,
                    client_order_id=ClientOrderId(f"W6B-CLOSE-{index}"),
                    order_side=OrderSide.SELL, quantity=harness.instrument.make_qty(1),
                    time_in_force=TimeInForce.FOK, reduce_only=True,
                    init_id=UUID4(), ts_init=100 + index,
                )
                close.apply(TestEventStubs.order_submitted(close, harness.client.account_id))
                cached_target = q3._TICKETS[1 - index] if swapped else target
                harness.cache.add_order(
                    close, client_id=harness.client.id, position_id=cached_target,
                )
                harness.cache.update_order(close)
                closes.append(close)
                sequence = 6 + index * 2
                position_ticket = f"700000{101 + index}"
                history.append(mt5._event(
                    harness.identity, sequence, "submission_reserved",
                    mt5._submission_payload(
                        close, "0.01", position_ticket=position_ticket,
                        position_identifier=target.value,
                    ),
                ))
                terminal = mt5._outcome(
                    harness.identity, close, "order_filled", sequence=sequence + 1,
                    quantity_lots="0.01", position_ticket=position_ticket,
                    position_identifier=target.value, venue_position_id=target.value,
                )
                cast(JsonObject, terminal["payload"]).update({
                    "venue_deal_id": f"800000{201 + index}",
                    "venue_order_id": f"700000{201 + index}",
                })
                history.append(terminal)

            # Both alternatives are complete under W6a: one owner, real positions,
            # correct clients and dependencies. Only external journal facts differ.
            assert validate_native_cache(
                harness.cache, trader_id=originals[0].trader_id, strategy_id=owner,
                routes={harness.instrument.id: (harness.client.account_id, harness.client.id)},
            ) is True
            before = _summary(harness)
            snapshot = deepcopy(harness.fake.current_snapshot)
            for position in cast(list[JsonObject], snapshot["positions"]):
                position["volume_lots"] = "0.99"
            harness.fake.current_snapshot = snapshot
            harness.fake.pages.append(mt5._page(
                harness.identity, after_cursor="0", events=history,
            ))
            await harness.connect()  # Read the complete journal; no local pending submission.
            assert harness.client.pending_client_order_ids == ()
            reconciled = await engine.reconcile_execution_state(timeout_secs=1)
            if swapped:
                assert reconciled is False
                assert _summary(harness) == before  # No misattributed fill reached native cache.
                assert not harness.client.execution_admitted and harness.fake.closed
            else:
                assert reconciled is True
                for index, (close, target) in enumerate(zip(closes, q3._TICKETS, strict=True)):
                    assert close.status is OrderStatus.FILLED
                    assert close.filled_qty.as_decimal() == 1
                    assert close.position_id == target
                    assert close.trade_ids == [TradeId(f"800000{201 + index}")]
                    assert harness.cache.position(target).quantity.as_decimal() == 99
                completed = _summary(harness)
                assert await engine.reconcile_execution_state(timeout_secs=1)
                assert _summary(harness) == completed
            assert not harness.fake.submit_calls and not harness.fake.close_calls
        finally:
            await harness.client._disconnect()
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", [None, "order-price", "opposing-extra-fills", "unknown-cid"])
@pytest.mark.parametrize("partial_cancel", [False, True], ids=["filled", "partial-canceled"])
def test_closed_source_history_cannot_hide_conflicts_behind_matching_net_position(
    tmp_path: Path, fault: str | None, partial_cancel: bool,
) -> None:
    async def scenario() -> None:
        harness = bfx._Harness(cid_store_path=tmp_path / "cids.json")
        engine = q3._source_engine(harness)
        engine.start()
        now_ms = harness.clock.timestamp_ns() // 1_000_000
        try:
            # Seed known events through the actual engine, with no live adapter
            # order objects. These are two historical fills, not new submissions.
            for index, side in enumerate((OrderSide.BUY, OrderSide.SELL)):
                order = harness.order(
                    client_order_id=ClientOrderId(f"W6B4-CLOSED-{index}"),
                    side=side, quantity="4" if partial_cancel else "2", price="3926.75",
                    tif=TimeInForce.IOC, post_only=False,
                )
                binding = harness.client._cid_store.allocate(
                    order.client_order_id.value, epoch_ms=now_ms,
                )
                venue_id = VenueOrderId(str(bfx.VENUE_ORDER_ID + index))
                trade_id = TradeId(str(810000100 + index))
                harness.cache.add_order(order, client_id=harness.client.id)
                engine.process(TestEventStubs.order_submitted(
                    order, harness.client.account_id, ts_event=(now_ms - 200) * 1_000_000,
                ))
                engine.process(TestEventStubs.order_accepted(
                    order, harness.client.account_id, venue_id,
                    ts_event=(now_ms - 100) * 1_000_000,
                ))
                engine.process(TestEventStubs.order_filled(
                    order=order, instrument=harness.instrument,
                    account_id=harness.client.account_id, venue_order_id=venue_id,
                    trade_id=trade_id, last_qty=harness.instrument.make_qty(2),
                    last_px=harness.instrument.make_price(Decimal("3926.75")),
                    commission=Money("0.10", USD), ts_event=now_ms * 1_000_000,
                ))
                for _ in range(20):
                    await asyncio.sleep(0)
                if partial_cancel:
                    engine.process(TestEventStubs.order_canceled(
                        order, harness.client.account_id,
                        ts_event=now_ms * 1_000_000,
                    ))
                    for _ in range(20):
                        await asyncio.sleep(0)
                assert order.status is (
                    OrderStatus.CANCELED if partial_cancel else OrderStatus.FILLED
                )
                sign = 1 if side is OrderSide.BUY else -1
                terminal = cast(list[object], harness.order_frame(
                    "oc", binding.cid, order, remaining=str(2 * sign) if partial_cancel else "0",
                    status="CANCELED" if partial_cancel else "EXECUTED @ 3926.75(2)",
                )[2])
                terminal[0] = int(venue_id.value)
                terminal[4:6] = [now_ms - 100, now_ms]
                terminal[17] = Decimal("3926.75")
                trade = cast(list[object], harness.trade_frame(
                    binding.cid, order, trade_id=int(trade_id.value),
                    quantity="2", maker=-1,
                )[2])
                trade[2:4] = [now_ms, int(venue_id.value)]
                if fault == "order-price":
                    terminal[16] = Decimal("3926.76")
                elif fault == "opposing-extra-fills":
                    trade[4] = Decimal(3 * sign)
                    if partial_cancel:
                        terminal[6] = Decimal(sign)  # Original quantity stays four.
                    else:
                        terminal[7] = trade[4]
                        terminal[13] = "EXECUTED @ 3926.75(3)"
                elif fault == "unknown-cid":
                    terminal[2] = trade[-1] = binding.cid + 10000
                harness.rest.history.append(terminal)
                harness.rest.trades.append(trade)

            assert not harness.cache.positions_open()
            assert not harness.client._by_cid  # Cold/retired closed orders take the same path.
            harness.rest.position_rows = []  # Venue net remains zero even with both extra fills.
            before = _summary(harness)
            await harness.connect()
            sent_before = deepcopy(harness.fake.sent)
            assert all(isinstance(message, dict) for message in sent_before)  # Auth only.
            assert await engine.reconcile_execution_state(timeout_secs=1) is (fault is None)
            assert _summary(harness) == before  # Neither conflict nor duplicate rewrites history.
            if fault is None:
                assert await engine.reconcile_execution_state(timeout_secs=1)
                assert _summary(harness) == before
            assert harness.fake.sent == sent_before
        finally:
            engine.stop()
            await asyncio.sleep(0)
            await harness.close()
            engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("record_prefix", [False, True], ids=["missing-all", "missing-suffix"])
def test_native_source_fills_project_once_without_resubmitting_or_mutating_native_state(
    tmp_path: Path, maker: bool, record_prefix: bool,
) -> None:
    # This is a native-event/store composition, not automatic startup recovery.
    # No strategy starts, and the native engine has only synthetic simulation venues.
    from py000_nautilus.source_projection import project_source_fills

    path = tmp_path / "business"
    source_id, hedge_id = _source_instrument().id, _hedge_instrument().id

    def load() -> JsonStateStore | MakerStateStore:
        if maker:
            return MakerStateStore(path, source_id.value, hedge_id.value)
        return JsonStateStore(path)

    business = load()
    view = (business.stores[SourceDirection.LONG]
            if isinstance(business, MakerStateStore) else business)
    cid = ClientOrderId("W6B2-SOURCE")
    view.begin_source(
        cid.value, BusinessOrderSide.BUY, Decimal(2),
        source_account_id="BITFINEX-001", source_client_id="BITFINEX",
        hedge_account_id="MT5-001", hedge_client_id="MT5",
    )
    with _engine() as engine:
        instrument = engine.cache.instrument(source_id)
        assert instrument is not None
        order = LimitOrder(
            trader_id=TestIdStubs.trader_id(), strategy_id=StrategyId("W6B2-001"),
            instrument_id=source_id, client_order_id=cid, order_side=OrderSide.BUY,
            quantity=instrument.make_qty(2), price=instrument.make_price(2400),
            time_in_force=TimeInForce.GTC, init_id=UUID4(), ts_init=0,
        )
        engine.cache.add_order(order, client_id=ClientId("BITFINEX"))
        process = engine.kernel.exec_engine.process
        account, venue_order = AccountId("BITFINEX-001"), VenueOrderId("710000101")
        process(TestEventStubs.order_submitted(order, account, ts_event=1))
        process(TestEventStubs.order_accepted(order, account, venue_order, 2))
        for index, amount in enumerate(("0.4", "0.7", "0.9")):
            event = TestEventStubs.order_filled(
                order=order, instrument=instrument, account_id=account,
                venue_order_id=venue_order, trade_id=TradeId(f"81000010{index}"),
                last_qty=instrument.make_qty(Decimal(amount)),
                last_px=instrument.make_price(2400), commission=Money(0, USD),
                ts_event=index + 3,
            )
            process(event)
            if record_prefix and index == 0:
                assert HedgeCoordinator(source_id, view).on_source_filled(event) is None
        assert order.status is OrderStatus.FILLED and order.filled_qty.as_decimal() == 2
        assert len(engine.cache.positions()) == 1
        assert engine.cache.positions()[0].quantity.as_decimal() == 2
        source_record = view.source_order(cid.value)
        assert source_record is not None and source_record.filled_ounces == (
            Decimal("0.4") if record_prefix else Decimal(0)
        )
        assert view.recover_for_start() is not None
        original_hold = view.halt_reason
        original_freeze = view.source_freeze_reason
        native_before = _summary(engine)
        assert validate_native_cache(
            engine.cache, trader_id=order.trader_id, strategy_id=order.strategy_id,
            routes={source_id: (account, ClientId("BITFINEX")),
                    hedge_id: (AccountId("MT5-001"), ClientId("MT5"))},
        )

        def project(target: JsonStateStore | MakerStateStore) -> int:
            return project_source_fills(
                target, [order], source_instrument_id=source_id,
                trader_id=order.trader_id, strategy_id=order.strategy_id,
                reason="source recovery awaits complete restart reconciliation",
            )

        assert project(business) == (2 if record_prefix else 3)
        assert _summary(engine) == native_before
        published = business.path.read_bytes()
        assert project(business) == 0 and business.path.read_bytes() == published
        reloaded = load()
        assert project(reloaded) == 0 and reloaded.path.read_bytes() == published

        def unexpected_dispatch(*_args: object) -> None:
            pytest.fail("recovered source obligation must not dispatch a hedge")

        for target in (business, reloaded):
            current = (target.stores[SourceDirection.LONG]
                       if isinstance(target, MakerStateStore) else target)
            record = current.source_order(cid.value)
            assert record is not None and record.filled_ounces == 2
            assert len(current.intents()) == 2 and current.net_unhedged_ounces == 2
            assert all(intent.status is ObligationStatus.BLOCKED for intent in current.intents())
            assert current.halt_reason == original_hold and not current.can_submit_source()
            if original_freeze is not None:
                assert current.source_freeze_reason == original_freeze
            if isinstance(target, MakerStateStore):
                assert all(part.source_freeze_reason for part in target.stores.values())
                assert target.next_pending_hedge() is None
                MakerStrategy._submit_next_pending_hedge(cast(MakerStrategy, SimpleNamespace(
                    _stores=target.stores, _state_store=target,
                    _durable_hedge_route=unexpected_dispatch, _submit_hedge=unexpected_dispatch,
                )))
            else:
                TakerStrategy._submit_next_pending_hedge(cast(TakerStrategy, SimpleNamespace(
                    state_store=current, _submit_hedge_intent=unexpected_dispatch,
                )))
        # Native TradeId dedup and business dedup remain separate, both idempotent.
        for fill in [event for event in order.events if isinstance(event, OrderFilled)]:
            process(fill)
        assert _summary(engine) == native_before and project(reloaded) == 0
        assert reloaded.path.read_bytes() == published
