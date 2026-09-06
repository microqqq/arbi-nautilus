"""Translate fills and MT5 HEDGING positions into durable hedge work."""

from collections.abc import Sequence
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.position import Position

from py000_nautilus.models import (
    BusinessOrderSide,
    HedgeIntent,
    HedgeLeg,
    ObligationStatus,
)
from py000_nautilus.store import JsonStateStore


class HedgePlanningError(RuntimeError):
    """Current MT5 ticket state cannot safely execute a planned hedge leg."""


class HedgeLaneBusy(HedgePlanningError):
    """An earlier obligation still owns the route; waiting is not a failed plan."""


def hedge_order_params(leg: HedgeLeg) -> dict[str, object]:
    """Carry the bound leg's prerequisite through native SubmitOrder routing."""
    params: dict[str, object] = {"py000_hedge_plan": True}
    if leg.is_close:
        params["py000_expected_position_ounces"] = leg.expected_position_quantity_ounces
    return params


def plan_hedge_delta(
    positions: Sequence[Position],
    side: BusinessOrderSide,
    quantity_ounces: Decimal,
) -> tuple[HedgeLeg, ...]:
    """Close opposing MT5 tickets in PositionId order, then open any residual.

    The returned legs express a signed *delta*, not a target net position.  Same-side
    tickets therefore do not absorb the request: any residual is a normal MT5 open.
    """

    if not isinstance(side, BusinessOrderSide):
        raise TypeError("hedge side must be BusinessOrderSide")
    if (
        not isinstance(quantity_ounces, Decimal)
        or not quantity_ounces.is_finite()
        or quantity_ounces <= 0
    ):
        raise ValueError("hedge quantity must be a positive finite Decimal")

    snapshots: list[tuple[str, Decimal, BusinessOrderSide]] = []
    seen_ids: set[str] = set()
    for position in positions:
        position_id = position.id.value
        if not position_id or position_id in seen_ids:
            raise HedgePlanningError("MT5 open positions contain a duplicate position ID")
        seen_ids.add(position_id)
        position_side = _position_side(position)
        position_quantity = Decimal(str(position.quantity))
        if not position_quantity.is_finite() or position_quantity <= 0:
            raise HedgePlanningError(
                f"MT5 position {position_id} has a non-positive or invalid quantity"
            )
        snapshots.append((position_id, position_quantity, position_side))

    remaining = quantity_ounces
    legs: list[HedgeLeg] = []
    for position_id, position_quantity, position_side in sorted(
        snapshots,
        key=lambda item: _position_id_sort_key(item[0]),
    ):
        if position_side is side:
            continue
        close_quantity = min(remaining, position_quantity)
        legs.append(
            HedgeLeg(
                side=side,
                quantity_ounces=close_quantity,
                position_id=position_id,
                expected_position_side=position_side,
                expected_position_quantity_ounces=position_quantity,
            )
        )
        remaining -= close_quantity
        if remaining == 0:
            break

    if remaining > 0:
        legs.append(HedgeLeg(side=side, quantity_ounces=remaining))
    return tuple(legs)


def validate_hedge_leg(leg: HedgeLeg, positions: Sequence[Position]) -> None:
    """Reauthenticate a planned close target immediately before its wire call."""

    if not isinstance(leg, HedgeLeg):
        raise TypeError("leg must be HedgeLeg")
    if not leg.is_close:
        opposing = [position for position in positions if _position_side(position) is not leg.side]
        if opposing:
            raise HedgePlanningError(
                "opposing MT5 ticket appeared before the planned open residual"
            )
        return

    targets = [position for position in positions if position.id.value == leg.position_id]
    if len(targets) != 1:
        raise HedgePlanningError(f"MT5 position {leg.position_id} disappeared after planning")
    target = targets[0]
    if _position_side(target) is not leg.expected_position_side:
        raise HedgePlanningError(f"MT5 position {leg.position_id} direction drifted after planning")
    observed_quantity = Decimal(str(target.quantity))
    if observed_quantity != leg.expected_position_quantity_ounces:
        raise HedgePlanningError(f"MT5 position {leg.position_id} quantity drifted after planning")


def _position_side(position: Position) -> BusinessOrderSide:
    if position.is_long and not position.is_short:
        return BusinessOrderSide.BUY
    if position.is_short and not position.is_long:
        return BusinessOrderSide.SELL
    raise HedgePlanningError(f"MT5 position {position.id.value} has an invalid direction")


