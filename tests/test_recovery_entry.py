"""Recovery choices are temporary CLI input; inspection never starts a node."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from msgspec.structs import replace
from nautilus_trader.config import DatabaseConfig
from test_live_maker_entry import _case
from test_live_taker_entry import _UnreadableEnvironment

import py000_nautilus.live_taker_entry as entry
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import BusinessOrderSide
from py000_nautilus.store import JsonStateStore


@pytest.mark.parametrize("kind", ["taker", "maker"])
@pytest.mark.parametrize("mode", ["validate", "rehearse", "retry_without_review"])
def test_recovery_choices_rejected_before_credentials_or_node(
    tmp_path: Path, kind: str, mode: str,
) -> None:
    profile, run, _ = _case(kind, tmp_path)

    def forbidden(**_: Any) -> Any:
        pytest.fail("invalid recovery mode constructed a node")

    with pytest.raises(ValueError, match="requires|require"):
        run(profile, rehearse=mode == "rehearse", run_paper=mode == "retry_without_review",
            resume_held=mode != "retry_without_review", retry_rejected_hedge="OLD-HEDGE",
            environment=_UnreadableEnvironment(), env_file=tmp_path / "missing-secret",
            node_builder=forbidden)


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_inspect_existing_state_without_credentials_database_or_builder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    profile, _, _ = _case(kind, tmp_path)
    profile = replace(profile, cache_database=DatabaseConfig(host="must-not-connect.invalid"))
    config = profile.strategy_config
    owner = (MakerStateStore(config.store_path_prefix, str(config.source_instrument_id),
                             str(config.hedge_instrument_id)) if kind == "maker"
             else JsonStateStore(config.store_path))
    view = next(iter(owner.stores.values())) if isinstance(owner, MakerStateStore) else owner
    view.begin_source("OLD-SOURCE", BusinessOrderSide.BUY, Decimal(2))
    view.mark_source_unknown("OLD-SOURCE", "operator inspection required")
    before, mtime = owner.path.read_bytes(), owner.path.stat().st_mtime_ns
    path = tmp_path / "profile.json"
    path.write_bytes(profile.json())

    def forbidden(**_: Any) -> Any:
        pytest.fail("inspection constructed a node")

    cli = entry.maker_main if kind == "maker" else entry.main
    assert cli(["--profile", str(path), "--inspect-recovery",
                "--env-file", str(tmp_path / "absent")],
               node_builder=forbidden, environment=_UnreadableEnvironment()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["native_and_venue_checked"] is False
    assert report["views"][0]["source_orders"][0]["client_order_id"] == "OLD-SOURCE"
    assert owner.path.read_bytes() == before and owner.path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_inspection_does_not_create_missing_business_file(tmp_path: Path, kind: str) -> None:
    profile, _, _ = _case(kind, tmp_path)
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="no existing"):
        entry.inspect_profile_recovery(profile)
    assert set(tmp_path.iterdir()) == before
