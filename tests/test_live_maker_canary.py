from __future__ import annotations

import asyncio
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from msgspec.structs import replace as struct_replace
from nautilus_trader.config import RoutingConfig
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

import py000_nautilus.live_maker_canary as canary
from py000_nautilus.app import _hedge_instrument, _quote, _source_instrument
from py000_nautilus.bitfinex_v1_data import PAPER_RAW_SYMBOL, BitfinexV1DataClientConfig
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecClientConfig
from py000_nautilus.bitfinex_v1_paper_canary import (
    PRIVATE_WS_URL,
    PUBLIC_WS_URL,
    REST_URL,
    PaperCanaryError,
    PaperSnapshot,
)
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport
from py000_nautilus.config import (
    CarryConfig,
    FxConfig,
    HedgeAccountRoute,
    MakerEconomicsConfig,
    MakerSideConfig,
    MakerStrategyConfig,
    RiskConfig,
    SourceAccountRoute,
)
from py000_nautilus.models import BusinessOrderSide, HedgeIntent, MakerQuote, SourceDirection
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig
from py000_nautilus.mt5_v1_transport import Mt5V1Transport
from py000_nautilus.strategies.maker import SourceTerminalResult

D = Decimal
SOURCE_ID = InstrumentId.from_str("XAUTUSDT-PERP.BITFINEX")
HEDGE_ID = InstrumentId.from_str("XAUUSD.MT5")
BITFINEX_ID, MT5_ID = ClientId("BITFINEX"), ClientId("MT5")
USER_ID = 269_312


def _profile(tmp_path: Path) -> canary.LiveMakerCanaryProfile:
    bfx_route = RoutingConfig(default=False, venues=frozenset({"BITFINEX"}))
    mt5_route = RoutingConfig(default=False, venues=frozenset({"MT5"}))
    account = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    bfx_data = BitfinexV1DataClientConfig(
        url=PUBLIC_WS_URL,
        instrument_id=SOURCE_ID,
        raw_symbol=PAPER_RAW_SYMBOL,
        price_precision=1,
        size_precision=8,
        price_increment=D("0.1"),
        size_increment=D("0.00000001"),
        min_quantity=D(2),
        max_quantity=D(10_000),
        margin_init=D("0.01"),
        margin_maint=D("0.005"),
        maker_fee=D(0),
        taker_fee=D("0.0002"),
        routing=bfx_route,
    )
    bfx_exec = BitfinexV1ExecClientConfig(
        url=PRIVATE_WS_URL,
        rest_url=REST_URL,
        api_key="",
        api_secret="",
        user_id=USER_ID,
        account_id=account,
        instrument_id=SOURCE_ID,
        raw_symbol=PAPER_RAW_SYMBOL,
        wallet_currency="TESTUSDTF0",
        cid_store_path=str(tmp_path / "cids.json"),
        routing=bfx_route,
    )
    identity: dict[str, object] = {
        "pub_url": "tcp://127.0.0.1:6001",
        "rep_url": "tcp://127.0.0.1:6002",
        "instrument_id": HEDGE_ID,
        "expected_account_id": "12345678",
        "expected_symbol": "XAUUSD",
        "expected_magic": "900000001",
        "expected_ea_build_id": "py000-mt5-ea-v1",
        "expected_source_sha256": "a" * 64,
        "expected_server_timezone": "Europe/Athens",
        "routing": mt5_route,
    }
    mt5_data = Mt5V1DataClientConfig(
        **identity,  # type: ignore[arg-type]
        expected_execution_enabled=True,
    )
    mt5_exec = Mt5V1ExecClientConfig(
        **identity,  # type: ignore[arg-type]
        expected_max_order_lots=D("0.02"),
        expected_stream_id="stream-test",
    )
    strategy = MakerStrategyConfig(
        source_instrument_id=SOURCE_ID,
        hedge_instrument_id=HEDGE_ID,
        source_accounts=(
            SourceAccountRoute(
                account_id=account,
                max_long_ounces=D(2),
                max_short_ounces=D(2),
                client_id=BITFINEX_ID,
                base_margin_level=D(100),
            ),
        ),
        hedge_accounts=(
            HedgeAccountRoute(
                account_id=AccountId("MT5-12345678"),
                max_long_ounces=D(2),
                max_short_ounces=D(2),
                client_id=MT5_ID,
            ),
        ),
        economics=MakerEconomicsConfig(
            bid=MakerSideConfig(open_quantity_ounces=D(2), open_spread=D("0.05"), delta=D(0)),
            ask=MakerSideConfig(open_quantity_ounces=D(0), open_spread=D("0.05"), delta=D(0)),
            margin_level=D(9900),
            carry=CarryConfig(total_trade_fee=D("0.00065")),
            fx=FxConfig(),
            risk=RiskConfig(source_max_abs=D(2), hedge_max_abs=D(2)),
        ),
        store_path_prefix=str(tmp_path / "maker"),
    )
    return canary.LiveMakerCanaryProfile(
        bitfinex_data_config=bfx_data,
        bitfinex_exec_config=bfx_exec,
        mt5_data_config=mt5_data,
        mt5_exec_config=mt5_exec,
        strategy_config=strategy,
        connection_timeout_seconds=2.0,
    )


