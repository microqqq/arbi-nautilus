"""One allocation owner for Maker bid/ask and the bidirectional Taker view."""

import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import py000_nautilus.store as store_module
from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent
from py000_nautilus.maker_store import MakerStateStore, maker_legacy_paths, maker_state_path
from py000_nautilus.models import BusinessOrderSide, HedgeIntent, HedgeLeg, ObligationStatus
from py000_nautilus.store import JsonStateStore

D = Decimal
BUY = BusinessOrderSide.BUY
SELL = BusinessOrderSide.SELL
IDS = ("MakerStrategy-MAKER", "TakerStrategy-TAKER")


def _owner(prefix: Path, **kwargs: Any) -> MakerStateStore:
    return MakerStateStore(prefix, "SOURCE.BITFINEX", "HEDGE.MT5",
                           shared_strategy_ids=IDS, **kwargs)


def _begin(view: JsonStateStore, cid: str, side: BusinessOrderSide,
           amount: str = "2", **route: Any) -> None:
    attributes: dict[str, Any] = dict(
        source_account_id="BITFINEX-001", source_client_id="BITFINEX",
        hedge_account_id="MT5-001", hedge_client_id="MT5",
    )
    attributes.update(route)
    view.begin_source(cid, side, D(amount), **attributes)


def _fill(view: JsonStateStore, cid: str, side: BusinessOrderSide,
          amount: str = "2", trade: str | None = None) -> HedgeIntent | None:
    trade = trade or f"T-{cid}"
    return view.reserve_source_fill(
        fill_key=f"{cid}|V-{cid}|{trade}", client_order_id=cid, trade_id=trade,
        source_side=side, fill_ounces=D(amount),
    )


def _complete(view: JsonStateStore, intent: HedgeIntent, cid: str) -> None:
    view.bind_hedge_order(intent.intent_id, cid)
    assert view.apply_hedge_fill(client_order_id=cid, trade_id=f"D-{cid}",
                               fill_ounces=intent.hedge_quantity_ounces)


def test_shared_file_binds_three_views_to_two_distinct_strategy_ids(tmp_path: Path) -> None:
    prefix = tmp_path / "shared"
    owner = _owner(prefix)
    assert owner.path == Path(f"{prefix}.shared.json")
    assert not owner.path.exists()
    assert len(owner.stores) == 2
    bid, ask, taker = owner.all_views()
    assert taker is owner.taker_store
    assert owner.strategy_id_for(bid) == owner.strategy_id_for(ask) == IDS[0]
    assert owner.strategy_id_for(taker) == IDS[1]
    _begin(taker, "T", BUY)
    payload = json.loads(owner.path.read_text())
    assert payload["schema_version"] == 9 and payload["kind"] == "shared"
    assert payload["strategy_ids"] == {"bid": IDS[0], "ask": IDS[0], "taker": IDS[1]}
    assert set(payload["directions"]) == {"bid", "ask", "taker"}
    before = owner.path.read_bytes()
    assert _owner(prefix)._snapshot() == owner._snapshot()
    assert owner.path.read_bytes() == before
    assert not Path(f"{prefix}.maker.json").exists()


@pytest.mark.parametrize("taker_side", [BUY, SELL])
def test_shared_taker_reuses_the_original_reducer_for_both_sides(
    tmp_path: Path, taker_side: BusinessOrderSide,
) -> None:
    owner = _owner(tmp_path / "taker")
    _, _, taker = owner.all_views()
    _begin(taker, "T", taker_side)
    intent = _fill(taker, "T", taker_side)
    assert intent is not None and intent.hedge_quantity_ounces == 2
    assert intent.hedge_side is (SELL if taker_side is BUY else BUY)
    assert owner.first_unfinished_hedge() == (taker, intent)
    assert owner.next_pending_hedge() is None  # Taker is not an ask direction.
    assert taker.hedge_dispatch_ready(intent.intent_id)
    assert not taker.release_completed_cycle()
    _complete(taker, intent, "HT")
    assert taker.release_completed_cycle()
    assert all(view.can_submit_source() for view in owner.all_views())


