"""One Maker state file with two views of the existing order/hedge algorithms."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.economics import round_hedge_ounces
from py000_nautilus.models import BusinessOrderSide, HedgeIntent, ObligationStatus, SourceDirection
from py000_nautilus.store import JsonStateStore, SourceOrderRecord, StoreState, _persist_payload

_DIRECTIONS = {SourceDirection.LONG: "bid", SourceDirection.SHORT: "ask"}
_ROUTE_FIELDS = (
    "source_account_id", "source_client_id", "hedge_account_id", "hedge_client_id",
    "isolated_source_order_id",
)
_Route = tuple[str | None, str | None, str | None, str | None, str | None]


@dataclass(frozen=True, slots=True)
class _Allocation:
    fill_key: str
    route: _Route
    signed_fill_ounces: Decimal
    allocated_ounces: Decimal

    def payload(self) -> dict[str, object]:
        return {
            "fill_key": self.fill_key, "route": dict(zip(_ROUTE_FIELDS, self.route, strict=True)),
            "signed_fill_ounces": str(self.signed_fill_ounces),
            "allocated_ounces": str(self.allocated_ounces),
        }


@dataclass(frozen=True, slots=True)
class _LegacyOrder:
    client_order_id: str
    direction: SourceDirection
    route: _Route
    filled_ounces: Decimal
    fill_keys: tuple[str, ...]
    intent_ids: tuple[str, ...]
    allocated_ounces: Decimal

    def payload(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id, "direction": self.direction.value,
            "route": dict(zip(_ROUTE_FIELDS, self.route, strict=True)),
            "filled_ounces": str(self.filled_ounces), "fill_keys": list(self.fill_keys),
            "intent_ids": list(self.intent_ids), "allocated_ounces": str(self.allocated_ounces),
        }


_Snapshot = tuple[dict[SourceDirection, StoreState], list[_Allocation]]


def _route(record: SourceOrderRecord) -> _Route:
    isolated = (record.client_order_id if not record.source_account_id
                or not record.hedge_account_id or record.hedge_position_id is not None else None)
    return (record.source_account_id, record.source_client_id, record.hedge_account_id,
            record.hedge_client_id, isolated)


def maker_state_path(prefix: str | Path) -> Path:
    return Path(f"{prefix}.maker.json")


def maker_legacy_paths(prefix: str | Path) -> tuple[Path, Path]:
    return Path(f"{prefix}.bid.json"), Path(f"{prefix}.ask.json")


class MakerStateStore:
    """Allocate each actual fill against its route's unallocated strict residual."""

    def __init__(
        self, prefix: str | Path, source_instrument_id: str, hedge_instrument_id: str,
        *, residual_limit_ounces: Decimal = Decimal(0),
        carry_route: tuple[str, str | None, str, str | None] | None = None,
    ) -> None:
        if not source_instrument_id or not hedge_instrument_id:
            raise ValueError("Maker state requires both instrument bindings")
        self.path = maker_state_path(prefix)
        self.source_instrument_id = source_instrument_id
        self.hedge_instrument_id = hedge_instrument_id
        if (not residual_limit_ounces.is_finite()
                or not 0 <= residual_limit_ounces <= Decimal("0.5")):
            raise ValueError("Maker residual limit must be finite and between zero and 0.5")
        if residual_limit_ounces > 0 and (carry_route is None or len(carry_route) != 4
                                        or not carry_route[0] or not carry_route[2]):
            raise ValueError("bounded Maker state requires an explicit account route")
        self._residual_limit = residual_limit_ounces
        self._carry_route: _Route | None = (*carry_route, None) if carry_route is not None else None
        self._allocations: list[_Allocation] = []
        self._legacy_sources: tuple[tuple[str, str], ...] | None = None
        self._legacy_orders: tuple[_LegacyOrder, ...] = ()
        if not self.path.exists() and any(path.exists() for path in maker_legacy_paths(prefix)):
            raise ValueError("legacy Maker state requires explicit migration before startup")
        states = self._load() if self.path.exists() else {
            direction: JsonStateStore._empty_state() for direction in _DIRECTIONS
        }
        self.stores: dict[SourceDirection, JsonStateStore] = {
            direction: _MakerDirectionStore(self, state) for direction, state in states.items()
        }
        self._validate()
        self._committed = self._snapshot()

    def freeze_sources(self, reason: str) -> None:
        if not reason:
            raise ValueError("freeze reason must not be empty")
        if self._set_freezes(reason):
            self._persist()

    def clear_source_freezes(self) -> bool:
        reasons = {view.source_freeze_reason for view in self.stores.values()
                   if view.source_freeze_reason is not None}
        if not self.source_balance_is_admissible() or len(reasons) != 1 or not all(
            view.cycle_evidence_complete() for view in self.stores.values()
        ):
            return False
        for view in self.stores.values():
            view._state.source_freeze_reason = None
        self._persist()
        return True

    def has_residuals(self) -> bool:
        return any(balance != 0 for balance, _direction in self._route_balances().values())

    @property
    def carry_residual_ounces(self) -> Decimal:
        if self._carry_route is None:
            return Decimal(0)
        return self._route_balances().get(self._carry_route, (Decimal(0), SourceDirection.LONG))[0]

    def residuals(self) -> dict[_Route, Decimal]:
        return {route: balance for route, (balance, _) in self._route_balances().items() if balance}

    def source_balance_is_admissible(self) -> bool:
        return all(
            route == self._carry_route and abs(balance) <= self._residual_limit
            for route, balance in self.residuals().items()
        )

    def next_pending_hedge(self) -> tuple[SourceDirection, HedgeIntent] | None:
        """Select the first unfinished allocation; never infer legacy execution order."""
        intents = {intent.fill_key: (direction, intent)
                   for direction, view in self.stores.items() for intent in view.intents()}
        legacy_ids = {intent_id for item in self._legacy_orders for intent_id in item.intent_ids}
        if any(intent.intent_id in legacy_ids and intent.status is not ObligationStatus.COMPLETED
               for _, intent in intents.values()):
            return None
        for allocation in self._allocations:
            selected = intents.get(allocation.fill_key)
            if selected is None or selected[1].status is ObligationStatus.COMPLETED:
                continue
            intent = selected[1]
            if intent.status is ObligationStatus.PENDING and intent.hedge_client_order_id is None:
                return selected
            return None
        return None

    def _validate_source_projection_tail(self, cid: str, prefix: tuple[str, ...]) -> None:
        if any(item.client_order_id == cid for item in self._legacy_orders):
            raise ValueError("source projection cannot append to a legacy Maker checkpoint order")
        if not prefix:
            if self._allocations:
                raise ValueError("source projection Maker suffix has no allocation anchor")
            return
        index = next((index for index, item in enumerate(self._allocations)
                      if item.fill_key == prefix[-1]), None)
        if index is None or any(item.fill_key.split("|")[0] != cid
                                for item in self._allocations[index + 1:]):
            raise ValueError("source projection Maker suffix crosses another source allocation")

    def _route_balances(self) -> dict[_Route, tuple[Decimal, SourceDirection]]:
        directions = {key: direction for direction, view in self.stores.items()
                      for key in view._state.seen_source_fills}
        balances = self._checkpoint_balances()
        for item in self._allocations:
            previous = balances.get(item.route, (Decimal(0), directions[item.fill_key]))[0]
            balances[item.route] = (
                previous + item.signed_fill_ounces - item.allocated_ounces,
                directions[item.fill_key],
            )
        return balances

    def _checkpoint_balances(self) -> dict[_Route, tuple[Decimal, SourceDirection]]:
        balances: dict[_Route, tuple[Decimal, SourceDirection]] = {}
        # This is a fixed historical display projection, not reconstructed fill order.
        for item in sorted(self._legacy_orders, key=lambda item: (
            item.direction is SourceDirection.SHORT, item.client_order_id,
        )):
            if item.filled_ounces == item.allocated_ounces == 0:
                continue
            previous = balances.get(item.route, (Decimal(0), item.direction))[0]
            signed = item.filled_ounces * (1 if item.direction is SourceDirection.LONG else -1)
            balances[item.route] = (previous + signed - item.allocated_ounces, item.direction)
        return balances

    def _allocate(self, record: SourceOrderRecord, fill_key: str, signed_fill: Decimal) -> int:
        route = _route(record)
        current = self._route_balances().get(route)
        combined = (current[0] if current is not None else Decimal(0)) + signed_fill
        allocated = round_hedge_ounces(combined)
        self._allocations.append(_Allocation(fill_key, route, signed_fill, Decimal(allocated)))
        return allocated

    def _set_freezes(self, reason: str) -> bool:
        changed = False
        for view in self.stores.values():
            if view.source_freeze_reason is None:
                view._state.source_freeze_reason = reason
                changed = True
        return changed

    def _snapshot(self) -> _Snapshot:
        return ({direction: deepcopy(view._state) for direction, view in self.stores.items()},
                self._allocations.copy())

    def _restore(self, snapshot: _Snapshot) -> None:
        for direction, state in snapshot[0].items():
            self.stores[direction]._state = deepcopy(state)
        self._allocations = snapshot[1].copy()

    def _persist(self) -> None:
        candidate = self._snapshot()
        try:
            self._validate()
            _persist_payload(self.path, self._to_payload())
        except ParentDirectorySyncError:
            self._committed = candidate
            raise
        except Exception:
            self._restore(self._committed)
            raise
        self._committed = candidate

    def _to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 3 if self._legacy_sources is None else 4,
            "kind": "maker", "source_instrument_id": self.source_instrument_id,
            "hedge_instrument_id": self.hedge_instrument_id,
            "allocations": [item.payload() for item in self._allocations],
            "directions": {key: self.stores[direction]._to_payload()
                           for direction, key in _DIRECTIONS.items()},
        }
        if self._legacy_sources is not None:
            payload["legacy_checkpoint"] = {
                "projection": "bid_then_ask",
                "sources": [{"path": path, "sha256": digest}
                            for path, digest in self._legacy_sources],
                "orders": [item.payload() for item in self._legacy_orders],
            }
        return payload

    def _load(self) -> dict[SourceDirection, StoreState]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        fields = {
            "schema_version", "kind", "source_instrument_id", "hedge_instrument_id", "directions",
            "allocations",
        }
        if isinstance(raw, dict) and raw.get("schema_version") == 4:
            fields.add("legacy_checkpoint")
        if (not isinstance(raw, dict) or set(raw) != fields
                or type(raw["schema_version"]) is not int or raw["schema_version"] not in {3, 4}):
            raise ValueError("unsupported Maker state schema; legacy state requires migration")
        if (
            raw["kind"] != "maker"
            or raw["source_instrument_id"] != self.source_instrument_id
            or raw["hedge_instrument_id"] != self.hedge_instrument_id
        ):
            raise ValueError("Maker state kind or instrument binding differs")
        directions = raw["directions"]
        if not isinstance(directions, dict) or set(directions) != {"bid", "ask"}:
            raise ValueError("Maker state requires exactly both directions")
        if any(not isinstance(state, dict) or type(state.get("schema_version")) is not int
               for state in directions.values()):
            raise ValueError("invalid Maker direction schema")
        try:
            if raw["schema_version"] == 4:
                self._legacy_sources, self._legacy_orders = _read_checkpoint(
                    raw["legacy_checkpoint"],
                )
            self._allocations = _read_allocations(raw["allocations"])
            return {
                direction: JsonStateStore._from_payload(directions[key])
                for direction, key in _DIRECTIONS.items()
            }
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("invalid Maker direction state") from exc

    def _validate(self) -> None:
        source_ids: set[str] = set()
        fill_keys: set[str] = set()
        intent_ids: set[str] = set()
        hedge_ids: set[str] = set()
        hedge_fill_keys: set[str] = set()
        source_trades: set[tuple[str | None, str]] = set()
        hedge_trades: set[tuple[str | None, str]] = set()
        for direction, view in self.stores.items():
            state = view._state
            side = (BusinessOrderSide.BUY if direction is SourceDirection.LONG
                    else BusinessOrderSide.SELL)
            if state.active_source_order_id is not None and (
                state.active_source_order_id not in state.source_orders
            ):
                raise ValueError("Maker active source identity is missing")
            if not state.net_unhedged_ounces.is_finite() or state.net_unhedged_ounces != 0:
                raise ValueError("Maker view residual must be zero; allocations own the balance")
            for key, record in state.source_orders.items():
                if key != record.client_order_id or not key or key in source_ids:
                    raise ValueError("conflicting Maker source identity")
                if record.side is not side:
                    raise ValueError("Maker source direction differs from its view")
                source_ids.add(key)
            if fill_keys & state.seen_source_fills or hedge_fill_keys & state.seen_hedge_fills:
                raise ValueError("conflicting Maker fill identity")
            fill_keys.update(state.seen_source_fills)
            hedge_fill_keys.update(state.seen_hedge_fills)
            for fill_key in state.seen_source_fills:
                parts = fill_key.split("|")
                if len(parts) != 3 or parts[0] not in state.source_orders or not all(parts):
                    raise ValueError("Maker source fill identity is invalid")
                source_account = state.source_orders[parts[0]].source_account_id
                if (source_account, parts[2]) in source_trades:
                    raise ValueError("conflicting Maker source fill identity")
                source_trades.add((source_account, parts[2]))
            hedge_accounts: dict[str, str | None] = {}
            for key, intent in state.hedge_intents.items():
                if key != intent.intent_id or key in intent_ids:
                    raise ValueError("conflicting Maker intent identity")
                if intent.source_side is not side:
                    raise ValueError("Maker intent direction differs from its view")
                if (intent.source_client_order_id not in state.source_orders
                        or intent.fill_key not in state.seen_source_fills):
                    raise ValueError("Maker intent source fill identity is missing")
                parts = intent.fill_key.split("|")
                if parts[0] != intent.source_client_order_id or parts[2] != intent.source_trade_id:
                    raise ValueError("Maker intent source fill identity differs")
                intent_ids.add(key)
                ids = set(intent.hedge_order_ids)
                if intent.hedge_client_order_id is not None:
                    ids.add(intent.hedge_client_order_id)
                if hedge_ids & ids:
                    raise ValueError("conflicting Maker hedge order identity")
                hedge_ids.update(ids)
                hedge_account = state.source_orders[intent.source_client_order_id].hedge_account_id
                hedge_accounts.update((order_id, hedge_account) for order_id in ids)
            for fill_key in state.seen_hedge_fills:
                parts = fill_key.split("|")
                if len(parts) != 2 or parts[0] not in hedge_accounts or not all(parts):
                    raise ValueError("Maker hedge fill identity is invalid")
                hedge_account = hedge_accounts[parts[0]]
                if (hedge_account, parts[1]) in hedge_trades:
                    raise ValueError("conflicting Maker hedge fill identity")
                hedge_trades.add((hedge_account, parts[1]))
        if source_ids & hedge_ids:
            raise ValueError("conflicting Maker source/hedge order identity")
        self._validate_allocations()

    def _validate_allocations(self) -> None:
        records = {key: record for view in self.stores.values()
                   for key, record in view._state.source_orders.items()}
        seen = set().union(*(view._state.seen_source_fills for view in self.stores.values()))
        intents = {intent.fill_key: intent for view in self.stores.values()
                   for intent in view.intents()}
        if len(intents) != sum(len(view.intents()) for view in self.stores.values()):
            raise ValueError("multiple Maker intents claim the same allocation")
        allocated_fills, source_totals = self._validate_checkpoint(records, intents)
        balances = {route: balance for route, (balance, _) in self._checkpoint_balances().items()}
        for item in self._allocations:
            if item.fill_key not in seen or item.fill_key in allocated_fills:
                raise ValueError("Maker allocation fill identity is missing or duplicated")
            allocated_fills.add(item.fill_key)
            client_order_id = item.fill_key.split("|")[0]
            record = records[client_order_id]
            signed = item.signed_fill_ounces
            if (not signed.is_finite() or signed == 0 or not item.allocated_ounces.is_finite()
                    or (signed > 0) != (record.side is BusinessOrderSide.BUY)
                    or item.route != _route(record)):
                raise ValueError("Maker allocation direction, quantity or route differs")
            combined = balances.get(item.route, Decimal(0)) + signed
            if item.allocated_ounces != round_hedge_ounces(combined):
                raise ValueError("Maker allocation does not match ordered route residual")
            balances[item.route] = combined - item.allocated_ounces
            source_totals[client_order_id] = (
                source_totals.get(client_order_id, Decimal(0)) + abs(signed)
            )
            intent = intents.pop(item.fill_key, None)
            if item.allocated_ounces == 0:
                if intent is not None:
                    raise ValueError("unallocated Maker fill cannot own a hedge intent")
            elif (
                intent is None or intent.hedge_quantity_ounces != abs(item.allocated_ounces)
                or intent.source_fill_ounces != abs(signed)
                or (intent.hedge_side is BusinessOrderSide.SELL) != (item.allocated_ounces > 0)
            ):
                raise ValueError("Maker allocated quantity does not match its hedge intent")
        if allocated_fills != seen or intents or any(
            source_totals.get(key, Decimal(0)) != record.filled_ounces
            for key, record in records.items()
        ):
            raise ValueError("Maker allocation history differs from source/intent facts")

    def _validate_checkpoint(
        self, records: dict[str, SourceOrderRecord], intents: dict[str, HedgeIntent],
    ) -> tuple[set[str], dict[str, Decimal]]:
        keys: set[str] = set()
        totals: dict[str, Decimal] = {}
        directions = {cid: direction for direction, view in self.stores.items()
                      for cid in view._state.source_orders}
        by_id = {intent.intent_id: intent for intent in intents.values()}
        for item in self._legacy_orders:
            cid = item.client_order_id
            if (cid not in records or cid in totals or item.direction is not directions[cid]
                    or item.route != _route(records[cid]) or not item.filled_ounces.is_finite()
                    or item.filled_ounces < 0 or not item.allocated_ounces.is_finite()):
                raise ValueError("legacy checkpoint source binding or quantity differs")
            if (len(set(item.fill_keys)) != len(item.fill_keys)
                    or keys.intersection(item.fill_keys)
                    or any(key.split("|")[0] != cid for key in item.fill_keys)
                    or bool(item.fill_keys) != (item.filled_ounces > 0)
                    or len(set(item.intent_ids)) != len(item.intent_ids)):
                raise ValueError("legacy checkpoint fill identities differ")
            known = Decimal(0)
            allocated = Decimal(0)
            for intent_id in item.intent_ids:
                intent = by_id.get(intent_id)
                if (intent is None or intent.source_client_order_id != cid
                        or intent.fill_key not in item.fill_keys
                        or not intent.source_fill_ounces.is_finite()
                        or intent.source_fill_ounces <= 0
                        or intents.pop(intent.fill_key, None) is None):
                    raise ValueError("legacy checkpoint intent identity differs")
                known += intent.source_fill_ounces
                allocated += intent.hedge_quantity_ounces * (
                    1 if intent.hedge_side is BusinessOrderSide.SELL else -1
                )
            unknown = len(item.fill_keys) - len(item.intent_ids)
            if (allocated != item.allocated_ounces or known > item.filled_ounces
                    or (unknown == 0 and known != item.filled_ounces)
                    or (unknown > 0 and known >= item.filled_ounces)):
                raise ValueError("legacy checkpoint cumulative or fixed allocation differs")
            keys.update(item.fill_keys)
            totals[cid] = item.filled_ounces
        return keys, totals