def _strategy(tmp_path: Path, query: Any) -> canary.MakerCanaryStrategy:
    return canary.MakerCanaryStrategy(
        _profile(tmp_path).strategy_config,
        live_submission_ready=lambda: True,
        hedge_quantity_ready=lambda _quantity: True,
        live_costs_from_adapters=False,
        source_terminal_query=query,
    )


def _bound_quote(profile: canary.LiveMakerCanaryProfile) -> MakerQuote:
    from py000_nautilus.models import MakerAccount, SourceAccount

    source = profile.strategy_config.source_accounts[0]
    hedge = profile.strategy_config.hedge_accounts[0]
    return MakerQuote(
        direction=SourceDirection.LONG,
        source_account=SourceAccount(source.account_id, source.client_id, D(), D(2), D(2), D(100)),
        hedge_account=MakerAccount(hedge.account_id, hedge.client_id, D(), D(2), D(2)),
        source_price_usdt=D(2000),
        hedge_reference_price_usd=D(2100),
        quantity_ounces=D(2),
        adjusted_spread=D("0.05"),
        leverage=1,
    )


class _CachedTriggerProbe(canary.MakerCanaryStrategy):
    def __init__(
        self,
        profile: canary.LiveMakerCanaryProfile,
        freshness: bool | BaseException,
    ) -> None:
        super().__init__(
            profile.strategy_config,
            live_submission_ready=lambda: True,
            hedge_quantity_ready=lambda _quantity: True,
            live_costs_from_adapters=False,
            source_terminal_query=lambda *_args: None,
        )
        now_ns = 1_000_000_000
        self.source_tick = _quote(_source_instrument(), "2000", "2001", "2", now_ns)
        self.hedge_tick = _quote(_hedge_instrument(), "2000", "2001", "2", now_ns)
        ticks = {SOURCE_ID: self.source_tick, HEDGE_ID: self.hedge_tick}
        self._test_cache = SimpleNamespace(quote_tick=ticks.get)
        self._test_clock = SimpleNamespace(timestamp_ns=lambda: now_ns)
        self._test_profile = profile
        self._test_freshness = freshness
        self.freshness_checks = 0

    @property
    def cache(self) -> Any:
        return self._test_cache

    @property
    def clock(self) -> Any:
        return self._test_clock

    def _inputs_are_fresh(
        self, _source: object, _hedge: object, _now_ns: int,
    ) -> bool:
        self.freshness_checks += 1
        if isinstance(self._test_freshness, BaseException):
            raise self._test_freshness
        return self._test_freshness

    def _carry_for_hedge_tick(
        self, _hedge_tick: object, _now_ns: int,
    ) -> CarryConfig:
        return CarryConfig()

    def _try_release_cycle(self) -> bool:
        return False

    def _global_obligation_block(self) -> bool:
        return False

    def _refresh_direction(
        self, direction: SourceDirection, _source_book: object, _hedge_book: object,
    ) -> None:
        if direction is SourceDirection.LONG:
            self._submit_source(_bound_quote(self._test_profile))


