"""One-file durable custody for source orders, fills, and hedge obligations."""

import json
import logging
import os
import tempfile
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast

from py000_nautilus.durability import (
    ParentDirectorySyncError,
    create_and_sync_parent,
    replace_and_sync_parent,
)
from py000_nautilus.economics import round_hedge_ounces
from py000_nautilus.models import (
    BusinessOrderSide,
    HedgeIntent,
    HedgeLeg,
    ObligationStatus,
    RejectedHedgeAttempt,
)


@dataclass(frozen=True, slots=True)
class SourceOrderRecord:
    client_order_id: str
    side: BusinessOrderSide
    quantity_ounces: Decimal
    source_account_id: str | None = None
    source_client_id: str | None = None
    hedge_account_id: str | None = None
    hedge_client_id: str | None = None
    hedge_position_id: str | None = None
    hedge_position_quantity_ounces: Decimal | None = None
    filled_ounces: Decimal = Decimal(0)
    status: str = "SUBMITTING"


@dataclass(slots=True)
class StoreState:
    source_orders: dict[str, SourceOrderRecord]
    active_source_order_id: str | None
    seen_source_fills: set[str]
    seen_hedge_fills: set[str]
    hedge_intents: dict[str, HedgeIntent]
    net_unhedged_ounces: Decimal
    halt_reason: str | None
    source_freeze_reason: str | None