def test_fractional_fills_share_one_route_residual_and_one_allocation_order(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path / "residual")
    bid, ask, taker = owner.all_views()
    for view, cid, side in ((bid, "B", BUY), (ask, "A", SELL), (taker, "T", BUY)):
        _begin(view, cid, side, "1")
    assert _fill(bid, "B", BUY, "0.4") is None
    assert _fill(ask, "A", SELL, "0.1") is None
    intent = _fill(taker, "T", BUY, "0.3")
    assert intent is not None and intent.hedge_quantity_ounces == 1
    assert [view.rounding_residual_ounces for view in owner.all_views()] == [D(0), D(0), D("-0.4")]
    payload = json.loads(owner.path.read_text())
    assert [item["allocated_ounces"] for item in payload["allocations"]] == ["0", "0", "1"]
    assert all(state["net_unhedged_ounces"] == "0"
               for state in payload["directions"].values())
    assert len([item for view in owner.all_views() for item in view.intents()]) == 1
    assert _owner(tmp_path / "residual")._snapshot() == owner._snapshot()


def test_shared_lane_holds_all_three_legs_including_each_unbound_gap(tmp_path: Path) -> None:
    owner = _owner(tmp_path / "lane")
    bid, ask, taker = owner.all_views()
    for view, cid, side in ((taker, "T", SELL), (bid, "B", BUY), (ask, "A", SELL)):
        _begin(view, cid, side, "3")
    intents = [_fill(taker, "T", SELL, "3"), _fill(bid, "B", BUY, "3"),
               _fill(ask, "A", SELL, "3")]
    assert all(intent is not None for intent in intents)
    first, second, third = intents
    assert first is not None and second is not None and third is not None
    taker.bind_hedge_plan(first.intent_id, (
        HedgeLeg(BUY, D(1), "OLD-A", SELL, D(1)),
        HedgeLeg(BUY, D(1), "OLD-B", SELL, D(1)), HedgeLeg(BUY, D(1)),
    ))
    for index in range(3):
        selected = owner.first_unfinished_hedge()
        assert selected is not None and selected[0] is taker
        assert selected[1].hedge_leg_index == index
        assert selected[1].hedge_client_order_id is None
        assert taker.hedge_dispatch_ready(first.intent_id)
        assert not bid.hedge_dispatch_ready(second.intent_id)
        assert not ask.hedge_dispatch_ready(third.intent_id)
        assert owner.next_pending_hedge() is None
        taker.bind_hedge_order(first.intent_id, f"HT-{index}")
        assert owner.first_unfinished_hedge() == (taker, taker.intent(first.intent_id))
        taker.apply_hedge_fill(client_order_id=f"HT-{index}", trade_id=f"D-{index}",
                               fill_ounces=D(1))
    assert taker.intent(first.intent_id).status is ObligationStatus.COMPLETED
    assert owner.first_unfinished_hedge() == (bid, second)
    assert bid.hedge_dispatch_ready(second.intent_id)
    maker_pending = owner.next_pending_hedge()
    assert maker_pending is not None and maker_pending[1] == second
    _complete(bid, second, "HB")
    assert owner.first_unfinished_hedge() == (ask, third)
    _complete(ask, third, "HA")
    assert owner.first_unfinished_hedge() is None
    assert taker.release_completed_cycle()


@pytest.mark.parametrize("ids", [("only",), ("same", "same"), ("", "T"),
                                 ("M", " "), ("M", 1), ["M", "T"]])
def test_shared_identity_configuration_is_explicit_and_distinct(tmp_path: Path, ids: Any) -> None:
    with pytest.raises(ValueError, match="strategy IDs"):
        MakerStateStore(tmp_path / "bad", "SOURCE.BITFINEX", "HEDGE.MT5",
                        shared_strategy_ids=ids)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("fault", ["strategy", "extra_strategy", "missing_taker", "extra_view",
                                   "old_schema", "kind", "provenance", "taker_side", "residual"])
