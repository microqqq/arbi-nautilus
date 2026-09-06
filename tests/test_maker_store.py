"""Maker one-file custody; no residual netting or legacy conversion is implied."""

import json
import os
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
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


def _owner(prefix: Path, **kwargs: Any) -> MakerStateStore:
    return MakerStateStore(prefix, "SOURCE.BITFINEX", "HEDGE.MT5", **kwargs)


def _begin(
    owner: MakerStateStore, direction: SourceDirection, quantity: str = "2",
    **route: Any,
) -> None:
    attributes: dict[str, Any] = dict(
        source_account_id="BITFINEX-001", source_client_id="BITFINEX",
        hedge_account_id="MT5-001", hedge_client_id="MT5",
    )
    attributes.update(route)
    owner.stores[direction].begin_source(
        f"S-{direction.value}", BUY if direction is LONG else SELL, D(quantity),
        **attributes,
    )


def _fill(owner: MakerStateStore, direction: SourceDirection, quantity: str = "2") -> Any:
    return owner.stores[direction].reserve_source_fill(
        fill_key=f"S-{direction.value}|V-{direction.value}|T-{direction.value}",
        client_order_id=f"S-{direction.value}", trade_id=f"T-{direction.value}",
        source_side=BUY if direction is LONG else SELL, fill_ounces=D(quantity),
    )


def _memory(owner: MakerStateStore) -> object:
    return deepcopy(owner._snapshot())


@pytest.mark.parametrize("first", [LONG, SHORT])
@pytest.mark.parametrize(("limit", "amount", "allowed"), [
    ("0.5", "0.5", True), ("0.49", "0.5", False), ("0", "0.5", False),
    ("0.1", "0.1", True), ("0.1", "0.2", False),
])
def test_bounded_residual_releases_only_after_both_terminals_and_retains_signed_history(
    tmp_path: Path, first: SourceDirection, limit: str, amount: str, allowed: bool,
) -> None:
    prefix = tmp_path / "bounded"
    route = ("BITFINEX-001", "BITFINEX", "MT5-001", "MT5")
    options = dict(residual_limit_ounces=D(limit), carry_route=route if D(limit) else None)
    owner = _owner(prefix, **options)
    for direction in (LONG, SHORT):
        _begin(owner, direction, "1")
    assert _fill(owner, first, amount) is None
    for index, direction in enumerate((LONG, SHORT)):
        view = owner.stores[direction]
        view.update_source_status(f"S-{direction.value}", "CANCELED")
        view.confirm_source_reconciled(f"S-{direction.value}")
        if index == 0:
            assert not owner.clear_source_freezes()
    assert owner.has_residuals()
    assert owner.clear_source_freezes() is allowed
    assert all(view.can_submit_source() is allowed for view in owner.stores.values())
    before = owner.path.read_bytes()
    reloaded = _owner(prefix, **options)
    expected = D(amount) if first is LONG else -D(amount)
    assert sum((v.rounding_residual_ounces for v in reloaded.stores.values()), D(0)) == expected
    assert all(view.can_submit_source() is allowed for view in reloaded.stores.values())
    strict = _owner(prefix)
    assert all(not view.can_submit_source() for view in strict.stores.values())
    assert owner.path.read_bytes() == before
    if allowed:
        view = reloaded.stores[LONG]
        view.begin_source("NEXT-BID", BUY, D(1), source_account_id=route[0],
                          source_client_id=route[1], hedge_account_id=route[2],
                          hedge_client_id=route[3])
        assert reloaded.stores[SHORT].can_submit_source()


def test_bounded_release_checks_each_completed_intent_not_zero_net_remaining(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path / "incomplete", residual_limit_ounces=D("0.5"),
                   carry_route=("BITFINEX-001", "BITFINEX", "MT5-001", "MT5"))
    for direction in (LONG, SHORT):
        _begin(owner, direction)
    for direction in (LONG, SHORT):
        intent = _fill(owner, direction, "0.6")
        assert intent is not None
        view = owner.stores[direction]
        # Deliberately inconsistent completion evidence, not simulated venue success.
        view._state.hedge_intents[intent.intent_id] = replace(
            intent, status=ObligationStatus.COMPLETED, hedge_filled_ounces=D("0.8"),
        )
        view.update_source_status(f"S-{direction.value}", "CANCELED")
        view.confirm_source_reconciled(f"S-{direction.value}")
    assert sum((view.net_unhedged_ounces for view in owner.stores.values()), D(0)) == 0
    assert not owner.clear_source_freezes()
    assert all(not view.can_submit_source() for view in owner.stores.values())


