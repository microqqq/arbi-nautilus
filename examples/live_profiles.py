"""Print a redacted ordinary profile; never read credentials, write state, or connect.

Run: python examples/live_profiles.py maker|taker|both
All identities/endpoints below are placeholders, not an authenticated deployment.
"""

import argparse
from decimal import Decimal as D

from nautilus_trader.config import DatabaseConfig, RoutingConfig
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId

from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, BitfinexV1DataClientConfig
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecClientConfig
from py000_nautilus.config import (
    CarryConfig,
    FxConfig,
    HedgeAccountRoute,
    MakerEconomicsConfig,
    MakerSideConfig,
    MakerStrategyConfig,
    RiskConfig,
    SourceAccountRoute,
    TakerEconomicsConfig,
    TakerStrategyConfig,
)
from py000_nautilus.live_both_entry import LiveBothProfile
from py000_nautilus.live_taker_entry import LiveMakerProfile, LiveTakerProfile
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig


def example_profile(mode: str) -> LiveMakerProfile | LiveTakerProfile | LiveBothProfile:
    """Use one common route/policy; both shares the 2oz cap, not two 2oz caps."""
    if mode not in {"maker", "taker", "both"}:
        raise ValueError("mode must be maker, taker, or both")
    source, hedge = InstrumentId.from_str("XAUTUSDT-PERP.BITFINEX"), InstrumentId.from_str(
        "XAUUSD.MT5",
    )
    source_client, hedge_client = ClientId("BITFINEX"), ClientId("MT5")
    source_account, hedge_account = AccountId("BITFINEX-PAPER-123456"), AccountId("MT5-12345678")
    source_routing = RoutingConfig(default=False, venues=frozenset({"BITFINEX"}))
    hedge_routing = RoutingConfig(default=False, venues=frozenset({"MT5"}))
    prefix = f"runtime/{mode}-example"
    data = BitfinexV1DataClientConfig(
        url="wss://replace-before-connecting.invalid/ws/2", instrument_id=source,
        raw_symbol=PAPER_RAW_SYMBOL, price_precision=1, size_precision=8,
        price_increment=D("0.1"), size_increment=D("0.00000001"),
        min_quantity=D(2), max_quantity=D(10000), margin_init=D("0.01"),
        margin_maint=D("0.005"), maker_fee=D(0), taker_fee=D("0.0002"),
        routing=source_routing,
    )
    execution = BitfinexV1ExecClientConfig(
        url=data.url, rest_url="https://replace-before-connecting.invalid",
        api_key="", api_secret="", user_id=123456, account_id=source_account,
        instrument_id=source, raw_symbol=PAPER_RAW_SYMBOL, wallet_currency="TESTUSDTF0",
        cid_store_path=f"{prefix}-cids.json", routing=source_routing,
    )
    mt5_data = Mt5V1DataClientConfig(
        pub_url="tcp://127.0.0.1:6001", rep_url="tcp://127.0.0.1:6002",
        instrument_id=hedge, expected_account_id="12345678", expected_symbol="XAUUSD",
        expected_magic="900000001", expected_ea_build_id="REPLACE-WITH-VERIFIED-BUILD",
        expected_source_sha256="0" * 64, expected_server_timezone="Europe/Athens",
        expected_execution_enabled=True, routing=hedge_routing,
    )
    mt5_exec = Mt5V1ExecClientConfig(
        pub_url=mt5_data.pub_url, rep_url=mt5_data.rep_url, instrument_id=hedge,
        expected_account_id=mt5_data.expected_account_id, expected_symbol="XAUUSD",
        expected_magic=mt5_data.expected_magic,
        expected_ea_build_id=mt5_data.expected_ea_build_id,
        expected_source_sha256=mt5_data.expected_source_sha256,
        expected_server_timezone=mt5_data.expected_server_timezone,
        expected_max_order_lots=D("0.02"), expected_stream_id="REPLACE-WITH-VERIFIED-STREAM",
        routing=hedge_routing,
    )
    route = SourceAccountRoute(
        account_id=source_account, client_id=source_client,
        max_long_ounces=D(2), max_short_ounces=D(2), base_margin_level=D(100),
    )
    # Economics are examples, not promises of profitability or runtime observations.
    carry = CarryConfig(total_trade_fee=D("0.00065"))
    fx, risk = FxConfig(), RiskConfig(source_max_abs=D(2), hedge_max_abs=D(2))
    side = MakerSideConfig(
        open_quantity_ounces=D(2), open_spread=D("0.001"), delta=D("0.0001"),
    )
    maker = MakerStrategyConfig(
        source_instrument_id=source, hedge_instrument_id=hedge, source_accounts=(route,),
        hedge_accounts=(HedgeAccountRoute(
            account_id=hedge_account, client_id=hedge_client,
            max_long_ounces=D(2), max_short_ounces=D(2),
        ),),
        economics=MakerEconomicsConfig(
            bid=side, ask=side, margin_level=D(500), carry=carry, fx=fx, risk=risk,
        ),
        store_path_prefix=f"{prefix}-maker", order_id_tag="M",
    )
    taker = TakerStrategyConfig(
        source_instrument_id=source, hedge_instrument_id=hedge, source_accounts=(route,),
        hedge_account_id=hedge_account, hedge_client_id=hedge_client,
        hedge_max_long_ounces=D(2), hedge_max_short_ounces=D(2),
        economics=TakerEconomicsConfig(
            base_book_quantity=D(2), open_quantity_long=D(2), open_quantity_short=D(2),
            threshold_long=D("0.001"), threshold_short=D("0.001"),
            margin_level=D(500), carry=carry, fx=fx, risk=risk,
        ),
        store_path=f"{prefix}-taker.json", order_id_tag="T",
    )
    # Native Redis is needed for real cross-process history; validation never connects.
    database = DatabaseConfig(host="127.0.0.1", port=6379)
    if mode == "maker":
        return LiveMakerProfile(
            bitfinex_data_config=data, bitfinex_exec_config=execution,
            mt5_data_config=mt5_data, mt5_exec_config=mt5_exec,
            cache_database=database, strategy_config=maker,
        )
    if mode == "taker":
        return LiveTakerProfile(
            bitfinex_data_config=data, bitfinex_exec_config=execution,
            mt5_data_config=mt5_data, mt5_exec_config=mt5_exec,
            cache_database=database, strategy_config=taker,
        )
    return LiveBothProfile(
        bitfinex_data_config=data, bitfinex_exec_config=execution,
        mt5_data_config=mt5_data, mt5_exec_config=mt5_exec,
        cache_database=database, maker_config=maker, taker_config=taker,
        shared_store_prefix=prefix,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("maker", "taker", "both"))
    print(example_profile(parser.parse_args().mode).json().decode())
