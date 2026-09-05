"""Maker one-file custody; no residual netting or legacy conversion is implied."""

import json
import os
from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import py000_nautilus.store as store_module
from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent
from py000_nautilus.maker_store import (
    MakerStateStore,
    maker_legacy_paths,
    maker_state_path,
)
from py000_nautilus.models import BusinessOrderSide, HedgeLeg, ObligationStatus, SourceDirection
from py000_nautilus.store import JsonStateStore

D = Decimal
BUY = BusinessOrderSide.BUY
SELL = BusinessOrderSide.SELL
LONG = SourceDirection.LONG
SHORT = SourceDirection.SHORT


def _owner(prefix: Path) -> MakerStateStore:
    return MakerStateStore(prefix, "SOURCE.BITFINEX", "HEDGE.MT5")


def _begin(owner: MakerStateStore, direction: SourceDirection, quantity: str = "2") -> None:
    owner.stores[direction].begin_source(
        f"S-{direction.value}", BUY if direction is LONG else SELL, D(quantity),
        source_account_id="BITFINEX-001", source_client_id="BITFINEX",
        hedge_account_id="MT5-001", hedge_client_id="MT5",
    )


def _fill(owner: MakerStateStore, direction: SourceDirection, quantity: str = "2") -> Any:
    return owner.stores[direction].reserve_source_fill(
        fill_key=f"S-{direction.value}|V-{direction.value}|T-{direction.value}",
        client_order_id=f"S-{direction.value}", trade_id=f"T-{direction.value}",
        source_side=BUY if direction is LONG else SELL, fill_ounces=D(quantity),
    )


def _memory(owner: MakerStateStore) -> dict[SourceDirection, object]:
    return {direction: deepcopy(view._state) for direction, view in owner.stores.items()}


def test_paths_and_new_empty_owner_do_not_create_or_read_legacy_files(tmp_path: Path) -> None:
    prefix = tmp_path / "maker.state"
    owner = _owner(prefix)
    assert owner.path == maker_state_path(prefix) == Path(f"{prefix}.maker.json")
    assert maker_legacy_paths(prefix) == (Path(f"{prefix}.bid.json"), Path(f"{prefix}.ask.json"))
    assert not owner.path.exists()
    assert set(owner.stores) == {LONG, SHORT}
    assert all(isinstance(view, JsonStateStore) for view in owner.stores.values())
    assert all(view.path == owner.path and view.can_submit_source()
               for view in owner.stores.values())


@pytest.mark.parametrize("existing_freeze", [None, "independent account HOLD"])
def test_fill_and_both_freezes_are_in_first_and_only_durable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_freeze: str | None,
) -> None:
    owner = _owner(tmp_path / "atomic-fill")
    _begin(owner, LONG)
    _begin(owner, SHORT)
    if existing_freeze is not None:
        owner.stores[SHORT].freeze_source_submissions(existing_freeze)
    snapshots: list[dict[str, Any]] = []
    original_replace = replace_and_sync_parent

    def observe(source: Path, destination: Path) -> None:
        snapshots.append(json.loads(source.read_text()))
        original_replace(source, destination)

    monkeypatch.setattr(store_module, "replace_and_sync_parent", observe)
    intent = _fill(owner, LONG)
    assert intent is not None and len(snapshots) == 1
    payload = snapshots[0]
    assert payload["schema_version"] == 2 and payload["kind"] == "maker"
    assert payload["source_instrument_id"] == "SOURCE.BITFINEX"
    assert payload["hedge_instrument_id"] == "HEDGE.MT5"
    bid, ask = payload["directions"]["bid"], payload["directions"]["ask"]
    assert bid["seen_source_fills"] == ["S-bid|V-bid|T-bid"]
    assert len(bid["hedge_intents"]) == 1
    reason = "Maker fill S-bid/T-bid requires authoritative two-sided reconciliation"
    assert bid["source_freeze_reason"] == reason
    assert ask["source_freeze_reason"] == (existing_freeze or reason)
    assert _memory(_owner(tmp_path / "atomic-fill")) == _memory(owner)
    assert not any(path.exists() for path in maker_legacy_paths(tmp_path / "atomic-fill"))