@pytest.mark.parametrize("different", [
    {"hedge_account_id": "MT5-OTHER"}, {"source_account_id": None},
    {"hedge_client_id": "OTHER"}, {"hedge_position_id": "ONE-SHOT",
                                  "hedge_position_quantity_ounces": D(1)},
])
def test_bounded_residual_cannot_use_another_or_isolated_route_budget(
    tmp_path: Path, different: dict[str, Any],
) -> None:
    owner = _owner(tmp_path / "foreign-carry", residual_limit_ounces=D("0.5"),
                   carry_route=("BITFINEX-001", "BITFINEX", "MT5-001", "MT5"))
    _begin(owner, LONG, **different)
    assert _fill(owner, LONG, "0.4") is None
    owner.stores[LONG].update_source_status("S-bid", "CANCELED")
    owner.stores[LONG].confirm_source_reconciled("S-bid")
    assert not owner.source_balance_is_admissible()
    assert not owner.clear_source_freezes()
    assert all(not view.can_submit_source() for view in owner.stores.values())


@pytest.mark.parametrize("first", [LONG, SHORT])
@pytest.mark.parametrize("reload_each", [False, True])
def test_pending_hedge_view_uses_allocation_order_skips_zero_and_completed(
    tmp_path: Path, first: SourceDirection, reload_each: bool,
) -> None:
    prefix = tmp_path / "ordered"
    owner = _owner(prefix)
    other = SHORT if first is LONG else LONG
    for direction in (first, other):
        _begin(owner, direction)
    expected = []
    for index, (direction, amount) in enumerate(
        [(first, "0.1"), (first, "0.5"), (other, "0.2"), (first, "0.2")],
    ):
        cid, trade = f"S-{direction.value}", f"ORDERED-{index}"
        intent = owner.stores[direction].reserve_source_fill(
            fill_key=f"{cid}|V-{direction.value}|{trade}", client_order_id=cid,
            trade_id=trade, source_side=BUY if direction is LONG else SELL,
            fill_ounces=D(amount),
        )
        if intent is not None:
            expected.append((direction, intent.intent_id))
    assert len(expected) == 3
    for index, (direction, intent_id) in enumerate(expected):
        if reload_each:
            owner = _owner(prefix)  # Format/read-order check, not live restart authorization.
        before = owner.path.read_bytes()
        pending = owner.next_pending_hedge()
        assert pending is not None and (pending[0], pending[1].intent_id) == (direction, intent_id)
        assert owner.path.read_bytes() == before  # Selection does not persist another queue.
        view = owner.stores[direction]
        view.bind_hedge_order(intent_id, f"H-{index}")
        assert owner.next_pending_hedge() is None
        view.apply_hedge_fill(client_order_id=f"H-{index}", trade_id=f"D-{index}", fill_ounces=D(1))
    assert owner.next_pending_hedge() is None


def test_pending_hedge_view_keeps_multileg_intent_ahead_of_other_direction(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path / "multileg-order")
    for direction in (SHORT, LONG):
        _begin(owner, direction)
    first, second = _fill(owner, SHORT), _fill(owner, LONG)
    assert first is not None and second is not None
    view = owner.stores[SHORT]
    view.bind_hedge_plan(first.intent_id, (
        HedgeLeg(BUY, D(1), "OLD-SELL", SELL, D(1)), HedgeLeg(BUY, D(1)),
    ))
    for index in range(2):
        selected = owner.next_pending_hedge()
        assert selected is not None and selected[1].intent_id == first.intent_id
        assert selected[1].hedge_leg_index == index
        view.bind_hedge_order(first.intent_id, f"H-LEG-{index}")
        assert owner.next_pending_hedge() is None
        view.apply_hedge_fill(
            client_order_id=f"H-LEG-{index}", trade_id=f"D-{index}", fill_ounces=D(1),
        )
    selected = owner.next_pending_hedge()
    assert selected is not None and selected[1].intent_id == second.intent_id