def _source_order(order_id: str = "O-CANARY") -> tuple[Any, Any]:
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(2000),
        time_in_force=TimeInForce.GTC,
        post_only=True,
        client_order_id=ClientOrderId(order_id),
    )
    account = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    order.apply(TestEventStubs.order_submitted(order, account_id=account))
    order.apply(TestEventStubs.order_accepted(order, account_id=account))
    canceled = TestEventStubs.order_canceled(order, account_id=account)
    order.apply(canceled)
    return order, canceled


def test_default_is_offline_disarmed_and_creates_no_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_open(_transport: object) -> None:
        raise AssertionError("offline validation opened a transport")

    monkeypatch.setattr(BitfinexV1Transport, "open", unexpected_open)
    monkeypatch.setattr(Mt5V1Transport, "open", unexpected_open)
    monkeypatch.setattr(
        canary,
        "load_bitfinex_test_credentials",
        lambda **_kwargs: pytest.fail("credentials were read"),
    )
    profile, output = _profile(tmp_path), tmp_path / "transcript.jsonl"
    result = canary.run_maker_canary(profile, output)

    assert result.outcome == "VALIDATED"
    assert not any(path.exists() for path in canary._state_paths(profile))
    assert output.stat().st_mode & 0o777 == 0o600


def test_validator_rejects_non_1x_or_non_expressible_quantity(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    canary.validate_maker_canary_profile(profile)
    economics = struct_replace(profile.strategy_config.economics, margin_level=D(500))
    with pytest.raises(ValueError, match="exactly 1x"):
        canary.validate_maker_canary_profile(
            struct_replace(
                profile,
                strategy_config=struct_replace(profile.strategy_config, economics=economics),
            )
        )
    with pytest.raises(ValueError, match="cannot express"):
        canary.validate_maker_canary_profile(
            struct_replace(
                profile,
                bitfinex_data_config=struct_replace(
                    profile.bitfinex_data_config, min_quantity=D(3)
                ),
            )
        )
    with pytest.raises(ValueError, match="paper account and symbol"):
        canary.validate_maker_canary_profile(
            struct_replace(
                profile,
                bitfinex_exec_config=struct_replace(
                    profile.bitfinex_exec_config, rest_url="https://wrong.invalid"
                ),
            )
        )


def test_transcript_cannot_overlap_state_path_before_creation(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    output = Path(profile.bitfinex_exec_config.cid_store_path)
    with pytest.raises(ValueError, match="transcript and state paths"):
        canary.run_maker_canary(profile, output)
    assert not output.exists()


def test_one_shot_latch_calls_base_submission_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[MakerQuote] = []
    monkeypatch.setattr(
        "py000_nautilus.strategies.maker.MakerStrategy._submit_source",
        lambda _self, quote: calls.append(quote),
    )
    strategy, quote = _strategy(tmp_path, lambda *_args: None), _bound_quote(_profile(tmp_path))
    strategy.arm()
    strategy._submit_source(quote)
    strategy._submit_source(quote)
    assert strategy.claimed and calls == [quote]


def test_cached_trigger_immediately_evaluates_one_fresh_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[MakerQuote] = []
    monkeypatch.setattr(
        "py000_nautilus.strategies.maker.MakerStrategy._submit_source",
        lambda _self, quote: submitted.append(quote),
    )
    strategy = _CachedTriggerProbe(_profile(tmp_path), True)
    strategy.arm()

    assert strategy.trigger_cached_once()
    assert strategy.claimed and strategy.freshness_checks == 1
    assert len(submitted) == 1
    assert submitted[0].direction is SourceDirection.LONG
    assert submitted[0].quantity_ounces == D(2)

    with pytest.raises(PaperCanaryError, match="cached trigger is not armed"):
        strategy.trigger_cached_once()
    strategy.on_quote_tick(strategy.source_tick)
    assert len(submitted) == 1 and strategy.freshness_checks == 1


def test_cached_trigger_stale_inputs_submit_nothing_and_disarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[MakerQuote] = []
    monkeypatch.setattr(
        "py000_nautilus.strategies.maker.MakerStrategy._submit_source",
        lambda _self, quote: submitted.append(quote),
    )
    strategy = _CachedTriggerProbe(_profile(tmp_path), False)
    strategy.arm()

    assert not strategy.trigger_cached_once()
    assert strategy.freshness_checks == 1 and submitted == []
    assert not strategy.claimed and not strategy.armed
    with pytest.raises(PaperCanaryError, match="only once"):
        strategy.arm()


def test_cached_trigger_preclaim_exception_disarms_and_propagates(tmp_path: Path) -> None:
    strategy = _CachedTriggerProbe(_profile(tmp_path), RuntimeError("injected"))
    strategy.arm()

    with pytest.raises(RuntimeError, match="injected"):
        strategy.trigger_cached_once()

    assert strategy.freshness_checks == 1
    assert not strategy.claimed and not strategy.armed


@pytest.mark.parametrize(
    ("claim", "expected_reason", "cleanup_saw_armed"),
    [
        (True, "hold_rest_resume_observed", True),
        (False, "cached_quote_not_eligible", False),
    ],
)
def test_lifecycle_cached_trigger_and_pre_cleanup_disarm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claim: bool,
    expected_reason: str,
    cleanup_saw_armed: bool,
) -> None:
    snapshot = PaperSnapshot(
        user_id=USER_ID,
        balance=D(10_000),
        available=D(10_000),
        active_orders=0,
        position_quantity=D(),
        permissions_ok=True,
        withdrawal_disabled=True,
        paper_enabled=True,
    )

    class Node:
        async def run_async(self) -> None:
            await asyncio.Event().wait()

    class Strategy:
        def __init__(self) -> None:
            self.armed = self.claimed = False
            self.failure_reason = None
            self.cleanup_reason = "not_run"
            self.trigger_calls = 0
            self.exposure = asyncio.Event()
            self.resumed = asyncio.Event()
            self.post_resume_quote = asyncio.Event()

        @property
        def resume_ready(self) -> bool:
            return self.claimed

        def planned_price(self) -> Decimal:
            return D(2_000)

        def arm(self) -> None:
            self.armed = True

        def trigger_cached_once(self) -> bool:
            self.trigger_calls += 1
            self.claimed = claim
            if claim:
                self.resumed.set()
            return claim

        def _disarm_unclaimed(self) -> None:
            if not self.claimed:
                self.armed = False

    cleanup_states: list[bool] = []

    async def wait_ready(*_args: object) -> None:
        return None

    async def read_clean(_rest: object) -> PaperSnapshot:
        return snapshot

    async def cleanup(active: Strategy, *_args: object) -> str:
        cleanup_states.append(active.armed)
        return "owned_terminal_settled" if active.claimed else "not_claimed"

    async def shutdown(
        _node: object, task: asyncio.Task[None], _timeout: float,
    ) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(canary, "_wait_ready", wait_ready)
    monkeypatch.setattr(canary, "read_snapshot", read_clean)
    monkeypatch.setattr(canary, "_runtime_flat", lambda *_args: True)
    monkeypatch.setattr(canary, "_cleanup_owned", cleanup)
    monkeypatch.setattr(canary, "_shutdown", shutdown)
    strategy = Strategy()

    _, reason = asyncio.run(
        canary._run_lifecycle(
            cast(Any, Node()),
            cast(Any, strategy),
            cast(Any, object()),
            USER_ID,
            0.1,
            0.1,
        )
    )

    assert reason == expected_reason and strategy.trigger_calls == 1
    assert cleanup_states == [cleanup_saw_armed]
    assert not strategy.post_resume_quote.is_set()


def test_cancel_holds_until_query_then_resumes_without_replacement(tmp_path: Path) -> None:
    completions: list[SourceTerminalResult] = []

    class Exact(canary.MakerCanaryStrategy):
        def _source_cancel_report_is_exact(self, *_args: object) -> bool:
            return True

    def query(_cid: object, _vid: object, complete: SourceTerminalResult) -> None:
        completions.append(complete)

    strategy = Exact(
        _profile(tmp_path).strategy_config,
        live_submission_ready=lambda: True,
        hedge_quantity_ready=lambda _quantity: True,
        live_costs_from_adapters=False,
        source_terminal_query=query,
    )
    _, event = _source_order()
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source("O-CANARY", BusinessOrderSide.BUY, D(2))
    strategy.claimed = strategy.cancel_sent = True
    strategy.on_order_canceled(event)

    assert strategy.hold_before_rest and not strategy.resume_ready
    assert store.active_source_order_id == "O-CANARY" and store.halt_reason
    complete: Any = completions[0]
    complete(object())
    observed: Any = strategy
    if not observed.resumed.is_set() or not observed.resume_ready:
        pytest.fail("exact REST completion did not resume the Maker")
    strategy.on_quote_tick(_quote(_source_instrument(), "2000", "2001", "2", 1))
    assert strategy.post_resume_quote.is_set()
    assert len(store.source_orders()) == 1


def test_qualification_needs_exact_resume_not_a_new_source_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Qualified(canary.MakerCanaryStrategy):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.owned: Order | None = None

        @property
        def source_order(self) -> Order | None:
            return self.owned

    profile = _profile(tmp_path)
    strategy = Qualified(
        profile.strategy_config,
        live_submission_ready=lambda: True,
        hedge_quantity_ready=lambda _quantity: True,
        live_costs_from_adapters=False,
        source_terminal_query=lambda *_args: None,
    )
    order, _ = _source_order()
    strategy.owned = order
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source("O-CANARY", BusinessOrderSide.BUY, D(2))
    store.update_source_status("O-CANARY", "CANCELED")
    store.confirm_source_reconciled("O-CANARY")
    strategy.claimed = strategy.cancel_sent = strategy.hold_before_rest = True
    strategy.venue_order_id = order.venue_order_id
    strategy.cleanup_reason = "owned_terminal_settled"
    strategy.resumed.set()
    snapshot = PaperSnapshot(
        user_id=USER_ID,
        balance=D(10_000),
        available=D(10_000),
        active_orders=0,
        position_quantity=D(),
        permissions_ok=True,
        withdrawal_disabled=True,
        paper_enabled=True,
    )
    evidence = SimpleNamespace(
        fill_indicated=False,
        trade_ids=(),
        active_venue_ids=(),
        complete=True,
        terminal_lifecycle_exact=True,
        record=lambda: {},
    )

    async def read_final(_rest: object) -> PaperSnapshot:
        return snapshot

    async def read_evidence(*_args: object, **_kwargs: object) -> object:
        return evidence

    cid_path = Path(profile.bitfinex_exec_config.cid_store_path)
    cid_path.touch(mode=0o600)
    monkeypatch.setattr(canary, "read_snapshot", read_final)
    monkeypatch.setattr(canary, "read_owned_evidence", read_evidence)
    monkeypatch.setattr(
        canary,
        "BitfinexV1CidStore",
        lambda *_args, **_kwargs: SimpleNamespace(
            binding_for_client=lambda _order_id: SimpleNamespace(cid=7)
        ),
    )
    loop = asyncio.new_event_loop()
    try:
        result = canary._qualify(
            loop,
            cast(Any, object()),
            profile,
            strategy,
            tmp_path / "qualified.jsonl",
            1,
            "hold_rest_resume_observed",
            StringIO(),
        )
    finally:
        loop.close()

    assert result.outcome == "PASSED"
    assert strategy.resume_ready and not strategy.post_resume_quote.is_set()


def test_unexpected_fill_never_dispatches_mt5(tmp_path: Path) -> None:
    class Probe(canary.MakerCanaryStrategy):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.hedge_calls = 0

        def _submit_hedge(
            self,
            direction: SourceDirection,
            hedge_account_id: AccountId,
            hedge_client_id: ClientId | None,
            intent: HedgeIntent,
        ) -> None:
            self.hedge_calls += 1

        def _cancel_working(
            self,
            direction: SourceDirection,
            *,
            expected_order_id: str | None = None,
            reason: str,
        ) -> None:
            pass

    strategy = Probe(
        _profile(tmp_path).strategy_config,
        live_submission_ready=lambda: True,
        hedge_quantity_ready=lambda _quantity: True,
        live_costs_from_adapters=False,
        source_terminal_query=lambda *_args: None,
    )
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(2000),
        client_order_id=ClientOrderId("O-FILL"),
    )
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source("O-FILL", BusinessOrderSide.BUY, D(2))
    strategy.claimed = True
    event = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        last_qty=instrument.make_qty(1),
        commission=Money(0, instrument.quote_currency),
    )
    strategy.on_order_filled(event)

    assert strategy.exposure.is_set() and strategy.filled == 1
    assert strategy.hedge_calls == 0
    record = store.source_order("O-FILL")
    assert record is not None and record.filled_ounces == 1
    intents = store.intents()
    assert len(intents) == 1
    assert intents[0].source_trade_id == event.trade_id.value
    assert store.net_unhedged_ounces == 1
    assert store.source_freeze_reason is not None