def test_shared_reload_rejects_conflicting_bindings_or_third_view_facts_without_writes(
    tmp_path: Path, fault: str,
) -> None:
    prefix = tmp_path / "bad-payload"
    owner = _owner(prefix)
    taker = owner.all_views()[2]
    _begin(taker, "T", BUY)
    assert _fill(taker, "T", BUY) is not None
    payload = json.loads(owner.path.read_text())
    state = payload["directions"]["taker"]
    if fault == "strategy":
        payload["strategy_ids"]["taker"] = IDS[0]
    elif fault == "extra_strategy":
        payload["strategy_ids"]["other"] = "Other"
    elif fault == "missing_taker":
        del payload["directions"]["taker"]
    elif fault == "extra_view":
        payload["directions"]["other"] = state
    elif fault == "old_schema":
        payload["schema_version"] = 7
    elif fault == "kind":
        payload["kind"] = "maker"
    elif fault == "provenance":
        state["source_freeze_reason"] = None
    elif fault == "taker_side":
        next(iter(state["hedge_intents"].values()))["source_side"] = "SELL"
    else:
        state["net_unhedged_ounces"] = "0.1"
    owner.path.write_text(json.dumps(payload))
    before = owner.path.read_bytes()
    with pytest.raises(ValueError):
        _owner(prefix)
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("old_file", ["maker", "bid", "ask"])
def test_shared_never_merges_or_overwrites_standalone_and_legacy_files(
    tmp_path: Path, old_file: str,
) -> None:
    prefix = tmp_path / "existing"
    path = {"maker": maker_state_path(prefix), "bid": maker_legacy_paths(prefix)[0],
            "ask": maker_legacy_paths(prefix)[1]}[old_file]
    path.write_bytes(b"not parsed or modified")
    with pytest.raises(ValueError, match="legacy|standalone"):
        _owner(prefix)
    assert path.read_bytes() == b"not parsed or modified"
    assert not Path(f"{prefix}.shared.json").exists()


def test_standalone_reader_rejects_shared_payload_and_preserves_default_apis(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "standalone"
    owner = MakerStateStore(prefix, "SOURCE.BITFINEX", "HEDGE.MT5")
    assert owner.shared_strategy_ids is None and owner.taker_store is None
    assert owner.all_views() == tuple(owner.stores.values())
    assert all(owner.strategy_id_for(view) is None and view.hedge_dispatch_ready("unknown")
               and not view.release_completed_cycle() for view in owner.all_views())
    shared = _owner(tmp_path / "shared")
    with pytest.raises(ValueError, match="belong"):
        shared.strategy_id_for(owner.all_views()[0])
    shared.freeze_sources("persist")
    owner.path.write_bytes(shared.path.read_bytes())
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        MakerStateStore(prefix, "SOURCE.BITFINEX", "HEDGE.MT5")
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["source_cid", "source_trade", "hedge_cid", "hedge_trade",
                                   "source_as_hedge", "intent_id", "maker_side"])