@pytest.mark.parametrize("old_status", list(ObligationStatus))
def test_pending_hedge_view_never_guesses_old_checkpoint_execution_order(
    tmp_path: Path, old_status: ObligationStatus,
) -> None:
    from py000_nautilus.maker_migration import migrate_maker_state

    old_prefix, new_prefix = tmp_path / "old", tmp_path / "new"
    legacy = JsonStateStore(maker_legacy_paths(old_prefix)[0])
    legacy.begin_source("OLD", BUY, D(2), source_account_id="BITFINEX-001",
                        source_client_id="BITFINEX", hedge_account_id="MT5-001",
                        hedge_client_id="MT5")
    old_intent = legacy.reserve_source_fill(
        fill_key="OLD|VENUE|OLD-TRADE", client_order_id="OLD", trade_id="OLD-TRADE",
        source_side=BUY, fill_ounces=D(1),
    )
    assert old_intent is not None
    if old_status is ObligationStatus.BLOCKED:
        legacy.block_hedge_intent(old_intent.intent_id, "old blocked")
    elif old_status is not ObligationStatus.PENDING:
        legacy.bind_hedge_order(old_intent.intent_id, "OLD-HEDGE")
        if old_status is ObligationStatus.COMPLETED:
            legacy.apply_hedge_fill(
                client_order_id="OLD-HEDGE", trade_id="OLD-DEAL", fill_ounces=D(1),
            )
        else:
            legacy.update_hedge_status("OLD-HEDGE", old_status)
    JsonStateStore(maker_legacy_paths(old_prefix)[1]).freeze_source_submissions("old state")
    owner = migrate_maker_state(
        old_prefix, new_prefix, "SOURCE.BITFINEX", "HEDGE.MT5", stopped=True,
    )
    new_intent = owner.stores[LONG].reserve_source_fill(
        fill_key="OLD|VENUE|NEW-TRADE", client_order_id="OLD", trade_id="NEW-TRADE",
        source_side=BUY, fill_ounces=D(1),
    )
    assert new_intent is not None
    for current in (owner, _owner(new_prefix)):
        before = current.path.read_bytes()
        selected = current.next_pending_hedge()
        if old_status is ObligationStatus.COMPLETED:
            assert selected is not None and selected[1].intent_id == new_intent.intent_id
        else:
            assert selected is None
        assert current.path.read_bytes() == before
        assert current.stores[LONG].intent(old_intent.intent_id).status is old_status


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
    assert payload["schema_version"] == 3 and payload["kind"] == "maker"
    assert payload["source_instrument_id"] == "SOURCE.BITFINEX"
    assert payload["hedge_instrument_id"] == "HEDGE.MT5"
    assert len(payload["allocations"]) == 1
    assert payload["allocations"][0]["fill_key"] == "S-bid|V-bid|T-bid"
    assert payload["allocations"][0]["signed_fill_ounces"] == "2"
    assert payload["allocations"][0]["allocated_ounces"] == "2"
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
def test_cross_route_opposing_dust_is_preserved_separately_and_does_not_unlock(
    tmp_path: Path, dust: str,
) -> None:
    prefix = tmp_path / "strict-dust"
    owner = _owner(prefix)
    _begin(owner, LONG, "1")
    _begin(owner, SHORT, "1", hedge_client_id="OTHER")
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


@pytest.mark.parametrize("first", [LONG, SHORT])
@pytest.mark.parametrize("dust", ["0.4", "0.5"])
def test_same_route_opposing_actual_dust_nets_before_next_normal_cycle(
    tmp_path: Path, first: SourceDirection, dust: str,
) -> None:
    prefix = tmp_path / "strict-net"
    owner = _owner(prefix)
    second = SHORT if first is LONG else LONG
    for direction in (first, second):
        _begin(owner, direction, "1")
    assert _fill(owner, first, dust) is None
    assert _fill(owner, second, dust) is None
    for direction in (first, second):
        view = owner.stores[direction]
        view.update_source_status(f"S-{direction.value}", "CANCELED")
        view.confirm_source_reconciled(f"S-{direction.value}")
    assert all(view.rounding_residual_ounces == 0 for view in owner.stores.values())
    assert not owner.has_residuals()
    assert owner.clear_source_freezes()
    reloaded = _owner(prefix)
    assert all(view.can_submit_source() for view in reloaded.stores.values())
    view = reloaded.stores[LONG]
    view.begin_source(
        "NEXT", BUY, D(1), source_account_id="BITFINEX-001", source_client_id="BITFINEX",
        hedge_account_id="MT5-001", hedge_client_id="MT5",
    )
    intent = view.reserve_source_fill(
        fill_key="NEXT|NEXT-VENUE|NEXT-TRADE", client_order_id="NEXT", trade_id="NEXT-TRADE",
        source_side=BUY, fill_ounces=D(1),
    )
    assert intent is not None and intent.hedge_quantity_ounces == 1
    assert intent.hedge_side is SELL


