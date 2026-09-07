"""Held business projection from synthetic native MT5 orders and real Positions.

This is not venue reconciliation, automatic startup, or an EA multi-fill claim.
No strategy or trading transport starts; source obligations use the existing reducer.
"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
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
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import MarketOrder, Order
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs
from restart_replay_worker import _engine, _summary

from py000_nautilus.app import _hedge_instrument, _source_instrument
from py000_nautilus.live_cache import validate_native_cache
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import (
    BusinessOrderSide,
    HedgeIntent,
    HedgeLeg,
    ObligationStatus,
    SourceDirection,
)
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.maker import MakerStrategy
from py000_nautilus.strategies.taker import TakerStrategy


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("opening", [False, True], ids=["current-close", "old-close-current-open"])
@pytest.mark.parametrize("prefix", [False, True], ids=["missing-all", "missing-last"])
def test_native_hedge_projection_keeps_tickets_and_never_dispatches_the_next_leg(
    tmp_path: Path,
    maker: bool,
    opening: bool,
    prefix: bool,
) -> None:
    from py000_nautilus.hedge_projection import project_hedge_fills

    source_id, hedge_id = _source_instrument().id, _hedge_instrument().id
    account, client = AccountId("MT5-001"), ClientId("MT5")
    trader, strategy = TestIdStubs.trader_id(), StrategyId("W6B3-001")
    reason = "hedge recovery awaits complete restart reconciliation"
    path = tmp_path / "business"

    def load() -> JsonStateStore | MakerStateStore:
        return (
            MakerStateStore(path, source_id.value, hedge_id.value)
            if maker
            else JsonStateStore(path)
        )

    business = load()

    def view_for(side: BusinessOrderSide) -> JsonStateStore:
        if isinstance(business, MakerStateStore):
            return business.stores[
                SourceDirection.LONG if side is BusinessOrderSide.BUY else SourceDirection.SHORT
            ]
        return business

    def source(
        cid: str, side: BusinessOrderSide, quantity: int
    ) -> tuple[JsonStateStore, HedgeIntent]:
        view = view_for(side)
        view.begin_source(
            cid,
            side,
            Decimal(quantity),
            source_account_id="BITFINEX-001",
            source_client_id="BITFINEX",
            hedge_account_id=account.value,
            hedge_client_id=client.value,
        )
        intent = view.reserve_source_fill(
            fill_key=f"{cid}|V-{cid}|T-{cid}",
            client_order_id=cid,
            trade_id=f"T-{cid}",
            source_side=side,
            fill_ounces=Decimal(quantity),
        )
        assert intent is not None
        return view, intent

    with _engine() as engine:
        instrument = engine.cache.instrument(hedge_id)
        assert instrument is not None
        process = engine.kernel.exec_engine.process
        orders: list[Order] = []

        def native(cid: str, side: OrderSide, *, close: bool, position: str) -> MarketOrder:
            order = MarketOrder(
                trader_id=trader,
                strategy_id=strategy,
                instrument_id=hedge_id,
                client_order_id=ClientOrderId(cid),
                order_side=side,
                quantity=instrument.make_qty(2),
                time_in_force=TimeInForce.FOK,
                reduce_only=close,
                init_id=UUID4(),
                ts_init=len(orders) * 10,
            )
            orders.append(order)
            engine.cache.add_order(
                order,
                client_id=client,
                position_id=PositionId(position) if close else None,
            )
            process(TestEventStubs.order_submitted(order, account, ts_event=order.ts_init + 1))
            process(
                TestEventStubs.order_accepted(
                    order,
                    account,
                    VenueOrderId(f"V-{cid}"),
                    order.ts_init + 2,
                )
            )
            return order

        def fill(order: Order, trade: str, quantity: int, position: str) -> None:
            process(
                TestEventStubs.order_filled(
                    order,
                    instrument,
                    account_id=account,
                    venue_order_id=order.venue_order_id,
                    trade_id=TradeId(trade),
                    position_id=PositionId(position),
                    last_qty=instrument.make_qty(quantity),
                    last_px=instrument.make_price(2400),
                    commission=Money(0, USD),
                    ts_event=order.ts_last + 1,
                )
            )

        # A completed earlier obligation really opens the ticket that the next plan closes.
        old_view, old = source("S-OLD", BusinessOrderSide.SELL, 2)
        old_view.bind_hedge_plan(old.intent_id, (HedgeLeg(BusinessOrderSide.BUY, Decimal(2)),))
        old_view.bind_hedge_order(old.intent_id, "H-OLD")
        prior = native("H-OLD", OrderSide.BUY, close=False, position="900000301")
        fill(prior, "T-OLD", 2, "900000301")
        assert old_view.apply_hedge_fill(
            client_order_id="H-OLD", trade_id="T-OLD", fill_ounces=Decimal(2)
        )
        if isinstance(business, MakerStateStore):
            assert business.clear_source_freezes()

        view, intent = source("S-CURRENT", BusinessOrderSide.BUY, 4)
        plan = (
            HedgeLeg(
                BusinessOrderSide.SELL, Decimal(2), "900000301", BusinessOrderSide.BUY, Decimal(2)
            ),
            HedgeLeg(BusinessOrderSide.SELL, Decimal(2)),
        )
        view.bind_hedge_plan(intent.intent_id, plan)
        view.bind_hedge_order(intent.intent_id, "H-CLOSE")
        current = native("H-CLOSE", OrderSide.SELL, close=True, position="900000301")
        position = "900000301"
        if opening:
            fill(current, "T-CLOSE", 2, position)
            assert view.apply_hedge_fill(
                client_order_id="H-CLOSE",
                trade_id="T-CLOSE",
                fill_ounces=Decimal(2),
            )
            view.bind_hedge_order(intent.intent_id, "H-OPEN")
            position = "900000302"
            current = native("H-OPEN", OrderSide.SELL, close=False, position=position)
        for index in range(2):
            trade = f"T-CURRENT-{index}"
            fill(current, trade, 1, position)
            if prefix and index == 0:
                view.update_hedge_status(current.client_order_id.value, ObligationStatus.UNKNOWN)
                assert view.apply_hedge_fill(
                    client_order_id=current.client_order_id.value,
                    trade_id=trade,
                    fill_ounces=Decimal(1),
                )
        assert current.status is OrderStatus.FILLED
        assert [item.signed_decimal_qty() for item in engine.cache.positions_open()] == (
            [Decimal(-2)] if opening else []
        )
        assert validate_native_cache(
            engine.cache,
            trader_id=trader,
            strategy_id=strategy,
            routes={
                source_id: (AccountId("BITFINEX-001"), ClientId("BITFINEX")),
                hedge_id: (account, client),
            },
        )
        assert view.recover_for_start() is not None
        before_intent = view.intent(intent.intent_id)
        old_completed = old_view.intent(old.intent_id)
        hold, freeze = view.halt_reason, view.source_freeze_reason
        native_before = _summary(engine)
        allocations = (
            deepcopy(business._allocations) if isinstance(business, MakerStateStore) else None
        )

        def project(target: JsonStateStore | MakerStateStore) -> int:
            return project_hedge_fills(
                target,
                list(reversed(orders)),
                hedge_instrument_id=hedge_id,
                trader_id=trader,
                strategy_id=strategy,
                reason=reason,
            )

        # Even when the bad target is on an already completed leg, totals cannot hide it.
        view._state.hedge_intents[intent.intent_id] = replace(
            before_intent,
            hedge_plan=(replace(plan[0], position_id="WRONG-TICKET"), plan[1]),
        )
        conflicting, disk = deepcopy(business._to_payload()), business.path.read_bytes()
        with pytest.raises(ValueError):
            project(business)
        assert business._to_payload() == conflicting and business.path.read_bytes() == disk
        assert _summary(engine) == native_before
        view._state.hedge_intents[intent.intent_id] = before_intent

        assert project(business) == (1 if prefix else 2)
        published = business.path.read_bytes()
        assert project(business) == 0 and project(load()) == 0
        assert business.path.read_bytes() == published and _summary(engine) == native_before
        if isinstance(business, MakerStateStore):
            assert business._allocations == allocations

        def unexpected_dispatch(*_args: object) -> None:
            pytest.fail("held hedge projection cannot submit the next leg")

        for target in (business, load()):
            view_now = (
                target.stores[SourceDirection.LONG]
                if isinstance(target, MakerStateStore)
                else target
            )
            projected = view_now.intent(intent.intent_id)
            assert projected.status is ObligationStatus.BLOCKED
            assert projected.hedge_client_order_id == current.client_order_id.value
            assert projected.hedge_leg_index == int(opening)
            assert projected.hedge_leg_filled_ounces == 2
            assert projected.hedge_filled_ounces == (4 if opening else 2)
            assert view_now.halt_reason == hold and not view_now.can_submit_source()
            assert view_now.source_freeze_reason == (freeze or reason)
            if isinstance(target, MakerStateStore):
                assert target.stores[SourceDirection.SHORT].intent(old.intent_id) == old_completed
                assert all(
                    part.halt_reason and part.source_freeze_reason
                    for part in target.stores.values()
                )
                MakerStrategy._submit_next_pending_hedge(
                    cast(
                        MakerStrategy,
                        SimpleNamespace(
                            _stores=target.stores,
                            _state_store=target,
                            _durable_hedge_route=unexpected_dispatch,
                            _submit_hedge=unexpected_dispatch,
                        ),
                    )
                )
            else:
                assert target.intent(old.intent_id) == old_completed
                TakerStrategy._submit_next_pending_hedge(
                    cast(
                        TakerStrategy,
                        SimpleNamespace(
                            state_store=target,
                            _submit_hedge_intent=unexpected_dispatch,
                        ),
                    )
                )
        for event in [event for event in current.events if isinstance(event, OrderFilled)]:
            process(event)
        assert _summary(engine) == native_before and project(load()) == 0
        assert business.path.read_bytes() == published
