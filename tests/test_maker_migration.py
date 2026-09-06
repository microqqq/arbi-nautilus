"""Offline explicit legacy checkpoints; no invented per-fill history or recovery."""

import json
import os
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import py000_nautilus.store as store_module
from py000_nautilus.durability import ParentDirectorySyncError, create_and_sync_parent
from py000_nautilus.maker_migration import main, migrate_maker_state
from py000_nautilus.maker_store import MakerStateStore, maker_legacy_paths, maker_state_path
from py000_nautilus.models import BusinessOrderSide, HedgeLeg, ObligationStatus, SourceDirection
from py000_nautilus.store import JsonStateStore

D = Decimal
BUY, SELL = BusinessOrderSide.BUY, BusinessOrderSide.SELL
LONG, SHORT = SourceDirection.LONG, SourceDirection.SHORT
SOURCE, HEDGE = "SOURCE.BITFINEX", "HEDGE.MT5"


def _fill(
    view: JsonStateStore, cid: str, side: BusinessOrderSide, quantity: str, trade: str,
) -> Any:
    return view.reserve_source_fill(
        fill_key=f"{cid}|V-{cid}|{trade}", client_order_id=cid, trade_id=trade,
        source_side=side, fill_ounces=D(quantity),
    )

def _begin(view: JsonStateStore, cid: str, side: BusinessOrderSide, **kwargs: Any) -> None:
    route: dict[str, Any] = dict(source_account_id="BITFINEX-1", source_client_id="BITFINEX",
                                 hedge_account_id="MT5-1", hedge_client_id="MT5")
    route.update(kwargs)
    view.begin_source(cid, side, D(2), **route)


def _legacy(
    prefix: Path, kind: str = "v2", quantities: tuple[str, ...] = ("0.1", "0.4"),
) -> dict[SourceDirection, JsonStateStore]:
    views = {direction: JsonStateStore(path)
             for direction, path in zip((LONG, SHORT), maker_legacy_paths(prefix), strict=True)}
    _begin(views[LONG], "OLD", BUY)
    for index, quantity in enumerate(quantities):
        _fill(views[LONG], "OLD", BUY, quantity, f"T{index}")
    views[LONG].update_source_status("OLD", "CANCELED")
    views[LONG].confirm_source_reconciled("OLD")
    for view in views.values():
        view.freeze_source_submissions("legacy cycle")
    if kind == "v2":
        _v2(prefix, views)
    return views


def _v2(prefix: Path, views: dict[SourceDirection, JsonStateStore]) -> None:
    maker_state_path(prefix).write_text(json.dumps({
        "schema_version": 2, "kind": "maker", "source_instrument_id": SOURCE,
        "hedge_instrument_id": HEDGE,
        "directions": {direction.value: view._to_payload() for direction, view in views.items()},
    }))


def _migrate(source: Path, destination: Path, *, stopped: bool = True) -> MakerStateStore:
    return migrate_maker_state(source, destination, SOURCE, HEDGE, stopped=stopped)


@pytest.mark.parametrize("version", [4, 6])
def test_checkpoint_origin_is_not_adopted_by_loading_or_later_fill(
    tmp_path: Path, version: int,
) -> None:
    prefix, output = tmp_path / "legacy", tmp_path / "output"
    _legacy(prefix)
    owner = _migrate(prefix, output)
    raw = owner._to_payload()
    raw["schema_version"] = version
    if version == 4:
        raw.pop("cycle_freeze_only")
    owner.path.write_text(json.dumps(raw))
    before = owner.path.read_bytes()
    loaded = MakerStateStore(output, SOURCE, HEDGE)
    assert not loaded.cycle_freeze_only and loaded.path.read_bytes() == before
    _fill(loaded.stores[LONG], "OLD", BUY, "0.5", "LATE")
    updated = json.loads(loaded.path.read_text())
    assert updated["schema_version"] == 6 and updated["cycle_freeze_only"] is False
    assert updated["legacy_checkpoint"] == raw["legacy_checkpoint"]


@pytest.mark.parametrize("marker", [1, "false", None])
def test_checkpoint_v6_also_requires_a_strict_boolean(tmp_path: Path, marker: object) -> None:
    prefix, output = tmp_path / "legacy", tmp_path / "invalid"
    _legacy(prefix)
    owner = _migrate(prefix, output)
    raw = owner._to_payload()
    raw["cycle_freeze_only"] = marker
    owner.path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="boolean"):
        MakerStateStore(output, SOURCE, HEDGE)