@pytest.mark.parametrize("first", [LONG, SHORT])
def test_shared_dust_is_combined_before_rounding_the_next_fill(
    tmp_path: Path, first: SourceDirection,
) -> None:
    prefix = tmp_path / "combine-first"
    owner = _owner(prefix)
    second = SHORT if first is LONG else LONG
    for direction in (first, second):
        _begin(owner, direction)
    assert _fill(owner, first, "0.5") is None
    assert _fill(owner, second, "0.6") is None
    expected = D("-0.1") if first is LONG else D("0.1")
    assert owner.stores[first].rounding_residual_ounces == 0
    assert owner.stores[second].rounding_residual_ounces == expected
    assert owner.has_residuals()
    assert not owner.clear_source_freezes()
    payload = json.loads(owner.path.read_text())
    assert [item["allocated_ounces"] for item in payload["allocations"]] == ["0", "0"]
    assert _memory(_owner(prefix)) == _memory(owner)


@pytest.mark.parametrize("first", [LONG, SHORT])
@pytest.mark.parametrize("status", [ObligationStatus.PENDING, ObligationStatus.SUBMITTED])
def test_already_allocated_pending_obligation_cannot_be_netted_away(
    tmp_path: Path, first: SourceDirection, status: ObligationStatus,
) -> None:
    owner = _owner(tmp_path / "allocated-not-residual")
    second = SHORT if first is LONG else LONG
    for direction in (first, second):
        _begin(owner, direction)
    first_intent = _fill(owner, first, "0.6")
    assert first_intent is not None and first_intent.hedge_quantity_ounces == 1
    assert owner.stores[first].rounding_residual_ounces == (
        D("-0.4") if first is LONG else D("0.4")
    )
    if status is ObligationStatus.SUBMITTED:
        owner.stores[first].bind_hedge_order(first_intent.intent_id, "H-INFLIGHT")
        owner.stores[first].update_hedge_status("H-INFLIGHT", status)
    original_intent = owner.stores[first].intent(first_intent.intent_id)
    second_intent = _fill(owner, second, "0.6")
    assert second_intent is not None and second_intent.hedge_quantity_ounces == 1
    assert all(view.rounding_residual_ounces == 0 for view in owner.stores.values())
    assert owner.stores[first].intent(first_intent.intent_id) == original_intent
    assert owner.stores[first].intent(first_intent.intent_id).status is status
    assert owner.stores[second].intent(second_intent.intent_id).status is ObligationStatus.PENDING
    assert not owner.clear_source_freezes()
    assert all(not view.can_submit_source() for view in owner.stores.values())


@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_same_direction_point_four_removes_rounding_residual_but_not_pending_hedge(
    tmp_path: Path, direction: SourceDirection,
) -> None:
    owner = _owner(tmp_path / "same-direction-remainder")
    _begin(owner, direction)
    view = owner.stores[direction]
    intent = _fill(owner, direction, "0.6")
    assert intent is not None
    assert view.reserve_source_fill(
        fill_key=f"S-{direction.value}|V-{direction.value}|SECOND",
        client_order_id=f"S-{direction.value}", trade_id="SECOND",
        source_side=BUY if direction is LONG else SELL, fill_ounces=D("0.4"),
    ) is None
    assert view.intent(intent.intent_id) == intent
    view.update_source_status(f"S-{direction.value}", "CANCELED")
    view.confirm_source_reconciled(f"S-{direction.value}")
    assert not owner.has_residuals()
    assert all(item.rounding_residual_ounces == 0 and item.active_source_order_id is None
               for item in owner.stores.values())
    assert not owner.clear_source_freezes()
    assert not view.cycle_evidence_complete() and not view.can_submit_source()


