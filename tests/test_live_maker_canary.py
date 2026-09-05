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
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.events import OrderCancelRejected
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
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
from py000_nautilus.live_maker import build_live_maker_node
from py000_nautilus.models import (
    BusinessOrderSide,
    HedgeIntent,
    HedgeLeg,
    MakerAccount,
    MakerQuote,
    ObligationStatus,
    SourceAccount,
    SourceDirection,
)
from py000_nautilus.mt5_v1_data import Mt5V1DataClientConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecClientConfig
from py000_nautilus.mt5_v1_transport import Mt5V1Transport
from py000_nautilus.strategies.maker import MakerStrategy, SourceTerminalResult

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


def _roundtrip_strategy(
    tmp_path: Path, direction: SourceDirection = SourceDirection.LONG
) -> canary.MakerRoundtripStrategy:
    return canary.MakerRoundtripStrategy(
        _profile(tmp_path).strategy_config,
        direction=direction,
        live_submission_ready=lambda: True,
        hedge_quantity_ready=lambda _quantity: True,
        live_costs_from_adapters=False,
        source_terminal_query=lambda *_args: None,
    )


class _RoundtripCancelProbe(canary.MakerRoundtripStrategy):
    def __init__(self, profile: canary.LiveMakerCanaryProfile) -> None:
        super().__init__(
            profile.strategy_config,
            direction=SourceDirection.LONG,
            live_submission_ready=lambda: True,
            hedge_quantity_ready=lambda _quantity: True,
            live_costs_from_adapters=False,
            source_terminal_query=lambda *_args: None,
        )
        self.owned: Order | None = None
        self.cancel_calls = 0
        self.observation_failure: str | None = None
        self._test_cache = SimpleNamespace(
            order=self._order,
            quote_tick=self._quote_tick,
        )
        self._test_clock = SimpleNamespace(timestamp_ns=self._timestamp_ns)

    @property
    def cache(self) -> Any:
        return self._test_cache

    @property
    def clock(self) -> Any:
        return self._test_clock

    def _order(self, client_order_id: ClientOrderId) -> Order | None:
        if self.owned is None or self.owned.client_order_id != client_order_id:
            return None
        return self.owned

    def _quote_tick(self, _instrument_id: InstrumentId) -> None:
        if self.observation_failure == "cache":
            raise RuntimeError("injected quote cache failure")

    def _timestamp_ns(self) -> int:
        if self.observation_failure == "clock":
            raise RuntimeError("injected clock failure")
        return 10_000_000_000

    def cancel_order(self, *_args: object, **_kwargs: object) -> None:
        self.cancel_calls += 1


class _RoundtripReadinessProbe:
    def __init__(self, profile: canary.LiveMakerCanaryProfile) -> None:
        self._config = profile.strategy_config
        self.now_ns = 10_000_000_000
        self.is_running = True
        self._live_costs_from_adapters = True
        self._hedge_instrument_valid = True
        self._hedge_instrument = SimpleNamespace(ts_event=self.now_ns)
        self._cost_snapshot_valid = True
        self._cost_ts_ns = self.now_ns
        self._session_ts_ns = self.now_ns
        self._hedge_session_open = True
        self.invalidations: list[str] = []
        self.ticks = {
            SOURCE_ID: _quote(_source_instrument(), "2000", "2001", "2", self.now_ns),
            HEDGE_ID: _quote(_hedge_instrument(), "2000", "2001", "2", self.now_ns),
        }
        self.cache = SimpleNamespace(quote_tick=self.ticks.get)
        self.clock = SimpleNamespace(timestamp_ns=lambda: self.now_ns)
        self._live_submission_ready = lambda: True

    def _required_hedge_instrument(self) -> Any:
        return self._hedge_instrument

    def _inputs_are_fresh(
        self,
        source_tick: Any,
        hedge_tick: Any,
        timestamp_ns: int,
    ) -> bool:
        return MakerStrategy._inputs_are_fresh(
            cast(Any, self), source_tick, hedge_tick, timestamp_ns
        )

    def _invalidate_cost_snapshot(self, reason: str) -> None:
        self.invalidations.append(reason)
        self._cost_snapshot_valid = False

    def quote(self, instrument_id: InstrumentId, ts_event: int) -> None:
        instrument = _source_instrument() if instrument_id == SOURCE_ID else _hedge_instrument()
        self.ticks[instrument_id] = _quote(instrument, "2000", "2001", "2", ts_event)


class _ConnectedEngine:
    @staticmethod
    def check_connected() -> bool:
        return True