def _position_id_sort_key(position_id: str) -> tuple[int, int | str, str]:
    """Sort native numeric MT5 identifiers numerically, with a deterministic fallback."""
    if position_id.isdecimal():
        return 0, int(position_id), position_id
    return 1, position_id, position_id


class HedgeCoordinator:
    """Exactly-once intent reservation keyed by Nautilus venue trade identity."""

    def __init__(self, source_instrument_id: InstrumentId, store: JsonStateStore) -> None:
        self._source_instrument_id = source_instrument_id
        self._store = store

    def on_source_filled(self, event: OrderFilled) -> HedgeIntent | None:
        if event.instrument_id != self._source_instrument_id:
            return None
        client_order_id = event.client_order_id.value
        if not self._store.knows_source_order(client_order_id):
            return None
        side = _business_side(event.order_side)
        fill_key = _fill_key(event)
        return self._store.reserve_source_fill(
            fill_key=fill_key,
            client_order_id=client_order_id,
            trade_id=event.trade_id.value,
            source_side=side,
            fill_ounces=Decimal(str(event.last_qty)),
        )

    def has_seen_source_fill(self, event: OrderFilled) -> bool:
        if event.instrument_id != self._source_instrument_id:
            return False
        client_order_id = event.client_order_id.value
        if not self._store.knows_source_order(client_order_id):
            return False
        return self._store.has_seen_source_fill(_fill_key(event))

    def next_hedge_leg(
        self,
        intent_id: str,
        positions: Sequence[Position],
    ) -> HedgeLeg:
        """Persist a plan once and revalidate its next exact ticket before submit."""
        intent = self._store.intent(intent_id)
        if not self._store.hedge_dispatch_ready(intent_id):
            raise HedgeLaneBusy("an earlier hedge obligation still owns the route")
        try:
            if not intent.hedge_plan:
                remaining = intent.hedge_quantity_ounces - intent.hedge_filled_ounces
                plan = plan_hedge_delta(positions, intent.hedge_side, remaining)
                self._validate_bound_exit(intent, plan)
                intent = self._store.bind_hedge_plan(intent.intent_id, plan)
            if intent.hedge_leg_index >= len(intent.hedge_plan):
                raise HedgePlanningError("hedge ticket plan has no remaining leg")
            leg = intent.hedge_plan[intent.hedge_leg_index]
            validate_hedge_leg(leg, positions)
            return leg
        except (HedgePlanningError, TypeError, ValueError) as exc:
            current = self._store.intent(intent_id)
            if current.status is not ObligationStatus.COMPLETED:
                self._store.block_hedge_intent(intent_id, str(exc))
            raise HedgePlanningError(str(exc)) from exc

    def bind_hedge_leg(self, intent_id: str, client_order_id: str) -> None:
        self._store.bind_hedge_order(intent_id, client_order_id)

    def on_hedge_filled(self, event: OrderFilled) -> bool:
        """Advance exactly one planned leg from its authoritative fill event."""
        return self._store.apply_hedge_fill(
            client_order_id=event.client_order_id.value,
            trade_id=event.trade_id.value,
            fill_ounces=Decimal(str(event.last_qty)),
        )

    @staticmethod
    def _validate_bound_exit(intent: HedgeIntent, plan: tuple[HedgeLeg, ...]) -> None:
        """Retain the existing one-shot exact-ticket promise when one was persisted."""
        if intent.hedge_position_id is None:
            return
        if (
            len(plan) != 1
            or not plan[0].is_close
            or plan[0].position_id != intent.hedge_position_id
            or plan[0].expected_position_quantity_ounces
            != intent.hedge_position_quantity_ounces
        ):
            raise HedgePlanningError("exit hedge MT5 position ID disappeared or changed")


def _business_side(side: OrderSide) -> BusinessOrderSide:
    if side is OrderSide.BUY:
        return BusinessOrderSide.BUY
    if side is OrderSide.SELL:
        return BusinessOrderSide.SELL
    raise ValueError(f"unsupported source fill side {side}")


def _fill_key(event: OrderFilled) -> str:
    return (
        f"{event.client_order_id.value}|{event.venue_order_id.value}|{event.trade_id.value}"
    )