def test_third_view_conflicts_roll_back_the_entire_owner(tmp_path: Path, fault: str) -> None:
    prefix = tmp_path / "identities"
    owner = _owner(prefix)
    bid, _, taker = owner.all_views()
    _begin(bid, "B", BUY)
    if fault not in {"source_cid", "maker_side"}:
        _begin(taker, "T", SELL)
    first = second = None
    if fault not in {"source_cid", "maker_side"}:
        first = _fill(bid, "B", BUY)
        assert first is not None
        if fault != "source_trade":
            second = _fill(taker, "T", SELL)
            assert second is not None
            bid.bind_hedge_order(first.intent_id, "HB")
    if fault == "hedge_trade":
        assert second is not None
        taker.bind_hedge_order(second.intent_id, "HT")
        bid.apply_hedge_fill(client_order_id="HB", trade_id="SAME-TRADE", fill_ounces=D(2))
    previous, disk = owner._snapshot(), owner.path.read_bytes()
    with pytest.raises(ValueError, match="identity|direction"):
        if fault == "source_cid":
            _begin(taker, "B", SELL)
        elif fault == "maker_side":
            _begin(owner.all_views()[1], "A", BUY)
        elif fault == "source_trade":
            _fill(taker, "T", SELL, trade="T-B")
        elif fault == "hedge_trade":
            taker.apply_hedge_fill(client_order_id="HT", trade_id="SAME-TRADE", fill_ounces=D(2))
        else:
            assert first is not None and second is not None
            if fault == "intent_id":
                # A malformed candidate is rejected before atomic publication.
                taker._state.hedge_intents.pop(second.intent_id)
                taker._state.hedge_intents[first.intent_id] = replace(
                    second, intent_id=first.intent_id,
                )
                owner._persist()
            else:
                cid = "B" if fault == "source_as_hedge" else "HB"
                taker.bind_hedge_order(second.intent_id, cid)
    assert owner._snapshot() == previous == _owner(prefix)._snapshot()
    assert owner.path.read_bytes() == disk


@pytest.mark.parametrize("external", ["before", "after", "same", "halt"])
def test_third_view_external_pause_never_becomes_releasable_cycle(
    tmp_path: Path, external: str,
) -> None:
    owner = _owner(tmp_path / "pause")
    bid, _, taker = owner.all_views()
    _begin(bid, "B", BUY)
    if external == "before":
        taker.freeze_source_submissions("external")
    first = _fill(bid, "B", BUY)
    assert first is not None
    if external in {"after", "same"}:
        reason = taker.source_freeze_reason if external == "same" else "external"
        assert reason is not None
        revoked: list[bool] = []
        taker._restart_pause_revoker = lambda: revoked.append(True)
        taker.freeze_source_submissions(reason)
        assert revoked == [True]
    elif external == "halt":
        taker.mark_source_unknown("no-source", "peer account UNKNOWN")
    _complete(bid, first, "HB")
    before = owner.path.read_bytes()
    assert not taker.release_completed_cycle()
    assert owner.path.read_bytes() == before
    assert any(view.source_freeze_reason is not None for view in owner.all_views())
    if external != "halt":
        assert not owner.cycle_freeze_only
    assert not _owner(tmp_path / "pause").all_views()[2].release_completed_cycle()


@pytest.mark.parametrize("operation", ["fill", "external", "release"])
@pytest.mark.parametrize("after_replace", [False, True])
def test_shared_publication_rolls_back_or_retains_all_views_as_one_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, after_replace: bool,
) -> None:
    prefix = tmp_path / "atomic"
    owner = _owner(prefix)
    bid, ask, taker = owner.all_views()
    for view, cid, side in ((bid, "B", BUY), (ask, "A", SELL), (taker, "T", BUY)):
        _begin(view, cid, side, "0.4")
    assert _fill(bid, "B", BUY, "0.4") is None
    assert _fill(ask, "A", SELL, "0.4") is None
    if operation == "release":
        taker.update_source_status("T", "REJECTED")
    previous, disk = owner._snapshot(), owner.path.read_bytes()
    candidates: list[bytes] = []

    def fail(source: Path, destination: Path) -> None:
        candidates.append(source.read_bytes())
        if after_replace:
            os.replace(source, destination)
            raise ParentDirectorySyncError("published without parent sync")
        raise OSError("not published")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "replace_and_sync_parent", fail)
        with pytest.raises(ParentDirectorySyncError if after_replace else OSError):
            if operation == "fill":
                _fill(taker, "T", BUY, "0.4")
            elif operation == "external":
                taker.freeze_source_submissions("external")
            else:
                assert taker.release_completed_cycle()
    assert len(candidates) == 1
    assert owner.path.read_bytes() == (candidates[0] if after_replace else disk)
    assert owner._snapshot() == _owner(prefix)._snapshot()
    assert (owner._snapshot() != previous) is after_replace
    if after_replace or operation == "external":
        assert not owner.cycle_freeze_only
        assert not taker.release_completed_cycle()
        owner._persist()
        assert not json.loads(owner.path.read_text())["cycle_freeze_only"]
    elif operation == "release":
        assert taker.release_completed_cycle()  # A pre-publication error is retryable.