@pytest.mark.parametrize("change", [
    "source_account_id", "source_client_id", "hedge_account_id", "hedge_client_id",
    "source_client_none", "hedge_client_none", "missing_source", "missing_hedge", "ticket",
])
def test_route_mismatch_unknown_account_and_exact_ticket_never_cross_net(
    tmp_path: Path, change: str,
) -> None:
    prefix = tmp_path / "route-isolation"
    owner = _owner(prefix)
    first: dict[str, Any] = {}
    second: dict[str, Any] = {}
    if change in {"missing_source", "missing_hedge"}:
        field = "source_account_id" if change == "missing_source" else "hedge_account_id"
        first[field] = second[field] = None
    elif change == "ticket":
        first = second = {
            "hedge_position_id": "same-ticket", "hedge_position_quantity_ounces": D(1),
        }
    elif change.endswith("_none"):
        second[change.removesuffix("_none") + "_id"] = None
    else:
        second[change] = "OTHER"
    _begin(owner, LONG, "1", **first)
    _begin(owner, SHORT, "1", **second)
    assert _fill(owner, LONG, "0.5") is None
    assert _fill(owner, SHORT, "0.5") is None
    for direction in (LONG, SHORT):
        view = owner.stores[direction]
        view.update_source_status(f"S-{direction.value}", "CANCELED")
        view.confirm_source_reconciled(f"S-{direction.value}")
    assert sum((view.rounding_residual_ounces for view in owner.stores.values()), D(0)) == 0
    assert owner.has_residuals()
    assert not owner.clear_source_freezes()
    assert all(not view.can_submit_source() and not view.cycle_evidence_complete()
               for view in owner.stores.values())
    assert _memory(_owner(prefix)) == _memory(owner)


def test_none_clients_match_only_exact_none_clients_for_known_accounts(tmp_path: Path) -> None:
    owner = _owner(tmp_path / "none-clients")
    for direction in (LONG, SHORT):
        _begin(owner, direction, "1", source_client_id=None, hedge_client_id=None)
    assert _fill(owner, LONG, "0.5") is None
    assert _fill(owner, SHORT, "0.5") is None
    assert not owner.has_residuals()
    assert all(view.rounding_residual_ounces == 0 for view in owner.stores.values())


@pytest.mark.parametrize("after_replace", [False, True])
def test_second_direction_fill_rolls_back_or_retains_ledger_and_both_views_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_replace: bool,
) -> None:
    prefix = tmp_path / "net-fault"
    owner = _owner(prefix)
    for direction in (LONG, SHORT):
        _begin(owner, direction)
    assert _fill(owner, LONG, "0.5") is None
    previous = _memory(owner)
    disk_before = owner.path.read_bytes()

    def fail(source: Path, destination: Path) -> None:
        if after_replace:
            os.replace(source, destination)
            raise ParentDirectorySyncError("post-replace")
        raise OSError("pre-replace")

    monkeypatch.setattr(store_module, "replace_and_sync_parent", fail)
    with pytest.raises(ParentDirectorySyncError if after_replace else OSError):
        _fill(owner, SHORT, "0.5")
    reloaded = _owner(prefix)
    assert _memory(owner) == _memory(reloaded)
    assert (_memory(owner) != previous) is after_replace
    assert (owner.path.read_bytes() != disk_before) is after_replace
    payload = json.loads(owner.path.read_text())
    assert len(payload["allocations"]) == (2 if after_replace else 1)
    assert owner.has_residuals() is not after_replace
    monkeypatch.setattr(store_module, "replace_and_sync_parent", replace_and_sync_parent)
    assert _fill(reloaded, SHORT, "0.5") is None
    assert len(json.loads(reloaded.path.read_text())["allocations"]) == 2
    assert not reloaded.has_residuals()


def test_v3_rejects_v2_without_overwriting_or_guessing_fill_history(tmp_path: Path) -> None:
    prefix = tmp_path / "no-fabrication"
    owner = _owner(prefix)
    _begin(owner, LONG)
    _fill(owner, LONG, "0.4")
    payload = json.loads(owner.path.read_text())
    payload["schema_version"] = 2
    payload.pop("allocations", None)
    owner.path.write_text(json.dumps(payload))
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="schema|migration"):
        _owner(prefix)
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("fault", [
    "missing_allocation", "duplicate_allocation", "reordered", "signed_quantity", "nonfinite",
    "allocated_quantity", "route", "missing_route", "raw_residual", "source_cumulative",
    "missing_intent", "intent_quantity", "intent_source_fill",
])
def test_allocation_history_must_match_processing_order_route_and_original_facts(
    tmp_path: Path, fault: str,
) -> None:
    prefix = tmp_path / "invalid-allocation"
    owner = _owner(prefix)
    for direction in (LONG, SHORT):
        _begin(owner, direction)
    assert _fill(owner, LONG, "0.5") is None
    assert _fill(owner, SHORT, "0.6") is None
    intent = owner.stores[SHORT].reserve_source_fill(
        fill_key="S-ask|V-ask|SECOND-ASK", client_order_id="S-ask", trade_id="SECOND-ASK",
        source_side=SELL, fill_ounces=D("0.6"),
    )
    assert intent is not None
    payload = json.loads(owner.path.read_text())
    allocations = payload["allocations"]
    bid, ask = payload["directions"]["bid"], payload["directions"]["ask"]
    if fault == "missing_allocation":
        allocations.pop(0)
    elif fault == "duplicate_allocation":
        allocations.append(allocations[0])
    elif fault == "reordered":
        allocations[0], allocations[1] = allocations[1], allocations[0]
    elif fault == "signed_quantity":
        allocations[0]["signed_fill_ounces"] = "0.4"
    elif fault == "nonfinite":
        allocations[0]["signed_fill_ounces"] = "NaN"
    elif fault == "allocated_quantity":
        allocations[2]["allocated_ounces"] = "0"
    elif fault == "route":
        allocations[0]["route"]["hedge_account_id"] = "OTHER"
    elif fault == "missing_route":
        del allocations[0]["route"]["source_client_id"]
    elif fault == "raw_residual":
        bid["net_unhedged_ounces"] = "0.5"
    elif fault == "source_cumulative":
        bid["source_orders"]["S-bid"]["filled_ounces"] = "0.4"
    elif fault == "missing_intent":
        ask["hedge_intents"].clear()
    else:
        field = "hedge_quantity_ounces" if fault == "intent_quantity" else "source_fill_ounces"
        ask["hedge_intents"][intent.intent_id][field] = "2"
    owner.path.write_text(json.dumps(payload))
    before = owner.path.read_bytes()
    with pytest.raises(ValueError):
        _owner(prefix)
    assert owner.path.read_bytes() == before