class _RoundtripReadinessNode:
    kernel = SimpleNamespace(
        data_engine=_ConnectedEngine(),
        exec_engine=_ConnectedEngine(),
    )

    @staticmethod
    def is_running() -> bool:
        return True


async def _run_forever() -> None:
    await asyncio.Event().wait()


def _submitted_roundtrip_source(
    strategy: _RoundtripCancelProbe,
) -> tuple[Order, AccountId]:
    instrument = _source_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(2_000),
        client_order_id=ClientOrderId("O-ROUNDTRIP-CLEANUP"),
    )
    account = AccountId(f"BITFINEX-PAPER-{USER_ID}")
    submitted = TestEventStubs.order_submitted(order, account_id=account)
    order.apply(submitted)
    strategy.owned = order
    strategy._stores[strategy.direction].begin_source(
        order.client_order_id.value,
        BusinessOrderSide.BUY,
        D(2),
    )
    strategy.on_order_submitted(submitted)
    strategy.claimed = True
    return order, account


def _cancel_rejected(
    order: Order,
    account: AccountId,
    reason: str,
) -> OrderCancelRejected:
    return OrderCancelRejected(
        trader_id=order.trader_id,
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=None,
        account_id=account,
        reason=reason,
        event_id=UUID4(),
        ts_event=2,
        ts_init=2,
    )


