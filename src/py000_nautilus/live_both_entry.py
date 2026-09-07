"""Thin shared-account profile and entry over the ordinary lifecycle runner."""

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from nautilus_trader.config import DatabaseConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.trading.strategy import Strategy

from py000_nautilus.bitfinex_v1_data import BitfinexV1DataClientConfig
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecClientConfig
from py000_nautilus.config import MakerStrategyConfig, TakerStrategyConfig
from py000_nautilus.live_both import (
    build_live_both_node,
    shared_strategy_ids,
    validate_shared_policy,
)
from py000_nautilus.live_taker_entry import (
    LiveTakerEntryResult,
    LiveTakerRehearsalRunner,
    _LiveProfile,
    _main,
    _required_path,
    _run_live_entry,
    _validate_live_profile,
)
from py000_nautilus.maker_store import MakerStateStore, shared_state_path
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig
from py000_nautilus.restart_recovery import StartupRecoveryOptions, describe_business_recovery


class LiveBothProfile(_LiveProfile, frozen=True, kw_only=True):
    maker_config: MakerStrategyConfig
    taker_config: TakerStrategyConfig
    shared_store_prefix: str


class LiveBothNodeBuilder(Protocol):
    def __call__(
        self, *, bitfinex_data_config: BitfinexV1DataClientConfig,
        bitfinex_exec_config: BitfinexV1ExecClientConfig,
        mt5_data_config: Mt5V1DataClientConfig, mt5_exec_config: Mt5V1ExecClientConfig,
        maker_config: MakerStrategyConfig, taker_config: TakerStrategyConfig,
        shared_store_prefix: str, cache_database: DatabaseConfig | None,
        loop: asyncio.AbstractEventLoop | None, connection_timeout_seconds: float,
        stop_timeout_seconds: float, startup_recovery: StartupRecoveryOptions | None = None,
    ) -> tuple[TradingNode, tuple[Strategy, ...]]: ...


def validate_live_both_profile(profile: LiveBothProfile) -> None:
    _validate_live_profile(profile, profile.maker_config)
    _validate_live_profile(profile, profile.taker_config)
    validate_shared_policy(profile.maker_config, profile.taker_config)
    prefix = _required_path(profile.shared_store_prefix, "shared state prefix")
    if shared_state_path(prefix) == _required_path(
        profile.bitfinex_exec_config.cid_store_path, "Bitfinex CID store",
    ):
        raise ValueError("shared state and Bitfinex CID stores must be distinct")


def parse_live_both_profile(raw: bytes | str) -> LiveBothProfile:
    profile = cast(LiveBothProfile, LiveBothProfile.parse(raw))
    validate_live_both_profile(profile)
    return profile


def load_live_both_profile(path: Path) -> LiveBothProfile:
    return parse_live_both_profile(path.read_bytes())


def inspect_both_recovery(profile: LiveBothProfile) -> dict[str, object]:
    validate_live_both_profile(profile)
    prefix = _required_path(profile.shared_store_prefix, "shared state prefix")
    if not shared_state_path(prefix).is_file():
        raise ValueError("no existing shared state to inspect")
    maker, taker = profile.maker_config, profile.taker_config
    carry_route = None
    if maker.residual_mode == "bounded-carry":
        source, hedge = maker.source_accounts[0], maker.hedge_accounts[0]
        carry_route = (str(source.account_id), str(source.client_id),
                       str(hedge.account_id), str(hedge.client_id))
    store = MakerStateStore(
        prefix, str(maker.source_instrument_id), str(maker.hedge_instrument_id),
        residual_limit_ounces=maker.residual_limit_ounces, carry_route=carry_route,
        shared_strategy_ids=shared_strategy_ids(maker, taker),
    )
    return describe_business_recovery(store)


def run_live_both_entry(
    profile: LiveBothProfile, *, rehearse: bool = False, run_paper: bool = False,
    resume_held: bool = False, retry_rejected_hedge: str | None = None,
    environment: Mapping[str, str] | None = None, env_file: Path | None = None,
    node_builder: LiveBothNodeBuilder = build_live_both_node,
    rehearsal_runner: LiveTakerRehearsalRunner | None = None,
) -> LiveTakerEntryResult:
    validate_live_both_profile(profile)

    def build(
        *, bitfinex_data_config: BitfinexV1DataClientConfig,
        bitfinex_exec_config: BitfinexV1ExecClientConfig,
        mt5_data_config: Mt5V1DataClientConfig, mt5_exec_config: Mt5V1ExecClientConfig,
        strategy_config: MakerStrategyConfig, cache_database: DatabaseConfig | None,
        loop: asyncio.AbstractEventLoop | None, connection_timeout_seconds: float,
        stop_timeout_seconds: float, startup_recovery: StartupRecoveryOptions | None = None,
    ) -> tuple[TradingNode, tuple[Strategy, ...]]:
        return node_builder(
            bitfinex_data_config=bitfinex_data_config, bitfinex_exec_config=bitfinex_exec_config,
            mt5_data_config=mt5_data_config, mt5_exec_config=mt5_exec_config,
            maker_config=strategy_config, taker_config=profile.taker_config,
            shared_store_prefix=profile.shared_store_prefix, cache_database=cache_database,
            loop=loop, connection_timeout_seconds=connection_timeout_seconds,
            stop_timeout_seconds=stop_timeout_seconds, startup_recovery=startup_recovery,
        )

    return _run_live_entry(
        profile, strategy_config=profile.maker_config, node_builder=build,
        rehearse=rehearse, run_paper=run_paper, environment=environment,
        env_file=env_file, rehearsal_runner=rehearsal_runner,
        resume_held=resume_held, retry_rejected_hedge=retry_rejected_hedge,
    )


def main(
    argv: Sequence[str] | None = None, *, environment: Mapping[str, str] | None = None,
    node_builder: LiveBothNodeBuilder = build_live_both_node,
    rehearsal_runner: LiveTakerRehearsalRunner | None = None,
) -> int:
    return _main(argv, mode="both", environment=environment, node_builder=node_builder,
                 rehearsal_runner=rehearsal_runner)


if __name__ == "__main__":
    raise SystemExit(main())
