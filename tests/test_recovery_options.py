"""An explicit startup review qualifies facts, never creates an automatic retry loop."""

import asyncio
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import test_hedge_projection as hedge_fixture
from nautilus_trader.model.orders import Order
from test_hedge_projection import D, _reload, _view
from test_source_projection import HEDGE, STRATEGY
from test_startup_settlement import _history, _project

import py000_nautilus.store as store_module
from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import ObligationStatus
from py000_nautilus.restart_recovery import (
    StartupRecoveryOptions,
    _settle_completed,
    _wait_for_retry_quote,
    capture_startup_receipt,
    describe_business_recovery,
)
from py000_nautilus.store import JsonStateStore


def _rejected(path: Path, maker: bool) -> tuple[
    JsonStateStore | MakerStateStore, list[Order], list[Order],
]:
    owner, sources, hedges = _history(
        path, maker=maker, legs=2, pending_leg=True, cycle_freeze=maker,
    )
    view = _view(owner)
    intent = view.intents()[0]
    view.bind_hedge_order(intent.intent_id, "H-REJECT")
    view.update_hedge_status("H-REJECT", ObligationStatus.REJECTED)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hedge_fixture, "STRATEGY", STRATEGY)
        hedges.append(hedge_fixture._order(
            "H-REJECT", (), quantity="2", side=intent.hedge_side, close=False,
            state="REJECTED",
        ))
    return owner, sources, hedges


@pytest.mark.parametrize("maker", [False, True])
def test_review_consumes_only_one_attempt_keeps_old_id_and_complete_prefix(
    tmp_path: Path, maker: bool,
) -> None:
    owner, sources, hedges = _rejected(tmp_path / "retry", maker)
    view = _view(owner)
    intent = view.intents()[0]
    original = deepcopy(view._state)
    assert not capture_startup_receipt(owner).eligible
    receipt = capture_startup_receipt(owner, StartupRecoveryOptions(True, "H-REJECT"))
    view.recover_for_start()
    _project(owner, sources, hedges, receipt)
    assert hedges[-1].venue_order_id is None  # Real native reject, not a fabricated acceptance.
    _settle_completed(owner, sources, hedges, receipt, resume_unbound=True,
                      rejected_retry_ready=True)
    after = view.intent(intent.intent_id)
    assert after.rejected_attempt is not None
    assert after.rejected_attempt.client_order_id == "H-REJECT"
    assert after.rejected_attempt.leg_index == after.hedge_leg_index == 1
    assert after.status is ObligationStatus.PENDING and after.hedge_client_order_id is None
    assert after.hedge_order_ids == intent.hedge_order_ids
    assert after.hedge_leg_order_ids == intent.hedge_order_ids[:-1]
    assert after.hedge_filled_ounces == intent.hedge_filled_ounces == D(2)
    assert view._state.seen_hedge_fills == original.seen_hedge_fills
    assert _view(_reload(owner)).intent(intent.intent_id) == after
    before = owner.path.read_bytes()
    _settle_completed(owner, sources, hedges, receipt, resume_unbound=True)
    assert owner.path.read_bytes() == before  # Repeated finalization does not consume again.
    view.bind_hedge_order(intent.intent_id, "H-NEW")
    assert view.intent(intent.intent_id).hedge_order_ids == (*intent.hedge_order_ids, "H-NEW")


@pytest.mark.parametrize("fault", ["no_review", "no_capacity", "expired", "wrong_cid",
                                   "second_pause", "publication_failed", "unplanned"])
