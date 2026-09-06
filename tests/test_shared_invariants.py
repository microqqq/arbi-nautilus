"""Independent W7 admission/ownership checks, with no trading connection or strategy run."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from msgspec.structs import replace as config_replace
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.cache.cache import Cache
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OmsType, OrderSide, TimeInForce
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
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import LimitOrder, MarketOrder, Order
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs
from restart_replay_worker import _engine, _summary
from test_accounting_report import FX, _History
from test_live_cache import _OWNER, _ROUTES, _TRADER, _cache, _netting_history, _order
from test_live_maker import _configs
from test_shared_account_native import _cross_owner_close, _facts

from py000_nautilus import live_lifecycle
from py000_nautilus import store as store_module
from py000_nautilus.accounting_report import build_run_accounting_report
from py000_nautilus.app import _hedge_instrument, _source_instrument
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionClient
from py000_nautilus.config import RiskConfig
from py000_nautilus.hedge_projection import project_hedge_fills
from py000_nautilus.live_cache import validate_native_cache
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import (
    BusinessOrderSide,
    HedgeAccount,
    HedgeIntent,
    HedgeLeg,
    ObligationStatus,
    SourceAccount,
)
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionClient
from py000_nautilus.restart_recovery import shared_business_owners
from py000_nautilus.shared_admission import SharedSourceAdmission
from py000_nautilus.source_projection import project_source_fills
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.maker import MakerStrategy
from py000_nautilus.strategies.taker import TakerStrategy

D = Decimal
BUY, SELL = BusinessOrderSide.BUY, BusinessOrderSide.SELL
OWNERS = (StrategyId("INVARIANT-MAKER"), StrategyId("INVARIANT-TAKER"))


class _Admission:
    """Actual Cache/order lifecycle, with explicitly normalized account-reader outputs."""

    def __init__(self, path: Path, *, source_net: int = 0, hedge_net: int = 0,
                 maximum: int = 10, minimum: int = 0, only_long: bool = False) -> None:
        config = _configs(path).strategy
        config = config_replace(config, economics=config_replace(config.economics, risk=RiskConfig(
            source_max_abs=D(maximum), hedge_max_abs=D(maximum),
            source_min_keep_abs=D(minimum), hedge_min_keep_abs=D(minimum), only_long=only_long,
        )))
        self.cache = Cache()
        for instrument in (_source_instrument(), _hedge_instrument()):
            self.cache.add_instrument(instrument)
            self.cache.add_quote_tick(QuoteTick(
                instrument.id, instrument.make_price(2400), instrument.make_price(2401),
                instrument.make_qty(100), instrument.make_qty(100), 1, 1,
            ))
        self.owner = MakerStateStore(
            path / "shared", str(config.source_instrument_id), str(config.hedge_instrument_id),
            shared_strategy_ids=(str(OWNERS[0]), str(OWNERS[1])),
        )
        route = config.source_accounts[0]
        self.source = SourceAccount(route.account_id, route.client_id, D(source_net),
                                    D(100), D(100), D(100))
        self.hedge = HedgeAccount(D(hedge_net), D(100), D(100))
        self.admit = SharedSourceAdmission(self.cache, self.owner, config)
        self.admit.reader = lambda *_: (self.source, self.hedge, 10, True)

    def working(self, view: JsonStateStore, side: BusinessOrderSide, quantity: int,
                state: str = "ACCEPTED") -> LimitOrder:
        instrument = self.cache.instrument(_source_instrument().id)
        assert instrument is not None
        cid = f"WORKING-{len(self.cache.orders())}"
        view.begin_source(cid, side, D(quantity), source_account_id=self.source.account_id.value,
                          source_client_id="BITFINEX", hedge_account_id="MT5-12345678",
                          hedge_client_id="MT5")
        owner_id = self.owner.strategy_id_for(view)
        assert owner_id is not None
        order = LimitOrder(
            trader_id=TestIdStubs.trader_id(), strategy_id=StrategyId(owner_id),
            instrument_id=instrument.id, client_order_id=ClientOrderId(cid),
            order_side=OrderSide.BUY if side is BUY else OrderSide.SELL,
            quantity=instrument.make_qty(quantity), price=instrument.make_price(2400),
            time_in_force=TimeInForce.GTC, init_id=UUID4(), ts_init=0,
        )
        self.cache.add_order(order, client_id=ClientId("BITFINEX"))
        order.apply(TestEventStubs.order_submitted(order, self.source.account_id, ts_event=1))
        order.apply(TestEventStubs.order_accepted(
            order, self.source.account_id, VenueOrderId(cid), ts_event=2,
        ))
        if state == "PENDING_CANCEL":
            order.apply(TestEventStubs.order_pending_cancel(order, ts_event=3))
        elif state == "PENDING_UPDATE":
            order.apply(TestEventStubs.order_pending_update(order, ts_event=3))
        self.cache.update_order(order)
        assert order.status.name == state and order.leaves_qty.as_decimal() == quantity
        return order


@pytest.mark.parametrize("pause", ["freeze", "unknown"])
def test_public_peer_pause_blocks_an_otherwise_qualified_taker_admission(
    tmp_path: Path, pause: str,
) -> None:
    h = _Admission(tmp_path)
    bid, _, taker = h.owner.all_views()
    assert h.admit(taker, BUY, D(1))
    if pause == "freeze":
        bid.freeze_source_submissions("external operator HOLD")
    else:
        bid.mark_source_unknown("no-active-order", "external account UNKNOWN")
    assert taker.can_submit_source()  # Positive control: it is specifically the peer gate.
    before = h.owner._snapshot(), h.owner.path.read_bytes()
    assert not h.admit(taker, BUY, D(1))
    assert (h.owner._snapshot(), h.owner.path.read_bytes()) == before


@pytest.mark.parametrize("state", ["ACCEPTED", "PENDING_CANCEL", "PENDING_UPDATE"])
@pytest.mark.parametrize("side", [BUY, SELL])
def test_shared_worst_leaves_do_not_net_opposing_working_orders(
    tmp_path: Path, state: str, side: BusinessOrderSide,
) -> None:
    h = _Admission(tmp_path, maximum=3)
    bid, ask, taker = h.owner.all_views()
    assert h.admit(taker, side, D(2))
    h.working(bid, BUY, 2, state)
    h.working(ask, SELL, 2, state)
    before = h.owner._snapshot()
    assert not h.admit(taker, side, D(2))  # 4 on one side, not a net reservation of 2.
    assert h.admit(taker, side, D(1))  # Exactly 3 on that side remains valid.
    assert h.owner._snapshot() == before


def test_cancel_ack_releases_leaves_only_after_terminal_business_confirmation(
    tmp_path: Path,
) -> None:
    h = _Admission(tmp_path, maximum=3)
    bid, _, taker = h.owner.all_views()
    order = h.working(bid, BUY, 2, "PENDING_CANCEL")
    assert not h.admit(taker, BUY, D(2))
    order.apply(TestEventStubs.order_canceled(order, ts_event=4))
    h.cache.update_order(order)
    bid.update_source_status(order.client_order_id.value, "CANCELED")
    assert not h.admit(taker, BUY, D(2))
    bid.confirm_source_reconciled(order.client_order_id.value)
    assert h.admit(taker, BUY, D(2))


@pytest.mark.parametrize("source_net,side,allowed", [(12, SELL, True), (12, BUY, False),
                                                     (-12, BUY, True), (-12, SELL, False)])
def test_over_limit_accounts_may_reduce_but_not_increase_worst_exposure(
    tmp_path: Path, source_net: int, side: BusinessOrderSide, allowed: bool,
) -> None:
    h = _Admission(tmp_path, source_net=source_net, hedge_net=-source_net)
    assert h.admit(h.owner.all_views()[2], side, D(2)) is allowed


@pytest.mark.parametrize("quantity,allowed", [(1, True), (2, False), (5, False)])
def test_only_long_keeps_each_venue_minimum_and_never_crosses_zero(
    tmp_path: Path, quantity: int, allowed: bool,
) -> None:
    h = _Admission(tmp_path, source_net=4, hedge_net=-4, minimum=3, only_long=True)
    assert h.admit(h.owner.all_views()[2], SELL, D(quantity)) is allowed


@pytest.mark.parametrize("venue", ["source", "hedge"])
def test_shared_working_leaves_consume_directional_dynamic_capacity(
    tmp_path: Path, venue: str,
) -> None:
    h = _Admission(tmp_path)
    bid, _, taker = h.owner.all_views()
    h.working(bid, BUY, 2)
    assert h.admit(taker, BUY, D(1))
    if venue == "source":
        h.source = replace(h.source, max_long_ounces=D(2))
    else:
        h.hedge = replace(h.hedge, max_short_ounces=D(2))
    assert not h.admit(taker, BUY, D(1))


@pytest.mark.parametrize("misowned", [False, True])
def test_native_cross_owner_close_requires_exact_cid_binding_not_an_allowed_owner_set(
    misowned: bool,
) -> None:
    async def scenario() -> None:
        async with _cross_owner_close("initial-owner" if misowned else None) as (h, engine, close):
            assert await engine.reconcile_execution_state(timeout_secs=1)
            orders = sorted(h.cache.orders(), key=lambda order: order.client_order_id.value)
            originals = [order for order in orders if order is not close]
            expected_close_owner = originals[1].strategy_id
            owners = {order.client_order_id.value: order.strategy_id for order in originals}
            owners[close.client_order_id.value] = expected_close_owner
            before = _facts(h)
            assert h.cache.check_integrity()
            if misowned:
                assert close.strategy_id in set(owners.values())
                with pytest.raises(ValueError, match="order identity mismatch"):
                    validate_native_cache(
                        h.cache, trader_id=close.trader_id, strategy_id=originals[0].strategy_id,
                        routes={h.instrument.id: (h.client.account_id, h.client.id)},
                        business_owners=owners,
                    )
            else:
                assert validate_native_cache(
                    h.cache, trader_id=close.trader_id, strategy_id=originals[0].strategy_id,
                    routes={h.instrument.id: (h.client.account_id, h.client.id)},
                    business_owners=owners,
                )
                position = h.cache.position(close.position_id)
                assert position.strategy_id != close.strategy_id == expected_close_owner
                assert position.opening_order_id == originals[0].client_order_id
            assert _facts(h) == before
    asyncio.run(scenario())


def test_shared_maker_normal_cycle_release_cannot_clear_taker_external_pause(
    tmp_path: Path,
) -> None:
    h = _Admission(tmp_path)
    maker = MakerStrategy(h.admit.config, state_store=h.owner)
    maker._draining = False  # The normal running-cycle method, not the stop-only branch.
    try:
        h.owner.all_views()[2].freeze_source_submissions("external Taker operator HOLD")
        assert not h.owner.cycle_freeze_only
        before = h.owner._snapshot(), h.owner.path.read_bytes()
        assert not maker._try_release_cycle()
        assert not maker._try_release_cycle(inputs_fresh=True)
        assert (h.owner._snapshot(), h.owner.path.read_bytes()) == before
    finally:
        maker.dispose()


def test_bitfinex_filled_reference_cannot_point_to_the_other_allowed_owners_position() -> None:
    _, history = _netting_history(1)
    original = history[0]  # A real native NETTING fill, including its canonical PID.
    cache = _cache()
    cache.add_order(original, client_id=_ROUTES[original.instrument_id][1])
    cache.update_order(original)
    peer = _order(owner=StrategyId("PEER-001"), cid="PEER-INITIALIZED")
    cache.add_order(peer, client_id=_ROUTES[peer.instrument_id][1])
    values = OrderFilled.to_dict(original.events[-1])
    values.update(strategy_id=str(peer.strategy_id), client_order_id=str(peer.client_order_id))
    # A contradictory retained Position is not a natural venue fill. Native integrity
    # nevertheless accepts its indexes, so the composition's semantic check is required.
    wrong = Position(_source_instrument(), OrderFilled.from_dict(values))
    cache.add_position(wrong, OmsType.NETTING)
    assert cache.check_integrity()
    assert wrong.id == original.position_id and wrong.strategy_id != original.strategy_id
    assert cache.order(wrong.opening_order_id).strategy_id == wrong.strategy_id
    with pytest.raises(ValueError, match="position.*owner|position.*identity"):
        validate_native_cache(
            cache, trader_id=_TRADER, strategy_id=_OWNER, routes=_ROUTES,
            business_owners={str(original.client_order_id): _OWNER,
                             str(peer.client_order_id): peer.strategy_id},
        )


class _Projection:
    def __init__(self, engine: BacktestEngine, path: Path, fault: str | None) -> None:
        self.engine = engine
        self.owner = MakerStateStore(
            path, str(_source_instrument().id), str(_hedge_instrument().id),
            shared_strategy_ids=(str(OWNERS[0]), str(OWNERS[1])),
        )
        self.sequence = 0
        bid, ask, taker = self.owner.all_views()
        self.maker_source = self.native(True, "SM", OrderSide.SELL, OWNERS[0], 2)
        self.begin(ask, self.maker_source)
        self.fill(self.maker_source, 2)
        maker_intent = self.record(ask, self.maker_source)
        ask.bind_hedge_order(maker_intent.intent_id, "HM")
        self.maker_hedge = self.native(False, "HM", OrderSide.BUY, OWNERS[0], 2)
        self.fill(self.maker_hedge, 2, "900000501")
        ask.apply_hedge_fill(client_order_id="HM", trade_id=self.maker_hedge.trade_ids[0].value,
                             fill_ounces=D(2))
        assert ask.release_completed_cycle()
        self.maker_intent = ask.intent(maker_intent.intent_id)
        self.taker_source = self.native(True, "ST", OrderSide.BUY,
                                        OWNERS[0] if fault == "source" else OWNERS[1], 2)
        self.begin(taker, self.taker_source)
        self.fill(self.taker_source, 1)
        taker_intent = self.record(taker, self.taker_source)
        taker.bind_hedge_plan(taker_intent.intent_id, (
            HedgeLeg(SELL, D(1), "900000501", BUY, D(2)),
        ))
        taker.bind_hedge_order(taker_intent.intent_id, "HT")
        self.fill(self.taker_source, 1)  # This known current-CID suffix is not business-seen yet.
        self.taker_hedge = self.native(False, "HT", OrderSide.SELL,
                                       OWNERS[0] if fault == "hedge" else OWNERS[1], 1,
                                       close="900000501")
        self.fill(self.taker_hedge, 1, "900000501")
        self.taker_intent_id = taker_intent.intent_id

    def native(self, source: bool, cid: str, side: OrderSide, strategy: StrategyId,
               quantity: int, close: str | None = None) -> Order:
        instrument = self.engine.cache.instrument(
            (_source_instrument() if source else _hedge_instrument()).id,
        )
        assert instrument is not None
        common: dict[str, Any] = dict(trader_id=TestIdStubs.trader_id(), strategy_id=strategy,
                      instrument_id=instrument.id, client_order_id=ClientOrderId(cid),
                      order_side=side, quantity=instrument.make_qty(quantity),
                      init_id=UUID4(), ts_init=0)
        order = (LimitOrder(**common, price=instrument.make_price(100),
                            time_in_force=TimeInForce.GTC) if source else
                 MarketOrder(**common, time_in_force=TimeInForce.FOK,
                             reduce_only=close is not None))
        client = ClientId(instrument.id.venue.value)
        self.engine.cache.add_order(order, client_id=client,
                                    position_id=PositionId(close) if close else None)
        account = AccountId(f"{client}-001")
        process = self.engine.kernel.exec_engine.process
        process(TestEventStubs.order_submitted(order, account, ts_event=1))
        process(TestEventStubs.order_accepted(order, account, VenueOrderId(f"V-{cid}"), 2))
        return order

    def fill(self, order: Order, quantity: int, ticket: str | None = None) -> None:
        self.sequence += 1
        instrument = self.engine.cache.instrument(order.instrument_id)
        self.engine.kernel.exec_engine.process(TestEventStubs.order_filled(
            order, instrument, account_id=order.account_id,
            trade_id=TradeId(f"TRADE-{self.sequence}"),
            position_id=PositionId(ticket) if ticket else None,
            last_qty=instrument.make_qty(quantity), last_px=instrument.make_price(100),
            commission=Money(0, USD), ts_event=self.sequence + 10,
        ))

    @staticmethod
    def begin(view: JsonStateStore, order: Order) -> None:
        view.begin_source(str(order.client_order_id), BUY if order.side is OrderSide.BUY else SELL,
                          order.quantity.as_decimal(), source_account_id="BITFINEX-001",
                          source_client_id="BITFINEX", hedge_account_id="MT5-001",
                          hedge_client_id="MT5")

    @staticmethod
    def record(view: JsonStateStore, order: Order) -> HedgeIntent:
        fill = cast(OrderFilled, order.events[-1])
        intent = view.reserve_source_fill(
            fill_key=f"{order.client_order_id}|{order.venue_order_id}|{fill.trade_id}",
            client_order_id=str(order.client_order_id), trade_id=fill.trade_id.value,
            source_side=BUY if order.side is OrderSide.BUY else SELL,
            fill_ounces=fill.last_qty.as_decimal(),
        )
        assert intent is not None
        return intent


@pytest.mark.parametrize("fault", [None, "source", "hedge"])
def test_joint_projectors_keep_original_owner_and_native_events_on_cross_owner_close(
    tmp_path: Path, fault: str | None,
) -> None:
    with _engine() as engine:
        h = _Projection(engine, tmp_path / "projection", fault)
        cache = engine.cache
        assert cache.check_integrity()
        owners = shared_business_owners(h.owner)
        assert owners is not None and set(owners) == {"SM", "HM", "ST", "HT"}
        routes = {_source_instrument().id: (AccountId("BITFINEX-001"), ClientId("BITFINEX")),
                  _hedge_instrument().id: (AccountId("MT5-001"), ClientId("MT5"))}
        before = _summary(engine), {str(p.id): tuple(str(e.id) for e in p.events)
                                    for p in cache.positions()}
        sources = (h.maker_source, h.taker_source)
        hedges = (h.maker_hedge, h.taker_hedge)
        arguments: dict[str, Any] = dict(trader_id=TestIdStubs.trader_id(),
                                         strategy_id=OWNERS[0], reason="review")
        business = h.owner._snapshot(), h.owner.path.read_bytes()
        if fault:
            with pytest.raises(ValueError, match="order identity"):
                validate_native_cache(cache, trader_id=TestIdStubs.trader_id(),
                                      strategy_id=OWNERS[0], routes=routes, business_owners=owners)
            with pytest.raises(ValueError, match="order.*facts"):
                if fault == "source":
                    project_source_fills(h.owner, sources,
                                         source_instrument_id=_source_instrument().id, **arguments)
                else:
                    project_hedge_fills(h.owner, hedges,
                                        hedge_instrument_id=_hedge_instrument().id, **arguments)
            assert (h.owner._snapshot(), h.owner.path.read_bytes()) == business
        else:
            assert validate_native_cache(
                cache, trader_id=TestIdStubs.trader_id(), strategy_id=OWNERS[0],
                routes=routes, business_owners=owners,
            )
            assert project_source_fills(
                h.owner, sources, source_instrument_id=_source_instrument().id, **arguments,
            ) == 1
            assert project_hedge_fills(h.owner, hedges,
                                       hedge_instrument_id=_hedge_instrument().id, **arguments) == 1
            taker = h.owner.all_views()[2]
            current = taker.intent(h.taker_intent_id)
            assert current.hedge_filled_ounces == current.hedge_leg_filled_ounces == 1
            assert current.hedge_leg_index == 0 and current.hedge_client_order_id == "HT"
            assert all(intent.status is ObligationStatus.BLOCKED for intent in taker.intents())
            assert h.owner.all_views()[1].intent(h.maker_intent.intent_id) == h.maker_intent
            position = cache.position(PositionId("900000501"))
            assert position.strategy_id == OWNERS[0] != h.taker_hedge.strategy_id
            assert position.signed_decimal_qty() == 1
            assert project_source_fills(
                h.owner, sources, source_instrument_id=_source_instrument().id, **arguments,
            ) == 0
            assert project_hedge_fills(h.owner, hedges,
                                       hedge_instrument_id=_hedge_instrument().id, **arguments) == 0
        assert (_summary(engine), {str(p.id): tuple(str(e.id) for e in p.events)
                                   for p in cache.positions()}) == before


def test_shared_accounting_aggregates_cross_owner_close_fees_without_transferring_position(
    tmp_path: Path,
) -> None:
    with _engine() as engine:
        history = _History(engine, tmp_path / "fees")
        history.fill(True, OrderSide.SELL, owner=OWNERS[0])
        opening = history.fill(False, OrderSide.BUY, owner=OWNERS[0])
        history.fill(True, OrderSide.BUY, owner=OWNERS[1])
        assert opening.position_id is not None
        history._position_ids[OWNERS[1]] = opening.position_id  # Explicit existing close target.
        close = history.fill(False, OrderSide.SELL, owner=OWNERS[1])
        position = engine.cache.position(opening.position_id)
        assert position.is_closed and position.strategy_id == OWNERS[0] != close.strategy_id
        before = _summary(engine)
        report = build_run_accounting_report(
            engine.cache, cast(BitfinexV1ExecutionClient, history.source),
            cast(Mt5V1ExecutionClient, history.hedge), trader_id=TestIdStubs.trader_id(),
            strategy_id=OWNERS[0], strategy_ids=OWNERS, fx=FX,
        )
        assert report.status == "FINAL", report.pending_reasons
        assert "native_virtual" in report.scope and "not_venue_realized_cashflow" in report.scope
        assert report.excluded_cashflows == (
            "funding", "swap", "other_broker_fees", "unrealized_pnl",
        )
        usd = report.currencies["USD"]
        assert (usd.native_booked_cost, usd.native_pnl_embedded_cost, usd.venue_raw_cost) == (
            D(5), D("2.50"), D(5),
        )
        assert usd.final_realized_pnl == -5 and report.final_realized_pnl_usdt == D("-5.10")
        single = build_run_accounting_report(
            engine.cache, cast(BitfinexV1ExecutionClient, history.source),
            cast(Mt5V1ExecutionClient, history.hedge), trader_id=TestIdStubs.trader_id(),
            strategy_id=OWNERS[0], fx=FX,
        )
        assert single.status == "PENDING" and single.final_realized_pnl_usdt is None
        assert _summary(engine) == before


def test_shared_pause_publication_failure_blocks_admission_even_without_durable_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Admission(tmp_path)
    bid, _, taker = h.owner.all_views()
    assert h.admit(taker, BUY, D(1))

    def fail(*_: object) -> None:
        raise OSError("injected pre-publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "replace_and_sync_parent", fail)
        with pytest.raises(OSError):
            bid.freeze_source_submissions("external")
    assert all(view.halt_reason is None and view.source_freeze_reason is None
               for view in h.owner.all_views())
    assert h.owner._freeze_publication_failed and taker.can_submit_source()
    assert not h.admit(taker, BUY, D(1))


def test_shared_taker_drain_snapshot_uses_the_owner_carry_budget_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Admission(tmp_path)
    h.owner = MakerStateStore(
        tmp_path / "bounded", str(_source_instrument().id), str(_hedge_instrument().id),
        shared_strategy_ids=(str(OWNERS[0]), str(OWNERS[1])), residual_limit_ounces=D("0.5"),
        carry_route=(h.source.account_id.value, "BITFINEX", "MT5-12345678", "MT5"),
    )
    taker = h.owner.all_views()[2]
    values = CryptoPerpetual.to_dict(_source_instrument())
    values.update(size_precision=1, size_increment="0.1", lot_size="0.1")
    instrument = CryptoPerpetual.from_dict(values)
    h.cache.add_instrument(instrument)
    order = h.working(taker, BUY, 1)
    fill = TestEventStubs.order_filled(
        order, instrument, account_id=h.source.account_id, last_qty=instrument.make_qty(D("0.4")),
        last_px=instrument.make_price(2400), commission=Money(0, USD),
        position_id=PositionId(f"{instrument.id}-{order.strategy_id}"), ts_event=3,
    )
    order.apply(fill)
    assert taker.reserve_source_fill(
        fill_key=f"{order.client_order_id}|{order.venue_order_id}|{fill.trade_id}",
        client_order_id=str(order.client_order_id), trade_id=str(fill.trade_id),
        source_side=BUY, fill_ounces=D("0.4"),
    ) is None
    order.apply(TestEventStubs.order_canceled(order, ts_event=4))
    h.cache.update_order(order)
    taker.update_source_status(str(order.client_order_id), "CANCELED")
    taker.confirm_source_reconciled(str(order.client_order_id))
    assert taker.release_completed_cycle()
    assert h.owner.source_balance_is_admissible() and taker.rounding_residual_ounces == D("0.4")
    # Isolate the final read-only drain classifier, not an actual kernel lifecycle.
    monkeypatch.setattr(live_lifecycle, "get_source_terminal_reconciler", lambda _: SimpleNamespace(
        is_running=True, restart_pending=False, busy=False, last_failure=None,
    ))
    node = cast(TradingNode, SimpleNamespace(
        cache=h.cache,
        kernel=SimpleNamespace(exec_engine=SimpleNamespace(check_connected=lambda: True)),
    ))
    strategy = cast(TakerStrategy, SimpleNamespace(is_running=True, state_store=taker))
    result = live_lifecycle._snapshot(node, strategy)
    assert result.complete, result
    assert len(result.residuals) == 1 and set(result.residuals.values()) == {"0.4"}


@pytest.mark.parametrize("peer_hold", [False, True])
def test_shared_market_input_pause_is_soft_but_cannot_release_a_peer_hold(
    tmp_path: Path, peer_hold: bool,
) -> None:
    h = _Admission(tmp_path)
    maker = MakerStrategy(h.admit.config, state_store=h.owner)
    maker._draining = False
    try:
        if peer_hold:
            h.owner.all_views()[2].freeze_source_submissions("external Taker HOLD")
        before = h.owner._snapshot()
        disk = h.owner.path.read_bytes() if h.owner.path.exists() else None
        maker._freeze_and_cancel_all("stale observation", market_input=True)
        assert maker._source_hold and h.owner._snapshot() == before
        assert not maker._try_release_cycle()  # A terminal/start callback is not a fresh quote.
        assert maker._try_release_cycle(inputs_fresh=True) is not peer_hold
        assert maker._source_hold is peer_hold
        assert h.owner._snapshot() == before
        assert (h.owner.path.read_bytes() if h.owner.path.exists() else None) == disk
    finally:
        maker.dispose()


def test_shared_unknown_execution_pause_stays_durable_despite_healthy_quotes(
    tmp_path: Path,
) -> None:
    h = _Admission(tmp_path)
    maker = MakerStrategy(h.admit.config, state_store=h.owner)
    maker._draining = False
    try:
        maker._freeze_and_cancel_all("unknown execution outcome")
        assert all(view.source_freeze_reason == "unknown execution outcome"
                   for view in h.owner.all_views())
        assert not h.owner.cycle_freeze_only
        before = h.owner.path.read_bytes()
        assert not maker._try_release_cycle(inputs_fresh=True)
        assert h.owner.path.read_bytes() == before and maker._source_hold
    finally:
        maker.dispose()
