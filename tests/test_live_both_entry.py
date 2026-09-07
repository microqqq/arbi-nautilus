"""Offline profile/lifecycle wiring; these tests do not certify joint trading or Redis."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import msgspec
import pytest
from msgspec.structs import replace
from nautilus_trader.config import DatabaseConfig
from nautilus_trader.live.node import TradingNode
from test_live_maker_entry import _CREDENTIALS, _maker_profile
from test_live_taker_entry import _paper_profile, _UnreadableEnvironment

from py000_nautilus import live_taker_entry as common
from py000_nautilus.live_both import build_live_both_node, shared_strategy_ids
from py000_nautilus.live_both_entry import (
    LiveBothProfile,
    inspect_both_recovery,
    main,
    parse_live_both_profile,
    run_live_both_entry,
)
from py000_nautilus.live_lifecycle import DrainingTradingNode, DrainResult
from py000_nautilus.maker_store import MakerStateStore, shared_state_path


def _profile(tmp_path: Path) -> LiveBothProfile:
    maker = _maker_profile(tmp_path)
    config = maker.strategy_config
    route = config.hedge_accounts[0]
    taker = replace(
        _paper_profile(tmp_path).strategy_config,
        source_accounts=config.source_accounts, hedge_account_id=route.account_id,
        hedge_client_id=route.client_id, hedge_max_long_ounces=route.max_long_ounces,
        hedge_max_short_ounces=route.max_short_ounces,
    )
    taker = replace(taker, economics=replace(
        taker.economics, risk=config.economics.risk,
        margin_level=config.economics.margin_level, fx=config.economics.fx,
    ))
    return LiveBothProfile(
        bitfinex_data_config=maker.bitfinex_data_config,
        bitfinex_exec_config=maker.bitfinex_exec_config,
        mt5_data_config=maker.mt5_data_config, mt5_exec_config=maker.mt5_exec_config,
        maker_config=config, taker_config=taker, shared_store_prefix=str(tmp_path / "both"),
    )


def test_profile_and_offline_composition_have_one_node_no_connection_or_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = replace(_profile(tmp_path), cache_database=DatabaseConfig(host="offline.invalid"))
    assert parse_live_both_profile(profile.json()) == profile

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("offline validation attempted database/venue connection")

    monkeypatch.setattr("nautilus_trader.system.kernel.CacheDatabaseAdapter", forbidden)
    monkeypatch.setattr("py000_nautilus.bitfinex_v1_transport.BitfinexV1Transport.open", forbidden)
    monkeypatch.setattr("py000_nautilus.mt5_v1_transport.Mt5V1Transport.open", forbidden)

    def build(**kwargs: Any) -> Any:
        assert kwargs["cache_database"] is None
        node, (maker, taker) = build_live_both_node(**kwargs)
        assert node.trader.strategies() == [maker, taker]
        assert len(node.trader.actors()) == 1
        assert len(node.kernel.exec_engine.registered_clients) == 2
        assert maker._state_store.taker_store is taker.state_store
        assert len(maker._state_store.all_views()) == 3
        assert maker.id != taker.id and maker.config.order_id_tag != taker.config.order_id_tag
        assert cast(DrainingTradingNode, node)._drain_strategies == (maker, taker)
        return node, (maker, taker)

    result = run_live_both_entry(profile, node_builder=build,
                                 environment=_UnreadableEnvironment(), env_file=tmp_path)
    assert result.outcome == "VALIDATED"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field", [None, "maker_config", "taker_config"])
def test_unknown_profile_fields_rejected(tmp_path: Path, field: str | None) -> None:
    raw = json.loads(_profile(tmp_path).json())
    (raw if field is None else raw[field])["unknown"] = 1
    with pytest.raises(msgspec.ValidationError, match="unknown field"):
        parse_live_both_profile(json.dumps(raw))


@pytest.mark.parametrize("field", ["risk", "margin_level", "fx", "tag", "route"])
def test_policy_mismatch_rejected_before_credentials_or_build(tmp_path: Path, field: str) -> None:
    profile = _profile(tmp_path)
    taker = profile.taker_config
    if field == "tag":
        taker = replace(taker, order_id_tag="M")
    elif field == "route":
        taker = replace(taker, hedge_max_long_ounces=Decimal("123"))
    else:
        value = (replace(taker.economics.risk, source_max_abs=Decimal("123")) if field == "risk"
                 else replace(taker.economics.fx, usd_usdt_ask=Decimal("1.01")) if field == "fx"
                 else Decimal("123"))
        taker = replace(taker, economics=replace(taker.economics, **{field: value}))
    with pytest.raises(ValueError, match="both"):
        run_live_both_entry(replace(profile, taker_config=taker), run_paper=True,
                            environment=_UnreadableEnvironment())
    assert not list(tmp_path.iterdir())


def test_inspection_reads_only_existing_shared_state(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    with pytest.raises(ValueError, match="no existing shared"):
        inspect_both_recovery(profile)
    maker, taker = profile.maker_config, profile.taker_config
    owner = MakerStateStore(
        profile.shared_store_prefix,
        str(maker.source_instrument_id), str(maker.hedge_instrument_id),
        shared_strategy_ids=shared_strategy_ids(maker, taker),
    )
    owner.freeze_sources("operator review")
    before = owner.path.read_bytes()
    result = inspect_both_recovery(profile)
    assert result["native_and_venue_checked"] is False
    assert len(cast(list[Any], result["views"])) == 3
    assert owner.path.read_bytes() == before


def test_rehearsal_removes_both_strategies_and_actor(tmp_path: Path) -> None:
    def rehearse(node: TradingNode, *, timeout_seconds: float) -> None:
        assert node.trader.strategies() == node.trader.actors() == []
        assert timeout_seconds == 10

    result = run_live_both_entry(_profile(tmp_path), rehearse=True,
                                 environment=_CREDENTIALS, rehearsal_runner=rehearse)
    assert result.outcome == "REHEARSED"
    assert not shared_state_path(_profile(tmp_path).shared_store_prefix).exists()


@pytest.mark.parametrize("complete", [True, False])
def test_cli_uses_joint_drain_and_explicit_virtual_accounting_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], complete: bool,
) -> None:
    path = tmp_path / "profile.json"
    path.write_bytes(_profile(tmp_path).json())

    def run(node: TradingNode) -> None:
        assert len(node.trader.strategies()) == 2
        cast(DrainingTradingNode, node).drain_result = DrainResult(complete, "joint", (), {})

    monkeypatch.setattr(common, "run_paper_node", run)
    assert main(["--profile", str(path), "--run-paper"], environment=_CREDENTIALS) == (
        0 if complete else 1
    )
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert output["drain_complete"] is complete
    assert "native_virtual" in output["accounting"]["scope"]
    assert "not_venue_realized_cashflow" in output["accounting"]["scope"]
