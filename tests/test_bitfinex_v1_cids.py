from __future__ import annotations

import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import py000_nautilus.bitfinex_v1_cids as cid_module
from py000_nautilus.bitfinex_v1_cids import (
    MAX_CID,
    BitfinexV1CidError,
    BitfinexV1CidStore,
)
from py000_nautilus.durability import ParentDirectorySyncError


def test_fee_metadata_v2_retains_native_origin_and_raw_fee_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "cids.json"
    store = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    binding = store.allocate("O-FEE-1", epoch_ms=100)
    native = cid_module.BitfinexNativeFill(
        trade_id="1234", native_fill_origin="te_paper", signed_quantity=Decimal(2),
        price=Decimal("3926.75"), ts_event_ns=123_000_000,
        liquidity_side="TAKER", commission=Decimal(0), commission_currency="USD",
    )
    pending = cid_module.BitfinexFeeTrade(
        trade_id=1234, ts_event_ms=123, execution_qty=Decimal(2),
        execution_price=Decimal("3926.75"), order_type="IOC",
        order_price=Decimal("4000"), maker=False, raw_fee=None, fee_currency=None,
    )
    scope = cid_module.BitfinexFeeMetadata(
        cid=binding.cid, venue_order_id=987, instrument_id="XAUTUSDT.BITFINEX",
        raw_symbol="tTESTXAUTF0:TESTUSDTF0",
    )
    store.record_native_fill(scope, native)
    store.record_venue_trade(scope, pending)
    final = replace(pending, raw_fee=Decimal("-0.061668"), fee_currency="USD")
    store.record_venue_trade(scope, final)
    store.record_venue_trade(scope, final)
    store.allocate("O-FEE-2", epoch_ms=101)

    restored = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    metadata = restored.fee_metadata_for_cid(binding.cid)
    assert metadata is not None
    assert metadata.native_fills == (native,)
    assert metadata.venue_trades == (final,)
    assert json.loads(path.read_text())["schema_version"] == 2
    with pytest.raises(BitfinexV1CidError, match="trade ID changed"):
        restored.record_venue_trade(
            scope, replace(final, raw_fee=Decimal("-0.061669")),
        )


def test_binding_is_durable_and_bidirectional_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "cids.json"
    first = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    binding = first.allocate("O-STRING-1", epoch_ms=1_700_000_000_123)

    assert binding.cid == 1_700_000_000_123
    assert binding.allocated_utc_date == "2023-11-14"
    assert first.binding_for_client("O-STRING-1") == binding
    assert first.binding_for_cid(binding.cid) == binding

    restarted = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    assert restarted.binding_for_client("O-STRING-1") == binding
    assert restarted.binding_for_cid(binding.cid) == binding
    after_restart = restarted.allocate("O-STRING-2", epoch_ms=binding.cid - 100)
    assert after_restart.cid == binding.cid + 1


def test_accounting_conflict_is_one_way_and_survives_successful_allocation(tmp_path: Path) -> None:
    path = tmp_path / "cids.json"
    store = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    binding = store.allocate("O-1", epoch_ms=100)
    assert not bool(store.accounting_conflict)
    store.mark_accounting_conflict()
    assert store.accounting_conflict and store.fees_durable
    store.allocate("O-2", epoch_ms=101)
    restarted = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    assert restarted.accounting_conflict and restarted.fees_durable
    assert restarted.binding_for_cid(binding.cid) == binding
    assert json.loads(path.read_text())["accounting_conflict"] is True


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], {}])
def test_v2_accounting_conflict_requires_a_boolean(tmp_path: Path, invalid: object) -> None:
    path = tmp_path / "cids.json"
    store = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    store.allocate("O-1", epoch_ms=100)
    raw = json.loads(path.read_text())
    raw["accounting_conflict"] = invalid
    path.write_text(json.dumps(raw))
    with pytest.raises(BitfinexV1CidError, match="accounting_conflict"):
        BitfinexV1CidStore(path, account_id="BITFINEX-001")
    raw.pop("accounting_conflict")
    path.write_text(json.dumps(raw))
    with pytest.raises(BitfinexV1CidError, match="accounting_conflict"):
        BitfinexV1CidStore(path, account_id="BITFINEX-001")


def test_allocation_is_globally_monotonic_when_clock_repeats_or_moves_back(
    tmp_path: Path,
) -> None:
    store = BitfinexV1CidStore(tmp_path / "cids.json", account_id="BITFINEX-001")
    first = store.allocate("O-1", epoch_ms=1_700_000_000_123)
    second = store.allocate("O-2", epoch_ms=1_700_000_000_123)
    third = store.allocate("O-3", epoch_ms=1_699_999_999_000)

    assert (first.cid, second.cid, third.cid) == (
        1_700_000_000_123,
        1_700_000_000_124,
        1_700_000_000_125,
    )
    assert third.allocated_utc_date == "2023-11-14"