@pytest.mark.parametrize("kind", ["v1", "v2"])
def test_migrate_explicit_checkpoint_without_fabricating_unknown_fill_sizes(
    tmp_path: Path, kind: str,
) -> None:
    prefix = tmp_path / "old"
    views = _legacy(prefix, kind)
    inputs = ((maker_state_path(prefix),) if kind == "v2" else maker_legacy_paths(prefix))
    original = {path: path.read_bytes() for path in inputs}
    with pytest.raises(ValueError, match="legacy|schema"):
        MakerStateStore(prefix, SOURCE, HEDGE)
    owner = _migrate(prefix, tmp_path / "new")
    payload = json.loads(owner.path.read_text())
    assert payload["schema_version"] == 6 and payload["allocations"] == []
    assert payload["cycle_freeze_only"] is False
    checkpoint = payload["legacy_checkpoint"]
    assert checkpoint["projection"] == "bid_then_ask"
    assert checkpoint["sources"] == [
        {"path": str(path.resolve()), "sha256": sha256(content).hexdigest()}
        for path, content in original.items()
    ]
    assert checkpoint["orders"] == [{
        "client_order_id": "OLD", "direction": "bid",
        "route": {"source_account_id": "BITFINEX-1", "source_client_id": "BITFINEX",
                  "hedge_account_id": "MT5-1", "hedge_client_id": "MT5",
                  "isolated_source_order_id": None},
        "filled_ounces": "0.5", "fill_keys": ["OLD|V-OLD|T0", "OLD|V-OLD|T1"],
        "intent_ids": [], "allocated_ounces": "0",
    }]
    for direction, view in views.items():
        assert owner.stores[direction]._state == replace(view._state, net_unhedged_ounces=D(0))
    assert owner.has_residuals() and owner.stores[LONG].rounding_residual_ounces == D("0.5")
    assert not owner.clear_source_freezes()
    assert {path: path.read_bytes() for path in inputs} == original
    before = owner.path.read_bytes()
    assert _fill(owner.stores[LONG], "OLD", BUY, "0.1", "T0") is None
    assert owner.path.read_bytes() == before
    new_intent = _fill(owner.stores[LONG], "OLD", BUY, "0.5", "NEW")
    assert new_intent is not None and new_intent.hedge_quantity_ounces == 1
    reloaded = MakerStateStore(tmp_path / "new", SOURCE, HEDGE)
    updated = json.loads(owner.path.read_text())
    assert updated["legacy_checkpoint"] == checkpoint and len(updated["allocations"]) == 1
    assert reloaded.stores[LONG].intents() == owner.stores[LONG].intents()
    assert not reloaded.cycle_freeze_only  # A late fill cannot adopt the old checkpoint pause.
    assert not reloaded.has_residuals()


def test_equal_old_totals_do_not_turn_into_fictional_allocations(tmp_path: Path) -> None:
    checkpoints: list[dict[str, Any]] = []
    for index, quantities in enumerate((("0.1", "0.4"), ("0.2", "0.3"))):
        source = tmp_path / f"old-{index}"
        _legacy(source, quantities=quantities)
        owner = _migrate(source, tmp_path / f"new-{index}")
        payload = json.loads(owner.path.read_text())
        assert payload["allocations"] == []
        checkpoints.append(payload["legacy_checkpoint"])
    assert checkpoints[0]["orders"] == checkpoints[1]["orders"]
    assert checkpoints[0]["sources"][0]["sha256"] == checkpoints[1]["sources"][0]["sha256"]


@pytest.mark.parametrize("status", [ObligationStatus.PENDING, ObligationStatus.UNKNOWN,
                                    ObligationStatus.COMPLETED])