def _two_leg_inflight_hedge(
    strategy: canary.MakerRoundtripStrategy,
) -> tuple[Any, HedgeIntent, Any]:
    store = strategy._stores[strategy.direction]
    source_route = strategy._config.source_accounts[0]
    hedge_route = strategy._config.hedge_accounts[0]
    store.begin_source(
        "O-CLEANUP-HEDGE",
        BusinessOrderSide.BUY,
        D(2),
        source_account_id=source_route.account_id.value,
        source_client_id=source_route.client_id.value if source_route.client_id else None,
        hedge_account_id=hedge_route.account_id.value,
        hedge_client_id=hedge_route.client_id.value if hedge_route.client_id else None,
    )
    intent = store.reserve_source_fill(
        fill_key="O-CLEANUP-HEDGE|V|T",
        client_order_id="O-CLEANUP-HEDGE",
        trade_id="T-CLEANUP-HEDGE",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None
    store.bind_hedge_plan(
        intent.intent_id,
        (
            HedgeLeg(BusinessOrderSide.SELL, D(1)),
            HedgeLeg(BusinessOrderSide.SELL, D(1)),
        ),
    )
    hedge = _hedge_instrument()
    order = TestExecStubs.market_order(
        instrument=hedge,
        order_side=OrderSide.SELL,
        quantity=hedge.make_qty(1),
        client_order_id=ClientOrderId("H-CLEANUP-LEG-1"),
        time_in_force=TimeInForce.FOK,
    )
    store.bind_hedge_order(intent.intent_id, order.client_order_id.value)
    store.update_hedge_status(order.client_order_id.value, ObligationStatus.ACCEPTED)
    event = TestEventStubs.order_filled(
        order=order,
        instrument=hedge,
        last_qty=hedge.make_qty(1),
        commission=Money(0, hedge.quote_currency),
    )
    return store, intent, event


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
    assert complete(object()) is True
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


def test_roundtrip_arm_ready_waits_for_same_bbo_refresh_with_headroom(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        probe = _RoundtripReadinessProbe(_profile(tmp_path))
        config, now_ns = probe._config, probe.now_ns
        probe.quote(SOURCE_ID, now_ns - config.max_quote_age_ns - 1)
        stale_source = probe.ticks[SOURCE_ID]
        probe.quote(HEDGE_ID, now_ns - config.max_cross_leg_skew_ns)
        node_task = asyncio.create_task(_run_forever())
        readiness = asyncio.create_task(
            canary._wait_roundtrip_arm_ready(
                cast(Any, _RoundtripReadinessNode()),
                cast(Any, probe),
                node_task,
                timeout=1.0,
            )
        )
        try:
            await asyncio.sleep(0.02)
            assert not readiness.done()

            probe.quote(SOURCE_ID, now_ns)
            heartbeat_source = probe.ticks[SOURCE_ID]
            assert heartbeat_source.bid_price == stale_source.bid_price
            assert heartbeat_source.ask_price == stale_source.ask_price
            assert heartbeat_source.bid_size == stale_source.bid_size
            assert heartbeat_source.ask_size == stale_source.ask_size
            await asyncio.sleep(0.02)
            assert not readiness.done()

            probe.quote(HEDGE_ID, now_ns - canary._roundtrip_arm_headroom_ns(config))

            await asyncio.wait_for(readiness, timeout=0.5)
        finally:
            if not readiness.done():
                readiness.cancel()
            node_task.cancel()
            await asyncio.gather(readiness, node_task, return_exceptions=True)

    asyncio.run(scenario())


def test_roundtrip_arm_ready_invalidates_future_cost_until_new_snapshot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        probe = _RoundtripReadinessProbe(_profile(tmp_path))
        probe._cost_ts_ns = probe.now_ns + 1
        node_task = asyncio.create_task(_run_forever())
        readiness = asyncio.create_task(
            canary._wait_roundtrip_arm_ready(
                cast(Any, _RoundtripReadinessNode()),
                cast(Any, probe),
                node_task,
                timeout=0.5,
            )
        )
        try:
            await asyncio.sleep(0.03)
            assert probe.invalidations == ["Maker costs are future-dated"]
            probe._cost_ts_ns = probe.now_ns
            await asyncio.sleep(0.12)
            assert not readiness.done()
            probe.now_ns += 1
            probe._cost_ts_ns = probe.now_ns
            probe._cost_snapshot_valid = True
            probe.quote(SOURCE_ID, probe.now_ns - 2)
            await asyncio.sleep(0.12)
            assert not readiness.done()
            probe.quote(SOURCE_ID, probe.now_ns)
            await asyncio.wait_for(readiness, timeout=0.5)
        finally:
            if not readiness.done():
                readiness.cancel()
            node_task.cancel()
            await asyncio.gather(readiness, node_task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("direction", [SourceDirection.LONG, SourceDirection.SHORT])
def test_roundtrip_dry_run_builds_only_selected_direction(
    tmp_path: Path,
    direction: SourceDirection,
) -> None:
    profile = _profile(tmp_path)
    built: list[canary.MakerRoundtripStrategy] = []

    def builder(**kwargs: Any) -> tuple[Any, canary.MakerRoundtripStrategy]:
        node, strategy = build_live_maker_node(**kwargs)
        assert isinstance(strategy, canary.MakerRoundtripStrategy)
        strategy.first_cancel_diagnostic = {"reason": "serialized-probe"}
        built.append(strategy)
        return node, strategy

    output = tmp_path / f"roundtrip-{direction.value}.jsonl"
    result = canary.run_maker_canary(
        profile,
        output,
        node_builder=cast(Any, builder),
        roundtrip_direction=direction,
    )

    assert result.outcome == "VALIDATED"
    assert len(built) == 1 and built[0].direction is direction
    bid = built[0]._config.economics.bid.open_quantity_ounces
    ask = built[0]._config.economics.ask.open_quantity_ounces
    assert (bid, ask) == ((D(2), D()) if direction is SourceDirection.LONG else (D(), D(2)))
    assert profile.strategy_config.economics.bid.open_quantity_ounces == D(2)
    assert profile.strategy_config.economics.ask.open_quantity_ounces == 0
    assert (
        '"first_cancel":{"reason":"serialized-probe"}'
        in output.read_text(encoding="utf-8").splitlines()[-1]
    )


@pytest.mark.parametrize("direction", [SourceDirection.LONG, SourceDirection.SHORT])
def test_roundtrip_flatten_is_ioc_reduce_only_and_submit_unknown_is_not_retried(
    tmp_path: Path,
    direction: SourceDirection,
) -> None:
    expected = D(2) if direction is SourceDirection.LONG else D(-2)
    ticks = {
        "value": _quote(_source_instrument(), "2000", "2001", "1", 1_000_000_000)
    }
    factory_calls: list[dict[str, Any]] = []
    submit_calls: list[object] = []

    class Factory:
        def limit(self, **kwargs: Any) -> object:
            factory_calls.append(kwargs)
            return SimpleNamespace(
                client_order_id=ClientOrderId("O-ROUNDTRIP-CLOSE"),
                quantity=kwargs["quantity"],
            )

    class Probe(canary.MakerRoundtripStrategy):
        def __init__(self) -> None:
            super().__init__(
                _profile(tmp_path).strategy_config,
                direction=direction,
                live_submission_ready=lambda: True,
                hedge_quantity_ready=lambda _quantity: True,
                live_costs_from_adapters=False,
                source_terminal_query=lambda *_args: None,
            )
            self._source_instrument = _source_instrument()
            self._test_cache = SimpleNamespace(
                quote_tick=lambda _instrument_id: ticks["value"]
            )
            self._test_clock = SimpleNamespace(timestamp_ns=lambda: 1_000_000_000)
            self._test_factory = Factory()

        @property
        def cache(self) -> Any:
            return self._test_cache

        @property
        def clock(self) -> Any:
            return self._test_clock

        @property
        def order_factory(self) -> Any:
            return self._test_factory

        def _source_account(self, account_id: AccountId) -> SourceAccount:
            route = self._config.source_accounts[0]
            return SourceAccount(account_id, route.client_id, expected, D(2), D(2), D(100))

        def _hedge_account(self, account_id: AccountId) -> MakerAccount:
            route = self._config.hedge_accounts[0]
            return MakerAccount(account_id, route.client_id, -expected, D(2), D(2))

        def _source_hedge_is_executable(
            self, _quote: MakerQuote, _source_quantity_ounces: Decimal
        ) -> bool:
            return True

        def _hedge_positions(self, _account_id: AccountId) -> list[Any]:
            hedge_is_long = -expected > 0
            return [
                SimpleNamespace(
                    id=PositionId("P-ROUNDTRIP"),
                    quantity=D(2),
                    is_long=hedge_is_long,
                    is_short=not hedge_is_long,
                )
            ]

        def submit_order(self, order: object, **_kwargs: Any) -> None:
            submit_calls.append(order)
            raise RuntimeError("injected uncertain submit")

    assert not Probe().submit_flatten_once()
    assert factory_calls == [] and submit_calls == []
    ticks["value"] = _quote(_source_instrument(), "2000", "2001", "2", 1_000_000_000)
    strategy = Probe()
    with pytest.raises(RuntimeError, match="uncertain submit"):
        strategy.submit_flatten_once()

    call = factory_calls[0]
    expected_side = OrderSide.SELL if direction is SourceDirection.LONG else OrderSide.BUY
    assert call["order_side"] is expected_side
    assert call["time_in_force"] is TimeInForce.IOC
    assert call["post_only"] is False and call["reduce_only"] is True
    assert Decimal(str(call["quantity"])) == D(2)
    record = strategy._stores[strategy.close_direction].source_orders()[0]
    assert record.status == "UNKNOWN" and len(submit_calls) == 1
    assert not strategy.submit_flatten_once() and len(factory_calls) == 1


def test_roundtrip_phase_exact_accepts_multi_ticket_mt5_close(tmp_path: Path) -> None:
    strategy = _roundtrip_strategy(tmp_path)
    store = strategy._stores[SourceDirection.SHORT]
    store.begin_source("O-CLOSE", BusinessOrderSide.SELL, D(2))
    intent = store.reserve_source_fill(
        fill_key="O-CLOSE|V|T",
        client_order_id="O-CLOSE",
        trade_id="T",
        source_side=BusinessOrderSide.SELL,
        fill_ounces=D(2),
    )
    assert intent is not None
    store.bind_hedge_plan(
        intent.intent_id,
        tuple(
            HedgeLeg(
                side=BusinessOrderSide.BUY,
                quantity_ounces=D(1),
                position_id=f"P-{ticket}",
                expected_position_side=BusinessOrderSide.SELL,
                expected_position_quantity_ounces=D(1),
            )
            for ticket in (1, 2)
        ),
    )
    for ticket in (1, 2):
        store.bind_hedge_order(intent.intent_id, f"H-{ticket}")
        assert store.apply_hedge_fill(
            client_order_id=f"H-{ticket}", trade_id=f"HT-{ticket}", fill_ounces=D(1)
        )

    assert strategy.phase_exact(SourceDirection.SHORT, closing=True)
    assert len(store.intent(intent.intent_id).hedge_order_ids) == 2


def test_roundtrip_close_never_submits_an_open_mt5_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy = _roundtrip_strategy(tmp_path)
    store = strategy._stores[strategy.close_direction]
    store.begin_source("O-DRIFT", BusinessOrderSide.SELL, D(2))
    intent = store.reserve_source_fill(
        fill_key="O-DRIFT|V|T",
        client_order_id="O-DRIFT",
        trade_id="T",
        source_side=BusinessOrderSide.SELL,
        fill_ounces=D(2),
    )
    assert intent is not None
    monkeypatch.setattr(strategy, "_hedge_positions", lambda _account_id: [])
    submissions: list[object] = []
    monkeypatch.setattr(
        "py000_nautilus.strategies.maker.MakerStrategy._submit_hedge",
        lambda *_args, **_kwargs: submissions.append(object()),
    )

    route = strategy._config.hedge_accounts[0]
    strategy._submit_hedge(strategy.close_direction, route.account_id, route.client_id, intent)

    held = store.intent(intent.intent_id)
    assert submissions == []
    assert held.status is ObligationStatus.BLOCKED
    assert held.hedge_plan and all(not leg.is_close for leg in held.hedge_plan)


def test_roundtrip_final_hedge_orders_bind_exact_shapes_and_ticket_ids(tmp_path: Path) -> None:
    strategy = _roundtrip_strategy(tmp_path)
    open_leg = HedgeLeg(BusinessOrderSide.SELL, D(2))
    close_legs = [
        HedgeLeg(
            BusinessOrderSide.BUY,
            D(1),
            position_id="P-1",
            expected_position_side=BusinessOrderSide.SELL,
            expected_position_quantity_ounces=D(expected),
        )
        for expected in (2, 1)
    ]
    strategy._stores[strategy.direction] = cast(
        Any,
        SimpleNamespace(
            intents=lambda: (
                SimpleNamespace(
                    hedge_plan=(open_leg,),
                    hedge_order_ids=("H-OPEN",),
                ),
            )
        ),
    )
    strategy._stores[strategy.close_direction] = cast(
        Any,
        SimpleNamespace(
            intents=lambda: (
                SimpleNamespace(
                    hedge_plan=(close_legs[0],),
                    hedge_order_ids=("H-CLOSE-1",),
                ),
                SimpleNamespace(
                    hedge_plan=(close_legs[1],),
                    hedge_order_ids=("H-CLOSE-2",),
                ),
            )
        ),
    )
    def quantity(value: int) -> object:
        return SimpleNamespace(as_decimal=lambda: D(value))

    account_id = strategy._config.hedge_accounts[0].account_id
    orders = {
        ClientOrderId(order_id): SimpleNamespace(
            instrument_id=HEDGE_ID,
            account_id=account_id,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.FOK,
            side=OrderSide.SELL if "OPEN" in order_id else OrderSide.BUY,
            is_reduce_only="CLOSE" in order_id,
            quantity=quantity(2 if order_id == "H-OPEN" else 1),
            filled_qty=quantity(2 if order_id == "H-OPEN" else 1),
            is_closed=True,
        )
        for order_id in ("H-OPEN", "H-CLOSE-1", "H-CLOSE-2")
    }
    positions = {
        ClientOrderId(order_id): PositionId("P-1")
        for order_id in ("H-OPEN", "H-CLOSE-1", "H-CLOSE-2")
    }
    node = SimpleNamespace(
        cache=SimpleNamespace(order=orders.get, position_id=positions.get)
    )

    assert canary._hedge_orders_exact(
        cast(Any, node), strategy, strategy.direction, closing=False
    )
    assert canary._hedge_orders_exact(
        cast(Any, node), strategy, strategy.close_direction, closing=True
    )
    orders[ClientOrderId("H-CLOSE-2")].is_reduce_only = False
    assert not canary._hedge_orders_exact(
        cast(Any, node), strategy, strategy.close_direction, closing=True
    )


@pytest.mark.parametrize(
    ("final_position", "task_stops", "expected_outcome", "expected_reason"),
    [
        (D(), False, "PASSED_FLAT", "maker_roundtrip_exact_flat"),
        (D("0.1"), False, "HOLD", "final_flat_evidence_not_exact"),
        (D(), True, "HOLD", "final_flat_evidence_not_exact"),
    ],
)
def test_roundtrip_final_gate_requires_external_exact_flat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_position: Decimal,
    task_stops: bool,
    expected_outcome: str,
    expected_reason: str,
) -> None:
    profile = _profile(tmp_path)
    snapshots = iter(
        PaperSnapshot(
            user_id=USER_ID,
            balance=D(10_000),
            available=D(10_000),
            active_orders=0,
            position_quantity=position,
            permissions_ok=True,
            withdrawal_disabled=True,
            paper_enabled=True,
        )
        for position in (D(), D(2), final_position)
    )
    strategy = _roundtrip_strategy(tmp_path)
    flatten_calls: list[None] = []

    class ExecEngine:
        async def reconcile_execution_state(self, **_kwargs: object) -> bool:
            return True

    class Runtime:
        last_failure = None

        async def reconcile(self, *, retry_failed: bool = True) -> bool:
            assert retry_failed is False
            return True  # Only final-evidence classification is in this unit test.

    class Node:
        kernel = SimpleNamespace(exec_engine=ExecEngine())

        async def run_async(self) -> None:
            if not task_stops:
                await asyncio.Event().wait()

        def is_running(self) -> bool:
            return True

    async def wait_ready(*_args: object) -> None:
        await asyncio.sleep(0)

    async def read_next(_rest: object) -> PaperSnapshot:
        return next(snapshots)

    async def phase(*_args: object) -> None:
        return None

    async def cleanup(*_args: object) -> None:
        return None

    async def shutdown(
        _node: object, task: asyncio.Task[None], _timeout: float
    ) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def flatten_once() -> bool:
        flatten_calls.append(None)
        return True

    monkeypatch.setattr(canary, "_wait_ready", wait_ready)
    monkeypatch.setattr(canary, "get_source_terminal_reconciler", lambda _node: Runtime())
    monkeypatch.setattr(canary, "_wait_roundtrip_arm_ready", wait_ready)
    monkeypatch.setattr(canary, "read_snapshot", read_next)
    monkeypatch.setattr(canary, "_wait_roundtrip_phase", phase)
    monkeypatch.setattr(canary, "_runtime_flat", lambda *_args: True)
    monkeypatch.setattr(canary, "_runtime_pair_exact", lambda *_args: True)
    monkeypatch.setattr(canary, "_roundtrip_orders_exact", lambda *_args: True)
    monkeypatch.setattr(canary, "_cleanup_roundtrip_sources", cleanup)
    monkeypatch.setattr(canary, "_shutdown", shutdown)
    monkeypatch.setattr(strategy, "trigger_cached_once", lambda: True)
    monkeypatch.setattr(strategy, "phase_exact", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(strategy, "submit_flatten_once", flatten_once)

    result = asyncio.run(
        canary._run_roundtrip_lifecycle(
            cast(Any, Node()),
            strategy,
            cast(Any, object()),
            profile,
            tmp_path / "roundtrip.jsonl",
            StringIO(),
            1,
        )
    )

    assert result.outcome == expected_outcome
    assert result.reason == expected_reason
    assert flatten_calls == [None]


@pytest.mark.parametrize(
    (
        "bitfinex_ack_ms",
        "bitfinex_rest_seconds",
        "mt5_mutation_ms",
        "mt5_poll_ms",
        "mt5_request_ms",
        "mt5_pagination_ms",
        "connection_seconds",
        "expected_seconds",
    ),
    [
        (10_000, 10, 15_000, 250, 1_000, 30_000, 2.0, 80.0),
        (60_000, 60, 50, 250, 50, 50, 1.0, 241.0),
        (100, 1, 60_000, 60_000, 60_000, 60_000, 1.0, 421.0),
        (100, 1, 50, 250, 50, 50, 60.0, 13.0),
        (100, 1, 50, 60_000, 50, 50, 1.0, 61.3),
    ],
)
def test_roundtrip_lifecycle_passes_bounded_sequential_cleanup_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bitfinex_ack_ms: int,
    bitfinex_rest_seconds: int,
    mt5_mutation_ms: int,
    mt5_poll_ms: int,
    mt5_request_ms: int,
    mt5_pagination_ms: int,
    connection_seconds: float,
    expected_seconds: float,
) -> None:
    base = _profile(tmp_path)
    profile = struct_replace(
        base,
        bitfinex_exec_config=struct_replace(
            base.bitfinex_exec_config,
            mutation_ack_timeout_ms=bitfinex_ack_ms,
            rest_timeout_secs=bitfinex_rest_seconds,
        ),
        mt5_exec_config=struct_replace(
            base.mt5_exec_config,
            mutation_timeout_ms=mt5_mutation_ms,
            event_poll_interval_ms=mt5_poll_ms,
            request_timeout_ms=mt5_request_ms,
            event_pagination_timeout_ms=mt5_pagination_ms,
            snapshot_refresh_interval_ms=max(1_000, mt5_poll_ms),
        ),
        connection_timeout_seconds=connection_seconds,
    )
    strategy = _roundtrip_strategy(tmp_path)
    cleanup_timeouts: list[float] = []

    class Node:
        async def run_async(self) -> None:
            await asyncio.Event().wait()

    async def stop_before_arm(*_args: object) -> None:
        raise PaperCanaryError("stop before arm")

    async def cleanup(_strategy: object, _task: object, timeout: float) -> None:
        cleanup_timeouts.append(timeout)

    async def shutdown(_node: object, task: asyncio.Task[None], _timeout: float) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(canary, "_wait_ready", stop_before_arm)
    monkeypatch.setattr(canary, "_cleanup_roundtrip_sources", cleanup)
    monkeypatch.setattr(canary, "_shutdown", shutdown)

    with pytest.raises(PaperCanaryError, match="stop before arm"):
        asyncio.run(
            canary._run_roundtrip_lifecycle(
                cast(Any, Node()),
                strategy,
                cast(Any, object()),
                profile,
                tmp_path / "roundtrip-budget.jsonl",
                StringIO(),
                1,
            )
        )

    assert cleanup_timeouts == [pytest.approx(expected_seconds)]
    assert cleanup_timeouts[0] >= mt5_mutation_ms / 1_000
    assert cleanup_timeouts[0] >= min(connection_seconds * 2, 12.0) + 1.0
    assert cleanup_timeouts[0] <= 421.0


def test_roundtrip_timeout_cleanup_marks_unproved_gtc_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy = _roundtrip_strategy(tmp_path)
    store = strategy._stores[strategy.direction]
    store.begin_source("O-OPEN", BusinessOrderSide.BUY, D(2))
    cancellations: list[SourceDirection] = []
    monkeypatch.setattr(
        strategy,
        "_cancel_working",
        lambda direction, **_kwargs: cancellations.append(direction),
    )

    async def exercise() -> None:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        try:
            await canary._cleanup_roundtrip_sources(strategy, task, 0.02)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())

    record = store.source_order("O-OPEN")
    assert record is not None and record.status == "UNKNOWN"
    assert store.halt_reason == "roundtrip shutdown did not prove terminal"
    assert cancellations == [strategy.direction, strategy.close_direction]
    failure = canary._roundtrip_failure(
        strategy, tmp_path / "roundtrip.jsonl", "timeout", True
    )
    assert failure.outcome == "HOLD"


def test_roundtrip_pre_ack_stale_cancel_is_sent_immediately_on_acceptance(
    tmp_path: Path,
) -> None:
    strategy = _RoundtripCancelProbe(_profile(tmp_path))
    order, account = _submitted_roundtrip_source(strategy)
    strategy._cancel_working(strategy.direction, reason="stale quote")
    assert strategy.cancel_calls == 0
    first = strategy.first_cancel_diagnostic
    assert first is not None
    assert first["reason"] == "stale quote"
    assert first["source_order_id"] == order.client_order_id.value

    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account,
        venue_order_id=VenueOrderId("V-ROUNDTRIP-CLEANUP"),
    )
    order.apply(accepted)
    strategy.on_order_accepted(accepted)

    assert strategy.cancel_calls == 1
    strategy._cancel_working(strategy.direction, reason="cleanup")
    strategy._send_required_cancel_once(strategy.direction, "cleanup")
    assert strategy.cancel_calls == 1
    assert strategy.first_cancel_diagnostic is first