@pytest.mark.parametrize("maker", [False, True])
def test_retry_refused_without_complete_current_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, maker: bool,
) -> None:
    owner, sources, hedges = _rejected(tmp_path / "refused", maker)
    view = _view(owner)
    if fault == "unplanned":
        # Reject an invented mapping, even when the caller supplies healthy capacity.
        intent = view.intents()[0]
        view._state.hedge_intents[intent.intent_id] = replace(
            intent, hedge_plan=(), hedge_leg_index=0, hedge_order_ids=("H-REJECT",),
        )
    options = StartupRecoveryOptions(True, "WRONG" if fault == "wrong_cid" else "H-REJECT")
    receipt = capture_startup_receipt(owner, None if fault == "no_review" else options)
    if fault == "expired":
        monkeypatch.setattr(time, "monotonic", lambda: 1e20)
    elif fault == "second_pause":
        view.freeze_source_submissions(view.source_freeze_reason or "new pause")
    elif fault == "publication_failed":
        receipt.fail_publication()
    before, disk = deepcopy(owner._to_payload()), owner.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        _settle_completed(owner, sources, hedges, receipt, resume_unbound=True,
                          rejected_retry_ready=fault != "no_capacity")
    assert owner._to_payload() == before and owner.path.read_bytes() == disk


@pytest.mark.parametrize("maker", [False, True])
def test_retry_candidate_rolls_back_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    owner, sources, hedges = _rejected(tmp_path / "rollback", maker)
    receipt = capture_startup_receipt(owner, StartupRecoveryOptions(True, "H-REJECT"))
    before, disk = deepcopy(owner._to_payload()), owner.path.read_bytes()

    def fail(*_: object) -> None:
        raise OSError("before publish")

    monkeypatch.setattr(_view(owner), "_persist", fail)
    with pytest.raises(OSError):
        _settle_completed(owner, sources, hedges, receipt, resume_unbound=True,
                          rejected_retry_ready=True)
    assert owner._to_payload() == before and owner.path.read_bytes() == disk


@pytest.mark.parametrize("maker", [False, True])
def test_published_retry_with_parent_sync_failure_invalidates_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    owner, sources, hedges = _rejected(tmp_path / "published", maker)
    receipt = capture_startup_receipt(owner, StartupRecoveryOptions(True, "H-REJECT"))
    original = _view(owner)._persist

    def publish_then_fail() -> None:
        original()
        raise ParentDirectorySyncError("published")

    monkeypatch.setattr(_view(owner), "_persist", publish_then_fail)
    with pytest.raises(ParentDirectorySyncError):
        _settle_completed(owner, sources, hedges, receipt, resume_unbound=True,
                          rejected_retry_ready=True)
    assert _view(_reload(owner)).intents()[0].rejected_attempt is not None
    with pytest.raises(RuntimeError, match="publication sync failure"):
        receipt.check(owner)


@pytest.mark.parametrize("maker", [False, True])
def test_inspection_does_not_write_or_claim_remote_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool,
) -> None:
    owner, _, _ = _rejected(tmp_path / "inspect", maker)
    before = owner.path.read_bytes()

    def unexpected_write(*_: object) -> None:
        pytest.fail("inspection wrote custody")

    monkeypatch.setattr(store_module, "_persist_payload", unexpected_write)
    report = describe_business_recovery(owner)
    assert report["native_and_venue_checked"] is False
    assert report["outcome"] == "RECOVERY_INSPECTED"
    assert "H-REJECT" in str(report["views"])
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("review,cid", [(False, "H"), (True, ""), (True, "a|b"), (1, None)])
def test_recovery_options_are_strict(review: bool, cid: str | None) -> None:
    with pytest.raises((TypeError, ValueError)):
        StartupRecoveryOptions(review, cid)


