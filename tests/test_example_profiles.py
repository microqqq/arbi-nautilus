"""Redacted examples stay parseable and offline; no deployment identity is certified."""

import runpy
from pathlib import Path
from typing import Any

import pytest
from test_live_taker_entry import _UnreadableEnvironment

from py000_nautilus.live_both_entry import parse_live_both_profile, run_live_both_entry
from py000_nautilus.live_taker_entry import (
    parse_live_maker_profile,
    parse_live_taker_profile,
    run_live_maker_entry,
    run_live_taker_entry,
)


@pytest.mark.parametrize("mode", ["maker", "taker", "both"])
def test_redacted_example_validates_without_credentials_database_or_venue(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Path(__file__).parents[1] / "examples" / "live_profiles.py"
    monkeypatch.chdir(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("redacted example attempted external access")

    monkeypatch.setattr("nautilus_trader.system.kernel.CacheDatabaseAdapter", forbidden)
    monkeypatch.setattr("py000_nautilus.bitfinex_v1_transport.BitfinexV1Transport.open", forbidden)
    monkeypatch.setattr("py000_nautilus.mt5_v1_transport.Mt5V1Transport.open", forbidden)
    profile = runpy.run_path(str(script))["example_profile"](mode)
    assert profile.bitfinex_exec_config.api_key == profile.bitfinex_exec_config.api_secret == ""
    raw = profile.json()
    if mode == "maker":
        result = run_live_maker_entry(parse_live_maker_profile(raw),
                                     environment=_UnreadableEnvironment())
    elif mode == "taker":
        result = run_live_taker_entry(parse_live_taker_profile(raw),
                                     environment=_UnreadableEnvironment())
    else:
        result = run_live_both_entry(parse_live_both_profile(raw),
                                    environment=_UnreadableEnvironment())
    assert result.outcome == "VALIDATED"
    assert not list(tmp_path.iterdir())