def test_old_full_allocation_not_hedge_filled_sets_baseline_and_preserves_plan(
    tmp_path: Path, status: ObligationStatus,
) -> None:
    source = tmp_path / "old-plan"
    views = _legacy(source, "v1", ("1",))
    view = views[LONG]
    intent = view.intents()[0]
    view.bind_hedge_plan(intent.intent_id, (HedgeLeg(SELL, D(1), "ticket", BUY, D(1)),))
    if status is not ObligationStatus.PENDING:
        view.bind_hedge_order(intent.intent_id, "OLD-HEDGE")
        if status is ObligationStatus.UNKNOWN:
            view.update_hedge_status("OLD-HEDGE", status)
        else:
            view.apply_hedge_fill(client_order_id="OLD-HEDGE", trade_id="DEAL", fill_ounces=D(1))
    _v2(source, views)
    owner = _migrate(source, tmp_path / "new-plan")
    assert owner.stores[LONG].intents() == view.intents()
    assert owner.stores[LONG].halt_reason == view.halt_reason
    assert owner.stores[LONG].active_source_order_id == view.active_source_order_id
    assert not owner.has_residuals()
    expected_unhedged = 0 if status is ObligationStatus.COMPLETED else 1
    assert owner.stores[LONG].net_unhedged_ounces == expected_unhedged
    if status is not ObligationStatus.COMPLETED:
        assert not owner.clear_source_freezes()
    owner.stores[LONG].recover_for_start()
    if status is ObligationStatus.UNKNOWN:
        assert owner.stores[LONG].intents()[0].status is ObligationStatus.UNKNOWN


def test_historical_cross_route_rounding_is_preserved_without_reallocation(tmp_path: Path) -> None:
    source = tmp_path / "mixed"
    views = {direction: JsonStateStore(path)
             for direction, path in zip((LONG, SHORT), maker_legacy_paths(source), strict=True)}
    view = views[LONG]
    for cid, account in (("A", "MT5-A"), ("B", "MT5-B")):
        _begin(view, cid, BUY, hedge_account_id=account)
        view.update_source_status(cid, "CANCELED")
        view.confirm_source_reconciled(cid)
    assert _fill(view, "A", BUY, "0.5", "TA") is None
    old_intent = _fill(view, "B", BUY, "0.1", "TB")
    assert old_intent is not None and old_intent.hedge_quantity_ounces == 1
    for item in views.values():
        item.freeze_source_submissions("legacy cycle")
    _v2(source, views)
    owner = _migrate(source, tmp_path / "new-mixed")
    assert owner.stores[LONG].intents() == view.intents()
    balances = sorted(balance for balance, _ in owner._route_balances().values())
    assert balances == [D("-0.9"), D("0.5")]
    assert owner.has_residuals() and not owner.clear_source_freezes()
    # A genuinely new fill can round the explicit -0.9 baseline; old intents stay untouched.
    new_intent = _fill(owner.stores[LONG], "B", BUY, "0.1", "NEW-B")
    assert new_intent is not None and new_intent.hedge_side is BUY
    assert owner.stores[LONG].intent(old_intent.intent_id) == old_intent
    assert MakerStateStore(tmp_path / "new-mixed", SOURCE, HEDGE).has_residuals()