def test_cleanup_waits_for_acceptance_then_sends_only_one_exact_cancel(tmp_path: Path) -> None:
    class Probe(canary.MakerCanaryStrategy):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.owned: Order | None = None
            self.cancel_calls = 0

        @property
        def source_order(self) -> Order | None:
            return self.owned

        def cancel_order(self, *args: Any, **kwargs: Any) -> None:
            self.cancel_calls += 1

    strategy = Probe(
        _profile(tmp_path).strategy_config,
        live_submission_ready=lambda: True,
        hedge_quantity_ready=lambda _quantity: True,
        live_costs_from_adapters=False,
        source_terminal_query=lambda *_args: None,
    )
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(2000),
        client_order_id=ClientOrderId("O-CLEANUP"),
    )
    account = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    order.apply(TestEventStubs.order_submitted(order, account_id=account))
    strategy.owned = order
    strategy._stores[SourceDirection.LONG].begin_source(
        "O-CLEANUP", BusinessOrderSide.BUY, D(2)
    )
    strategy.claimed = True

    async def exercise() -> str:
        node_task = asyncio.create_task(asyncio.sleep(1))
        cleanup = asyncio.create_task(canary._cleanup_owned(strategy, node_task, 0.06))
        try:
            await asyncio.sleep(0.02)
            assert strategy.cancel_calls == 0 and not strategy.cancel_sent
            accepted = TestEventStubs.order_accepted(
                order, account_id=account, venue_order_id=VenueOrderId("V-CLEANUP")
            )
            order.apply(accepted)
            strategy.on_order_accepted(accepted)
            return await cleanup
        finally:
            node_task.cancel()
            await asyncio.gather(node_task, return_exceptions=True)

    assert asyncio.run(exercise()) == "owned_cleanup_timeout"
    assert strategy.cancel_calls == 1 and strategy.cancel_sent
    strategy.on_stop()
    assert strategy.cancel_calls == 1


