"""One Maker state file with two views of the existing order/hedge algorithms."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.models import BusinessOrderSide, HedgeIntent, SourceDirection
from py000_nautilus.store import JsonStateStore, StoreState, _persist_payload

_DIRECTIONS = {SourceDirection.LONG: "bid", SourceDirection.SHORT: "ask"}


def maker_state_path(prefix: str | Path) -> Path:
    return Path(f"{prefix}.maker.json")


def maker_legacy_paths(prefix: str | Path) -> tuple[Path, Path]:
    return Path(f"{prefix}.bid.json"), Path(f"{prefix}.ask.json")


class MakerStateStore:
    """Persist both direction views together; residual allocation remains unchanged."""

    def __init__(
        self, prefix: str | Path, source_instrument_id: str, hedge_instrument_id: str,
    ) -> None:
        if not source_instrument_id or not hedge_instrument_id:
            raise ValueError("Maker state requires both instrument bindings")
        self.path = maker_state_path(prefix)
        self.source_instrument_id = source_instrument_id
        self.hedge_instrument_id = hedge_instrument_id
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
        if len(reasons) != 1 or not all(
            view.cycle_evidence_complete() for view in self.stores.values()
        ):
            return False
        for view in self.stores.values():
            view._state.source_freeze_reason = None
        self._persist()
        return True

    def _set_freezes(self, reason: str) -> bool:
        changed = False
        for view in self.stores.values():
            if view.source_freeze_reason is None:
                view._state.source_freeze_reason = reason
                changed = True
        return changed

    def _snapshot(self) -> dict[SourceDirection, StoreState]:
        return {direction: deepcopy(view._state) for direction, view in self.stores.items()}

    def _restore(self, states: dict[SourceDirection, StoreState]) -> None:
        for direction, state in states.items():
            self.stores[direction]._state = deepcopy(state)

    def _persist(self) -> None:
        candidate = self._snapshot()
        try:
            self._validate()
            _persist_payload(self.path, {
                "schema_version": 2,
                "kind": "maker",
                "source_instrument_id": self.source_instrument_id,
                "hedge_instrument_id": self.hedge_instrument_id,
                "directions": {
                    key: self.stores[direction]._to_payload()
                    for direction, key in _DIRECTIONS.items()
                },
            })
        except ParentDirectorySyncError:
            self._committed = candidate
            raise
        except Exception:
            self._restore(self._committed)
            raise
        self._committed = candidate

    def _load(self) -> dict[SourceDirection, StoreState]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "kind", "source_instrument_id", "hedge_instrument_id", "directions",
        } or type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
            raise ValueError("unsupported Maker state schema")
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
            if not state.net_unhedged_ounces.is_finite():
                raise ValueError("Maker residual must be finite")
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
                if intent.source_side is not side or intent.hedge_side is side:
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


class _MakerDirectionStore(JsonStateStore):
    def __init__(self, owner: MakerStateStore, state: StoreState) -> None:
        self._owner = owner
        self.path = owner.path
        self._state = state

    def _persist(self) -> None:
        self._owner._persist()

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