def test_round_trip_dedup_and_ticket_plan_reuse_existing_algorithms(tmp_path: Path) -> None:
    prefix = tmp_path / "round-trip"
    owner = _owner(prefix)
    _begin(owner, LONG)
    intent = _fill(owner, LONG)
    assert intent is not None
    plan = (
        HedgeLeg(SELL, D(1), "ticket-1", BUY, D(1)),
        HedgeLeg(SELL, D(1)),
    )
    view = owner.stores[LONG]
    view.bind_hedge_plan(intent.intent_id, plan)
    view.bind_hedge_order(intent.intent_id, "H-CLOSE")
    assert view.apply_hedge_fill(client_order_id="H-CLOSE", trade_id="H-T1", fill_ounces=D(1))
    reloaded = _owner(prefix)
    view = reloaded.stores[LONG]
    assert view.intent(intent.intent_id).hedge_plan == plan
    assert view.intent(intent.intent_id).hedge_leg_index == 1
    assert _fill(reloaded, LONG) is None
    assert not view.apply_hedge_fill(
        client_order_id="H-CLOSE", trade_id="H-T1", fill_ounces=D(1),
    )
    view.bind_hedge_order(intent.intent_id, "H-OPEN")
    assert view.apply_hedge_fill(client_order_id="H-OPEN", trade_id="H-T2", fill_ounces=D(1))
    assert view.intent(intent.intent_id).status is ObligationStatus.COMPLETED
    assert reloaded.clear_source_freezes() is True
    before = reloaded.path.read_bytes()
    assert _fill(reloaded, LONG) is None
    assert reloaded.path.read_bytes() == before
    assert all(item.source_freeze_reason is None for item in reloaded.stores.values())
    assert _memory(_owner(prefix)) == _memory(reloaded)
    with pytest.raises(ValueError, match="schema"):
        JsonStateStore(reloaded.path)


def test_unknown_fill_does_not_freeze_or_create_file(tmp_path: Path) -> None:
    owner = _owner(tmp_path / "unknown")
    assert _fill(owner, LONG) is None
    assert not owner.path.exists()
    assert all(view.source_freeze_reason is None for view in owner.stores.values())


@pytest.mark.parametrize("operation", ["fill", "fill-dust", "freeze", "clear", "view"])
@pytest.mark.parametrize("after_replace", [False, True])
def test_whole_candidate_rollback_or_post_replace_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, after_replace: bool,
) -> None:
    prefix = tmp_path / "failure"
    owner = _owner(prefix)
    owner.freeze_sources("initial")
    assert owner.clear_source_freezes()
    if operation.startswith("fill"):
        _begin(owner, LONG)
    elif operation == "clear":
        owner.freeze_sources("release")
    previous = _memory(owner)
    disk_before = owner.path.read_bytes()
    candidates: list[bytes] = []

    def fail(source: Path, destination: Path) -> None:
        candidates.append(source.read_bytes())
        if after_replace:
            os.replace(source, destination)
            raise ParentDirectorySyncError("injected post-replace failure")
        raise OSError("injected pre-replace failure")

    monkeypatch.setattr(store_module, "replace_and_sync_parent", fail)
    actions: dict[str, Callable[[], object]] = {
        "fill": lambda: _fill(owner, LONG),
        "fill-dust": lambda: _fill(owner, LONG, "0.4"),
        "freeze": lambda: owner.freeze_sources("new freeze"),
        "clear": owner.clear_source_freezes,
        "view": lambda: _begin(owner, LONG),
    }
    with pytest.raises(ParentDirectorySyncError if after_replace else OSError):
        actions[operation]()
    assert len(candidates) == 1
    assert owner.path.read_bytes() == (candidates[0] if after_replace else disk_before)
    assert _memory(owner) == _memory(_owner(prefix))
    assert (_memory(owner) != previous) is after_replace
    if after_replace and operation.startswith("fill"):
        assert all(view.source_freeze_reason is not None for view in owner.stores.values())
        assert len(owner.stores[LONG].intents()) == (1 if operation == "fill" else 0)


