"""Current-startup receipts and atomic settlement; no venue or dispatch authority."""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import test_hedge_projection as hedge_fixture
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.orders import Order
from test_hedge_projection import D, _new, _observe, _record, _reload, _view
from test_source_projection import STRATEGY, _begin
from test_source_projection import _order as _source_order

import py000_nautilus.store as store_module
from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent
from py000_nautilus.hedge_projection import project_hedge_fills
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import BusinessOrderSide, HedgeLeg, ObligationStatus
from py000_nautilus.restart_recovery import (
    _HELD,
    _settle_completed,
    _StartupReceipt,
    capture_startup_receipt,
)
from py000_nautilus.source_projection import project_source_fills
from py000_nautilus.store import JsonStateStore


def _history(
    path: Path, *, maker: bool = False, legs: int = 1, planned: bool = True,
    pending_leg: bool = False,
) -> tuple[JsonStateStore | MakerStateStore, list[Order], list[Order]]:
    owner = _new(path, maker)
    sources: list[Order] = []
    hedges: list[Order] = []
    # Synthetic eligible Maker state exercises its atomic owner only. Ordinary
    # Maker fill freezes are retained and tested separately, never waived here.
    for side in ((BusinessOrderSide.BUY, BusinessOrderSide.SELL) if maker
                 else (BusinessOrderSide.BUY,)):
        view = _view(owner, side)
        cid = f"S-{side.value}"
        source = _source_order(cid, (str(2 * legs),), side=side, quantity=str(2 * legs))
        _begin(view, source)
        fill = next(event for event in source.events if isinstance(event, OrderFilled))
        intent = view._reserve_source_fill(
            fill_key=f"{cid}|{fill.venue_order_id.value}|{fill.trade_id.value}",
            client_order_id=cid, trade_id=fill.trade_id.value,
            source_side=side, fill_ounces=fill.last_qty.as_decimal(),
        )
        assert intent is not None
        view._persist()
        if planned:
            view.bind_hedge_plan(intent.intent_id, tuple(
                HedgeLeg(intent.hedge_side, D(2), f"90000010{index}", side, D(2))
                if index < legs - 1 else HedgeLeg(intent.hedge_side, D(2))
                for index in range(legs)
            ))
        sources.append(source)
        for index in range(legs):
            if pending_leg and index == legs - 1:
                break
            hedge_cid = f"H-{side.value}-{index}"
            view.bind_hedge_order(intent.intent_id, hedge_cid)
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(hedge_fixture, "STRATEGY", STRATEGY)
                hedge = hedge_fixture._order(
                    hedge_cid, ("2",), quantity="2", side=intent.hedge_side,
                    close=index < legs - 1, position=f"90000010{index}",
                )
            hedges.append(hedge)
            if index < legs - 1:
                _record(view, hedge, 1)
        view.update_hedge_status(hedge_cid, ObligationStatus.ACCEPTED)
        record = view.source_order(cid)
        assert record is not None
        view._state.source_orders[cid] = replace(record, status="ACCEPTED")
        view._state.active_source_order_id = cid
        view._persist()
    return owner, sources, hedges


def _project(
    owner: JsonStateStore | MakerStateStore, sources: list[Order], hedges: list[Order],
    receipt: _StartupReceipt,
) -> None:
    views = tuple(owner.stores.values()) if isinstance(owner, MakerStateStore) else (owner,)
    assert project_source_fills(
        owner, sources, source_instrument_id=hedge_fixture.SOURCE,
        trader_id=hedge_fixture.TRADER, strategy_id=STRATEGY, reason=_HELD,
    ) == 0
    pauses = tuple((view.halt_reason, view.source_freeze_reason) for view in views)
    project_hedge_fills(
        owner, hedges, hedge_instrument_id=hedge_fixture.HEDGE,
        trader_id=hedge_fixture.TRADER, strategy_id=STRATEGY, reason=_HELD,
    )
    receipt.claim_projected(pauses)


def _prepared(
    path: Path, *, maker: bool = False, legs: int = 1, planned: bool = True,
) -> tuple[JsonStateStore | MakerStateStore, list[Order], list[Order], _StartupReceipt]:
    owner, sources, hedges = _history(path, maker=maker, legs=legs, planned=planned)
    receipt = capture_startup_receipt(owner)
    assert receipt.eligible
    for view in (owner.stores.values() if isinstance(owner, MakerStateStore) else (owner,)):
        view.recover_for_start()
    _project(owner, sources, hedges, receipt)
    return owner, sources, hedges, receipt