class JsonStateStore:
    """Atomically persist the small state needed to fail closed on restart."""

    _restart_halt_recorder: Callable[[str], None] | None = None
    _restart_failure_recorder: Callable[[], None] | None = None
    _restart_pause_revoker: Callable[[], None] | None = None

    def _revoke_restart_permission(self) -> None:
        revoker = getattr(self, "_restart_pause_revoker", None)
        if revoker is not None:
            revoker()

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._state = self._load() if self.path.exists() else self._empty_state()

    @staticmethod
    def _empty_state() -> StoreState:
        return StoreState(
            source_orders={},
            active_source_order_id=None,
            seen_source_fills=set(),
            seen_hedge_fills=set(),
            hedge_intents={},
            net_unhedged_ounces=Decimal(0),
            halt_reason=None,
            source_freeze_reason=None,
        )

    @property
    def halt_reason(self) -> str | None:
        return self._state.halt_reason

    @property
    def source_freeze_reason(self) -> str | None:
        return self._state.source_freeze_reason

    @property
    def net_unhedged_ounces(self) -> Decimal:
        outstanding = self.rounding_residual_ounces
        for intent in self._state.hedge_intents.values():
            remaining = intent.hedge_quantity_ounces - intent.hedge_filled_ounces
            sign = Decimal(1) if intent.hedge_side is BusinessOrderSide.SELL else Decimal(-1)
            outstanding += sign * remaining
        return outstanding

    @property
    def rounding_residual_ounces(self) -> Decimal:
        """Signed fill residual not yet large enough to become a hedge obligation."""
        return self._state.net_unhedged_ounces

    @property
    def active_source_order_id(self) -> str | None:
        return self._state.active_source_order_id

    def can_submit_source(self) -> bool:
        return (
            self._state.halt_reason is None
            and self._state.source_freeze_reason is None
            and self._state.active_source_order_id is None
            and self._source_balance_is_admissible()
            and all(
                intent.status is ObligationStatus.COMPLETED
                for intent in self._state.hedge_intents.values()
            )
        )

    def has_unresolved_hedges(self) -> bool:
        return any(
            intent.status is not ObligationStatus.COMPLETED
            for intent in self._state.hedge_intents.values()
        )

    def _source_balance_is_admissible(self) -> bool:
        return self.rounding_residual_ounces == 0 and self.net_unhedged_ounces == 0

    def freeze_source_submissions(self, reason: str) -> None:
        """Persist a Maker-wide hold without inventing an order terminal state."""
        if not reason:
            raise ValueError("freeze reason must not be empty")
        self._revoke_restart_permission()
        if self._state.source_freeze_reason is not None:
            return
        self._state.source_freeze_reason = reason
        self._persist()

    def cycle_evidence_complete(self) -> bool:
        terminal = {"FILLED", "DENIED", "REJECTED", "CANCELED", "EXPIRED"}
        return (
            self._state.halt_reason is None
            and self._state.active_source_order_id is None
            and all(record.status in terminal for record in self._state.source_orders.values())
            and all(
                intent.status is ObligationStatus.COMPLETED
                for intent in self._state.hedge_intents.values()
            )
            and self._source_balance_is_admissible()
        )

    def clear_source_freeze(self) -> None:
        if not self.cycle_evidence_complete():
            raise RuntimeError("Maker cycle evidence is incomplete")
        self._state.source_freeze_reason = None
        self._persist()

    def recover_for_start(self) -> str | None:
        """Turn any crash-surviving in-flight work into an explicit stop."""
        active = self._state.active_source_order_id
        unresolved = [
            intent
            for intent in self._state.hedge_intents.values()
            if intent.status is not ObligationStatus.COMPLETED
        ]
        if active is None and not unresolved:
            return self._state.halt_reason
        details = []
        if active is not None:
            current = self._state.source_orders[active]
            self._state.source_orders[active] = replace(current, status="UNKNOWN")
            details.append(f"source={active}")
        if unresolved:
            in_flight = {
                ObligationStatus.PENDING,
                ObligationStatus.SUBMITTING,
                ObligationStatus.SUBMITTED,
                ObligationStatus.ACCEPTED,
            }
            for intent in unresolved:
                if intent.status in in_flight:
                    self._state.hedge_intents[intent.intent_id] = replace(
                        intent,
                        status=ObligationStatus.UNKNOWN,
                    )
            details.append(f"hedges={','.join(intent.intent_id for intent in unresolved)}")
        new_halt = self._state.halt_reason is None
        if new_halt:
            self._state.halt_reason = "restart requires reconciliation: " + " ".join(details)
        try:
            self._persist()
        except ParentDirectorySyncError:
            failed = getattr(self, "_restart_failure_recorder", None)
            if failed is not None:
                failed()
            raise
        recorder = getattr(self, "_restart_halt_recorder", None)
        if new_halt and recorder is not None:
            assert self._state.halt_reason is not None
            recorder(self._state.halt_reason)
        return self._state.halt_reason

    def begin_source(
        self,
        client_order_id: str,
        side: BusinessOrderSide,
        quantity_ounces: Decimal,
        *,
        source_account_id: str | None = None,
        source_client_id: str | None = None,
        hedge_account_id: str | None = None,
        hedge_client_id: str | None = None,
        hedge_position_id: str | None = None,
        hedge_position_quantity_ounces: Decimal | None = None,
        source_freeze_reason: str | None = None,
    ) -> None:
        if not self.can_submit_source():
            raise RuntimeError("source submission blocked by persisted state")
        if quantity_ounces <= 0:
            raise ValueError("source quantity must be positive")
        if source_freeze_reason is not None and not source_freeze_reason:
            raise ValueError("source freeze reason must not be empty")
        if (hedge_position_id is None) != (hedge_position_quantity_ounces is None):
            raise ValueError("hedge position ID and quantity must appear together")
        if hedge_position_id is not None and not hedge_position_id:
            raise ValueError("hedge position ID must not be empty")
        if hedge_position_quantity_ounces is not None and (
            not hedge_position_quantity_ounces.is_finite()
            or hedge_position_quantity_ounces <= 0
        ):
            raise ValueError("hedge position quantity must be positive and finite")
        record = SourceOrderRecord(
            client_order_id=client_order_id,
            side=side,
            quantity_ounces=quantity_ounces,
            source_account_id=source_account_id,
            source_client_id=source_client_id,
            hedge_account_id=hedge_account_id,
            hedge_client_id=hedge_client_id,
            hedge_position_id=hedge_position_id,
            hedge_position_quantity_ounces=hedge_position_quantity_ounces,
        )
        self._state.source_orders[client_order_id] = record
        self._state.active_source_order_id = client_order_id
        self._state.source_freeze_reason = source_freeze_reason
        self._persist()

    def knows_source_order(self, client_order_id: str) -> bool:
        return client_order_id in self._state.source_orders

    def has_seen_source_fill(self, fill_key: str) -> bool:
        return fill_key in self._state.seen_source_fills

    def source_order(self, client_order_id: str) -> SourceOrderRecord | None:
        return self._state.source_orders.get(client_order_id)

    def source_orders(self) -> tuple[SourceOrderRecord, ...]:
        return tuple(self._state.source_orders.values())

    def update_source_status(self, client_order_id: str, status: str) -> None:
        record = self._state.source_orders.get(client_order_id)
        if record is None:
            return
        if status in {"UNKNOWN", "DENIED", "REJECTED", "CANCELED", "EXPIRED"}:
            self._revoke_restart_permission()
        self._state.source_orders[client_order_id] = replace(record, status=status)
        if (
            status in {"DENIED", "FILLED", "REJECTED"}
            and self._state.active_source_order_id == client_order_id
        ):
            self._state.active_source_order_id = None
        if status in {"CANCELED", "EXPIRED"} and self._state.halt_reason is None:
            self._state.halt_reason = _source_reconcile_reason(client_order_id)
        self._persist()

    def confirm_source_reconciled(self, client_order_id: str) -> None:
        """Release a canceled source only after an adapter supplies an authoritative report."""
        record = self._state.source_orders.get(client_order_id)
        if record is None or record.status not in {"CANCELED", "EXPIRED"}:
            raise ValueError("source order is not awaiting terminal reconciliation")
        previous_state = deepcopy(self._state)
        if self._state.active_source_order_id == client_order_id:
            self._state.active_source_order_id = None
        if self._state.halt_reason == _source_reconcile_reason(client_order_id):
            self._state.halt_reason = None
        try:
            self._persist()
        except ParentDirectorySyncError:
            raise
        except Exception:
            self._state = previous_state
            raise

    def mark_source_unknown(self, client_order_id: str, reason: str) -> None:
        self._revoke_restart_permission()
        record = self._state.source_orders.get(client_order_id)
        if record is not None:
            self._state.source_orders[client_order_id] = replace(record, status="UNKNOWN")
        self._state.halt_reason = reason
        self._persist()

    def reserve_source_fill(
        self,
        *,
        fill_key: str,
        client_order_id: str,
        trade_id: str,
        source_side: BusinessOrderSide,
        fill_ounces: Decimal,
    ) -> HedgeIntent | None:
        """Record one actual fill and, when integer ounces accrue, one intent."""
        if self.has_seen_source_fill(fill_key) or not self.knows_source_order(client_order_id):
            return None
        previous_state = deepcopy(self._state)
        try:
            intent = self._reserve_source_fill(
                fill_key=fill_key, client_order_id=client_order_id, trade_id=trade_id,
                source_side=source_side, fill_ounces=fill_ounces,
            )
        except Exception:
            self._state = previous_state
            raise
        self._persist_source_reservation(previous_state)
        return intent

    def _reserve_source_fill(
        self, *, fill_key: str, client_order_id: str, trade_id: str,
        source_side: BusinessOrderSide, fill_ounces: Decimal,
        blocked_reason: str | None = None,
    ) -> HedgeIntent | None:
        """Apply the existing allocation without I/O; callers own atomic publication."""
        if fill_key in self._state.seen_source_fills:
            return None
        record = self._state.source_orders.get(client_order_id)
        if record is None:
            return None
        if record.side is not source_side:
            raise ValueError("source fill side does not match submitted order")
        if fill_ounces <= 0:
            raise ValueError("fill quantity must be positive")

        self._state.seen_source_fills.add(fill_key)
        filled = record.filled_ounces + fill_ounces
        status = "FILLED" if filled >= record.quantity_ounces else "PARTIALLY_FILLED"
        self._state.source_orders[client_order_id] = replace(
            record,
            filled_ounces=filled,
            status=status,
        )
        if (blocked_reason is None and status == "FILLED"
                and self._state.active_source_order_id == client_order_id):
            self._state.active_source_order_id = None
            if self._state.halt_reason == _source_reconcile_reason(client_order_id):
                self._state.halt_reason = None

        signed_fill = fill_ounces if source_side is BusinessOrderSide.BUY else -fill_ounces
        rounded_ounces = self._allocate_source_fill(record, fill_key, signed_fill)
        if rounded_ounces == 0:
            return None

        digest = sha256(fill_key.encode()).hexdigest()[:24]
        hedge_side = (
            BusinessOrderSide.SELL if rounded_ounces > 0 else BusinessOrderSide.BUY
        )
        hedge_ounces = Decimal(abs(rounded_ounces))
        intent = HedgeIntent(
            intent_id=f"hedge-{digest}",
            fill_key=fill_key,
            source_client_order_id=client_order_id,
            source_trade_id=trade_id,
            source_side=source_side,
            source_fill_ounces=fill_ounces,
            hedge_side=hedge_side,
            hedge_quantity_ounces=hedge_ounces,
            hedge_position_id=record.hedge_position_id,
            hedge_position_quantity_ounces=record.hedge_position_quantity_ounces,
            status=(ObligationStatus.BLOCKED if blocked_reason is not None
                    else ObligationStatus.PENDING),
        )
        self._state.hedge_intents[intent.intent_id] = intent
        return intent

    def _allocate_source_fill(
        self, record: SourceOrderRecord, fill_key: str, signed_fill: Decimal,
    ) -> int:
        self._state.net_unhedged_ounces += signed_fill
        rounded = round_hedge_ounces(self._state.net_unhedged_ounces)
        self._state.net_unhedged_ounces -= Decimal(rounded)
        return rounded

    def bind_hedge_order(self, intent_id: str, client_order_id: str) -> None:
        intent = self._state.hedge_intents[intent_id]
        if (
            intent.status is not ObligationStatus.PENDING
            or intent.hedge_client_order_id is not None
            or not client_order_id
            or any(
                client_order_id == existing.hedge_client_order_id
                or client_order_id in existing.hedge_order_ids
                for existing in self._state.hedge_intents.values()
            )
        ):
            raise ValueError("hedge order binding requires a pending unique leg")
        self._state.hedge_intents[intent_id] = replace(
            intent,
            hedge_client_order_id=client_order_id,
            hedge_order_ids=(*intent.hedge_order_ids, client_order_id),
            status=ObligationStatus.SUBMITTING,
        )
        self._persist()

    def bind_hedge_plan(self, intent_id: str, plan: tuple[HedgeLeg, ...]) -> HedgeIntent:
        """Durably bind one deterministic multi-ticket plan before its first order."""
        intent = self._state.hedge_intents[intent_id]
        if intent.hedge_plan:
            if intent.hedge_plan != plan:
                raise ValueError("hedge intent already has a different ticket plan")
            return intent
        if (
            intent.status is not ObligationStatus.PENDING
            or intent.hedge_client_order_id is not None
            or intent.hedge_filled_ounces != 0
        ):
            raise ValueError("hedge plan must be bound before the first hedge order")
        if not plan:
            raise ValueError("hedge plan must contain at least one leg")
        if any(leg.side is not intent.hedge_side for leg in plan):
            raise ValueError("hedge plan side does not match the durable intent")
        remaining = intent.hedge_quantity_ounces - intent.hedge_filled_ounces
        if sum((leg.quantity_ounces for leg in plan), Decimal(0)) != remaining:
            raise ValueError("hedge plan quantity does not match the durable intent")
        planned = replace(
            intent,
            hedge_plan=plan,
            hedge_leg_index=0,
            hedge_leg_filled_ounces=Decimal(0),
        )
        self._state.hedge_intents[intent_id] = planned
        self._persist()
        return planned

    def block_hedge_intent(self, intent_id: str, reason: str) -> None:
        """Persist a known planning block without inventing a venue outcome."""
        intent = self._state.hedge_intents[intent_id]
        if intent.status is ObligationStatus.COMPLETED:
            raise ValueError("completed hedge intent cannot be blocked")
        if not reason:
            raise ValueError("hedge block reason must not be empty")
        self._revoke_restart_permission()
        self._state.hedge_intents[intent_id] = replace(
            intent,
            status=ObligationStatus.BLOCKED,
        )
        self._state.halt_reason = f"hedge {intent_id} blocked: {reason}"
        self._persist()

    def update_hedge_status(
        self, client_order_id: str, status: ObligationStatus, *, native_status: str | None = None,
    ) -> None:
        intent = self._intent_for_hedge_order(client_order_id)
        if intent is None:
            return
        if _is_rejected_attempt(intent, client_order_id):
            if ((status is not ObligationStatus.REJECTED
                 or native_status not in {None, "REJECTED"})
                    and self._hold_rejected_attempt(intent, client_order_id, "terminal status")):
                self._persist()
            return
        if status in {
            ObligationStatus.BLOCKED, ObligationStatus.REJECTED, ObligationStatus.UNKNOWN,
        }:
            self._revoke_restart_permission()
        if intent.status in {
            ObligationStatus.BLOCKED,
            ObligationStatus.REJECTED,
            ObligationStatus.UNKNOWN,
        }:
            return
        self._state.hedge_intents[intent.intent_id] = replace(intent, status=status)
        if status in {ObligationStatus.REJECTED, ObligationStatus.UNKNOWN}:
            self._state.halt_reason = (
                f"hedge {client_order_id} has unresolved status {status.value}"
            )
        self._persist()

    def apply_hedge_fill(
        self,
        *,
        client_order_id: str,
        trade_id: str,
        fill_ounces: Decimal,
    ) -> bool:
        if (f"{client_order_id}|{trade_id}" in self._state.seen_hedge_fills
                or self._intent_for_hedge_order(client_order_id) is None):
            return False
        previous_state = deepcopy(self._state)
        try:
            changed = self._apply_hedge_fill(
                client_order_id=client_order_id, trade_id=trade_id, fill_ounces=fill_ounces,
            )
            if changed:
                self._persist()
        except ParentDirectorySyncError:
            raise  # The candidate was already published.
        except Exception:
            self._state = previous_state
            raise
        return changed

    def _apply_hedge_fill(
        self, *, client_order_id: str, trade_id: str, fill_ounces: Decimal,
        blocked_reason: str | None = None,
    ) -> bool:
        """Apply a fill without I/O; held projection never advances the current leg."""
        fill_key = f"{client_order_id}|{trade_id}"
        if fill_key in self._state.seen_hedge_fills:
            return False
        intent = self._intent_for_hedge_order(client_order_id)
        if intent is None:
            return False
        if _is_rejected_attempt(intent, client_order_id):
            return self._hold_rejected_attempt(intent, client_order_id, "late fill")
        if fill_ounces <= 0:
            raise ValueError("hedge fill quantity must be positive")
        self._state.seen_hedge_fills.add(fill_key)
        filled = intent.hedge_filled_ounces + fill_ounces
        if blocked_reason is not None or intent.status in {
            ObligationStatus.BLOCKED,
            ObligationStatus.REJECTED,
            ObligationStatus.UNKNOWN,
        }:
            leg_filled = intent.hedge_leg_filled_ounces
            if intent.hedge_plan and intent.hedge_leg_index < len(intent.hedge_plan):
                leg_filled += fill_ounces
            self._state.hedge_intents[intent.intent_id] = replace(
                intent,
                hedge_filled_ounces=filled,
                hedge_leg_filled_ounces=leg_filled,
                status=ObligationStatus.BLOCKED,
            )
            if blocked_reason is None:
                self._revoke_restart_permission()
                self._state.halt_reason = (
                    f"hedge {client_order_id} filled after unresolved status "
                    f"{intent.status.value}"
                )
            elif self._state.halt_reason is None:
                self._state.halt_reason = blocked_reason
            return True
        if intent.hedge_plan:
            if intent.hedge_leg_index >= len(intent.hedge_plan):
                self._revoke_restart_permission()
                self._state.hedge_intents[intent.intent_id] = replace(
                    intent,
                    hedge_filled_ounces=filled,
                    status=ObligationStatus.BLOCKED,
                )
                self._state.halt_reason = (
                    f"hedge {client_order_id} filled after its ticket plan completed"
                )
                return True
            leg = intent.hedge_plan[intent.hedge_leg_index]
            expected = leg.quantity_ounces - intent.hedge_leg_filled_ounces
            if fill_ounces != expected:
                self._revoke_restart_permission()
                self._state.hedge_intents[intent.intent_id] = replace(
                    intent,
                    hedge_filled_ounces=filled,
                    hedge_leg_filled_ounces=(
                        intent.hedge_leg_filled_ounces + fill_ounces
                    ),
                    status=ObligationStatus.BLOCKED,
                )
                self._state.halt_reason = (
                    f"hedge {client_order_id} fill quantity {fill_ounces} "
                    f"does not match planned leg remainder {expected}"
                )
                return True
            next_leg_index = intent.hedge_leg_index + 1
            completed = next_leg_index == len(intent.hedge_plan)
            self._state.hedge_intents[intent.intent_id] = replace(
                intent,
                hedge_filled_ounces=filled,
                status=(
                    ObligationStatus.COMPLETED
                    if completed
                    else ObligationStatus.PENDING
                ),
                # Every planned leg is submitted MARKET/FOK. An exact OrderFilled is
                # therefore that leg's terminal event; incomplete fills retain this
                # ID above and enter BLOCKED instead of advancing to another ticket.
                hedge_client_order_id=None,
                hedge_leg_index=next_leg_index,
                hedge_leg_filled_ounces=Decimal(0),
            )
            return True
        status = (
            ObligationStatus.COMPLETED
            if filled >= intent.hedge_quantity_ounces
            else ObligationStatus.ACCEPTED
        )
        self._state.hedge_intents[intent.intent_id] = replace(
            intent,
            hedge_filled_ounces=filled,
            status=status,
        )
        return True

    def intent(self, intent_id: str) -> HedgeIntent:
        return self._state.hedge_intents[intent_id]

    def intents(self) -> tuple[HedgeIntent, ...]:
        return tuple(self._state.hedge_intents.values())

    def _hold_rejected_attempt(self, intent: HedgeIntent, client_order_id: str, fact: str) -> bool:
        """Keep contradictory old evidence out of the current leg's quantity/seen set."""
        self._revoke_restart_permission()
        blocked = replace(intent, status=ObligationStatus.BLOCKED)
        reason = f"archived rejected hedge {client_order_id} has conflicting {fact}"
        changed = blocked != intent or self._state.halt_reason != reason
        self._state.hedge_intents[intent.intent_id] = blocked
        self._state.halt_reason = reason
        return changed

    def _intent_for_hedge_order(self, client_order_id: str) -> HedgeIntent | None:
        return next(
            (
                intent
                for intent in self._state.hedge_intents.values()
                if intent.hedge_client_order_id == client_order_id
                or _is_rejected_attempt(intent, client_order_id)
            ),
            None,
        )

    def _persist(self) -> None:
        _persist_payload(self.path, self._to_payload())

    def _persist_source_reservation(self, previous_state: StoreState) -> None:
        try:
            self._persist()
        except ParentDirectorySyncError:
            raise
        except Exception:
            self._state = previous_state
            raise

    def _to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "source_orders": {
                key: _decimal_strings(asdict(record))
                for key, record in self._state.source_orders.items()
            },
            "active_source_order_id": self._state.active_source_order_id,
            "seen_source_fills": sorted(self._state.seen_source_fills),
            "seen_hedge_fills": sorted(self._state.seen_hedge_fills),
            "hedge_intents": {
                key: _decimal_strings(asdict(intent))
                for key, intent in self._state.hedge_intents.items()
            },
            "net_unhedged_ounces": str(self._state.net_unhedged_ounces),
            "halt_reason": self._state.halt_reason,
            "source_freeze_reason": self._state.source_freeze_reason,
        }

    def _load(self) -> StoreState:
        raw = cast(dict[str, object], json.loads(self.path.read_text(encoding="utf-8")))
        return self._from_payload(raw)

    @staticmethod
    def _from_payload(raw: dict[str, object]) -> StoreState:
        version = raw.get("schema_version")
        if type(version) is not int or version not in {1, 2}:
            raise ValueError("unsupported state schema")
        source_raw = cast(dict[str, dict[str, object]], raw["source_orders"])
        intents_raw = cast(dict[str, dict[str, object]], raw["hedge_intents"])
        if any((version == 1 and value.get("rejected_attempt") is not None)
               or (version == 2 and "rejected_attempt" not in value)
               for value in intents_raw.values()):
            raise ValueError("rejected hedge attempt does not match state schema")
        source_orders = {
            key: SourceOrderRecord(
                client_order_id=str(value["client_order_id"]),
                side=BusinessOrderSide(str(value["side"])),
                quantity_ounces=Decimal(str(value["quantity_ounces"])),
                source_account_id=_optional_string(value.get("source_account_id")),
                source_client_id=_optional_string(value.get("source_client_id")),
                hedge_account_id=_optional_string(value.get("hedge_account_id")),
                hedge_client_id=_optional_string(value.get("hedge_client_id")),
                hedge_position_id=_optional_string(value.get("hedge_position_id")),
                hedge_position_quantity_ounces=(
                    Decimal(str(value["hedge_position_quantity_ounces"]))
                    if value.get("hedge_position_quantity_ounces") is not None
                    else None
                ),
                filled_ounces=Decimal(str(value["filled_ounces"])),
                status=str(value["status"]),
            )
            for key, value in source_raw.items()
        }
        hedge_intents = {
            key: HedgeIntent(
                intent_id=str(value["intent_id"]),
                fill_key=str(value["fill_key"]),
                source_client_order_id=str(value["source_client_order_id"]),
                source_trade_id=str(value["source_trade_id"]),
                source_side=BusinessOrderSide(str(value["source_side"])),
                source_fill_ounces=Decimal(str(value["source_fill_ounces"])),
                hedge_side=BusinessOrderSide(str(value["hedge_side"])),
                hedge_quantity_ounces=Decimal(str(value["hedge_quantity_ounces"])),
                hedge_position_id=_optional_string(value.get("hedge_position_id")),
                hedge_position_quantity_ounces=(
                    Decimal(str(value["hedge_position_quantity_ounces"]))
                    if value.get("hedge_position_quantity_ounces") is not None
                    else None
                ),
                status=ObligationStatus(str(value["status"])),
                hedge_client_order_id=_optional_string(value["hedge_client_order_id"]),
                hedge_filled_ounces=Decimal(str(value["hedge_filled_ounces"])),
                hedge_plan=tuple(
                    _hedge_leg_from_payload(item)
                    for item in cast(list[dict[str, object]], value.get("hedge_plan", []))
                ),
                hedge_leg_index=_exact_int(value.get("hedge_leg_index", 0)),
                hedge_leg_filled_ounces=Decimal(
                    str(value.get("hedge_leg_filled_ounces", 0))
                ),
                hedge_order_ids=tuple(
                    str(item) for item in cast(list[object], value.get("hedge_order_ids", []))
                ),
                rejected_attempt=_rejected_attempt_from_payload(value.get("rejected_attempt")),
            )
            for key, value in intents_raw.items()
        }
        state = StoreState(
            source_orders=source_orders,
            active_source_order_id=_optional_string(raw["active_source_order_id"]),
            seen_source_fills=set(cast(list[str], raw["seen_source_fills"])),
            seen_hedge_fills=set(cast(list[str], raw["seen_hedge_fills"])),
            hedge_intents=hedge_intents,
            net_unhedged_ounces=Decimal(str(raw["net_unhedged_ounces"])),
            halt_reason=_optional_string(raw["halt_reason"]),
            source_freeze_reason=_optional_string(raw.get("source_freeze_reason")),
        )
        if any(intent.rejected_attempt is not None and any(
            key.split("|")[0] == intent.rejected_attempt.client_order_id
            for key in state.seen_hedge_fills
        ) for intent in hedge_intents.values()):
            raise ValueError("archived rejected hedge attempt cannot have recorded fills")
        return state