def test_completed_legacy_hedge_and_zero_total_cannot_hide_distinct_route_residuals(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed-zero"
    views = {direction: JsonStateStore(path)
             for direction, path in zip((LONG, SHORT), maker_legacy_paths(source), strict=True)}
    view = views[LONG]
    _begin(view, "A", BUY, hedge_account_id="MT5-A")
    view.update_source_status("A", "CANCELED")
    view.confirm_source_reconciled("A")
    _begin(view, "B", BUY, hedge_account_id="MT5-B")
    assert _fill(view, "A", BUY, "0.4", "TA") is None
    intent = _fill(view, "B", BUY, "0.6", "TB")
    assert intent is not None
    view.bind_hedge_order(intent.intent_id, "OLD-HEDGE")
    view.apply_hedge_fill(client_order_id="OLD-HEDGE", trade_id="D", fill_ounces=D(1))
    for cid in ("A", "B"):
        view.update_source_status(cid, "CANCELED")
        view.confirm_source_reconciled(cid)
    assert view.rounding_residual_ounces == 0 and view.cycle_evidence_complete()
    for item in views.values():
        item.freeze_source_submissions("legacy cycle")
    _v2(source, views)
    owner = _migrate(source, tmp_path / "new-zero")
    assert owner.stores[LONG].intents()[0].status is ObligationStatus.COMPLETED
    assert all(item.halt_reason is None and item.active_source_order_id is None
               and item.net_unhedged_ounces == 0 for item in owner.stores.values())
    balances = sorted(balance for balance, _ in owner._route_balances().values())
    assert balances == [D("-0.4"), D("0.4")]
    assert owner.has_residuals() and not owner.clear_source_freezes()
    assert all(not item.can_submit_source() for item in owner.stores.values())
    assert MakerStateStore(tmp_path / "new-zero", SOURCE, HEDGE).has_residuals()


@pytest.mark.parametrize("fault", ["fixed_allocation", "missing_old_key", "missing_intent",
                                   "old_new_overlap", "route", "direction", "projection"])
def test_checkpoint_readback_rejects_changed_bindings_and_old_new_partition(
    tmp_path: Path, fault: str,
) -> None:
    source = tmp_path / "old"
    _legacy(source, quantities=("1", "0.1"))
    owner = _migrate(source, tmp_path / "new")
    _fill(owner.stores[LONG], "OLD", BUY, "0.2", "NEW")
    payload = json.loads(owner.path.read_text())
    checkpoint = payload["legacy_checkpoint"]
    order = checkpoint["orders"][0]
    if fault == "fixed_allocation":
        intent = next(iter(payload["directions"]["bid"]["hedge_intents"].values()))
        intent["hedge_quantity_ounces"] = "2"
    elif fault == "missing_old_key":
        order["fill_keys"].remove("OLD|V-OLD|T1")
    elif fault == "missing_intent":
        order["intent_ids"] = []
    elif fault == "old_new_overlap":
        order["fill_keys"].append("OLD|V-OLD|NEW")
    elif fault == "route":
        order["route"]["hedge_account_id"] = "OTHER"
    elif fault == "direction":
        order["direction"] = "ask"
    else:
        checkpoint["projection"] = "guessed_history"
    owner.path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        MakerStateStore(tmp_path / "new", SOURCE, HEDGE)


@pytest.mark.parametrize("after_publish", [False, True])
def test_new_fill_failure_on_checkpoint_retains_whole_committed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_publish: bool,
) -> None:
    source = tmp_path / "old"
    _legacy(source)
    owner = _migrate(source, tmp_path / "new")
    before = json.loads(owner.path.read_text())

    def fail(temporary: Path, target: Path) -> None:
        if after_publish:
            os.replace(temporary, target)
            raise ParentDirectorySyncError("renamed but parent sync failed")
        raise OSError("before rename")

    monkeypatch.setattr(store_module, "replace_and_sync_parent", fail)
    with pytest.raises(ParentDirectorySyncError if after_publish else OSError):
        _fill(owner.stores[LONG], "OLD", BUY, "0.5", "NEW")
    actual = json.loads(owner.path.read_text())
    assert actual == owner._to_payload()
    assert actual["legacy_checkpoint"] == before["legacy_checkpoint"]
    assert len(actual["allocations"]) == int(after_publish)
    assert owner.stores[LONG].has_seen_source_fill("OLD|V-OLD|NEW") is after_publish
    reloaded = MakerStateStore(tmp_path / "new", SOURCE, HEDGE)
    assert reloaded._to_payload() == actual


@pytest.mark.parametrize("fault", ["raw", "missing_key", "known_exceeds", "unknown_zero",
                                   "direction", "binding"])
def test_incompatible_legacy_facts_are_refused_without_output(tmp_path: Path, fault: str) -> None:
    source = tmp_path / "bad"
    _legacy(source, quantities=("1", "0.1"))
    path = maker_state_path(source)
    payload = json.loads(path.read_text())
    bid = payload["directions"]["bid"]
    if fault == "raw":
        bid["net_unhedged_ounces"] = "9"
    elif fault == "missing_key":
        bid["seen_source_fills"].remove("OLD|V-OLD|T1")
    elif fault == "known_exceeds":
        next(iter(bid["hedge_intents"].values()))["source_fill_ounces"] = "2"
    elif fault == "unknown_zero":
        next(iter(bid["hedge_intents"].values()))["source_fill_ounces"] = "1.1"
    elif fault == "direction":
        bid["source_orders"]["OLD"]["side"] = "SELL"
    else:
        payload["source_instrument_id"] = "OTHER"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        _migrate(source, tmp_path / "rejected")
    assert path.read_bytes() == before and not maker_state_path(tmp_path / "rejected").exists()


@pytest.mark.parametrize("collision", ["same", "maker", "bid", "ask", "race"])
def test_no_overwrite_or_same_prefix_even_when_target_appears_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collision: str,
) -> None:
    source, destination = tmp_path / "old", tmp_path / "new"
    _legacy(source)
    if collision == "same":
        destination = source
        target = maker_state_path(source)
    else:
        target = (Path(f"{destination}.{collision}.json") if collision != "race"
                  else maker_state_path(destination))
        if collision != "race":
            target.write_text("do not overwrite")
    if collision == "race":
        def race(temporary: Path, actual: Path) -> None:
            actual.write_text("racing target")
            create_and_sync_parent(temporary, actual)
        monkeypatch.setattr(store_module, "create_and_sync_parent", race)
    before = target.read_bytes() if target.exists() else b"racing target"
    with pytest.raises((ValueError, FileExistsError)):
        _migrate(source, destination)
    assert target.read_bytes() == before