@pytest.mark.parametrize("maker", [False, True])
def test_restart_records_only_a_new_durably_published_hold(tmp_path: Path, maker: bool) -> None:
    owner = _new(tmp_path / "receipt", maker)
    view = _view(owner)
    view.begin_source("S", BusinessOrderSide.BUY, D(2))
    recorded: list[str] = []
    view._restart_halt_recorder = recorded.append
    reason = view.recover_for_start()
    assert reason and recorded == [reason]
    view.recover_for_start()
    assert recorded == [reason]
    if isinstance(owner, MakerStateStore):
        assert _view(owner, BusinessOrderSide.SELL).halt_reason is None


@pytest.mark.parametrize("fault", ["old_halt", "old_freeze", "source_unknown",
                                   "source_rejected", "intent_unknown", "intent_blocked",
                                   "intent_rejected"])
def test_capture_does_not_adopt_old_text_or_unresolved_status(tmp_path: Path, fault: str) -> None:
    owner, _, _ = _history(tmp_path / "old")
    view = _view(owner)
    intent = view.intents()[0]
    if fault == "old_halt":
        view._state.halt_reason = _HELD  # Internal-looking text is not provenance.
    elif fault == "old_freeze":
        view.freeze_source_submissions(_HELD)
    elif fault.startswith("source_"):
        cid = intent.source_client_order_id
        record = view.source_order(cid)
        assert record is not None
        view._state.source_orders[cid] = replace(record, status=fault.split("_")[1].upper())
    else:
        view._state.hedge_intents[intent.intent_id] = replace(
            intent, status=ObligationStatus(fault.split("_")[1].upper()),
        )
    view._persist()
    before = owner.path.read_bytes()
    receipt = capture_startup_receipt(owner)
    assert not receipt.eligible and not receipt.halts and not receipt.freezes
    assert owner.path.read_bytes() == before
    assert not capture_startup_receipt(_reload(owner)).eligible


@pytest.mark.parametrize("maker", [False, True])
def test_fresh_receipt_settles_once_and_reload_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    owner, sources, hedges, receipt = _prepared(tmp_path / "final", maker=maker)
    before = deepcopy(owner._to_payload())
    native = [tuple(order.events) for order in (*sources, *hedges)]
    publications = _observe(monkeypatch)
    _settle_completed(owner, sources, hedges, receipt)
    assert len(publications) == 1
    views = owner.stores.values() if isinstance(owner, MakerStateStore) else (owner,)
    for view in views:
        assert view.can_submit_source() and view.cycle_evidence_complete()
        assert all(intent.status is ObligationStatus.COMPLETED
                   and intent.hedge_leg_index == 1 and intent.hedge_client_order_id is None
                   and intent.hedge_leg_filled_ounces == 0 for intent in view.intents())
    assert owner._to_payload().get("allocations") == before.get("allocations")
    assert [tuple(order.events) for order in (*sources, *hedges)] == native
    _settle_completed(owner, sources, hedges, receipt)
    loaded = _reload(owner)
    _settle_completed(loaded, sources, hedges, capture_startup_receipt(loaded))
    assert len(publications) == 1 and loaded._to_payload() == owner._to_payload()


@pytest.mark.parametrize("planned", [False, True])
def test_final_leg_retains_unique_history_and_legacy_current_shape(
    tmp_path: Path, planned: bool,
) -> None:
    owner, sources, hedges, receipt = _prepared(
        tmp_path / "legs", legs=3 if planned else 1, planned=planned,
    )
    view = _view(owner)
    before = view.intents()[0]
    _settle_completed(owner, sources, list(reversed(hedges)), receipt)
    after = view.intents()[0]
    assert after.status is ObligationStatus.COMPLETED
    assert after.hedge_order_ids == before.hedge_order_ids and after.hedge_plan == before.hedge_plan
    assert after.hedge_filled_ounces == before.hedge_filled_ounces == after.hedge_quantity_ounces
    assert after.hedge_client_order_id == (None if planned else before.hedge_client_order_id)
    assert after.hedge_leg_index == (3 if planned else 0)


@pytest.mark.parametrize("pause", ["halt", "freeze"])
def test_a_new_external_pause_cannot_be_cleared_by_a_valid_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pause: str,
) -> None:
    owner, sources, hedges, receipt = _prepared(tmp_path / "external")
    setattr(_view(owner)._state, "halt_reason" if pause == "halt" else "source_freeze_reason",
            "new external account uncertainty")
    _view(owner)._persist()
    before, disk = deepcopy(owner._to_payload()), owner.path.read_bytes()
    publications = _observe(monkeypatch)
    with pytest.raises(ValueError, match="not owned"):
        _settle_completed(owner, sources, hedges, receipt)
    assert not publications and owner._to_payload() == before and owner.path.read_bytes() == disk