@pytest.mark.parametrize("maker", [False, True])
def test_old_hold_needs_explicit_review_and_still_requires_complete_execution(
    tmp_path: Path, maker: bool,
) -> None:
    owner, sources, hedges = _history(tmp_path / "held", maker=maker, cycle_freeze=maker)
    view = _view(owner)
    view.freeze_source_submissions("old operator pause")
    assert not capture_startup_receipt(owner).eligible
    receipt = capture_startup_receipt(owner, StartupRecoveryOptions(resume_held=True))
    view.recover_for_start()
    _project(owner, sources, hedges, receipt)
    native = [tuple(order.events) for order in sources + hedges]
    before = deepcopy(owner._to_payload())
    with pytest.raises(ValueError):
        _settle_completed(owner, sources, [], receipt, resume_unbound=True)
    assert owner._to_payload() == before
    _settle_completed(owner, sources, hedges, receipt, resume_unbound=True)
    assert view.can_submit_source() and view.cycle_evidence_complete()
    assert [tuple(order.events) for order in sources + hedges] == native


@pytest.mark.parametrize("maker", [False, True])
def test_second_rejection_cannot_obtain_a_second_replacement(tmp_path: Path, maker: bool) -> None:
    owner, sources, hedges = _rejected(tmp_path / "second", maker)
    view = _view(owner)
    intent = view.intents()[0]
    receipt = capture_startup_receipt(owner, StartupRecoveryOptions(True, "H-REJECT"))
    _settle_completed(owner, sources, hedges, receipt, resume_unbound=True,
                      rejected_retry_ready=True)
    view.bind_hedge_order(intent.intent_id, "H-NEW")
    view.update_hedge_status("H-NEW", ObligationStatus.REJECTED)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hedge_fixture, "STRATEGY", STRATEGY)
        hedges.append(hedge_fixture._order(
            "H-NEW", (), quantity="2", side=intent.hedge_side, close=False, state="REJECTED",
        ))
    for target in ("H-REJECT", "H-NEW"):
        receipt = capture_startup_receipt(owner, StartupRecoveryOptions(True, target))
        before = owner.path.read_bytes()
        with pytest.raises(ValueError, match="incomplete|already consumed"):
            _settle_completed(owner, sources, hedges, receipt, resume_unbound=True,
                              rejected_retry_ready=True)
        assert owner.path.read_bytes() == before


def test_a_second_capture_invalidates_the_original_review(tmp_path: Path) -> None:
    owner, _, _ = _rejected(tmp_path / "old-receipt", False)
    options = StartupRecoveryOptions(True, "H-REJECT")
    first = capture_startup_receipt(owner, options)
    second = capture_startup_receipt(owner, options)
    with pytest.raises(RuntimeError, match="revoked"):
        first.check(owner)
    second.check(owner)


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("outcome", ["quote", "deadline", "cancel", "new_pause"])
def test_first_quote_wait_is_bounded_read_only_and_revocable(
    tmp_path: Path, maker: bool, outcome: str,
) -> None:
    async def scenario() -> None:
        owner, _, hedges = _rejected(tmp_path / "first-quote", maker)
        receipt = capture_startup_receipt(owner, StartupRecoveryOptions(True, "H-REJECT"))
        before = owner.path.read_bytes()
        intents = _view(owner).intents()
        facts = SimpleNamespace(quote=None)
        cache = SimpleNamespace(order=lambda _: hedges[-1], quote_tick=lambda _: facts.quote)
        if outcome == "deadline":
            receipt.retry_deadline = time.monotonic() + .05
        task = asyncio.create_task(_wait_for_retry_quote(cast(Any, cache), owner, receipt, HEDGE))
        await asyncio.sleep(.01)
        assert not task.done() and owner.path.read_bytes() == before
        if outcome == "quote":
            # Arrival only; this helper certifies no price or execution fact.
            facts.quote = object()
            await asyncio.wait_for(task, .5)
        elif outcome == "deadline":
            with pytest.raises(TimeoutError, match="first MT5 quote"):
                await task
        elif outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            _view(owner).freeze_source_submissions("new pause while awaiting PUB")
            with pytest.raises(RuntimeError, match="revoked"):
                await task
        assert _view(owner).intents() == intents
        assert hedges[-1].filled_qty == 0 and not hedges[-1].trade_ids
        if outcome != "new_pause":
            assert owner.path.read_bytes() == before

    asyncio.run(scenario())