@pytest.mark.parametrize("failure", ["clock", "cache"])
def test_roundtrip_cancel_observation_failure_does_not_block_cancel(
    tmp_path: Path, failure: str
) -> None:
    strategy = _RoundtripCancelProbe(_profile(tmp_path))
    order, account = _submitted_roundtrip_source(strategy)
    strategy.observation_failure = failure

    strategy._cancel_working(strategy.direction, reason="stale timer")
    strategy.observation_failure = None
    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account,
        venue_order_id=VenueOrderId("V-ROUNDTRIP-OBSERVATION-FAILURE"),
    )
    order.apply(accepted)
    strategy.on_order_accepted(accepted)

    assert strategy.cancel_calls == 1
    assert strategy.first_cancel_diagnostic == {
        "reason": "stale timer",
        "direction": "LONG",
        "source_order_id": order.client_order_id.value,
        "observation_error": "RuntimeError",
    }


def test_roundtrip_cleanup_loop_sends_deferred_cancel_after_venue_id_arrives(
    tmp_path: Path,
) -> None:
    strategy = _RoundtripCancelProbe(_profile(tmp_path))
    order, account = _submitted_roundtrip_source(strategy)
    store = strategy._stores[strategy.direction]

    async def exercise() -> None:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        node_task = asyncio.create_task(wait_forever())
        cleanup = asyncio.create_task(canary._cleanup_roundtrip_sources(strategy, node_task, 0.04))
        try:
            await asyncio.sleep(0.01)
            assert strategy.cancel_calls == 0
            accepted = TestEventStubs.order_accepted(
                order,
                account_id=account,
                venue_order_id=VenueOrderId("V-ROUNDTRIP-CLEANUP"),
            )
            order.apply(accepted)
            store.update_source_status(order.client_order_id.value, "ACCEPTED")
            await cleanup
        finally:
            node_task.cancel()
            await asyncio.gather(node_task, return_exceptions=True)

    asyncio.run(exercise())

    assert strategy.cancel_calls == 1
    record = store.source_order(order.client_order_id.value)
    assert record is not None and record.status == "UNKNOWN"