def test_owned_pause_is_not_inherited_by_a_reload_or_different_store(tmp_path: Path) -> None:
    owner, sources, hedges, receipt = _prepared(tmp_path / "identity")
    loaded = _reload(owner)
    with pytest.raises(ValueError, match="another business store"):
        _settle_completed(loaded, sources, hedges, receipt)
    new_receipt = capture_startup_receipt(loaded)
    assert not new_receipt.eligible
    with pytest.raises(ValueError, match="does not authorize"):
        _settle_completed(loaded, sources, hedges, new_receipt)


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("after_replace", [False, True])
def test_final_publication_failure_preserves_published_held_facts_or_final_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, after_replace: bool,
) -> None:
    owner, sources, hedges, receipt = _prepared(tmp_path / "atomic", maker=maker)
    before, disk = deepcopy(owner._to_payload()), owner.path.read_bytes()
    attempts = 0

    def fail(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if after_replace:
            os.replace(source, destination)
            raise ParentDirectorySyncError("final candidate published; directory sync failed")
        raise OSError("final candidate not published")

    monkeypatch.setattr(store_module, "replace_and_sync_parent", fail)
    with pytest.raises(ParentDirectorySyncError if after_replace else OSError):
        _settle_completed(owner, sources, hedges, receipt)
    assert attempts == 1 and receipt.publication_failed is after_replace
    assert owner._to_payload() == _reload(owner)._to_payload()
    assert (owner._to_payload() != before) is after_replace
    assert (owner.path.read_bytes() != disk) is after_replace
    monkeypatch.setattr(store_module, "replace_and_sync_parent", replace_and_sync_parent)
    if after_replace:
        with pytest.raises(RuntimeError, match="invalid after publication"):
            _settle_completed(owner, sources, hedges, receipt)
    else:
        _settle_completed(owner, sources, hedges, receipt)
    assert owner._to_payload().get("allocations") == before.get("allocations")


def test_candidate_validation_failure_restores_both_maker_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, sources, hedges, receipt = _prepared(tmp_path / "candidate", maker=True)
    assert isinstance(owner, MakerStateStore)
    before, disk = deepcopy(owner._to_payload()), owner.path.read_bytes()
    publications = _observe(monkeypatch)

    def fail() -> None:
        assert all(view.halt_reason is None and view.active_source_order_id is None
                   for view in owner.stores.values())
        raise ValueError("candidate-only validation failed")

    with monkeypatch.context() as patch:
        patch.setattr(owner, "_validate", fail)
        with pytest.raises(ValueError, match="candidate-only"):
            _settle_completed(owner, sources, hedges, receipt)
    assert not publications and owner._to_payload() == before and owner.path.read_bytes() == disk
    _settle_completed(owner, sources, hedges, receipt)


def test_a_fully_filled_old_leg_does_not_authorize_an_unbound_future_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, sources, hedges = _history(tmp_path / "future", legs=2, pending_leg=True)
    receipt = capture_startup_receipt(owner)
    _view(owner).recover_for_start()
    _project(owner, sources, hedges, receipt)
    before, disk = deepcopy(owner._to_payload()), owner.path.read_bytes()
    publications = _observe(monkeypatch)
    with pytest.raises(ValueError, match="incomplete or unbound"):
        _settle_completed(owner, sources, hedges, receipt)
    assert not publications and owner._to_payload() == before and owner.path.read_bytes() == disk


def test_partial_native_current_cannot_reach_final_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, sources, hedges = _history(tmp_path / "partial")
    receipt = capture_startup_receipt(owner)
    _view(owner).recover_for_start()
    with monkeypatch.context() as patch:
        patch.setattr(hedge_fixture, "STRATEGY", STRATEGY)
        partial = hedge_fixture._order(
            hedges[0].client_order_id.value, ("1",), quantity="2", close=False,
            position="900000100",
        )
    before, disk = deepcopy(owner._to_payload()), owner.path.read_bytes()
    publications = _observe(monkeypatch)
    with pytest.raises(ValueError, match="complete current FOK"):
        _project(owner, sources, [partial], receipt)
    assert not publications and owner._to_payload() == before and owner.path.read_bytes() == disk


@pytest.mark.parametrize("after_replace", [False, True])
def test_restart_hold_receipt_requires_successful_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_replace: bool,
) -> None:
    owner, _, _ = _history(tmp_path / "restart-publish")
    receipt = capture_startup_receipt(owner)

    def fail(source: Path, destination: Path) -> None:
        if after_replace:
            os.replace(source, destination)
            raise ParentDirectorySyncError("restart hold published")
        raise OSError("restart hold not published")

    monkeypatch.setattr(store_module, "replace_and_sync_parent", fail)
    with pytest.raises(ParentDirectorySyncError if after_replace else OSError):
        _view(owner).recover_for_start()
    assert not receipt.halts and receipt.publication_failed is after_replace
