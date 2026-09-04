"""Real Nautilus fill identities and deterministic Maker lifecycle edges."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from msgspec.structs import replace as struct_replace
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    PositionId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money, Quantity
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from py000_nautilus.app import (
    _hedge_instrument,
    _maker_strategy_config,
    _quote,
    _source_instrument,
)
from py000_nautilus.config import CarryConfig, FxConfig
from py000_nautilus.hedge import HedgeCoordinator
from py000_nautilus.models import (
    BookTop,
    BusinessOrderSide,
    HedgeIntent,
    MakerAccount,
    MakerQuote,
    ObligationStatus,
    SourceAccount,
    SourceDirection,
)
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.maker import (
    MakerStrategy,
    _maker_timer_name,
    _maker_timer_target,
)

D = Decimal


def _bound_quote() -> MakerQuote:
    return MakerQuote(
        direction=SourceDirection.LONG,
        source_account=SourceAccount(
            account_id=AccountId("BITFINEX-001"),
            client_id=ClientId("SOURCE-CLIENT"),
            position_ounces=D(0),
            max_long_ounces=D(10),
            max_short_ounces=D(10),
            base_margin_level=D(100),
        ),
        hedge_account=MakerAccount(
            account_id=AccountId("MT5-001"),
            client_id=ClientId("HEDGE-CLIENT"),
            position_ounces=D(0),
            max_long_ounces=D(10),
            max_short_ounces=D(10),
        ),
        source_price_usdt=D(2400),
        hedge_reference_price_usd=D(2401),
        quantity_ounces=D(2),
        adjusted_spread=D("0.001"),
        leverage=16,
    )


class RecordingMakerStrategy(MakerStrategy):
    def __init__(
        self,
        state_prefix: Path,
        *,
        hedge_client_id: ClientId | None = None,
    ) -> None:
        config = _maker_strategy_config(state_prefix)
        if hedge_client_id is not None:
            route = struct_replace(config.hedge_accounts[0], client_id=hedge_client_id)
            config = struct_replace(config, hedge_accounts=(route,))
        super().__init__(config)
        self.recorded: list[
            tuple[SourceDirection, AccountId, ClientId | None, HedgeIntent]
        ] = []
        self.canceled: list[SourceDirection] = []

    def _submit_hedge(
        self,
        direction: SourceDirection,
        hedge_account_id: AccountId,
        hedge_client_id: ClientId | None,
        intent: HedgeIntent,
    ) -> None:
        self.recorded.append((direction, hedge_account_id, hedge_client_id, intent))
        self._stores[direction].bind_hedge_order(
            intent.intent_id,
            f"H-RECORDED-{len(self.recorded)}",
        )

    def _cancel_working(
        self,
        direction: SourceDirection,
        *,
        expected_order_id: str | None = None,
        reason: str,
    ) -> None:
        self.canceled.append(direction)


class CancelFaultMakerStrategy(RecordingMakerStrategy):
    def __init__(self, state_prefix: Path) -> None:
        super().__init__(state_prefix)
        self.actions: list[str] = []

    def _submit_hedge(
        self,
        direction: SourceDirection,
        hedge_account_id: AccountId,
        hedge_client_id: ClientId | None,
        intent: HedgeIntent,
    ) -> None:
        self.actions.append("hedge")
        super()._submit_hedge(
            direction,
            hedge_account_id,
            hedge_client_id,
            intent,
        )

    def _cancel_working(
        self,
        direction: SourceDirection,
        *,
        expected_order_id: str | None = None,
        reason: str,
    ) -> None:
        self.actions.append(f"cancel-{direction.value}")
        if direction is SourceDirection.LONG:
            raise OSError("injected first-side cancel failure")
        super()._cancel_working(
            direction,
            expected_order_id=expected_order_id,
            reason=reason,
        )


class CyclePlacementMakerStrategy(RecordingMakerStrategy):
    def __init__(self, state_prefix: Path) -> None:
        super().__init__(state_prefix)
        self.placed_source_ids: list[str] = []

    def _new_quote(
        self,
        direction: SourceDirection,
        source_book: BookTop,
        hedge_book: BookTop,
    ) -> MakerQuote:
        price = D(2398) if direction is SourceDirection.LONG else D(2402)
        return replace(
            _bound_quote(),
            direction=direction,
            source_price_usdt=price,
            quantity_ounces=D(1),
        )

    def _submit_source(self, quote: MakerQuote) -> None:
        client_order_id = f"O-CYCLE-2-{quote.direction.value}"
        side = (
            BusinessOrderSide.BUY
            if quote.direction is SourceDirection.LONG
            else BusinessOrderSide.SELL
        )
        self._stores[quote.direction].begin_source(
            client_order_id,
            side,
            quote.quantity_ounces,
            source_account_id=quote.source_account.account_id.value,
            source_client_id=(
                quote.source_account.client_id.value
                if quote.source_account.client_id is not None
                else None
            ),
            hedge_account_id=quote.hedge_account.account_id.value,
            hedge_client_id=(
                quote.hedge_account.client_id.value
                if quote.hedge_account.client_id is not None
                else None
            ),
        )
        self._working_quotes[client_order_id] = quote
        self.placed_source_ids.append(client_order_id)


def _filled_events(quantity: int = 2) -> tuple[Any, Any]:
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(quantity),
        price=instrument.make_price(2400),
        client_order_id=ClientOrderId("O-MAKER-EVENTS"),
    )
    partial = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("V-MAKER"),
        trade_id=TradeId("T-PARTIAL"),
        last_qty=instrument.make_qty(1),
        commission=Money(0, instrument.quote_currency),
    )
    final = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("V-MAKER"),
        trade_id=TradeId("T-FINAL"),
        last_qty=instrument.make_qty(1),
        commission=Money(0, instrument.quote_currency),
    )
    return partial, final


def _rejected(order: Any, reason: str) -> OrderRejected:
    template = TestEventStubs.order_rejected(order)
    return OrderRejected(
        trader_id=template.trader_id,
        strategy_id=template.strategy_id,
        instrument_id=template.instrument_id,
        client_order_id=template.client_order_id,
        account_id=template.account_id,
        reason=reason,
        event_id=UUID4(),
        ts_event=template.ts_event,
        ts_init=template.ts_init,
    )


def _subunit_fill() -> Any:
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(1),
        price=instrument.make_price(2400),
        client_order_id=ClientOrderId("O-MAKER-EVENTS"),
    )
    return TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("V-MAKER"),
        trade_id=TradeId("T-SUBUNIT"),
        last_qty=Quantity.from_str("0.5"),
        commission=Money(0, instrument.quote_currency),
    )


def test_partial_final_duplicate_and_late_fills_keep_pair_and_freeze_both_sides(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(
        tmp_path / "maker.state",
        hedge_client_id=ClientId("HEDGE-CLIENT"),
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(2),
        source_account_id="BITFINEX-001",
        source_client_id="SOURCE-CLIENT",
        hedge_account_id="MT5-001",
        hedge_client_id="HEDGE-CLIENT",
    )
    strategy._working_quotes["O-MAKER-EVENTS"] = _bound_quote()
    partial, final = _filled_events()

    strategy.on_order_filled(partial)
    assert strategy.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    strategy._finish_or_reject("O-MAKER-EVENTS", "CANCELED")
    assert "O-MAKER-EVENTS" not in strategy._working_quotes
    strategy.on_order_filled(final)  # distinct late fill after cancel
    strategy.on_order_filled(partial)  # duplicate early fill
    strategy.on_order_filled(final)  # duplicate late fill

    assert [entry[3].source_trade_id for entry in strategy.recorded] == ["T-PARTIAL"]
    strategy.on_order_filled(
        cast(
            Any,
            SimpleNamespace(
                instrument_id=_hedge_instrument().id,
                client_order_id=ClientOrderId("H-RECORDED-1"),
                trade_id=TradeId("HEDGE-PARTIAL"),
                last_qty=Quantity.from_int(1),
            ),
        )
    )

    assert [entry[3].source_trade_id for entry in strategy.recorded] == [
        "T-PARTIAL",
        "T-FINAL",
    ]
    assert all(entry[1] == AccountId("MT5-001") for entry in strategy.recorded)
    assert all(entry[2] == ClientId("HEDGE-CLIENT") for entry in strategy.recorded)
    assert store.rounding_residual_ounces == 0
    assert store.net_unhedged_ounces == D(1)


def test_unknown_engine_rejection_freezes_source_without_declaring_failure(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "unknown.state")
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(1),
        price=instrument.make_price(2400),
        client_order_id=ClientOrderId("O-MAKER-UNKNOWN"),
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        order.client_order_id.value,
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    strategy._working_quotes[order.client_order_id.value] = _bound_quote()

    strategy.on_order_rejected(_rejected(order, "UNKNOWN"))

    record = store.source_order(order.client_order_id.value)
    assert record is not None and record.status == "UNKNOWN"
    assert store.active_source_order_id == order.client_order_id.value
    assert not store.can_submit_source()
    assert strategy.canceled == [SourceDirection.LONG, SourceDirection.SHORT]


def test_restart_late_fill_uses_durable_pair_and_keeps_reconciliation_hold(
    tmp_path: Path,
) -> None:
    state_prefix = tmp_path / "restart.state"
    original = RecordingMakerStrategy(
        state_prefix,
        hedge_client_id=ClientId("HEDGE-CLIENT"),
    )
    original._stores[SourceDirection.LONG].begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(2),
        source_account_id="BITFINEX-001",
        source_client_id="SOURCE-CLIENT",
        hedge_account_id="MT5-001",
        hedge_client_id="HEDGE-CLIENT",
    )

    restarted = RecordingMakerStrategy(
        state_prefix,
        hedge_client_id=ClientId("HEDGE-CLIENT"),
    )
    store = restarted._stores[SourceDirection.LONG]
    assert store.recover_for_start() is not None
    partial, _ = _filled_events()
    restarted.on_order_filled(partial)

    assert len(restarted.recorded) == 1
    assert restarted.recorded[0][1] == AccountId("MT5-001")
    assert restarted.recorded[0][2] == ClientId("HEDGE-CLIENT")
    assert store.has_unresolved_hedges()
    assert store.net_unhedged_ounces == D(1)
    assert store.halt_reason is not None


@pytest.mark.parametrize(
    ("configured_client_id", "persisted_client_id"),
    [
        (None, "HEDGE-OLD"),
        (ClientId("HEDGE-CURRENT"), None),
        (ClientId("HEDGE-CURRENT"), "HEDGE-OLD"),
    ],
    ids=["unexpected-persisted", "persisted-missing", "client-drift"],
)
def test_maker_durable_hedge_client_mismatch_blocks_before_submit(
    tmp_path: Path,
    configured_client_id: ClientId | None,
    persisted_client_id: str | None,
) -> None:
    strategy = RecordingMakerStrategy(
        tmp_path / f"client-route-{persisted_client_id}.state",
        hedge_client_id=configured_client_id,
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-CLIENT-ROUTE",
        BusinessOrderSide.BUY,
        D(1),
        hedge_account_id="MT5-001",
        hedge_client_id=persisted_client_id,
    )
    intent = store.reserve_source_fill(
        fill_key="O-CLIENT-ROUTE|V|T",
        client_order_id="O-CLIENT-ROUTE",
        trade_id="T-CLIENT-ROUTE",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(1),
    )
    assert intent is not None

    strategy._submit_next_pending_hedge()

    assert strategy.recorded == []
    assert store.intent(intent.intent_id).status is ObligationStatus.BLOCKED
    source = store.source_order("O-CLIENT-ROUTE")
    assert source is not None and source.status == "UNKNOWN"
    assert "durable Maker hedge route" in cast(str, store.halt_reason)


def test_subunit_fill_with_no_rounded_intent_still_freezes_and_cancels_both(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "subunit.state")
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    strategy.on_order_filled(_subunit_fill())

    assert strategy.recorded == []
    assert strategy.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    assert store.rounding_residual_ounces == D("0.5")
    assert store.net_unhedged_ounces == D("0.5")
    assert all(not item.can_submit_source() for item in strategy._stores.values())
    assert not strategy._try_release_cycle()


def test_hedge_dispatch_precedes_cancel_and_first_cancel_failure_is_isolated(
    tmp_path: Path,
) -> None:
    strategy = CancelFaultMakerStrategy(tmp_path / "cancel-fault.state")
    strategy._stores[SourceDirection.LONG].begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    full, _ = _filled_events(quantity=1)

    strategy.on_order_filled(full)

    assert strategy.actions == ["hedge", "cancel-bid", "cancel-ask"]
    assert len(strategy.recorded) == 1
    assert strategy.canceled == [SourceDirection.SHORT]
    assert strategy._global_obligation_block()


def test_maker_two_direction_obligations_share_one_global_mt5_flight(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(
        tmp_path / "global-flight.state",
        hedge_client_id=ClientId("HEDGE-CLIENT"),
    )
    for direction, side in (
        (SourceDirection.LONG, BusinessOrderSide.BUY),
        (SourceDirection.SHORT, BusinessOrderSide.SELL),
    ):
        store = strategy._stores[direction]
        source_id = f"O-{direction.value}"
        store.begin_source(
            source_id,
            side,
            D(1),
            source_account_id="BITFINEX-001",
            hedge_account_id="MT5-001",
            hedge_client_id="HEDGE-CLIENT",
        )
        assert store.reserve_source_fill(
            fill_key=f"{source_id}|V|T",
            client_order_id=source_id,
            trade_id=f"T-{direction.value}",
            source_side=side,
            fill_ounces=D(1),
        ) is not None

    strategy._submit_next_pending_hedge()
    strategy._submit_next_pending_hedge()

    assert [entry[0] for entry in strategy.recorded] == [SourceDirection.LONG]
    strategy.on_order_filled(
        cast(
            Any,
            SimpleNamespace(
                instrument_id=_hedge_instrument().id,
                client_order_id=ClientOrderId("H-RECORDED-1"),
                trade_id=TradeId("HT-FIRST"),
                last_qty=Quantity.from_int(1),
            ),
        )
    )

    assert [entry[0] for entry in strategy.recorded] == [
        SourceDirection.LONG,
        SourceDirection.SHORT,
    ]


def test_maker_uses_shared_multi_ticket_planner_for_exact_close_legs(
    tmp_path: Path,
) -> None:
    store = JsonStateStore(tmp_path / "maker-shared-plan.json")
    store.begin_source(
        "O-MAKER-PLAN",
        BusinessOrderSide.BUY,
        D(2),
        hedge_account_id="MT5-001",
        hedge_client_id="HEDGE-CLIENT",
    )
    intent = store.reserve_source_fill(
        fill_key="O-MAKER-PLAN|V|T",
        client_order_id="O-MAKER-PLAN",
        trade_id="T-MAKER-PLAN",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    positions = [
        SimpleNamespace(
            id=PositionId("2"),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        ),
        SimpleNamespace(
            id=PositionId("1"),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        ),
    ]
    calls: list[dict[str, object]] = []
    submissions: list[PositionId | None] = []

    def market(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(client_order_id=ClientOrderId(f"H-MAKER-{len(calls)}"))

    coordinator = HedgeCoordinator(_source_instrument().id, store)
    harness = SimpleNamespace(
        _config=SimpleNamespace(hedge_instrument_id=_hedge_instrument().id),
        _hedges={SourceDirection.LONG: coordinator},
        _stores={SourceDirection.LONG: store},
        cache=SimpleNamespace(
            quote_tick=lambda _instrument_id: object(),
            positions_open=lambda **_kwargs: positions,
        ),
        order_factory=SimpleNamespace(market=market),
        log=SimpleNamespace(error=lambda _message: None),
        _quote_is_fresh=lambda _tick: True,
        _required_hedge_instrument=lambda: _hedge_instrument(),
        _hedge_positions=lambda _account_id: positions,
        submit_order=lambda _order, *, position_id, client_id: submissions.append(position_id),
    )

    MakerStrategy._submit_hedge(
        cast(Any, harness),
        SourceDirection.LONG,
        AccountId("MT5-001"),
        ClientId("HEDGE-CLIENT"),
        intent,
    )
    positions.pop()
    assert coordinator.on_hedge_filled(
        cast(
            Any,
            SimpleNamespace(
                client_order_id=ClientOrderId("H-MAKER-1"),
                trade_id=TradeId("HT-MAKER-1"),
                last_qty=Quantity.from_int(1),
            ),
        )
    )
    MakerStrategy._submit_hedge(
        cast(Any, harness),
        SourceDirection.LONG,
        AccountId("MT5-001"),
        ClientId("HEDGE-CLIENT"),
        store.intent(intent.intent_id),
    )

    assert [call["reduce_only"] for call in calls] == [True, True]
    assert submissions == [PositionId("1"), PositionId("2")]


@pytest.mark.parametrize("source_missing", [True, False], ids=["source-missing", "source-hold"])
def test_maker_fresh_hedge_quote_retries_pending_leg_exactly_once(
    tmp_path: Path,
    source_missing: bool,
) -> None:
    prefix = tmp_path / f"quote-retry-{source_missing}.state"
    config = _maker_strategy_config(prefix)
    stores = {
        direction: JsonStateStore(f"{prefix}.{direction.value}.json")
        for direction in (SourceDirection.LONG, SourceDirection.SHORT)
    }
    hedges = {
        direction: HedgeCoordinator(config.source_instrument_id, stores[direction])
        for direction in (SourceDirection.LONG, SourceDirection.SHORT)
    }
    store = stores[SourceDirection.LONG]
    store.begin_source(
        "O-QUOTE-RETRY",
        BusinessOrderSide.BUY,
        D(2),
        hedge_account_id="MT5-001",
        hedge_client_id=None,
    )
    intent = store.reserve_source_fill(
        fill_key="O-QUOTE-RETRY|V|T",
        client_order_id="O-QUOTE-RETRY",
        trade_id="T-QUOTE-RETRY",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    positions = [
        SimpleNamespace(
            id=PositionId("2"),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        ),
        SimpleNamespace(
            id=PositionId("1"),
            quantity=Quantity.from_int(1),
            is_long=True,
            is_short=False,
        ),
    ]
    source_tick = _quote(_source_instrument(), "2399", "2400", "5", 1_000_000_000)
    hedge_tick = _quote(_hedge_instrument(), "2401", "2402", "5", 1_000_000_000)
    fresh = {"value": True}
    market_calls: list[dict[str, object]] = []
    submissions: list[PositionId | None] = []
    frozen: list[str] = []

    def quote_tick(instrument_id: object) -> Any:
        if instrument_id == config.hedge_instrument_id:
            return hedge_tick
        return None if source_missing else source_tick

    def market(**kwargs: object) -> object:
        market_calls.append(kwargs)
        return SimpleNamespace(client_order_id=ClientOrderId(f"H-WAKE-{len(market_calls)}"))

    harness = SimpleNamespace(
        _config=config,
        _stores=stores,
        _hedges=hedges,
        cache=SimpleNamespace(
            quote_tick=quote_tick,
            positions_open=lambda **_kwargs: positions,
        ),
        order_factory=SimpleNamespace(market=market),
        log=SimpleNamespace(error=lambda _message: None),
        _quote_is_fresh=lambda _tick: fresh["value"],
        _required_hedge_instrument=lambda: _hedge_instrument(),
        _hedge_positions=lambda _account_id: positions,
        submit_order=lambda _order, *, position_id, client_id: submissions.append(position_id),
        _try_release_cycle=lambda: False,
        _inputs_are_fresh=lambda _source, _hedge: True,
        _global_obligation_block=lambda: True,
        _freeze_and_cancel_all=lambda reason: frozen.append(reason),
        _refresh_direction=lambda *_args: (_ for _ in ()).throw(
            AssertionError("source quote maintenance must stay blocked")
        ),
    )
    harness._durable_hedge_route = lambda direction, client_order_id: (
        MakerStrategy._durable_hedge_route(
            cast(Any, harness),
            direction,
            client_order_id,
        )
    )
    harness._submit_hedge = lambda direction, account_id, client_id, current: (
        MakerStrategy._submit_hedge(
            cast(Any, harness),
            direction,
            account_id,
            client_id,
            current,
        )
    )
    harness._submit_next_pending_hedge = lambda: MakerStrategy._submit_next_pending_hedge(
        cast(Any, harness)
    )

    harness._submit_next_pending_hedge()
    assert submissions == [PositionId("1")]
    positions.pop()
    fresh["value"] = False
    MakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(
            Any,
            SimpleNamespace(
                instrument_id=config.hedge_instrument_id,
                client_order_id=ClientOrderId("H-WAKE-1"),
                trade_id=TradeId("HT-WAKE-1"),
                last_qty=Quantity.from_int(1),
            ),
        ),
    )
    waiting = store.intent(intent.intent_id)
    assert submissions == [PositionId("1")]
    assert waiting.status is ObligationStatus.PENDING
    assert waiting.hedge_client_order_id is None

    fresh["value"] = True
    MakerStrategy.on_quote_tick(cast(Any, harness), hedge_tick)
    MakerStrategy.on_quote_tick(cast(Any, harness), hedge_tick)

    assert submissions == [PositionId("1"), PositionId("2")]
    submitted = store.intent(intent.intent_id)
    assert submitted.status is ObligationStatus.SUBMITTING
    assert submitted.hedge_client_order_id == "H-WAKE-2"
    if not source_missing:
        assert frozen == ["stale, closed, or unresolved"] * 2


def _seed_filled_two_sided_cycle(
    strategy: RecordingMakerStrategy,
) -> tuple[JsonStateStore, JsonStateStore, HedgeIntent]:
    bid_store = strategy._stores[SourceDirection.LONG]
    ask_store = strategy._stores[SourceDirection.SHORT]
    bid_store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    ask_store.begin_source(
        "O-SIBLING",
        BusinessOrderSide.SELL,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    full, _ = _filled_events(quantity=1)
    strategy.on_order_filled(full)
    return bid_store, ask_store, bid_store.intents()[0]


def test_complete_source_terminals_and_hedge_evidence_release_next_cycle(
    tmp_path: Path,
) -> None:
    strategy = CyclePlacementMakerStrategy(tmp_path / "cycle-complete.state")
    bid_store, ask_store, intent = _seed_filled_two_sided_cycle(strategy)
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    bid_store.apply_hedge_fill(
        client_order_id=hedge_order_id,
        trade_id="HT-COMPLETE",
        fill_ounces=D(1),
    )
    strategy._finish_or_reject("O-SIBLING", "CANCELED")

    assert not strategy._try_release_cycle()
    assert strategy.confirm_source_reconciled(SourceDirection.SHORT, "O-SIBLING")

    assert bid_store.source_freeze_reason is None
    assert ask_store.source_freeze_reason is None
    assert bid_store.can_submit_source()
    assert ask_store.can_submit_source()
    assert not strategy._global_obligation_block()

    source_book = BookTop(D(2399), D(2400), D(5), D(5))
    hedge_book = BookTop(D(2401), D(2402), D(5), D(5))
    for direction in (SourceDirection.LONG, SourceDirection.SHORT):
        strategy._refresh_direction(direction, source_book, hedge_book)

    assert strategy.placed_source_ids == ["O-CYCLE-2-bid", "O-CYCLE-2-ask"]
    assert bid_store.active_source_order_id == "O-CYCLE-2-bid"
    assert ask_store.active_source_order_id == "O-CYCLE-2-ask"

    hedge_count = len(strategy.recorded)
    intent_count = len(bid_store.intents())
    cancel_count = len(strategy.canceled)
    old_duplicate, _ = _filled_events(quantity=1)
    strategy.on_order_filled(old_duplicate)

    assert len(strategy.recorded) == hedge_count
    assert len(bid_store.intents()) == intent_count
    assert len(strategy.canceled) == cancel_count
    assert bid_store.active_source_order_id == "O-CYCLE-2-bid"
    assert ask_store.active_source_order_id == "O-CYCLE-2-ask"
    assert bid_store.source_freeze_reason is None
    assert ask_store.source_freeze_reason is None


def test_missing_sibling_terminal_does_not_release_completed_hedge(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "cycle-missing-sibling.state")
    bid_store, ask_store, intent = _seed_filled_two_sided_cycle(strategy)
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    bid_store.apply_hedge_fill(
        client_order_id=hedge_order_id,
        trade_id="HT-COMPLETE",
        fill_ounces=D(1),
    )

    assert not strategy._try_release_cycle()
    assert ask_store.active_source_order_id == "O-SIBLING"
    assert strategy._global_obligation_block()


@pytest.mark.parametrize("hedge_outcome", ["partial", "rejected"])
def test_partial_or_rejected_hedge_does_not_release_cycle(
    tmp_path: Path,
    hedge_outcome: str,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / f"cycle-{hedge_outcome}.state")
    bid_store, _, intent = _seed_filled_two_sided_cycle(strategy)
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    if hedge_outcome == "partial":
        bid_store.apply_hedge_fill(
            client_order_id=hedge_order_id,
            trade_id="HT-PARTIAL",
            fill_ounces=D("0.5"),
        )
    else:
        bid_store.update_hedge_status(hedge_order_id, ObligationStatus.REJECTED)
    strategy._finish_or_reject("O-SIBLING", "CANCELED")
    assert not strategy.confirm_source_reconciled(SourceDirection.SHORT, "O-SIBLING")

    assert strategy._global_obligation_block()
    assert bid_store.source_freeze_reason is not None


def test_restart_releases_only_from_persisted_complete_cycle_evidence(
    tmp_path: Path,
) -> None:
    state_prefix = tmp_path / "cycle-restart-complete.state"
    original = RecordingMakerStrategy(state_prefix)
    bid_store, ask_store, intent = _seed_filled_two_sided_cycle(original)
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    bid_store.apply_hedge_fill(
        client_order_id=hedge_order_id,
        trade_id="HT-COMPLETE",
        fill_ounces=D(1),
    )
    ask_store.update_source_status("O-SIBLING", "CANCELED")
    ask_store.confirm_source_reconciled("O-SIBLING")

    restarted = MakerStrategy(_maker_strategy_config(state_prefix))

    assert restarted._try_release_cycle()
    assert not restarted._global_obligation_block()


def test_restart_does_not_release_incomplete_persisted_cycle(tmp_path: Path) -> None:
    state_prefix = tmp_path / "cycle-restart-incomplete.state"
    original = RecordingMakerStrategy(state_prefix)
    _seed_filled_two_sided_cycle(original)

    restarted = MakerStrategy(_maker_strategy_config(state_prefix))

    assert not restarted._try_release_cycle()
    assert restarted._global_obligation_block()


def test_keep_last_accounts_true_is_rejected_until_restart_safe(tmp_path: Path) -> None:
    config = _maker_strategy_config(tmp_path / "unsupported-keep-last.state")

    with pytest.raises(ValueError, match="restart-safe"):
        struct_replace(config, keep_last_accounts=True)


def test_old_timer_order_identity_cannot_target_replacement() -> None:
    old_bid = _maker_timer_name(SourceDirection.LONG, "O-A")
    current_bid = _maker_timer_name(SourceDirection.LONG, "O-B")
    current_ask = _maker_timer_name(SourceDirection.SHORT, "O-ASK")

    assert _maker_timer_target(old_bid, "O-B", "O-ASK") is None
    assert _maker_timer_target(current_bid, "O-B", "O-ASK") == (
        SourceDirection.LONG,
        "O-B",
    )
    assert _maker_timer_target(current_ask, "O-B", "O-ASK") == (
        SourceDirection.SHORT,
        "O-ASK",
    )


class _ModifyInstrument:
    def make_price(self, value: Decimal) -> Decimal:
        return value

    def make_qty(self, value: Decimal) -> Decimal:
        return value


class _ModifyHarness:
    def __init__(self, state_prefix: Path) -> None:
        self._config = _maker_strategy_config(state_prefix)
        self.modified: list[dict[str, Any]] = []

    def _required_source_instrument(self) -> _ModifyInstrument:
        return _ModifyInstrument()

    def modify_order(self, order: Any, **kwargs: Any) -> None:
        self.modified.append({"order": order, **kwargs})


def test_fixed_amount_false_requote_modifies_price_with_quantity_none(tmp_path: Path) -> None:
    harness = _ModifyHarness(tmp_path / "modify.state")
    order = SimpleNamespace(
        is_pending_update=False,
        is_pending_cancel=False,
        price=D(100),
    )
    desired = replace(_bound_quote(), source_price_usdt=D("101.01"))

    MakerStrategy._requote(cast(Any, harness), cast(Any, order), desired)

    assert not harness._config.fixed_amount
    assert len(harness.modified) == 1
    assert harness.modified[0]["price"] == D("101.01")
    assert harness.modified[0]["quantity"] is None


class _TimerClock:
    def __init__(self) -> None:
        self.timer_names: set[str] = set()
        self.canceled: list[str] = []

    def cancel_timer(self, name: str) -> None:
        self.canceled.append(name)
        self.timer_names.remove(name)

    def set_time_alert_ns(self, *, name: str, alert_time_ns: int, callback: Any) -> None:
        assert alert_time_ns > 0
        assert callback is not None
        self.timer_names.add(name)


class _TimerCache:
    def quote_tick(self, instrument_id: Any) -> Any:
        return SimpleNamespace(ts_event=100)


def test_each_side_replaces_instead_of_accumulating_stale_timers(tmp_path: Path) -> None:
    harness = SimpleNamespace(
        _stale_timer_names={},
        _config=_maker_strategy_config(tmp_path / "timer.state"),
        _cost_ts_ns=100,
        _session_ts_ns=100,
        cache=_TimerCache(),
        clock=_TimerClock(),
        _on_stale_timer=lambda event: None,
    )

    MakerStrategy._schedule_stale_timer(cast(Any, harness), SourceDirection.LONG, "O-A")
    MakerStrategy._schedule_stale_timer(cast(Any, harness), SourceDirection.LONG, "O-B")

    assert harness.clock.timer_names == {
        _maker_timer_name(SourceDirection.LONG, "O-B")
    }
    assert harness.clock.canceled == [_maker_timer_name(SourceDirection.LONG, "O-A")]


class _StopStore:
    def __init__(self, active: str) -> None:
        self.active_source_order_id = active
        self.unknown: list[tuple[str, str]] = []
        self.freeze_reason: str | None = None

    def mark_source_unknown(self, client_order_id: str, reason: str) -> None:
        self.unknown.append((client_order_id, reason))

    def freeze_source_submissions(self, reason: str) -> None:
        self.freeze_reason = reason

    def knows_source_order(self, client_order_id: str) -> bool:
        return client_order_id == self.active_source_order_id


class _WorkingOrder:
    is_closed = False
    is_pending_cancel = False


class _StopCache:
    def __init__(self) -> None:
        self.orders = {"O-BID": _WorkingOrder(), "O-ASK": _WorkingOrder()}
        self.requested: list[str] = []

    def order(self, client_order_id: ClientOrderId) -> _WorkingOrder | None:
        self.requested.append(client_order_id.value)
        return self.orders.get(client_order_id.value)

    def quote_tick(self, instrument_id: Any) -> Any:
        return SimpleNamespace(ts_event=1)


class _StopHarness:
    def __init__(self) -> None:
        self._stores = {
            SourceDirection.LONG: _StopStore("O-BID"),
            SourceDirection.SHORT: _StopStore("O-ASK"),
        }
        self.cache = _StopCache()
        self.canceled: list[_WorkingOrder] = []
        self._source_hold = False
        self._config = _maker_strategy_config(Path("/tmp/stop-harness.state"))
        self._stale_timer_names: dict[SourceDirection, str] = {}
        self._cost_ts_ns = 0
        self._carry = CarryConfig()
        self._fx = FxConfig()
        self._session_ts_ns = 0
        self._hedge_session_open = True
        self.clock: Any = SimpleNamespace(timestamp_ns=lambda: 0)

    def cancel_order(self, order: _WorkingOrder) -> None:
        self.canceled.append(order)

    def _cancel_working(
        self,
        direction: SourceDirection,
        *,
        expected_order_id: str | None = None,
        reason: str,
    ) -> None:
        MakerStrategy._cancel_working(
            cast(Any, self),
            direction,
            expected_order_id=expected_order_id,
            reason=reason,
        )

    def _freeze_all_best_effort(self, reason: str) -> None:
        for store in self._stores.values():
            store.freeze_source_submissions(reason)

    def _freeze_and_cancel_all(self, reason: str) -> None:
        MakerStrategy._freeze_and_cancel_all(cast(Any, self), reason)

    def _cancel_all_best_effort(self, reason: str) -> None:
        for direction in (SourceDirection.LONG, SourceDirection.SHORT):
            self._cancel_working(direction, reason=reason)

    def _inputs_are_fresh(self, source_tick: Any, hedge_tick: Any) -> bool:
        return False

    def _direction_for_source_order(
        self,
        client_order_id: str,
    ) -> SourceDirection | None:
        return MakerStrategy._direction_for_source_order(cast(Any, self), client_order_id)


def test_stop_cancels_exact_two_active_gtc_orders_without_releasing_gates() -> None:
    harness = _StopHarness()

    MakerStrategy.on_stop(cast(Any, harness))

    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2
    assert harness._stores[SourceDirection.LONG].active_source_order_id == "O-BID"
    assert harness._stores[SourceDirection.SHORT].active_source_order_id == "O-ASK"


def test_current_stale_timer_freezes_and_cancels_both_exact_active_ids() -> None:
    harness = _StopHarness()
    timer_name = _maker_timer_name(SourceDirection.LONG, "O-BID")
    harness._stale_timer_names[SourceDirection.LONG] = timer_name

    MakerStrategy._on_stale_timer(
        cast(Any, harness),
        cast(Any, SimpleNamespace(name=timer_name)),
    )

    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2
    assert all(store.freeze_reason == "stale timer" for store in harness._stores.values())


def test_old_stale_timer_is_a_total_noop_for_replacement_orders() -> None:
    harness = _StopHarness()
    old_timer = _maker_timer_name(SourceDirection.LONG, "O-OLD")

    MakerStrategy._on_stale_timer(
        cast(Any, harness),
        cast(Any, SimpleNamespace(name=old_timer)),
    )

    assert harness.cache.requested == []
    assert harness.canceled == []
    assert all(store.freeze_reason is None for store in harness._stores.values())


def test_source_unknown_immediately_freezes_and_cancels_both_without_tick() -> None:
    harness = _StopHarness()

    MakerStrategy._mark_source_unknown(
        cast(Any, harness),
        "O-BID",
        "injected source unknown",
    )

    assert harness._source_hold
    assert harness._stores[SourceDirection.LONG].unknown == [
        ("O-BID", "injected source unknown")
    ]
    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2


class _SessionHarness:
    def __init__(self) -> None:
        self._session_ts_ns = 1
        self._hedge_session_open = True
        self.canceled: list[SourceDirection] = []

    def _cancel_working(self, direction: SourceDirection, *, reason: str) -> None:
        assert reason == "hedge session closed or future-dated"
        self.canceled.append(direction)

    def _reschedule_active_timers(self) -> None:
        raise AssertionError("a closed session must cancel, not reschedule")

    def _freeze_and_cancel_all(self, reason: str) -> None:
        for direction in (SourceDirection.LONG, SourceDirection.SHORT):
            self._cancel_working(direction, reason=reason)


def test_session_close_immediately_freezes_both_gtc_sides_without_a_new_tick() -> None:
    harness = _SessionHarness()

    MakerStrategy.update_hedge_session(cast(Any, harness), False, 2)

    assert harness.canceled == [SourceDirection.LONG, SourceDirection.SHORT]


def test_cost_change_immediately_cancels_exact_active_ids_without_market_tick() -> None:
    harness = _StopHarness()
    harness._cost_ts_ns = 1
    harness._carry = CarryConfig()
    harness._fx = FxConfig()

    MakerStrategy.update_cost_snapshot(
        cast(Any, harness),
        CarryConfig(total_trade_fee=D("0.001")),
        FxConfig(usd_usdt_bid=D("0.999"), usd_usdt_ask=D("1.001")),
        2,
    )

    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2


class _NowClock:
    def timestamp_ns(self) -> int:
        return 2


def test_future_session_fact_immediately_cancels_instead_of_scheduling() -> None:
    harness = _StopHarness()
    harness._session_ts_ns = 1
    harness._hedge_session_open = True
    harness.clock = _NowClock()

    MakerStrategy.update_hedge_session(cast(Any, harness), True, 3)

    assert harness.cache.requested == ["O-BID", "O-ASK"]
    assert len(harness.canceled) == 2


class _QuoteCache:
    def __init__(self) -> None:
        source = _source_instrument()
        hedge = _hedge_instrument()
        self._ticks = {
            source.id: _quote(source, "2399", "2400", "5", 1_000_000_000),
            hedge.id: _quote(hedge, "2401", "2402", "5", 1_000_000_000),
        }

    def quote_tick(self, instrument_id: Any) -> Any:
        return self._ticks[instrument_id]


class _QuoteGateHarness:
    def __init__(self, state_prefix: Path, *, blocked: bool, fresh: bool) -> None:
        self._config = _maker_strategy_config(state_prefix)
        self.cache = _QuoteCache()
        self.blocked = blocked
        self.fresh = fresh
        self.canceled: list[SourceDirection] = []
        self.refreshed: list[SourceDirection] = []

    def _global_obligation_block(self) -> bool:
        return self.blocked

    def _try_release_cycle(self) -> bool:
        return False

    def _inputs_are_fresh(self, source_tick: Any, hedge_tick: Any) -> bool:
        return self.fresh

    def _cancel_working(self, direction: SourceDirection, *, reason: str) -> None:
        assert reason == "stale, closed, or unresolved"
        self.canceled.append(direction)

    def _freeze_and_cancel_all(self, reason: str) -> None:
        for direction in (SourceDirection.LONG, SourceDirection.SHORT):
            self._cancel_working(direction, reason=reason)

    def _refresh_direction(
        self,
        direction: SourceDirection,
        source_book: Any,
        hedge_book: Any,
    ) -> None:
        self.refreshed.append(direction)


def test_completed_hedge_before_sibling_cancel_terminal_cannot_reopen_quotes(
    tmp_path: Path,
) -> None:
    strategy = RecordingMakerStrategy(tmp_path / "race.state")
    bid_store = strategy._stores[SourceDirection.LONG]
    ask_store = strategy._stores[SourceDirection.SHORT]
    bid_store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    ask_store.begin_source(
        "O-SIBLING",
        BusinessOrderSide.SELL,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    full, _ = _filled_events(quantity=1)

    strategy.on_order_filled(full)
    intent = bid_store.intents()[0]
    hedge_order_id = cast(str, intent.hedge_client_order_id)
    bid_store.apply_hedge_fill(
        client_order_id=hedge_order_id,
        trade_id="HT-COMPLETE",
        fill_ounces=D(1),
    )
    assert bid_store.net_unhedged_ounces == 0
    assert strategy._global_obligation_block()

    harness = _QuoteGateHarness(tmp_path / "gate.state", blocked=True, fresh=True)
    tick = harness.cache.quote_tick(harness._config.source_instrument_id)
    MakerStrategy.on_quote_tick(cast(Any, harness), tick)

    assert harness.refreshed == []
    assert harness.canceled == [SourceDirection.LONG, SourceDirection.SHORT]


def test_stale_market_gate_cancels_both_sides_without_quote_maintenance(
    tmp_path: Path,
) -> None:
    harness = _QuoteGateHarness(tmp_path / "stale.state", blocked=False, fresh=False)
    tick = harness.cache.quote_tick(harness._config.source_instrument_id)

    MakerStrategy.on_quote_tick(cast(Any, harness), tick)

    assert harness.refreshed == []
    assert harness.canceled == [SourceDirection.LONG, SourceDirection.SHORT]


@pytest.mark.parametrize("failing_direction", [SourceDirection.LONG, SourceDirection.SHORT])
def test_fill_is_durable_before_either_freeze_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_direction: SourceDirection,
) -> None:
    state_prefix = tmp_path / f"freeze-fault-{failing_direction.value}.state"
    strategy = RecordingMakerStrategy(state_prefix)
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    def fail_freeze(reason: str) -> None:
        raise OSError("injected freeze write failure")

    monkeypatch.setattr(
        strategy._stores[failing_direction],
        "freeze_source_submissions",
        fail_freeze,
    )
    full, _ = _filled_events(quantity=1)

    strategy.on_order_filled(full)
    strategy.on_order_filled(full)  # duplicate identity remains idempotent

    persisted = JsonStateStore(store.path)
    assert len(persisted.intents()) == 1
    assert persisted.net_unhedged_ounces == D(1)
    assert len(strategy.recorded) == 1
    assert strategy._global_obligation_block()

    restarted = MakerStrategy(_maker_strategy_config(state_prefix))
    assert restarted._global_obligation_block()


def test_fill_wal_failure_rolls_back_memory_and_replay_commits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_prefix = tmp_path / "fill-wal-replay.state"
    strategy = RecordingMakerStrategy(state_prefix)
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )
    persist = store._persist

    def fail_fill_wal() -> None:
        raise OSError("injected fill WAL failure")

    monkeypatch.setattr(store, "_persist", fail_fill_wal)
    full, _ = _filled_events(quantity=1)

    with pytest.raises(OSError, match="fill WAL"):
        strategy.on_order_filled(full)

    assert strategy._source_hold
    assert strategy.recorded == []
    assert strategy.canceled == [SourceDirection.LONG, SourceDirection.SHORT]
    assert store.intents() == ()
    assert JsonStateStore(store.path).intents() == ()

    monkeypatch.setattr(store, "_persist", persist)
    strategy.on_order_filled(full)
    strategy.on_order_filled(full)

    assert len(JsonStateStore(store.path).intents()) == 1
    assert len(strategy.recorded) == 1
    assert strategy.canceled == [
        SourceDirection.LONG,
        SourceDirection.SHORT,
        SourceDirection.LONG,
        SourceDirection.SHORT,
    ]


def test_restart_after_unreplayed_fill_wal_failure_holds_active_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_prefix = tmp_path / "fill-wal-restart.state"
    strategy = RecordingMakerStrategy(state_prefix)
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source(
        "O-MAKER-EVENTS",
        BusinessOrderSide.BUY,
        D(1),
        source_account_id="BITFINEX-001",
        hedge_account_id="MT5-001",
    )

    def fail_fill_wal() -> None:
        raise OSError("injected fill WAL failure")

    monkeypatch.setattr(store, "_persist", fail_fill_wal)
    full, _ = _filled_events(quantity=1)
    with pytest.raises(OSError, match="fill WAL"):
        strategy.on_order_filled(full)

    restarted = MakerStrategy(_maker_strategy_config(state_prefix))
    reason = restarted._stores[SourceDirection.LONG].recover_for_start()

    assert reason is not None
    assert restarted._global_obligation_block()
    assert not restarted._stores[SourceDirection.LONG].can_submit_source()