def test_atomic_release_requires_all_evidence_and_unique_existing_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _owner(tmp_path / "release")
    _begin(owner, LONG, "1")
    owner.freeze_sources("cycle")
    assert not owner.clear_source_freezes()
    with pytest.raises(RuntimeError, match="incomplete"):
        owner.stores[SHORT].clear_source_freeze()
    owner.stores[LONG].update_source_status("S-bid", "REJECTED")
    snapshots: list[dict[str, Any]] = []
    original_replace = replace_and_sync_parent

    def observe(source: Path, destination: Path) -> None:
        snapshots.append(json.loads(source.read_text()))
        original_replace(source, destination)

    monkeypatch.setattr(store_module, "replace_and_sync_parent", observe)
    assert owner.clear_source_freezes()
    assert len(snapshots) == 1
    assert all(state["source_freeze_reason"] is None
               for state in snapshots[0]["directions"].values())
    assert not owner.clear_source_freezes()
    assert len(snapshots) == 1
    owner.stores[LONG].freeze_source_submissions("one")
    owner.stores[SHORT].freeze_source_submissions("two")
    owner.freeze_sources("must not replace either")
    assert not owner.clear_source_freezes()
    assert owner.stores[LONG].source_freeze_reason == "one"
    assert owner.stores[SHORT].source_freeze_reason == "two"


@pytest.mark.parametrize("dust", ["0.4", "0.5"])
def test_opposing_dust_is_preserved_separately_and_does_not_unlock(
    tmp_path: Path, dust: str,
) -> None:
    prefix = tmp_path / "strict-dust"
    owner = _owner(prefix)
    for direction in (LONG, SHORT):
        _begin(owner, direction, "1")
    for direction in (LONG, SHORT):
        assert _fill(owner, direction, dust) is None
        view = owner.stores[direction]
        view.update_source_status(f"S-{direction.value}", "CANCELED")
        view.confirm_source_reconciled(f"S-{direction.value}")
    assert [view.rounding_residual_ounces for view in owner.stores.values()] == [
        D(dust), -D(dust),
    ]
    assert not owner.clear_source_freezes()
    assert all(not view.can_submit_source() for view in owner.stores.values())
    assert _memory(_owner(prefix)) == _memory(owner)


@pytest.mark.parametrize("which", ["bid", "ask", "both"])
def test_legacy_presence_requires_explicit_future_migration(tmp_path: Path, which: str) -> None:
    prefix = tmp_path / "legacy"
    paths = maker_legacy_paths(prefix)
    for index, path in enumerate(paths):
        if which == "both" or which == ("bid", "ask")[index]:
            path.write_text("historical evidence, even if malformed")
    before = {path: path.read_bytes() for path in paths if path.exists()}
    with pytest.raises(ValueError, match="legacy|migration"):
        _owner(prefix)
    assert {path: path.read_bytes() for path in paths if path.exists()} == before
    assert not maker_state_path(prefix).exists()


def test_existing_v2_does_not_reimport_retained_legacy_files(tmp_path: Path) -> None:
    prefix = tmp_path / "new-wins"
    owner = _owner(prefix)
    _begin(owner, LONG)
    for path in maker_legacy_paths(prefix):
        path.write_text("old evidence")
    assert _memory(_owner(prefix)) == _memory(owner)
    assert all(path.read_text() == "old evidence" for path in maker_legacy_paths(prefix))


