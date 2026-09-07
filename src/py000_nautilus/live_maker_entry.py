"""Ordinary Maker entry using the shared four-client runner."""

from py000_nautilus.live_taker_entry import LiveMakerNodeBuilder as LiveMakerNodeBuilder
from py000_nautilus.live_taker_entry import LiveMakerProfile as LiveMakerProfile
from py000_nautilus.live_taker_entry import load_live_maker_profile as load_live_maker_profile
from py000_nautilus.live_taker_entry import maker_main as main
from py000_nautilus.live_taker_entry import parse_live_maker_profile as parse_live_maker_profile
from py000_nautilus.live_taker_entry import run_live_maker_entry as run_live_maker_entry
from py000_nautilus.live_taker_entry import (
    validate_live_maker_profile as validate_live_maker_profile,
)

__all__ = [
    "LiveMakerNodeBuilder", "LiveMakerProfile", "load_live_maker_profile", "main",
    "parse_live_maker_profile", "run_live_maker_entry", "validate_live_maker_profile",
]

if __name__ == "__main__":
    raise SystemExit(main())
