"""Focused Taker routing tests for the MT5 HEDGING boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USD, USDT
from nautilus_trader.model.data import InstrumentStatus, QuoteTick
from nautilus_trader.model.enums import (
    AccountType,
    BookType,
    MarketStatusAction,
    OmsType,
    OrderSide,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
)
from nautilus_trader.model.objects import Money, Quantity

from py000_nautilus.app import (
    BITFINEX,
    HEDGE_ID,
    MT5,
    SOURCE_ID,
    _book_snapshot,
    _hedge_instrument,
    _quote,
    _source_instrument,
    _strategy_config,
)
from py000_nautilus.hedge import HedgeCoordinator, HedgePlanningError, plan_hedge_delta
from py000_nautilus.models import (
    BusinessOrderSide,
    HedgeIntent,
    ObligationStatus,
    Opportunity,
    SourceAccount,
    SourceDirection,
)
from py000_nautilus.store import JsonStateStore
from py000_nautilus.strategies.taker import TakerStrategy


@dataclass(frozen=True, slots=True)
class _Position:
    id: PositionId
    quantity: Quantity
    is_long: bool
    is_short: bool


class _Cache:
    def __init__(self, positions: list[_Position]) -> None:
        self.positions = positions
        hedge = _hedge_instrument()
        self.tick: QuoteTick | None = _quote(hedge, "2400", "2401", "10", 1)

    def quote_tick(self, instrument_id: InstrumentId) -> QuoteTick | None:
        assert instrument_id == _hedge_instrument().id
        return self.tick

    def positions_open(
        self,
        *,
        instrument_id: InstrumentId,
        account_id: AccountId,
    ) -> list[_Position]:
        assert instrument_id == _hedge_instrument().id
        assert account_id == AccountId("MT5-ACCOUNT")
        return self.positions


class _OrderFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.before_limit: Callable[[], None] | None = None

    def market(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(client_order_id=ClientOrderId(f"H-{len(self.calls)}"))

    def limit(self, **kwargs: object) -> object:
        if self.before_limit is not None:
            self.before_limit()
        self.calls.append(kwargs)
        return SimpleNamespace(
            client_order_id=ClientOrderId(f"S-{len(self.calls)}"),
            quantity=kwargs["quantity"],
        )


class _Log:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)


class _SubmitHarness:
    def __init__(
        self,
        path: Path,
        positions: list[_Position],
        *,
        hedge_quantity_ready: object | None = None,
        one_shot: bool = False,
        hedge_must_reduce_only: bool = False,
    ) -> None:
        self._draining = False
        hedge = _hedge_instrument()
        self._config = SimpleNamespace(
            source_instrument_id=_source_instrument().id,
            hedge_instrument_id=hedge.id,
            hedge_account_id=AccountId("MT5-ACCOUNT"),
            hedge_client_id=ClientId("MT5"),
        )
        self.state_store = JsonStateStore(path)
        self._hedges = HedgeCoordinator(
            self._config.source_instrument_id,
            self.state_store,
        )
        self.cache = _Cache(positions)
        self.order_factory = _OrderFactory()
        self.log = _Log()
        self._hedge_quantity_ready = hedge_quantity_ready
        self._one_shot = one_shot
        self._hedge_must_reduce_only = hedge_must_reduce_only
        self._one_shot_hedge_position_id: PositionId | None = None
        self._one_shot_hedge_position_quantity_ounces: Decimal | None = None
        self._allowed_source_direction = None
        self._one_shot_armed = False
        self._one_shot_claimed = False
        self._one_shot_closed = False
        self._source_book_callback_count = 0
        self._last_decision_gate = "not_armed"
        self.hedge_quote_fresh = True
        self.is_running = False
        self.source_subscriptions = 0
        self.submitted: list[tuple[object, PositionId | None, ClientId | None]] = []

    def _subscribe_source_book(self) -> None:
        self.source_subscriptions += 1

    def _evaluate_and_submit(self) -> None:
        TakerStrategy._evaluate_and_submit(cast(Any, self))

    def _required_source_instrument(self) -> object:
        return _source_instrument()

    def _required_hedge_instrument(self) -> object:
        return _hedge_instrument()

    def _quote_is_fresh(self, _tick: object) -> bool:
        return self.hedge_quote_fresh

    def _hedge_positions(self) -> list[_Position]:
        return self.cache.positions

    def _source_hedge_is_executable(
        self,
        opportunity: Opportunity,
        source_quantity_ounces: Decimal,
    ) -> bool:
        return TakerStrategy._source_hedge_is_executable(
            cast(Any, self),
            opportunity,
            source_quantity_ounces,
        )

    def _claim_one_shot(self) -> bool:
        return TakerStrategy._claim_one_shot(cast(Any, self))

    def _submit_next_pending_hedge(self) -> None:
        TakerStrategy._submit_next_pending_hedge(cast(Any, self))

    def _submit_hedge_intent(self, intent: HedgeIntent) -> None:
        TakerStrategy._submit_hedge_intent(cast(Any, self), intent)

    def submit_order(
        self,
        order: object,
        *,
        position_id: PositionId | None = None,
        client_id: ClientId | None,
        params: dict[str, object] | None = None,
    ) -> None:
        del params
        self.submitted.append((order, position_id, client_id))


def _intent(store: JsonStateStore, *, source_side: BusinessOrderSide, ounces: str) -> HedgeIntent:
    store.begin_source(
        "SOURCE-1",
        source_side,
        Decimal(ounces),
        hedge_account_id="MT5-ACCOUNT",
        hedge_client_id="MT5",
    )
    intent = store.reserve_source_fill(
        fill_key="SOURCE-1|VENUE-1|TRADE-1",
        client_order_id="SOURCE-1",
        trade_id="TRADE-1",
        source_side=source_side,
        fill_ounces=Decimal(ounces),
    )
    assert intent is not None
    return intent


def test_signed_delta_planner_covers_open_add_reduce_flat_and_numeric_ticket_order() -> None:
    long_two = _Position(PositionId("2"), Quantity.from_int(2), True, False)
    long_ten = _Position(PositionId("10"), Quantity.from_int(3), True, False)

    flat_open = plan_hedge_delta([], BusinessOrderSide.BUY, Decimal(2))
    same_side_add = plan_hedge_delta(
        [long_two],
        BusinessOrderSide.BUY,
        Decimal(1),
    )
    partial_reduce = plan_hedge_delta(
        [long_two],
        BusinessOrderSide.SELL,
        Decimal(1),
    )
    exact_flat = plan_hedge_delta(
        [long_two],
        BusinessOrderSide.SELL,
        Decimal(2),
    )
    ordered = plan_hedge_delta(
        [long_ten, long_two],
        BusinessOrderSide.SELL,
        Decimal(4),
    )
    ordered_reversed = plan_hedge_delta(
        [long_two, long_ten],
        BusinessOrderSide.SELL,
        Decimal(4),
    )

    assert [(leg.position_id, leg.quantity_ounces) for leg in flat_open] == [(None, Decimal(2))]
    assert [(leg.position_id, leg.quantity_ounces) for leg in same_side_add] == [
        (None, Decimal(1))
    ]
    assert [(leg.position_id, leg.quantity_ounces) for leg in partial_reduce] == [
        ("2", Decimal(1))
    ]
    assert [(leg.position_id, leg.quantity_ounces) for leg in exact_flat] == [
        ("2", Decimal(2))
    ]
    assert [(leg.position_id, leg.quantity_ounces) for leg in ordered] == [
        ("2", Decimal(2)),
        ("10", Decimal(2)),
    ]
    assert ordered_reversed == ordered


def test_signed_delta_planner_rejects_duplicate_or_invalid_ticket_shape() -> None:
    duplicate = [
        _Position(PositionId("7"), Quantity.from_int(1), True, False),
        _Position(PositionId("7"), Quantity.from_int(1), True, False),
    ]
    invalid = [_Position(PositionId("8"), Quantity.from_int(1), True, True)]

    with pytest.raises(HedgePlanningError, match="duplicate"):
        plan_hedge_delta(duplicate, BusinessOrderSide.SELL, Decimal(1))
    with pytest.raises(HedgePlanningError, match="invalid direction"):
        plan_hedge_delta(invalid, BusinessOrderSide.SELL, Decimal(1))


@pytest.mark.parametrize(
    ("source_side", "expected_side"),
    [
        (BusinessOrderSide.BUY, OrderSide.SELL),
        (BusinessOrderSide.SELL, OrderSide.BUY),
    ],
)
def test_flat_source_fill_routes_one_open_hedge_to_mt5(
    tmp_path: Path,
    source_side: BusinessOrderSide,
    expected_side: OrderSide,
) -> None:
    harness = _SubmitHarness(tmp_path / f"flat-{source_side}.json", [])
    intent = _intent(harness.state_store, source_side=source_side, ounces="1")

    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)

    assert len(harness.order_factory.calls) == 1
    order_args = harness.order_factory.calls[0]
    assert order_args["order_side"] == expected_side
    assert str(order_args["quantity"]) == "1"
    assert order_args["reduce_only"] is False
    assert harness.submitted[0][1:] == (None, ClientId("MT5"))
    assert harness.state_store.intent(intent.intent_id).hedge_client_order_id == "H-1"


def test_reverse_fill_uses_single_exact_mt5_position_id(tmp_path: Path) -> None:
    position_id = PositionId("800000001")
    harness = _SubmitHarness(
        tmp_path / "close.json",
        [
            _Position(
                id=position_id,
                quantity=Quantity.from_int(2),
                is_long=True,
                is_short=False,
            )
        ],
    )
    intent = _intent(harness.state_store, source_side=BusinessOrderSide.BUY, ounces="1")

    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)

    assert harness.order_factory.calls[0]["order_side"] == OrderSide.SELL
    assert harness.order_factory.calls[0]["reduce_only"] is True
    assert harness.submitted[0][1:] == (position_id, ClientId("MT5"))


@pytest.mark.parametrize(
    "positions_after_source_fill",
    [
        [],
        [_Position(PositionId("P-REPLACEMENT"), Quantity.from_int(2), False, True)],
        [_Position(PositionId("P-ORIGINAL"), Quantity.from_int(3), False, True)],
    ],
    ids=["ticket-disappears", "ticket-is-replaced", "ticket-quantity-drifts"],
)
def test_exit_hedge_never_opens_when_bound_mt5_ticket_changes_after_source_fill(
    tmp_path: Path,
    positions_after_source_fill: list[_Position],
) -> None:
    position_id = PositionId("P-ORIGINAL")
    harness = _SubmitHarness(
        tmp_path / "exit-ticket-race.json",
        [_Position(position_id, Quantity.from_int(2), False, True)],
        one_shot=True,
        hedge_must_reduce_only=True,
    )
    opportunity = Opportunity(
        direction=SourceDirection.SHORT,
        source_account=SourceAccount(
            account_id=AccountId("BITFINEX-ACCOUNT"),
            client_id=ClientId("BITFINEX"),
            position_ounces=Decimal(2),
            max_long_ounces=Decimal(2),
            max_short_ounces=Decimal(2),
            base_margin_level=Decimal(100),
        ),
        source_price_usdt=Decimal(2400),
        hedge_reference_price_usd=Decimal(2401),
        source_quantity_ounces=Decimal(2),
        net_return=Decimal("0.001"),
        leverage=10,
    )
    TakerStrategy.arm_one_shot(cast(Any, harness))

    TakerStrategy._submit_source(cast(Any, harness), opportunity)

    source = harness.state_store.source_orders()[0]
    assert source.hedge_position_id == position_id.value
    assert harness.order_factory.calls[0]["reduce_only"] is True
    intent = harness.state_store.reserve_source_fill(
        fill_key="SOURCE|VENUE|TRADE",
        client_order_id=source.client_order_id,
        trade_id="TRADE",
        source_side=BusinessOrderSide.SELL,
        fill_ounces=Decimal(2),
    )
    assert intent is not None
    assert intent.hedge_position_id == position_id.value
    harness.cache.positions = positions_after_source_fill

    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)

    assert len(harness.order_factory.calls) == 1
    assert len(harness.submitted) == 1
    assert harness.state_store.intent(intent.intent_id).status is ObligationStatus.BLOCKED
    assert "position ID disappeared or changed" in cast(str, harness.state_store.halt_reason)
    reloaded = JsonStateStore(harness.state_store.path)
    assert reloaded.intent(intent.intent_id).hedge_position_id == position_id.value
    assert reloaded.intent(intent.intent_id).status is ObligationStatus.BLOCKED


def test_legacy_active_source_id_only_fill_blocks_before_hedge_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-active-id-only.json"
    seed = JsonStateStore(path)
    seed.begin_source(
        "SOURCE-LEGACY",
        BusinessOrderSide.BUY,
        Decimal(2),
        hedge_account_id="MT5-ACCOUNT",
        hedge_client_id="MT5",
        hedge_position_id="77",
        hedge_position_quantity_ounces=Decimal(2),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["source_orders"]["SOURCE-LEGACY"][
        "hedge_position_quantity_ounces"
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    harness = _SubmitHarness(
        path,
        [_Position(PositionId("77"), Quantity.from_int(2), True, False)],
    )
    intent = harness.state_store.reserve_source_fill(
        fill_key="SOURCE-LEGACY|VENUE-1|TRADE-1",
        client_order_id="SOURCE-LEGACY",
        trade_id="TRADE-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=Decimal(2),
    )
    assert intent is not None
    assert intent.hedge_position_id == "77"
    assert intent.hedge_position_quantity_ounces is None

    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)

    assert harness.order_factory.calls == []
    assert harness.submitted == []
    blocked = JsonStateStore(path).intent(intent.intent_id)
    assert blocked.status is ObligationStatus.BLOCKED
    assert "position ID disappeared or changed" in cast(
        str,
        harness.state_store.halt_reason,
    )


@pytest.mark.parametrize(
    ("hedge_account_id", "hedge_client_id"),
    [
        (None, None),
        ("MT5-OTHER", "MT5"),
        ("MT5-ACCOUNT", None),
        ("MT5-ACCOUNT", "MT5-OTHER"),
    ],
    ids=["missing", "account-drift", "client-missing", "client-drift"],
)
def test_restart_late_source_fill_with_missing_or_changed_hedge_route_blocks(
    tmp_path: Path,
    hedge_account_id: str | None,
    hedge_client_id: str | None,
) -> None:
    path = tmp_path / f"route-{hedge_account_id}-{hedge_client_id}.json"
    seed = JsonStateStore(path)
    seed.begin_source(
        "SOURCE-ROUTE",
        BusinessOrderSide.BUY,
        Decimal(1),
        hedge_account_id=hedge_account_id,
        hedge_client_id=hedge_client_id,
    )
    harness = _SubmitHarness(path, [])
    assert harness.state_store.recover_for_start() is not None
    intent = harness.state_store.reserve_source_fill(
        fill_key="SOURCE-ROUTE|VENUE-1|TRADE-1",
        client_order_id="SOURCE-ROUTE",
        trade_id="TRADE-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=Decimal(1),
    )
    assert intent is not None

    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)

    assert harness.order_factory.calls == []
    assert harness.submitted == []
    blocked = JsonStateStore(path).intent(intent.intent_id)
    assert blocked.status is ObligationStatus.BLOCKED
    assert "durable Taker hedge route" in cast(str, harness.state_store.halt_reason)


def test_restart_late_source_fill_with_same_durable_route_can_hedge(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route-match.json"
    seed = JsonStateStore(path)
    seed.begin_source(
        "SOURCE-ROUTE",
        BusinessOrderSide.BUY,
        Decimal(1),
        hedge_account_id="MT5-ACCOUNT",
        hedge_client_id="MT5",
    )
    harness = _SubmitHarness(path, [])
    assert harness.state_store.recover_for_start() is not None
    intent = harness.state_store.reserve_source_fill(
        fill_key="SOURCE-ROUTE|VENUE-1|TRADE-1",
        client_order_id="SOURCE-ROUTE",
        trade_id="TRADE-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=Decimal(1),
    )
    assert intent is not None

    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)

    assert len(harness.order_factory.calls) == 1
    assert len(harness.submitted) == 1
    assert harness.state_store.intent(intent.intent_id).status is ObligationStatus.SUBMITTING


def _hedge_fill(client_order_id: str, trade_id: str, quantity: Decimal) -> object:
    return SimpleNamespace(
        instrument_id=_hedge_instrument().id,
        client_order_id=ClientOrderId(client_order_id),
        trade_id=SimpleNamespace(value=trade_id),
        last_qty=quantity,
    )


def test_multiple_opposing_tickets_close_in_stable_exact_order(tmp_path: Path) -> None:
    second = _Position(PositionId("P-2"), Quantity.from_int(1), True, False)
    harness = _SubmitHarness(
        tmp_path / "multi-ticket.json",
        [second, _Position(PositionId("P-1"), Quantity.from_int(1), True, False)],
    )
    intent = _intent(harness.state_store, source_side=BusinessOrderSide.BUY, ounces="2")

    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)

    assert [submitted[1] for submitted in harness.submitted] == [PositionId("P-1")]
    assert [call["reduce_only"] for call in harness.order_factory.calls] == [True]
    harness.cache.positions = [second]
    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(Any, _hedge_fill("H-1", "HT-1", Decimal(1))),
    )

    assert [submitted[1] for submitted in harness.submitted] == [
        PositionId("P-1"),
        PositionId("P-2"),
    ]
    harness.cache.positions = []
    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(Any, _hedge_fill("H-2", "HT-2", Decimal(1))),
    )

    persisted = JsonStateStore(harness.state_store.path).intent(intent.intent_id)
    assert persisted.status is ObligationStatus.COMPLETED
    assert persisted.hedge_leg_index == 2
    assert all(leg.is_close for leg in persisted.hedge_plan)


def test_taker_fresh_hedge_quote_retries_pending_leg_exactly_once(
    tmp_path: Path,
) -> None:
    second = _Position(PositionId("P-2"), Quantity.from_int(1), True, False)
    harness = _SubmitHarness(
        tmp_path / "quote-retry.json",
        [
            second,
            _Position(PositionId("P-1"), Quantity.from_int(1), True, False),
        ],
    )
    intent = _intent(harness.state_store, source_side=BusinessOrderSide.BUY, ounces="2")
    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)
    fresh_tick = harness.cache.tick
    assert fresh_tick is not None
    harness.cache.positions = [second]
    harness.cache.tick = None

    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(Any, _hedge_fill("H-1", "HT-FIRST", Decimal(1))),
    )

    waiting = harness.state_store.intent(intent.intent_id)
    assert len(harness.submitted) == 1
    assert waiting.status is ObligationStatus.PENDING
    assert waiting.hedge_client_order_id is None

    harness.cache.tick = fresh_tick
    TakerStrategy.on_quote_tick(cast(Any, harness), fresh_tick)
    TakerStrategy.on_quote_tick(cast(Any, harness), fresh_tick)

    assert [submitted[1] for submitted in harness.submitted] == [
        PositionId("P-1"),
        PositionId("P-2"),
    ]
    submitted = harness.state_store.intent(intent.intent_id)
    assert submitted.status is ObligationStatus.SUBMITTING
    assert submitted.hedge_client_order_id == "H-2"


def test_cross_zero_closes_ticket_before_one_residual_open(tmp_path: Path) -> None:
    harness = _SubmitHarness(
        tmp_path / "cross-zero.json",
        [_Position(PositionId("P-1"), Quantity.from_int(1), True, False)],
    )
    intent = _intent(harness.state_store, source_side=BusinessOrderSide.BUY, ounces="2")

    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)
    harness.cache.positions = []
    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(Any, _hedge_fill("H-1", "HT-CLOSE", Decimal(1))),
    )

    assert [call["reduce_only"] for call in harness.order_factory.calls] == [True, False]
    assert [str(call["quantity"]) for call in harness.order_factory.calls] == ["1", "1"]
    assert [submitted[1] for submitted in harness.submitted] == [PositionId("P-1"), None]
    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(Any, _hedge_fill("H-2", "HT-OPEN", Decimal(1))),
    )

    assert harness.state_store.intent(intent.intent_id).status is ObligationStatus.COMPLETED


def test_ticket_drift_between_close_legs_is_persistently_blocked(tmp_path: Path) -> None:
    harness = _SubmitHarness(
        tmp_path / "ticket-drift.json",
        [
            _Position(PositionId("P-2"), Quantity.from_int(1), True, False),
            _Position(PositionId("P-1"), Quantity.from_int(1), True, False),
        ],
    )
    intent = _intent(harness.state_store, source_side=BusinessOrderSide.BUY, ounces="2")
    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)
    harness.cache.positions = [
        _Position(PositionId("P-2"), Quantity.from_int(2), True, False)
    ]

    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(Any, _hedge_fill("H-1", "HT-FIRST", Decimal(1))),
    )

    assert len(harness.submitted) == 1
    persisted = JsonStateStore(harness.state_store.path).intent(intent.intent_id)
    assert persisted.status is ObligationStatus.BLOCKED
    assert "quantity drifted" in cast(str, harness.state_store.halt_reason)


def test_incomplete_fok_leg_fill_is_persistently_blocked_without_next_leg(
    tmp_path: Path,
) -> None:
    harness = _SubmitHarness(
        tmp_path / "partial-fok.json",
        [
            _Position(PositionId("P-1"), Quantity.from_int(1), True, False),
            _Position(PositionId("P-2"), Quantity.from_int(1), True, False),
        ],
    )
    intent = _intent(harness.state_store, source_side=BusinessOrderSide.BUY, ounces="2")
    TakerStrategy._submit_hedge_intent(cast(Any, harness), intent)

    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(Any, _hedge_fill("H-1", "HT-PARTIAL", Decimal("0.5"))),
    )

    assert len(harness.submitted) == 1
    persisted = JsonStateStore(harness.state_store.path).intent(intent.intent_id)
    assert persisted.status is ObligationStatus.BLOCKED
    assert persisted.hedge_filled_ounces == Decimal("0.5")
    assert persisted.hedge_client_order_id == "H-1"
    assert "does not match planned leg remainder" in cast(
        str,
        harness.state_store.halt_reason,
    )

    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(Any, _hedge_fill("H-1", "HT-PARTIAL-2", Decimal("0.5"))),
    )
    held = JsonStateStore(harness.state_store.path).intent(intent.intent_id)
    assert len(harness.submitted) == 1
    assert held.status is ObligationStatus.BLOCKED
    assert held.hedge_client_order_id == "H-1"
    assert held.hedge_leg_index == 0
    assert held.hedge_filled_ounces == Decimal(1)


@pytest.mark.parametrize(
    "positions",
    [
        [
            _Position(PositionId("P-1"), Quantity.from_int(1), True, False),
            _Position(PositionId("P-2"), Quantity.from_int(1), True, False),
        ],
        [_Position(PositionId("P-1"), Quantity.from_int(1), True, False)],
    ],
    ids=["multiple-opposing-tickets", "one-ticket-too-small"],
)
def test_source_order_is_admitted_when_multi_ticket_transition_is_exact(
    tmp_path: Path,
    positions: list[_Position],
) -> None:
    harness = _SubmitHarness(tmp_path / "preflight.json", positions)
    opportunity = Opportunity(
        direction=SourceDirection.LONG,
        source_account=SourceAccount(
            account_id=AccountId("BITFINEX-ACCOUNT"),
            client_id=ClientId("BITFINEX"),
            position_ounces=Decimal(0),
            max_long_ounces=Decimal(10),
            max_short_ounces=Decimal(10),
            base_margin_level=Decimal(100),
        ),
        source_price_usdt=Decimal(2400),
        hedge_reference_price_usd=Decimal(2401),
        source_quantity_ounces=Decimal(2),
        net_return=Decimal("0.001"),
        leverage=10,
    )

    TakerStrategy._submit_source(cast(Any, harness), opportunity)

    assert len(harness.order_factory.calls) == 1
    assert len(harness.submitted) == 1
    assert harness.state_store.path.exists()
    assert harness.log.errors == []


@pytest.mark.parametrize("predicate_outcome", ["false", "raises"])
def test_source_order_is_not_created_when_live_hedge_quantity_is_not_executable(
    tmp_path: Path,
    predicate_outcome: str,
) -> None:
    observed_quantities: list[Decimal] = []

    def hedge_quantity_ready(quantity_ounces: Decimal) -> bool:
        observed_quantities.append(quantity_ounces)
        if predicate_outcome == "raises":
            raise RuntimeError("capacity unavailable")
        return False

    harness = _SubmitHarness(
        tmp_path / f"capacity-{predicate_outcome}.json",
        [],
        hedge_quantity_ready=hedge_quantity_ready,
    )
    opportunity = Opportunity(
        direction=SourceDirection.LONG,
        source_account=SourceAccount(
            account_id=AccountId("BITFINEX-ACCOUNT"),
            client_id=ClientId("BITFINEX"),
            position_ounces=Decimal(0),
            max_long_ounces=Decimal(10),
            max_short_ounces=Decimal(10),
            base_margin_level=Decimal(100),
        ),
        source_price_usdt=Decimal(2400),
        hedge_reference_price_usd=Decimal(2401),
        source_quantity_ounces=Decimal(2),
        net_return=Decimal("0.001"),
        leverage=10,
    )

    TakerStrategy._submit_source(cast(Any, harness), opportunity)

    assert observed_quantities == [Decimal(2)]
    assert harness.order_factory.calls == []
    assert harness.submitted == []
    assert not harness.state_store.path.exists()
    assert len(harness.log.errors) == 1


def test_one_shot_claims_before_order_factory_and_known_reject_never_retries(
    tmp_path: Path,
) -> None:
    harness = _SubmitHarness(tmp_path / "one-shot.json", [], one_shot=True)
    harness.is_running = True
    opportunity = Opportunity(
        direction=SourceDirection.LONG,
        source_account=SourceAccount(
            account_id=AccountId("BITFINEX-ACCOUNT"),
            client_id=ClientId("BITFINEX"),
            position_ounces=Decimal(0),
            max_long_ounces=Decimal(2),
            max_short_ounces=Decimal(2),
            base_margin_level=Decimal(100),
        ),
        source_price_usdt=Decimal(2400),
        hedge_reference_price_usd=Decimal(2401),
        source_quantity_ounces=Decimal(2),
        net_return=Decimal("0.001"),
        leverage=10,
    )
    TakerStrategy.arm_one_shot(cast(Any, harness))

    assert harness.source_subscriptions == 1

    def assert_claimed() -> None:
        assert harness._one_shot_claimed

    harness.order_factory.before_limit = assert_claimed

    TakerStrategy._submit_source(cast(Any, harness), opportunity)

    assert not harness._one_shot_armed
    assert harness._one_shot_claimed
    assert len(harness.order_factory.calls) == 1
    assert len(harness.submitted) == 1
    source = harness.state_store.source_orders()[0]
    assert source.source_account_id == "BITFINEX-ACCOUNT"
    assert source.source_client_id == "BITFINEX"
    assert source.hedge_account_id == "MT5-ACCOUNT"
    assert source.hedge_client_id == "MT5"
    TakerStrategy._finish_or_reject(cast(Any, harness), source.client_order_id, "REJECTED")
    assert harness.state_store.active_source_order_id is None
    assert harness.state_store.source_freeze_reason == "one-shot source attempt claimed"
    assert not harness.state_store.can_submit_source()

    TakerStrategy._submit_source(cast(Any, harness), opportunity)

    assert len(harness.order_factory.calls) == 1
    assert len(harness.submitted) == 1
    assert JsonStateStore(harness.state_store.path).source_freeze_reason == (
        "one-shot source attempt claimed"
    )


def test_disarmed_one_shot_cannot_be_rearmed(tmp_path: Path) -> None:
    harness = _SubmitHarness(tmp_path / "closed-one-shot.json", [], one_shot=True)

    TakerStrategy.arm_one_shot(cast(Any, harness))
    TakerStrategy.disarm_one_shot(cast(Any, harness))

    assert not harness._one_shot_armed
    with pytest.raises(RuntimeError, match="already armed or claimed"):
        TakerStrategy.arm_one_shot(cast(Any, harness))


def test_partial_source_fills_queue_exactly_one_mt5_hedge_at_a_time(tmp_path: Path) -> None:
    harness = _SubmitHarness(tmp_path / "single-flight.json", [])
    harness.state_store.begin_source(
        "SOURCE-1",
        BusinessOrderSide.BUY,
        Decimal(3),
        hedge_account_id="MT5-ACCOUNT",
        hedge_client_id="MT5",
    )
    first = harness.state_store.reserve_source_fill(
        fill_key="SOURCE-1|VENUE-1|TRADE-1",
        client_order_id="SOURCE-1",
        trade_id="TRADE-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=Decimal(1),
    )
    assert first is not None

    harness._submit_next_pending_hedge()
    second = harness.state_store.reserve_source_fill(
        fill_key="SOURCE-1|VENUE-1|TRADE-2",
        client_order_id="SOURCE-1",
        trade_id="TRADE-2",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=Decimal(1),
    )
    assert second is not None

    harness._submit_next_pending_hedge()
    assert len(harness.submitted) == 1
    assert harness.state_store.intent(second.intent_id).status is ObligationStatus.PENDING
    harness.state_store.update_hedge_status("H-1", ObligationStatus.ACCEPTED)
    harness._submit_next_pending_hedge()
    assert len(harness.submitted) == 1
    harness.state_store.update_source_status("SOURCE-1", "CANCELED")
    assert harness.state_store.halt_reason is not None

    TakerStrategy.on_order_filled(
        cast(Any, harness),
        cast(
            Any,
            SimpleNamespace(
                instrument_id=_hedge_instrument().id,
                client_order_id=ClientOrderId("H-1"),
                trade_id=SimpleNamespace(value="HEDGE-TRADE-1"),
                last_qty=Decimal(1),
            ),
        ),
    )

    assert len(harness.submitted) == 2
    assert harness.state_store.intent(first.intent_id).status is ObligationStatus.COMPLETED
    assert harness.state_store.intent(second.intent_id).hedge_client_order_id == "H-2"
    assert harness.state_store.intent(second.intent_id).status is ObligationStatus.SUBMITTING


@pytest.mark.parametrize(
    "terminal_status",
    [ObligationStatus.REJECTED, ObligationStatus.UNKNOWN],
)
def test_failed_hedge_stops_the_remaining_single_flight_queue(
    tmp_path: Path,
    terminal_status: ObligationStatus,
) -> None:
    harness = _SubmitHarness(tmp_path / f"queue-{terminal_status}.json", [])
    harness.state_store.begin_source(
        "SOURCE-1",
        BusinessOrderSide.BUY,
        Decimal(3),
        hedge_account_id="MT5-ACCOUNT",
        hedge_client_id="MT5",
    )
    first = harness.state_store.reserve_source_fill(
        fill_key="SOURCE-1|VENUE-1|TRADE-1",
        client_order_id="SOURCE-1",
        trade_id="TRADE-1",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=Decimal(1),
    )
    assert first is not None
    harness._submit_next_pending_hedge()
    second = harness.state_store.reserve_source_fill(
        fill_key="SOURCE-1|VENUE-1|TRADE-2",
        client_order_id="SOURCE-1",
        trade_id="TRADE-2",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=Decimal(1),
    )
    assert second is not None

    harness.state_store.update_hedge_status("H-1", terminal_status)
    harness._submit_next_pending_hedge()

    assert len(harness.submitted) == 1
    assert harness.state_store.intent(first.intent_id).status is terminal_status
    assert harness.state_store.intent(second.intent_id).status is ObligationStatus.PENDING


def test_live_readiness_is_an_independent_fail_closed_gate() -> None:
    harness = SimpleNamespace(
        live_calls=0,
        _live_costs_from_adapters=False,
        _one_shot=False,
        _one_shot_armed=False,
        _one_shot_claimed=False,
    )

    def not_ready() -> bool:
        harness.live_calls += 1
        return False

    harness._live_submission_ready = not_ready

    assert not TakerStrategy._inputs_are_fresh(
        cast(Any, harness),
        1,
        cast(Any, object()),
        1,
    )
    assert harness.live_calls == 1


def test_mt5_instrument_status_updates_session_fact_only() -> None:
    hedge_id = _hedge_instrument().id
    observed: list[tuple[bool, int]] = []
    harness = SimpleNamespace(
        _config=SimpleNamespace(hedge_instrument_id=hedge_id),
        update_hedge_session=lambda is_open, ts_event: observed.append((is_open, ts_event)),
    )
    status = InstrumentStatus(
        instrument_id=hedge_id,
        action=MarketStatusAction.TRADING,
        ts_event=123,
        ts_init=124,
        is_trading=True,
    )

    TakerStrategy.on_instrument_status(cast(Any, harness), status)

    assert observed == [(True, 123)]


def test_hedging_engine_reversal_closes_the_exact_open_ticket(tmp_path: Path) -> None:
    engine = BacktestEngine(
        BacktestEngineConfig(
            logging=LoggingConfig(bypass_logging=True),
            run_analysis=False,
        )
    )
    engine.add_venue(
        venue=BITFINEX,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        book_type=BookType.L2_MBP,
        starting_balances=[Money(1_000_000, USDT)],
        base_currency=USDT,
        default_leverage=Decimal(16),
    )
    engine.add_venue(
        venue=MT5,
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(1_000_000, USD)],
        base_currency=USD,
        default_leverage=Decimal(10),
    )
    source = _source_instrument()
    hedge = _hedge_instrument()
    engine.add_instrument(source)
    engine.add_instrument(hedge)
    state_path = tmp_path / "reversal.json"
    engine.add_strategy(TakerStrategy(_strategy_config(state_path)))
    engine.add_data(
        [
            # Introduce only the intended long at 3s, then its exact-ticket reversal at 5s.
            _book_snapshot(source, "2402", "2403", "5", 1_000_000_000),
            _quote(hedge, "2404", "2405", "10", 2_000_000_000),
            _book_snapshot(source, "2398", "2400", "5", 3_000_000_000),
            _quote(hedge, "2400", "2401", "10", 4_000_000_000),
            _book_snapshot(source, "2405", "2407", "5", 5_000_000_000),
        ]
    )
    try:
        engine.run()
        source_orders = sorted(
            (order for order in engine.cache.orders() if order.instrument_id == SOURCE_ID),
            key=lambda order: order.ts_init,
        )
        hedge_orders = sorted(
            (order for order in engine.cache.orders() if order.instrument_id == HEDGE_ID),
            key=lambda order: order.ts_init,
        )
        opening_hedge = next(order for order in hedge_orders if not order.is_reduce_only)
        closing_hedge = next(order for order in hedge_orders if order.is_reduce_only)
        opened_position_id = engine.cache.position_id(opening_hedge.client_order_id)
        closed_position_id = engine.cache.position_id(closing_hedge.client_order_id)
        intents = JsonStateStore(state_path).intents()

        assert [order.side for order in source_orders] == [OrderSide.BUY, OrderSide.SELL]
        assert [order.is_reduce_only for order in source_orders] == [False, True]
        assert [order.side for order in hedge_orders] == [OrderSide.SELL, OrderSide.BUY]
        assert opened_position_id is not None
        assert closed_position_id == opened_position_id
        assert engine.cache.positions_open(instrument_id=HEDGE_ID) == []
        assert len(intents) == 2
        assert all(intent.status is ObligationStatus.COMPLETED for intent in intents)
    finally:
        engine.dispose()