class _MakerDirectionStore(JsonStateStore):
    def __init__(self, owner: MakerStateStore, state: StoreState) -> None:
        self._owner = owner
        self.path = owner.path
        self._state = state

    def _persist(self) -> None:
        self._owner._persist()

    @property
    def rounding_residual_ounces(self) -> Decimal:
        direction = next(key for key, view in self._owner.stores.items() if view is self)
        return sum((balance for balance, last_direction in self._owner._route_balances().values()
                    if last_direction is direction), Decimal(0))

    def _source_balance_is_admissible(self) -> bool:
        return (
            self._owner.source_balance_is_admissible()
            and self.net_unhedged_ounces == self.rounding_residual_ounces
            and all(intent.hedge_quantity_ounces == intent.hedge_filled_ounces
                    for intent in self.intents())
        )

    def _allocate_source_fill(
        self, record: SourceOrderRecord, fill_key: str, signed_fill: Decimal,
    ) -> int:
        return self._owner._allocate(record, fill_key, signed_fill)

    def reserve_source_fill(
        self, *, fill_key: str, client_order_id: str, trade_id: str,
        source_side: BusinessOrderSide, fill_ounces: Decimal,
    ) -> HedgeIntent | None:
        if self.has_seen_source_fill(fill_key) or not self.knows_source_order(client_order_id):
            return None
        previous = self._owner._snapshot()
        self._owner._set_freezes(
            f"Maker fill {client_order_id}/{trade_id} "
            "requires authoritative two-sided reconciliation",
        )
        try:
            return super().reserve_source_fill(
                fill_key=fill_key, client_order_id=client_order_id, trade_id=trade_id,
                source_side=source_side, fill_ounces=fill_ounces,
            )
        except ParentDirectorySyncError:
            raise
        except Exception:
            self._owner._restore(previous)
            raise

    def clear_source_freeze(self) -> None:
        if not self._owner.clear_source_freezes() and any(
            view.source_freeze_reason is not None for view in self._owner.stores.values()
        ):
            raise RuntimeError("Maker cycle evidence is incomplete")


