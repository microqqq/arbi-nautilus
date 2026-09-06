"""One retained zero-fill rejection, not an automatic order-retry mechanism."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from test_hedge_projection import (
    BUY,
    SELL,
    _assert_refused,
    _new,
    _order,
    _project,
    _record,
    _reload,
    _seed,
    _view,
)

import py000_nautilus.models as models
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import HedgeLeg, ObligationStatus
from py000_nautilus.restart_recovery import StartupRecoveryOptions, capture_startup_receipt
from py000_nautilus.store import JsonStateStore, _persist_payload

D = Decimal


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("pause", ["freeze", "unknown", "block", "rejected"])
def test_repeated_public_pause_revokes_in_memory_review_even_when_text_is_unchanged(
    tmp_path: Path, maker: bool, pause: str,
) -> None:
    store = _new(tmp_path / "paused", maker)
    view = _view(store)
    intent_id = _seed(view)

    def pause_again() -> None:
        if pause == "freeze":
            view.freeze_source_submissions("operator reviewed this exact text")
        elif pause == "unknown":
            view.mark_source_unknown("S", "operator reviewed this exact text")
        elif pause == "block":
            view.block_hedge_intent(intent_id, "operator reviewed this exact text")
        else:
            view.update_hedge_status("H-CLOSE", ObligationStatus.REJECTED)

    pause_again()
    revoked: list[bool] = []
    view._restart_pause_revoker = lambda: revoked.append(True)
    before = store._to_payload()
    pause_again()
    assert revoked == [True]
    assert store._to_payload() == before


@pytest.mark.parametrize("status", ["CANCELED", "EXPIRED", "DENIED", "REJECTED"])
def test_public_source_failure_revokes_review_even_when_existing_halt_is_unchanged(
    tmp_path: Path, status: str,
) -> None:
    # This tests the public store boundary; startup-gated native strategy
    # callbacks cannot themselves reach it during ordinary reconciliation.
    store = JsonStateStore(tmp_path / "source-pause.json")
    store.begin_source("S", BUY, D(2))
    store.update_source_status("S", "CANCELED")
    old_halt = store.halt_reason
    assert old_halt is not None
    receipt = capture_startup_receipt(store, StartupRecoveryOptions(resume_held=True))
    assert receipt.eligible
    store.update_source_status("S", status)
    assert store.halt_reason == old_halt
    assert receipt.review_revoked
    with pytest.raises(RuntimeError, match="review revoked"):
        receipt.check(store)


def test_whole_maker_same_freeze_revokes_review_without_requiring_a_publication(
    tmp_path: Path,
) -> None:
    store = _new(tmp_path / "maker", True)
    assert isinstance(store, MakerStateStore)
    store.freeze_sources("same external cause")
    before = store.path.read_bytes()
    revoked: list[bool] = []
    for view in store.stores.values():
        view._restart_pause_revoker = lambda: revoked.append(True)
    store.freeze_sources("same external cause")
    assert revoked
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
def test_internal_restart_and_held_projection_do_not_revoke_review(
    tmp_path: Path, maker: bool,
) -> None:
    store = _new(tmp_path / "internal", maker)
    view = _view(store)
    _seed(view)
    revoked: list[bool] = []
    view._restart_pause_revoker = lambda: revoked.append(True)
    view.recover_for_start()
    assert _project(store, _order()) == 1
    assert not revoked


def _archived(
    path: Path, maker: bool, index: int = 0,
) -> tuple[JsonStateStore | MakerStateStore, str, list[Any]]:
    """Construct the already-authorized publication; no recovery permission is implied."""
    store = _new(path, maker)
    view = _view(store)
    legs = (HedgeLeg(SELL, D(1), "900000101", BUY, D(1)),
            HedgeLeg(SELL, D(1), "900000102", BUY, D(1)), HedgeLeg(SELL, D(1)))
    intent_id = _seed(view, quantity="3", legs=legs,
                      hedge_cid="H-FIRST" if index else "H-REJECT")
    orders = []
    if index:
        first = _order("H-FIRST")
        _record(view, first, 1)
        orders.append(first)
        view.bind_hedge_order(intent_id, "H-REJECT")
    view.update_hedge_status("H-REJECT", ObligationStatus.REJECTED)
    original = view.intent(intent_id)
    view._state.hedge_intents[intent_id] = replace(
        original, rejected_attempt=models.RejectedHedgeAttempt("H-REJECT", index),
        hedge_client_order_id=None, status=ObligationStatus.PENDING,
    )
    view._persist()
    orders.append(_order("H-REJECT", (), state="REJECTED"))
    return store, intent_id, orders


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("index", [0, 1], ids=["first-leg", "middle-leg"])
def test_archived_zero_rejection_stays_required_but_does_not_shift_leg_fill_allocation(
    tmp_path: Path, maker: bool, index: int,
) -> None:
    store, intent_id, orders = _archived(tmp_path / "projection", maker, index)
    view = _view(store)
    original = view.intent(intent_id)
    assert original.hedge_leg_order_ids == (() if index == 0 else ("H-FIRST",))
    assert original.hedge_order_ids[-1] == "H-REJECT"
    assert _project(store, *orders) == 0
    view.bind_hedge_order(intent_id, "H-RETRY")
    current = _order("H-RETRY", position="900000101" if index == 0 else "900000102")
    before = deepcopy(store._to_payload())
    assert _project(store, *orders, current) == 1
    updated = view.intent(intent_id)
    assert updated.status is ObligationStatus.BLOCKED
    assert updated.hedge_leg_index == index and updated.hedge_client_order_id == "H-RETRY"
    assert updated.hedge_filled_ounces == D(index + 1)
    assert updated.hedge_leg_filled_ounces == 1
    assert updated.hedge_order_ids == (*original.hedge_order_ids, "H-RETRY")
    assert updated.rejected_attempt == original.rejected_attempt
    expected_seen = {"H-RETRY|H-RETRY-T0"}
    if index:
        expected_seen.add("H-FIRST|H-FIRST-T0")
    assert view._state.seen_hedge_fills == expected_seen
    assert store._to_payload().get("allocations") == before.get("allocations")
    reloaded = _reload(store)
    assert _project(reloaded, *orders, current) == 0
    assert reloaded._to_payload() == store._to_payload()


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("index", [0, 1])
def test_normal_reducer_completes_remaining_legs_without_reusing_the_archived_id(
    tmp_path: Path, maker: bool, index: int,
) -> None:
    store, intent_id, orders = _archived(tmp_path / "normal", maker, index)
    view = _view(store)
    pause = view.halt_reason
    for leg_index in range(index, 3):
        cid = f"H-NEXT-{leg_index}"
        view.bind_hedge_order(intent_id, cid)
        leg = view.intent(intent_id).hedge_plan[leg_index]
        order = _order(cid, position=leg.position_id or "900000103", close=leg.is_close)
        _record(view, order, 1)
        orders.append(order)
        assert view.intent(intent_id).hedge_leg_index == leg_index + 1
        assert view.intent(intent_id).hedge_filled_ounces == leg_index + 1
        assert view.intent(intent_id).hedge_client_order_id is None
        assert view.halt_reason == pause  # This reducer does not authorize a restart.
    completed = view.intent(intent_id)
    assert completed.status is ObligationStatus.COMPLETED
    assert len(completed.hedge_order_ids) == 4 and len(completed.hedge_leg_order_ids) == 3
    assert all(not key.startswith("H-REJECT|") for key in view._state.seen_hedge_fills)
    assert _project(_reload(store), *orders) == 0


@pytest.mark.parametrize("fault", ["missing", "extra", "accepted", "filled", "seen", "quantity"])
def test_archived_rejection_requires_exact_original_zero_fill_native_evidence(
    tmp_path: Path, fault: str,
) -> None:
    store, _intent_id, orders = _archived(tmp_path / "conflict", False)
    if fault == "missing":
        orders.clear()
    elif fault == "extra":
        orders.append(_order("UNKNOWN", (), state="REJECTED"))
    elif fault == "accepted":
        orders[0] = _order("H-REJECT", ())
    elif fault == "filled":
        orders[0] = _order("H-REJECT")
    elif fault == "quantity":
        orders[0] = _order("H-REJECT", (), state="REJECTED", quantity="2")
    else:
        _view(store)._state.seen_hedge_fills.add("H-REJECT|unexpected")
    _assert_refused(store, *orders)


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("fact", ["fill", "ACCEPTED", "UNKNOWN", "DENIED", "CANCELED", "EXPIRED"])
def test_late_archived_contradiction_holds_without_crediting_the_current_leg(
    tmp_path: Path, maker: bool, fact: str,
) -> None:
    store, intent_id, _orders = _archived(tmp_path / "late", maker)
    view = _view(store)
    view.bind_hedge_order(intent_id, "H-RETRY")
    before = view.intent(intent_id)
    seen = set(view._state.seen_hedge_fills)
    revoked: list[bool] = []
    view._restart_pause_revoker = lambda: revoked.append(True)
    if fact == "fill":
        assert view.apply_hedge_fill(client_order_id="H-REJECT", trade_id="LATE", fill_ounces=D(1))
    else:
        status = (ObligationStatus(fact) if fact in {"ACCEPTED", "UNKNOWN"}
                  else ObligationStatus.REJECTED)
        view.update_hedge_status("H-REJECT", status, native_status=fact)
    assert revoked
    assert view.intent(intent_id) == replace(before, status=ObligationStatus.BLOCKED)
    assert view._state.seen_hedge_fills == seen
    assert not view.can_submit_source() and view.halt_reason
    if isinstance(store, MakerStateStore):
        assert store.next_pending_hedge() is None
    assert _reload(store)._to_payload() == store._to_payload()


def test_identical_archived_rejection_is_noop_and_old_id_cannot_be_reused(tmp_path: Path) -> None:
    store, intent_id, _orders = _archived(tmp_path / "duplicate", False)
    view = _view(store)
    before = deepcopy(store._to_payload()), store.path.read_bytes()
    view.update_hedge_status("H-REJECT", ObligationStatus.REJECTED, native_status="REJECTED")
    assert (store._to_payload(), store.path.read_bytes()) == before
    with pytest.raises(ValueError, match="unique"):
        view.bind_hedge_order(intent_id, "H-REJECT")
    view.bind_hedge_order(intent_id, "H-RETRY")
    view.update_hedge_status("H-RETRY", ObligationStatus.REJECTED)
    assert view.intent(intent_id).rejected_attempt == models.RejectedHedgeAttempt("H-REJECT", 0)
    assert view.intent(intent_id).status is ObligationStatus.REJECTED


@pytest.mark.parametrize("field,value", [
    ("client_order_id", ""), ("client_order_id", 1), ("client_order_id", "A|B"),
    ("leg_index", True), ("leg_index", -1), ("leg_index", "0"),
])
def test_rejected_attempt_identity_types_are_strict(field: str, value: Any) -> None:
    fields = {"client_order_id": "H-REJECT", "leg_index": 0, field: value}
    with pytest.raises(ValueError, match="rejected hedge attempt"):
        models.RejectedHedgeAttempt(**fields)


@pytest.mark.parametrize("fault", ["unplanned", "missing_cid", "wrong_index", "current_old",
                                   "extra_leg_id", "not_one_record"])
def test_attempt_must_describe_one_real_retained_plan_slot(tmp_path: Path, fault: str) -> None:
    store, intent_id, _orders = _archived(tmp_path / "shape", False)
    intent = _view(store).intent(intent_id)
    changes: dict[str, Any]
    if fault == "unplanned":
        changes = {"hedge_plan": ()}
    elif fault == "missing_cid":
        changes = {"rejected_attempt": models.RejectedHedgeAttempt("FOREIGN", 0)}
    elif fault == "wrong_index":
        changes = {"rejected_attempt": models.RejectedHedgeAttempt("H-REJECT", 1)}
    elif fault == "current_old":
        changes = {"hedge_client_order_id": "H-REJECT"}
    elif fault == "extra_leg_id":
        changes = {"hedge_order_ids": ("H-REJECT", "UNBOUND")}
    else:
        changes = {"rejected_attempt": [intent.rejected_attempt, intent.rejected_attempt]}
    with pytest.raises(ValueError, match="rejected hedge attempt"):
        replace(intent, **changes)


@pytest.mark.parametrize("maker", [False, True])
def test_new_format_retains_retry_budget_and_legacy_loader_does_not_rewrite(
    tmp_path: Path, maker: bool,
) -> None:
    store, intent_id, _orders = _archived(tmp_path / "new", maker)
    payload = json.loads(store.path.read_text())
    assert payload["schema_version"] == (7 if maker else 2)
    assert _view(_reload(store)).intent(intent_id).rejected_attempt == (
        models.RejectedHedgeAttempt("H-REJECT", 0)
    )
    legacy = _new(tmp_path / "old", maker)
    _seed(_view(legacy))
    raw = legacy._to_payload()
    views = cast(dict[str, dict[str, Any]], raw["directions"]).values() if maker else [raw]
    for value in views:
        value["schema_version"] = 1
        for intent_value in cast(dict[str, dict[str, Any]], value["hedge_intents"]).values():
            intent_value.pop("rejected_attempt")
    if maker:
        raw["schema_version"] = 5
    _persist_payload(legacy.path, raw)
    before = legacy.path.read_bytes()
    loaded = _reload(legacy)
    assert legacy.path.read_bytes() == before
    assert all(intent.rejected_attempt is None for intent in _view(loaded).intents())
    _view(loaded)._persist()
    assert json.loads(legacy.path.read_text())["schema_version"] == (7 if maker else 2)


@pytest.mark.parametrize("fault", ["missing_field", "old_version", "seen_fill", "extra_field",
                                   "wrong_type"])
def test_loader_rejects_incomplete_or_contradictory_new_attempt_record(
    tmp_path: Path, fault: str,
) -> None:
    store, intent_id, _orders = _archived(tmp_path / "invalid", False)
    raw = store._to_payload()
    value = cast(dict[str, dict[str, Any]], raw["hedge_intents"])[intent_id]
    if fault == "missing_field":
        del value["rejected_attempt"]
    elif fault == "old_version":
        raw["schema_version"] = 1
    elif fault == "seen_fill":
        raw["seen_hedge_fills"] = ["H-REJECT|BAD"]
    elif fault == "extra_field":
        value["rejected_attempt"]["second_attempt"] = True
    else:
        value["rejected_attempt"] = [value["rejected_attempt"]]
    with pytest.raises(ValueError, match="rejected hedge attempt|archived rejected"):
        JsonStateStore._from_payload(raw)