@pytest.mark.parametrize("fault", [
    "schema", "kind", "source_instrument", "hedge_instrument", "missing_ask", "extra_direction",
    "source_side", "intent_side", "hedge_side", "nested_schema", "intent_trade",
    "source_id", "fill_key", "intent_id", "hedge_id",
])
def test_wrong_schema_binding_direction_or_cross_view_identity_is_rejected(
    tmp_path: Path, fault: str,
) -> None:
    prefix = tmp_path / "corrupt"
    owner = _owner(prefix)
    for direction in (LONG, SHORT):
        _begin(owner, direction)
    for direction in (LONG, SHORT):
        intent = _fill(owner, direction)
        owner.stores[direction].bind_hedge_order(intent.intent_id, f"H-{direction.value}")
    payload = json.loads(owner.path.read_text())
    bid, ask = payload["directions"]["bid"], payload["directions"]["ask"]
    bid_intent = next(iter(bid["hedge_intents"].values()))
    ask_intent = next(iter(ask["hedge_intents"].values()))
    if fault == "schema":
        payload["schema_version"] = 99
    elif fault == "kind":
        payload["kind"] = "taker"
    elif fault.endswith("instrument"):
        payload[f"{fault}_id"] = "OTHER.VENUE"
    elif fault == "missing_ask":
        del payload["directions"]["ask"]
    elif fault == "extra_direction":
        payload["directions"]["other"] = bid
    elif fault == "source_side":
        ask["source_orders"]["S-ask"]["side"] = "BUY"
    elif fault == "intent_side":
        ask_intent["source_side"] = "BUY"
    elif fault == "hedge_side":
        ask_intent["hedge_side"] = "SELL"
    elif fault == "nested_schema":
        ask["schema_version"] = True
    elif fault == "intent_trade":
        ask_intent["source_trade_id"] = "DIFFERENT"
    elif fault == "source_id":
        ask["source_orders"]["S-bid"] = {**bid["source_orders"]["S-bid"], "side": "SELL"}
    elif fault == "fill_key":
        ask["seen_source_fills"].append(bid["seen_source_fills"][0])
    elif fault == "intent_id":
        key = ask_intent["intent_id"]
        ask_intent["intent_id"] = bid_intent["intent_id"]
        ask["hedge_intents"][bid_intent["intent_id"]] = ask["hedge_intents"].pop(key)
    else:
        ask_intent["hedge_client_order_id"] = "H-bid"
        ask_intent["hedge_order_ids"] = ["H-bid"]
    owner.path.write_text(json.dumps(payload))
    before = owner.path.read_bytes()
    with pytest.raises(ValueError):
        _owner(prefix)
    assert owner.path.read_bytes() == before


def test_invalid_direction_mutation_rolls_back_entire_owner(tmp_path: Path) -> None:
    owner = _owner(tmp_path / "wrong-side")
    previous = _memory(owner)
    with pytest.raises(ValueError, match="direction"):
        owner.stores[LONG].begin_source("WRONG", SELL, D(1))
    assert _memory(owner) == previous and not owner.path.exists()


@pytest.mark.parametrize("wrong_side", [False, True])
def test_invalid_fill_before_persistence_cannot_leave_added_freezes(
    tmp_path: Path, wrong_side: bool,
) -> None:
    owner = _owner(tmp_path / "invalid-fill")
    _begin(owner, LONG)
    previous = _memory(owner)
    disk_before = owner.path.read_bytes()
    with pytest.raises(ValueError):
        owner.stores[LONG].reserve_source_fill(
            fill_key="S-bid|V|T", client_order_id="S-bid", trade_id="T",
            source_side=SELL if wrong_side else BUY, fill_ounces=D(1) if wrong_side else D(0),
        )
    assert _memory(owner) == previous
    assert owner.path.read_bytes() == disk_before


@pytest.mark.parametrize("leg", ["source", "hedge"])
def test_same_venue_fill_cannot_be_claimed_by_different_direction_order_ids(
    tmp_path: Path, leg: str,
) -> None:
    owner = _owner(tmp_path / "venue-fill-collision")
    for direction in (LONG, SHORT):
        _begin(owner, direction)
    if leg == "source":
        owner.stores[LONG].reserve_source_fill(
            fill_key="S-bid|SHARED-VENUE|SHARED-TRADE", client_order_id="S-bid",
            trade_id="SHARED-TRADE", source_side=BUY, fill_ounces=D(2),
        )
    else:
        for direction in (LONG, SHORT):
            intent = _fill(owner, direction)
            owner.stores[direction].bind_hedge_order(intent.intent_id, f"H-{direction.value}")
        owner.stores[LONG].apply_hedge_fill(
            client_order_id="H-bid", trade_id="SHARED-DEAL", fill_ounces=D(2),
        )
    def action() -> object:
        if leg == "source":
            return owner.stores[SHORT].reserve_source_fill(
                fill_key="S-ask|SHARED-VENUE|SHARED-TRADE", client_order_id="S-ask",
                trade_id="SHARED-TRADE", source_side=SELL, fill_ounces=D(2),
            )
        return owner.stores[SHORT].apply_hedge_fill(
            client_order_id="H-ask", trade_id="SHARED-DEAL", fill_ounces=D(2),
        )
    before = _memory(owner)
    disk_before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="fill identity"):
        action()
    assert _memory(owner) == before
    assert owner.path.read_bytes() == disk_before