def test_cancel_send_failure_is_not_retried_on_cleanup_or_stop(tmp_path: Path) -> None:
    class Probe(canary.MakerCanaryStrategy):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.owned: Order | None = None
            self.cancel_calls = 0

        @property
        def source_order(self) -> Order | None:
            return self.owned

        def cancel_order(self, *args: Any, **kwargs: Any) -> None:
            self.cancel_calls += 1
            raise RuntimeError("injected")

    strategy = Probe(
        _profile(tmp_path).strategy_config,
        live_submission_ready=lambda: True,
        hedge_quantity_ready=lambda _quantity: True,
        live_costs_from_adapters=False,
        source_terminal_query=lambda *_args: None,
    )
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(2000),
        client_order_id=ClientOrderId("O-CANCEL-FAIL"),
    )
    account = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    order.apply(TestEventStubs.order_submitted(order, account_id=account))
    accepted = TestEventStubs.order_accepted(
        order, account_id=account, venue_order_id=VenueOrderId("V-CANCEL-FAIL")
    )
    order.apply(accepted)
    strategy.owned = order
    strategy._stores[SourceDirection.LONG].begin_source(
        "O-CANCEL-FAIL", BusinessOrderSide.BUY, D(2)
    )
    strategy.claimed = True
    strategy.on_order_accepted(accepted)

    async def exercise() -> str:
        node_task = asyncio.create_task(asyncio.sleep(1))
        try:
            return await canary._cleanup_owned(strategy, node_task, 0.02)
        finally:
            node_task.cancel()
            await asyncio.gather(node_task, return_exceptions=True)

    assert asyncio.run(exercise()) == "owned_cancel_send_failed"
    strategy.on_stop()
    assert strategy.cancel_calls == 1