def _read_allocations(raw: object) -> list[_Allocation]:
    if not isinstance(raw, list):
        raise ValueError("Maker allocations must be an ordered list")
    result: list[_Allocation] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "fill_key", "route", "signed_fill_ounces", "allocated_ounces",
        } or not isinstance(item["fill_key"], str):
            raise ValueError("invalid Maker allocation record")
        route = item["route"]
        if not isinstance(route, dict) or set(route) != set(_ROUTE_FIELDS) or any(
            value is not None and not isinstance(value, str) for value in route.values()
        ):
            raise ValueError("invalid Maker allocation route snapshot")
        try:
            signed = Decimal(item["signed_fill_ounces"])
            allocated = Decimal(item["allocated_ounces"])
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValueError("invalid Maker allocation quantity") from exc
        result.append(_Allocation(item["fill_key"], cast(_Route, tuple(
            route[key] for key in _ROUTE_FIELDS
        )), signed, allocated))
    return result


def _read_checkpoint(raw: object) -> tuple[tuple[tuple[str, str], ...], tuple[_LegacyOrder, ...]]:
    if not isinstance(raw, dict) or set(raw) != {"projection", "sources", "orders"} or (
        raw["projection"] != "bid_then_ask" or not isinstance(raw["sources"], list)
        or len(raw["sources"]) not in {1, 2} or not isinstance(raw["orders"], list)
    ):
        raise ValueError("invalid legacy checkpoint")
    sources: list[tuple[str, str]] = []
    for source in raw["sources"]:
        if (not isinstance(source, dict) or set(source) != {"path", "sha256"}
                or not isinstance(source["path"], str) or not source["path"]
                or not isinstance(source["sha256"], str) or len(source["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in source["sha256"])):
            raise ValueError("invalid legacy checkpoint source metadata")
        sources.append((source["path"], source["sha256"]))
    if len({path for path, _ in sources}) != len(sources):
        raise ValueError("duplicate legacy checkpoint source")
    orders: list[_LegacyOrder] = []
    for item in raw["orders"]:
        if not isinstance(item, dict) or set(item) != {
            "client_order_id", "direction", "route", "filled_ounces", "fill_keys", "intent_ids",
            "allocated_ounces",
        } or not isinstance(item["client_order_id"], str):
            raise ValueError("invalid legacy checkpoint order")
        route = item["route"]
        if not isinstance(route, dict) or set(route) != set(_ROUTE_FIELDS) or any(
            value is not None and not isinstance(value, str) for value in route.values()
        ) or any(not isinstance(item[key], list) or any(not isinstance(value, str)
                   for value in item[key]) for key in ("fill_keys", "intent_ids")):
            raise ValueError("invalid legacy checkpoint route or identities")
        try:
            orders.append(_LegacyOrder(
                item["client_order_id"], SourceDirection(item["direction"]),
                cast(_Route, tuple(route[key] for key in _ROUTE_FIELDS)),
                Decimal(item["filled_ounces"]), tuple(item["fill_keys"]), tuple(item["intent_ids"]),
                Decimal(item["allocated_ounces"]),
            ))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValueError("invalid legacy checkpoint quantity or direction") from exc
    return tuple(sources), tuple(orders)