def test_allocation_records_the_utc_day_without_reusing_cid_at_midnight(
    tmp_path: Path,
) -> None:
    store = BitfinexV1CidStore(tmp_path / "cids.json", account_id="BITFINEX-001")
    before = store.allocate("O-BEFORE", epoch_ms=1_704_067_199_999)
    after = store.allocate("O-AFTER", epoch_ms=1_704_067_200_000)

    assert before.allocated_utc_date == "2023-12-31"
    assert after.allocated_utc_date == "2024-01-01"
    assert after.cid == before.cid + 1


def test_persist_failure_does_not_allocate_in_memory_or_replace_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cids.json"
    store = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    first = store.allocate("O-1", epoch_ms=100)
    original = path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.allocate("O-2", epoch_ms=200)

    assert store.binding_for_client("O-2") is None
    assert store.binding_for_cid(first.cid + 1) is None
    assert path.read_bytes() == original
    assert not tuple(tmp_path.glob(".cids.json.*"))


def test_post_replace_sync_failure_conservatively_burns_the_cid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cids.json"
    store = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    original_replace = os.replace

    def replace_then_fail(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        original_replace(source, destination)
        raise ParentDirectorySyncError("replacement completed but parent sync failed")

    monkeypatch.setattr(cid_module, "replace_and_sync_parent", replace_then_fail)

    with pytest.raises(ParentDirectorySyncError, match="replacement completed"):
        store.allocate("O-UNCERTAIN", epoch_ms=200)

    binding = store.binding_for_client("O-UNCERTAIN")
    assert binding is not None
    assert store.binding_for_cid(binding.cid) == binding
    reloaded = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    assert reloaded.binding_for_cid(binding.cid) == binding
    with pytest.raises(BitfinexV1CidError, match="already bound"):
        store.allocate("O-UNCERTAIN", epoch_ms=201)


def test_existing_client_id_is_a_conflict_and_lookup_keeps_original(tmp_path: Path) -> None:
    store = BitfinexV1CidStore(tmp_path / "cids.json", account_id="BITFINEX-001")
    original = store.allocate("O-1", epoch_ms=100)

    with pytest.raises(BitfinexV1CidError, match="already bound"):
        store.allocate("O-1", epoch_ms=200)

    assert store.binding_for_client("O-1") == original


@pytest.mark.parametrize(
    "bindings, message",
    [
        (
            [
                {"client_order_id": "O-1", "cid": 1, "allocated_utc_date": "2026-09-02"},
                {"client_order_id": "O-2", "cid": 1, "allocated_utc_date": "2026-09-02"},
            ],
            "duplicate persisted binding",
        ),
        (
            [
                {"client_order_id": "O-1", "cid": 1, "allocated_utc_date": "2026-09-02"},
                {"client_order_id": "O-1", "cid": 2, "allocated_utc_date": "2026-09-02"},
            ],
            "duplicate persisted binding",
        ),
    ],
)
def test_persisted_bijection_conflicts_fail_closed(
    tmp_path: Path,
    bindings: list[dict[str, object]],
    message: str,
) -> None:
    path = tmp_path / "cids.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "account_id": "BITFINEX-001",
                "last_cid": 2 if bindings[-1]["cid"] == 2 else 1,
                "bindings": bindings,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BitfinexV1CidError, match=message):
        BitfinexV1CidStore(path, account_id="BITFINEX-001")


def test_account_scope_and_last_cid_corruption_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "cids.json"
    store = BitfinexV1CidStore(path, account_id="BITFINEX-001")
    store.allocate("O-1", epoch_ms=100)

    with pytest.raises(BitfinexV1CidError, match="account_id"):
        BitfinexV1CidStore(path, account_id="BITFINEX-002")

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["last_cid"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BitfinexV1CidError, match="last_cid"):
        BitfinexV1CidStore(path, account_id="BITFINEX-001")


def test_int45_maximum_is_allocatable_once_then_exhausted(tmp_path: Path) -> None:
    store = BitfinexV1CidStore(tmp_path / "cids.json", account_id="BITFINEX-001")
    binding = store.allocate("O-MAX", epoch_ms=MAX_CID)
    assert binding.cid == MAX_CID

    with pytest.raises(BitfinexV1CidError, match="exceeds int45"):
        store.allocate("O-OVERFLOW", epoch_ms=MAX_CID)
    assert store.binding_for_client("O-OVERFLOW") is None


@pytest.mark.parametrize("invalid", [-1, True, MAX_CID + 1])
def test_invalid_epoch_milliseconds_do_not_allocate(tmp_path: Path, invalid: int) -> None:
    store = BitfinexV1CidStore(tmp_path / "cids.json", account_id="BITFINEX-001")
    with pytest.raises(BitfinexV1CidError):
        store.allocate("O-1", epoch_ms=invalid)
    assert not (tmp_path / "cids.json").exists()