def test_acceptance_store_failure_still_sends_exact_cancel_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Probe(canary.MakerCanaryStrategy):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.owned: Order | None = None
            self.cancel_calls = 0

        @property
        def source_order(self) -> Order | None:
            return self.owned

        def cancel_order(self, *args: Any, **kwargs: Any) -> None:
            self.cancel_calls += 1

    strategy = Probe(
        _profile(tmp_path).strategy_config,
        live_submission_ready=lambda: True,
        hedge_quantity_ready=lambda _quantity: True,
        live_costs_from_adapters=False,
        source_terminal_query=lambda *_args: None,
    )
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(2000),
        client_order_id=ClientOrderId("O-ACCEPT-STORE-FAIL"),
    )
    account = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    order.apply(TestEventStubs.order_submitted(order, account_id=account))
    accepted = TestEventStubs.order_accepted(
        order, account_id=account, venue_order_id=VenueOrderId("V-ACCEPT-STORE-FAIL")
    )
    order.apply(accepted)
    strategy.owned = order
    store = strategy._stores[SourceDirection.LONG]
    store.begin_source("O-ACCEPT-STORE-FAIL", BusinessOrderSide.BUY, D(2))
    strategy.claimed = True

    def fail_status(_order_id: str, _status: str) -> None:
        raise OSError("injected store failure")

    monkeypatch.setattr(store, "update_source_status", fail_status)
    with pytest.raises(OSError, match="injected store failure"):
        strategy.on_order_accepted(accepted)

    assert strategy.venue_order_id == accepted.venue_order_id
    assert strategy.cancel_calls == 1 and strategy.cancel_sent