def test_roundtrip_cleanup_never_retries_unknown_cancel_outcome(tmp_path: Path) -> None:
    strategy = _RoundtripCancelProbe(_profile(tmp_path))
    order, account = _submitted_roundtrip_source(strategy)
    store = strategy._stores[strategy.direction]
    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account,
        venue_order_id=VenueOrderId("V-ROUNDTRIP-CLEANUP"),
    )
    order.apply(accepted)
    strategy.on_order_accepted(accepted)
    strategy._cancel_working(strategy.direction, reason="cleanup")
    assert strategy.cancel_calls == 1
    strategy.on_order_cancel_rejected(_cancel_rejected(order, account, "UNKNOWN"))
    strategy._cancel_working(strategy.direction, reason="cleanup retry")
    strategy._send_required_cancel_once(strategy.direction, "cleanup retry")

    assert strategy.cancel_calls == 1
    record = store.source_order(order.client_order_id.value)
    assert record is not None and record.status == "UNKNOWN"
    assert store.halt_reason == "maker cancel rejected"


def test_roundtrip_cleanup_blocks_second_leg_after_inflight_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _roundtrip_strategy(tmp_path)
    store, intent, fill = _two_leg_inflight_hedge(strategy)
    submissions: list[object] = []
    monkeypatch.setattr(
        strategy,
        "_submit_hedge",
        lambda *_args, **_kwargs: submissions.append(object()),
    )

    async def exercise() -> None:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        cleanup = asyncio.create_task(canary._cleanup_roundtrip_sources(strategy, task, 1.0))
        try:
            while not strategy.cleanup_started:
                await asyncio.sleep(0)
            strategy.on_order_filled(fill)
            await asyncio.wait_for(cleanup, 0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())

    blocked = store.intent(intent.intent_id)
    assert submissions == []
    assert blocked.status is ObligationStatus.BLOCKED
    assert blocked.hedge_client_order_id is None and blocked.hedge_leg_index == 1
    assert store.halt_reason == (
        f"hedge {intent.intent_id} blocked: "
        "roundtrip cleanup cannot start another MT5 leg"
    )