def test_actual_per_fill_history_is_not_replaced_by_equal_legacy_totals(tmp_path: Path) -> None:
    payloads: list[dict[str, Any]] = []
    for index, quantities in enumerate((("0.1", "0.4"), ("0.2", "0.3"))):
        prefix = tmp_path / f"history-{index}"
        owner = _owner(prefix)
        _begin(owner, LONG)
        for sequence, quantity in enumerate(quantities):
            assert owner.stores[LONG].reserve_source_fill(
                fill_key=f"S-bid|V|T{sequence}", client_order_id="S-bid",
                trade_id=f"T{sequence}", source_side=BUY, fill_ounces=D(quantity),
            ) is None
        assert _memory(_owner(prefix)) == _memory(owner)
        payloads.append(json.loads(owner.path.read_text()))
    assert payloads[0]["directions"] == payloads[1]["directions"]
    assert payloads[0]["allocations"] != payloads[1]["allocations"]
    assert [row["signed_fill_ounces"] for row in payloads[0]["allocations"]] == ["0.1", "0.4"]
    assert [row["signed_fill_ounces"] for row in payloads[1]["allocations"]] == ["0.2", "0.3"]


def test_zero_view_projection_cannot_hide_two_nonzero_route_residuals(tmp_path: Path) -> None:
    prefix = tmp_path / "zero-projection"
    owner = _owner(prefix)
    view = owner.stores[LONG]
    for cid, hedge_account in (("A", "MT5-001"), ("B", "MT5-002")):
        view.begin_source(cid, BUY, D(1), source_account_id="BITFINEX-001",
                          source_client_id="BITFINEX", hedge_account_id=hedge_account)
        view.update_source_status(cid, "CANCELED")
        view.confirm_source_reconciled(cid)
    assert view.reserve_source_fill(
        fill_key="A|VA|TA", client_order_id="A", trade_id="TA", source_side=BUY,
        fill_ounces=D("0.4"),
    ) is None
    intent = view.reserve_source_fill(
        fill_key="B|VB|TB", client_order_id="B", trade_id="TB", source_side=BUY,
        fill_ounces=D("0.6"),
    )
    assert intent is not None and intent.hedge_quantity_ounces == 1
    view.bind_hedge_order(intent.intent_id, "HB")
    assert view.apply_hedge_fill(client_order_id="HB", trade_id="HBT", fill_ounces=D(1))
    for cid in ("A", "B"):
        view.update_source_status(cid, "CANCELED")
        view.confirm_source_reconciled(cid)
    assert all(item.rounding_residual_ounces == item.net_unhedged_ounces == 0
               for item in owner.stores.values())
    assert all(item.active_source_order_id is None and not item.has_unresolved_hedges()
               and item.halt_reason is None for item in owner.stores.values())
    assert owner.has_residuals()
    assert not owner.clear_source_freezes()
    assert all(not item.can_submit_source() and not item.cycle_evidence_complete()
               for item in owner.stores.values())
    assert _owner(prefix).has_residuals()