def test_shared_fill_freezes_all_views_in_one_write_and_duplicate_is_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _owner(tmp_path / "single-publication")
    taker = owner.all_views()[2]
    _begin(taker, "T", BUY)
    publications: list[dict[str, Any]] = []

    def observe(source: Path, destination: Path) -> None:
        publications.append(json.loads(source.read_text()))
        replace_and_sync_parent(source, destination)

    monkeypatch.setattr(store_module, "replace_and_sync_parent", observe)
    first = _fill(taker, "T", BUY)
    assert first is not None and len(publications) == 1
    payload = publications[0]
    assert payload["cycle_freeze_only"] is True
    assert len({view["source_freeze_reason"] for view in payload["directions"].values()}) == 1
    assert all(view["source_freeze_reason"] for view in payload["directions"].values())
    assert _fill(taker, "T", BUY) is None and len(publications) == 1
    assert _fill(taker, "UNKNOWN", BUY) is None and len(publications) == 1
    assert owner.first_unfinished_hedge() == (taker, first)
    assert not owner.all_views()[0].hedge_dispatch_ready("UNKNOWN")


@pytest.mark.parametrize("different_route", [False, True])
def test_shared_carry_is_one_route_budget_not_one_budget_per_strategy(
    tmp_path: Path, different_route: bool,
) -> None:
    route = ("BITFINEX-001", "BITFINEX", "MT5-001", "MT5")
    options = dict(residual_limit_ounces=D("0.5"), carry_route=route)
    prefix = tmp_path / "carry"
    owner = _owner(prefix, **options)
    bid, _, taker = owner.all_views()
    _begin(bid, "B", BUY, "0.4")
    _begin(taker, "T", BUY, "0.4", hedge_account_id="MT5-OTHER" if different_route else route[2])
    assert _fill(bid, "B", BUY, "0.4") is None
    second = _fill(taker, "T", BUY, "0.4")
    if different_route:
        assert second is None and len(owner.residuals()) == 2
        assert owner.carry_residual_ounces == D("0.4")
        assert not owner.source_balance_is_admissible()
        assert not taker.release_completed_cycle()
    else:
        assert second is not None and second.hedge_quantity_ounces == 1
        assert owner.carry_residual_ounces == D("-0.2")
        assert not taker.release_completed_cycle()
        _complete(taker, second, "HT")
        assert taker.release_completed_cycle()
        assert owner.carry_residual_ounces == D("-0.2")  # Never cleared at release.
    assert _owner(prefix, **options)._snapshot() == owner._snapshot()


def test_blocked_first_shared_intent_does_not_yield_lane_to_a_later_maker_fill(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path / "blocked-lane")
    bid, _, taker = owner.all_views()
    _begin(bid, "B", BUY)
    _begin(taker, "T", SELL)
    first, second = _fill(taker, "T", SELL), _fill(bid, "B", BUY)
    assert first is not None and second is not None
    taker.block_hedge_intent(first.intent_id, "ticket facts incomplete")
    before = owner._snapshot(), owner.path.read_bytes()
    assert owner.first_unfinished_hedge() == (taker, taker.intent(first.intent_id))
    assert not bid.hedge_dispatch_ready(second.intent_id)
    assert owner.next_pending_hedge() is None
    assert taker.intent(first.intent_id).status is ObligationStatus.BLOCKED
    assert bid.intent(second.intent_id).status is ObligationStatus.PENDING
    assert (owner._snapshot(), owner.path.read_bytes()) == before