def test_claimed_exception_attempts_evidence_once_and_cannot_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _strategy(tmp_path, lambda *_args: None)
    strategy.claimed = True
    calls: list[bool] = []

    def qualify(*_args: Any, **kwargs: Any) -> canary.MakerCanaryResult:
        calls.append(kwargs["allow_pass"])
        return canary.MakerCanaryResult("PASSED", "injected", tmp_path / "evidence")

    monkeypatch.setattr(canary, "_qualify", qualify)
    loop, transcript = asyncio.new_event_loop(), StringIO()
    rest: Any = object()
    try:
        result = canary._exception_result(
            loop, rest, _profile(tmp_path), strategy, tmp_path / "out",
            1, "injected_error", transcript, False, unclaimed="FAILED",
        )
        second = canary._exception_result(
            loop, rest, _profile(tmp_path), strategy, tmp_path / "out",
            1, "second", transcript, True, unclaimed="FAILED",
        )
    finally:
        loop.close()
    assert result.outcome == second.outcome == "UNKNOWN"
    assert calls == [False]


def test_claimed_canceled_node_task_gets_one_evidence_attempt_and_finished_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path)
    snapshot = PaperSnapshot(
        user_id=USER_ID,
        balance=D(10_000),
        available=D(10_000),
        active_orders=0,
        position_quantity=D(),
        permissions_ok=True,
        withdrawal_disabled=True,
        paper_enabled=True,
    )
    strategy = _strategy(tmp_path, lambda *_args: None)
    evidence_calls: list[bool] = []

    async def read_clean(_rest: object) -> object:
        return snapshot

    async def canceled_lifecycle(
        _node: object, active: canary.MakerCanaryStrategy, *_args: object,
    ) -> tuple[object, str]:
        active.claimed = True
        task = asyncio.create_task(asyncio.sleep(0))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        canary._raise_node_task(task, "injected node stop")
        raise AssertionError("unreachable")

    def builder(**_kwargs: object) -> tuple[Any, canary.MakerCanaryStrategy]:
        return object(), strategy

    def qualify(*_args: object, **kwargs: object) -> canary.MakerCanaryResult:
        evidence_calls.append(kwargs["allow_pass"] is True)
        return canary.MakerCanaryResult("UNKNOWN", "observed", tmp_path / "out")

    monkeypatch.setattr(canary, "_lock_canary", lambda _user_id: StringIO())
    monkeypatch.setattr(
        canary,
        "load_bitfinex_test_credentials",
        lambda **_kwargs: SimpleNamespace(api_key="paper", api_secret="paper"),
    )
    monkeypatch.setattr(canary, "BitfinexV1RestClient", lambda **_kwargs: object())
    monkeypatch.setattr(canary, "read_snapshot", read_clean)
    monkeypatch.setattr(canary, "_run_lifecycle", canceled_lifecycle)
    monkeypatch.setattr(canary, "_qualify", qualify)
    monkeypatch.setattr(canary, "_dispose_node", lambda _node: None)
    output = tmp_path / "canceled-task.jsonl"

    result = canary.run_maker_canary(profile, output, execute=True, node_builder=builder)

    assert result.outcome == "UNKNOWN"
    assert evidence_calls == [False]
    records = output.read_text(encoding="utf-8").splitlines()
    assert records and '"kind":"finished"' in records[-1]