def _persist_payload(
    path: Path, payload: dict[str, object], *, overwrite: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
        temporary_path = Path(handle.name)
    if overwrite:
        replace_and_sync_parent(temporary_path, path)
    else:
        create_and_sync_parent(temporary_path, path)
        try:
            temporary_path.unlink()
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Created state at %s; temporary file cleanup failed, retained %s: %s",
                path, temporary_path, exc,
            )


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _is_rejected_attempt(intent: HedgeIntent, client_order_id: str) -> bool:
    return (intent.rejected_attempt is not None
            and intent.rejected_attempt.client_order_id == client_order_id)


def _rejected_attempt_from_payload(value: object) -> RejectedHedgeAttempt | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"client_order_id", "leg_index"}
            or not isinstance(value["client_order_id"], str)):
        raise ValueError("invalid rejected hedge attempt record")
    return RejectedHedgeAttempt(value["client_order_id"], _exact_int(value["leg_index"]))


def _exact_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("persisted hedge plan index must be an exact int")
    return value


def _source_reconcile_reason(client_order_id: str) -> str:
    return f"source {client_order_id} terminal event requires fill reconciliation"


def _hedge_leg_from_payload(value: dict[str, object]) -> HedgeLeg:
    return HedgeLeg(
        side=BusinessOrderSide(str(value["side"])),
        quantity_ounces=Decimal(str(value["quantity_ounces"])),
        position_id=_optional_string(value.get("position_id")),
        expected_position_side=(
            BusinessOrderSide(str(value["expected_position_side"]))
            if value.get("expected_position_side") is not None
            else None
        ),
        expected_position_quantity_ounces=(
            Decimal(str(value["expected_position_quantity_ounces"]))
            if value.get("expected_position_quantity_ounces") is not None
            else None
        ),
    )


def _decimal_strings(value: dict[str, object]) -> dict[str, object]:
    return {key: _json_value(item) for key, item in value.items()}


def _json_value(value: object) -> object:
    if isinstance(value, Decimal | BusinessOrderSide | ObligationStatus):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value