def test_roundtrip_cleanup_blocks_new_pending_leg_near_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _roundtrip_strategy(tmp_path)
    store, intent, fill = _two_leg_inflight_hedge(strategy)
    submissions: list[object] = []
    monkeypatch.setattr(
        strategy,
        "_submit_hedge",
        lambda *_args, **_kwargs: submissions.append(object()),
    )

    async def exercise() -> None:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        cleanup = asyncio.create_task(canary._cleanup_roundtrip_sources(strategy, task, 0.08))
        try:
            while not strategy.cleanup_started:
                await asyncio.sleep(0)
            await asyncio.sleep(0.06)
            assert store.apply_hedge_fill(
                client_order_id=fill.client_order_id.value,
                trade_id=fill.trade_id.value,
                fill_ounces=fill.last_qty.as_decimal(),
            )
            await cleanup
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())

    blocked = store.intent(intent.intent_id)
    assert submissions == []
    assert blocked.status is ObligationStatus.BLOCKED
    assert blocked.hedge_client_order_id is None
    assert store.halt_reason is not None
    assert store.halt_reason.endswith("roundtrip cleanup cannot start another MT5 leg")


def test_roundtrip_cleanup_waits_for_hedge_after_source_is_terminal(tmp_path: Path) -> None:
    strategy = _roundtrip_strategy(tmp_path)
    store = strategy._stores[strategy.direction]
    store.begin_source("O-FILLED", BusinessOrderSide.BUY, D(2))
    intent = store.reserve_source_fill(
        fill_key="O-FILLED|V|T",
        client_order_id="O-FILLED",
        trade_id="T",
        source_side=BusinessOrderSide.BUY,
        fill_ounces=D(2),
    )
    assert intent is not None and store.active_source_order_id is None

    async def exercise() -> None:
        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        try:
            await canary._cleanup_roundtrip_sources(strategy, task, 0.02)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())

    held = store.intent(intent.intent_id)
    assert held.status is ObligationStatus.BLOCKED
    assert store.halt_reason == (
        f"hedge {intent.intent_id} blocked: "
        "roundtrip cleanup cannot start another MT5 leg"
    )