def test_post_rest_freshness_timeout_holds_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path)
    snapshot = PaperSnapshot(
        user_id=USER_ID,
        balance=D(10_000),
        available=D(10_000),
        active_orders=0,
        position_quantity=D(),
        permissions_ok=True,
        withdrawal_disabled=True,
        paper_enabled=True,
    )
    strategy = _strategy(tmp_path, lambda *_args: None)
    lifecycle: list[str] = []
    built = False
    owner_loop: asyncio.AbstractEventLoop | None = None

    class Node:
        async def run_async(self) -> None:
            await asyncio.Event().wait()

    async def read_clean(_rest: object) -> PaperSnapshot:
        if built:
            lifecycle.append("snapshot")
        return snapshot

    async def wait_ready(
        _node: object,
        _strategy: canary.MakerCanaryStrategy,
        _task: asyncio.Task[None],
        _timeout: float,
    ) -> None:
        lifecycle.append("initial_ready" if not lifecycle else "post_rest_ready")
        if lifecycle[-1] == "post_rest_ready":
            raise PaperCanaryError("Maker readiness timed out")

    async def shutdown(
        _node: object, task: asyncio.Task[None], _timeout: float,
    ) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def builder(**kwargs: object) -> tuple[Any, canary.MakerCanaryStrategy]:
        nonlocal built, owner_loop
        built = True
        owner_loop = kwargs["loop"]  # type: ignore[assignment]
        return Node(), strategy

    def runtime_flat(_node: object, _strategy: canary.MakerCanaryStrategy) -> bool:
        lifecycle.append("runtime_flat")
        return True

    def dispose(_node: object) -> None:
        assert owner_loop is not None
        owner_loop.close()

    monkeypatch.setattr(canary, "_lock_canary", lambda _user_id: StringIO())
    monkeypatch.setattr(
        canary,
        "load_bitfinex_test_credentials",
        lambda **_kwargs: SimpleNamespace(api_key="paper", api_secret="paper"),
    )
    monkeypatch.setattr(canary, "BitfinexV1RestClient", lambda **_kwargs: object())
    monkeypatch.setattr(canary, "read_snapshot", read_clean)
    monkeypatch.setattr(canary, "_wait_ready", wait_ready)
    monkeypatch.setattr(canary, "_runtime_flat", runtime_flat)
    monkeypatch.setattr(canary, "_shutdown", shutdown)
    monkeypatch.setattr(canary, "_dispose_node", dispose)
    output = tmp_path / "post-rest-stale.jsonl"

    result = canary.run_maker_canary(profile, output, execute=True, node_builder=builder)

    assert lifecycle == ["initial_ready", "snapshot", "runtime_flat", "post_rest_ready"]
    assert result.outcome == "HOLD"
    assert result.reason == "Maker readiness timed out"
    assert result.source_order_id is None
    assert not strategy.armed and not strategy.claimed