@pytest.mark.parametrize("after_publish", [False, True])
def test_publication_failure_preserves_inputs_and_complete_post_publish_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_publish: bool,
) -> None:
    source, destination = tmp_path / "old", tmp_path / "new"
    _legacy(source)
    before = maker_state_path(source).read_bytes()

    def fail(temporary: Path, target: Path) -> None:
        if after_publish:
            os.link(temporary, target)
            raise ParentDirectorySyncError("published but parent sync failed")
        raise OSError("before publish")

    monkeypatch.setattr(store_module, "create_and_sync_parent", fail)
    with pytest.raises(ParentDirectorySyncError if after_publish else OSError):
        _migrate(source, destination)
    assert maker_state_path(source).read_bytes() == before
    assert maker_state_path(destination).exists() is after_publish
    if after_publish:
        assert MakerStateStore(destination, SOURCE, HEDGE).has_residuals()


def test_input_changes_between_two_reads_abort_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, destination = tmp_path / "old", tmp_path / "new"
    _legacy(source)
    path = maker_state_path(source)
    original = Path.read_bytes
    reads = 0

    def changed(current: Path) -> bytes:
        nonlocal reads
        if current == path:
            reads += 1
            if reads == 2:
                return original(current) + b" "
        return original(current)

    monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(ValueError, match="changed|stable"):
        _migrate(source, destination)
    assert reads == 2 and not maker_state_path(destination).exists()


def test_stopped_declaration_missing_pair_and_repeated_migration_are_refused(
    tmp_path: Path,
) -> None:
    source, destination = tmp_path / "old", tmp_path / "new"
    with pytest.raises(ValueError, match="stopped"):
        _migrate(source, destination, stopped=False)
    JsonStateStore(maker_legacy_paths(source)[0]).freeze_source_submissions("only bid")
    with pytest.raises(ValueError, match="both|missing"):
        _migrate(source, destination)
    source = tmp_path / "complete-old"
    _legacy(source)
    _migrate(source, destination)
    with pytest.raises(ValueError, match="schema|legacy"):
        _migrate(destination, tmp_path / "twice")
    fresh = tmp_path / "v3"
    MakerStateStore(fresh, SOURCE, HEDGE).freeze_sources("new")
    with pytest.raises(ValueError, match="schema|legacy"):
        _migrate(fresh, tmp_path / "v3-converted")


def test_cli_requires_explicit_stopped_and_reports_success(tmp_path: Path, capsys: Any) -> None:
    source, destination = tmp_path / "old", tmp_path / "new"
    _legacy(source)
    arguments = ["--input-prefix", str(source), "--output-prefix", str(destination),
                 "--source-instrument", SOURCE, "--hedge-instrument", HEDGE]
    assert main(arguments) == 1
    assert "stopped" in capsys.readouterr().err
    assert main([*arguments, "--stopped"]) == 0
    assert str(maker_state_path(destination)) in capsys.readouterr().out


def test_temporary_cleanup_failure_reports_success_and_retains_complete_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    capsys: Any,
) -> None:
    source, destination = tmp_path / "old", tmp_path / "new"
    _legacy(source)
    original = maker_state_path(source).read_bytes()
    unlink = Path.unlink

    def fail_temporary_cleanup(path: Path, missing_ok: bool = False) -> None:
        if path.parent == tmp_path and path.name.startswith(".new.maker.json."):
            raise OSError("injected cleanup failure")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)
    arguments = ["--input-prefix", str(source), "--output-prefix", str(destination),
                 "--source-instrument", SOURCE, "--hedge-instrument", HEDGE, "--stopped"]
    assert main(arguments) == 0
    assert str(maker_state_path(destination)) in capsys.readouterr().out
    assert "temporary file cleanup failed" in caplog.text
    assert "retained" in caplog.text
    assert MakerStateStore(destination, SOURCE, HEDGE).has_residuals()
    assert maker_state_path(source).read_bytes() == original
    published = maker_state_path(destination).read_bytes()
    assert main(arguments) == 1
    assert maker_state_path(destination).read_bytes() == published
