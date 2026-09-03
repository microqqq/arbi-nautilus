from __future__ import annotations

import asyncio
import io
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import (
    AccountType,
    BookType,
    OmsType,
    OrderSide,
    OrderStatus,
    TimeInForce,
)
from nautilus_trader.model.identifiers import TradeId, VenueOrderId
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

import py000_nautilus.bitfinex_v1_paper_canary as canary
from py000_nautilus.bitfinex_v1_data import INSTRUMENT_ID, PAPER_RAW_SYMBOL, instrument_from_config
from py000_nautilus.bitfinex_v1_paper_canary import (
    PaperCanaryError,
    PaperCanaryStrategy,
    PaperCanaryStrategyConfig,
    build_paper_node,
    credentials,
    paper_data_config,
    read_owned_evidence,
    read_snapshot,
)
from py000_nautilus.bitfinex_v1_protocol import POST_ONLY_FLAG


class _Rest:
    def __init__(self) -> None:
        self.info: list[object] = [None] * 22
        self.info[0] = 269_312
        self.info[21] = 1
        self.permission_rows: list[object] = [
            ["account", 1, 0],
            ["history", 1, 0],
            ["orders", 1, 1],
            ["positions", 1, 1],
            ["wallets", 1, 1],
            ["withdraw", 0, 0],
        ]
        self.wallet_rows: list[object] = [
            ["margin", "TESTUSDTF0", Decimal("20000"), Decimal(0), Decimal("20000")]
        ]
        self.orders: list[object] = []
        self.position_rows: list[object] = []
        self.history: list[object] = []
        self.trades: list[object] = []

    async def user_info(self) -> object:
        return self.info

    async def permissions(self) -> object:
        return self.permission_rows

    async def wallets(self) -> object:
        return self.wallet_rows

    async def active_orders_by_symbol(self, symbol: str) -> object:
        assert symbol == PAPER_RAW_SYMBOL
        return self.orders

    async def positions(self) -> object:
        return self.position_rows

    async def order_history_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2_500,
    ) -> object:
        del start, end, limit
        assert symbol == PAPER_RAW_SYMBOL
        return self.history

    async def trades_by_symbol(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2_500,
    ) -> object:
        del start, end, limit
        assert symbol == PAPER_RAW_SYMBOL
        return self.trades


def _order_row(
    *,
    status: str = "ACTIVE",
    remaining: str = "2",
    average: str = "0",
) -> list[object]:
    return [
        123,
        None,
        456,
        PAPER_RAW_SYMBOL,
        1_700_000_000_000,
        1_700_000_000_100,
        Decimal(remaining),
        Decimal("2"),
        "LIMIT",
        None,
        None,
        None,
        POST_ONLY_FLAG,
        status,
        None,
        None,
        Decimal("4370.0"),
        Decimal(average),
    ]


def _trade_row() -> list[object]:
    return [
        789,
        PAPER_RAW_SYMBOL,
        1_700_000_000_200,
        123,
        Decimal("1"),
        Decimal("4370.0"),
        "LIMIT",
        Decimal("4370.0"),
        1,
        Decimal("-0.1"),
        "TESTUSDTF0",
        456,
    ]


def _position_row(quantity: str = "2") -> list[object]:
    row: list[object] = [None] * 16
    row[0] = PAPER_RAW_SYMBOL
    row[2] = Decimal(quantity)
    return row


def test_snapshot_proves_paper_identity_permissions_wallet_and_flat_state() -> None:
    result = asyncio.run(read_snapshot(_Rest()))

    assert result.paper_enabled
    assert result.permissions_ok
    assert result.withdrawal_disabled
    assert result.active_orders == 0
    assert result.position_quantity == 0
    assert result.holds == ()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda rest: rest.orders.append(_order_row()), "target_has_preexisting_active_orders"),
        (
            lambda rest: rest.position_rows.append(_position_row()),
            "target_has_preexisting_position",
        ),
        (
            lambda rest: rest.wallet_rows[0].__setitem__(4, Decimal(0)),
            "target_wallet_has_no_available_balance",
        ),
        (
            lambda rest: rest.permission_rows.__setitem__(2, ["orders", 1, 0]),
            "api_permissions_are_insufficient",
        ),
        (lambda rest: rest.info.__setitem__(21, 0), "account_is_not_paper"),
        (
            lambda rest: rest.permission_rows.__setitem__(5, ["withdraw", 0, 1]),
            "api_key_has_withdrawal_permission",
        ),
    ],
)
def test_snapshot_holds_each_mutation_boundary(mutate, reason: str) -> None:  # type: ignore[no-untyped-def]
    rest = _Rest()
    mutate(rest)

    assert reason in asyncio.run(read_snapshot(rest)).holds


def test_snapshot_rejects_malformed_or_ambiguous_rows() -> None:
    rest = _Rest()
    rest.permission_rows.append(["orders", 1, 1])
    with pytest.raises(PaperCanaryError, match="duplicated"):
        asyncio.run(read_snapshot(rest))

    rest = _Rest()
    rest.position_rows = [_position_row(), _position_row("-2")]
    with pytest.raises(PaperCanaryError, match="multiple NETTING"):
        asyncio.run(read_snapshot(rest))


def test_owned_evidence_requires_exact_cancel_and_no_active_order_or_trade() -> None:
    rest = _Rest()
    rest.history = [_order_row(status="CANCELED")]
    clean = asyncio.run(
        read_owned_evidence(
            rest, cid=456, venue_order_id=123, price=Decimal("4370.0"), start_ms=0
        )
    )
    assert clean.terminal_exact
    assert clean.complete
    assert not clean.fill_indicated
    assert clean.active_venue_ids == ()
    assert clean.trade_ids == ()

    rest.orders = [_order_row()]
    rest.trades = [_trade_row()]
    unsafe = asyncio.run(
        read_owned_evidence(
            rest, cid=456, venue_order_id=123, price=Decimal("4370.0"), start_ms=0
        )
    )
    assert unsafe.active_venue_ids == (123,)
    assert unsafe.trade_ids == (789,)

    rest.orders = []
    rest.trades = []
    rest.history = [_order_row(status="CANCELED", remaining="1", average="4370")]
    partial = asyncio.run(
        read_owned_evidence(
            rest, cid=456, venue_order_id=123, price=Decimal("4370.0"), start_ms=0
        )
    )
    assert partial.fill_indicated
    assert not partial.terminal_exact

    timed = _order_row(status="CANCELED")
    timed[10] = 1_700_000_100_000
    rest.history = [timed]
    not_gtc = asyncio.run(
        read_owned_evidence(
            rest, cid=456, venue_order_id=123, price=Decimal("4370.0"), start_ms=0
        )
    )
    assert not not_gtc.terminal_exact

    rest.history = []
    missing = asyncio.run(
        read_owned_evidence(
            rest, cid=456, venue_order_id=123, price=Decimal("4370.0"), start_ms=0
        )
    )
    assert not missing.terminal_exact


def test_credentials_load_only_named_values_and_environment_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "IGNORED=VALUE\nBFX_TEST_API_KEY='FILE-KEY'\n"
        "BFX_TEST_API_SECRET=FILE-SECRET\nBFX_TEST_USER_ID=269312\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BFX_TEST_API_KEY", "ENV-KEY")

    assert credentials(path) == ("ENV-KEY", "FILE-SECRET", 269_312)


def test_profile_and_node_build_are_paper_only_and_offline(tmp_path: Path) -> None:
    profile = paper_data_config()
    assert profile.raw_symbol == PAPER_RAW_SYMBOL
    assert profile.min_quantity == Decimal(2)
    assert profile.max_quantity == Decimal(10000)
    assert profile.margin_init == Decimal("0.01")
    assert profile.margin_maint == Decimal("0.005")

    loop = asyncio.new_event_loop()
    node, strategy = build_paper_node(
        "KEY", "SECRET", 269_312, tmp_path / "cids.state.json", loop, 1.0
    )
    try:
        assert node.kernel.exec_engine.check_disconnected()
        assert node.trader.strategies() == [strategy]
        assert strategy.order is None
    finally:
        node.dispose()
    assert loop.is_closed()


def _run_strategy(available: Decimal) -> tuple[PaperCanaryStrategy, BacktestEngine]:
    engine = BacktestEngine(
        BacktestEngineConfig(
            logging=LoggingConfig(bypass_logging=True),
            run_analysis=False,
        )
    )
    engine.add_venue(
        venue=INSTRUMENT_ID.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        book_type=BookType.L1_MBP,
        starting_balances=[Money(100_000, USDT)],
        base_currency=USDT,
    )
    instrument = instrument_from_config(paper_data_config(), ts_init=0)
    engine.add_instrument(instrument)
    strategy = PaperCanaryStrategy(PaperCanaryStrategyConfig())
    strategy.arm(available)
    engine.add_strategy(strategy)
    engine.add_data(
        [
            QuoteTick(
                instrument_id=INSTRUMENT_ID,
                bid_price=Price.from_str("4600.0"),
                ask_price=Price.from_str("4600.1"),
                bid_size=Quantity.from_str("10.00000000"),
                ask_size=Quantity.from_str("10.00000000"),
                ts_event=1_000_000_000,
                ts_init=1_000_000_000,
            )
        ]
    )
    engine.run()
    return strategy, engine


def test_strategy_submits_one_fixed_passive_order_and_cancels_on_acceptance() -> None:
    strategy, engine = _run_strategy(Decimal("100000"))
    try:
        orders = engine.cache.orders()
        assert len(orders) == 1
        order = orders[0]
        assert order.status == OrderStatus.CANCELED
        assert order.price.as_decimal() == Decimal("4370.0")
        assert order.quantity.as_decimal() == Decimal("2.00000000")
        assert order.time_in_force == TimeInForce.GTC
        assert order.is_post_only
        assert strategy.outcome == "CANCELED"
        assert strategy.filled == 0
        with pytest.raises(PaperCanaryError, match="only once"):
            strategy.arm(Decimal("100000"))
    finally:
        engine.dispose()


def test_strategy_refuses_to_raise_leverage_when_1x_balance_is_insufficient() -> None:
    strategy, engine = _run_strategy(Decimal("100"))
    try:
        assert engine.cache.orders() == []
        assert strategy.outcome == "FAILED"
        assert strategy.reason == "available balance cannot cover the 1x canary"
    finally:
        engine.dispose()


def test_partial_fill_waits_for_cancel_and_can_never_become_a_pass() -> None:
    instrument = instrument_from_config(paper_data_config(), ts_init=0)
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(4370),
    )
    strategy = PaperCanaryStrategy(PaperCanaryStrategyConfig())
    harness = cast(Any, strategy)
    harness._order = order
    harness._cancel_sent = True
    fill = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        venue_order_id=VenueOrderId("123"),
        trade_id=TradeId("789"),
        last_qty=instrument.make_qty(1),
        commission=Money(0, USDT),
    )

    strategy.on_order_filled(fill)
    assert strategy.outcome == "FILLED_HOLD"
    assert not strategy.finished.is_set()

    strategy.on_order_canceled(
        cast(Any, SimpleNamespace(client_order_id=order.client_order_id))
    )
    assert strategy.finished.is_set()
    assert strategy.outcome == "FILLED_HOLD"
    assert strategy.reason == "partial_fill_then_canceled"


def test_run_default_is_read_only_and_redacts_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rest = _Rest()
    monkeypatch.setattr(canary, "BitfinexV1RestClient", lambda **_: rest)
    monkeypatch.setattr(
        canary,
        "build_paper_node",
        lambda *_args, **_kwargs: pytest.fail("read-only mode must not build a node"),
    )
    output = tmp_path / "ready.jsonl"
    result = canary.run_paper_canary(
        "KEY", "SECRET", output, tmp_path / "cids.state.json", execute=False
    )

    assert result.outcome == "READY"
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert all(
        "KEY" not in json.dumps(record) and "SECRET" not in json.dumps(record)
        for record in records
    )
    assert output.stat().st_mode & 0o777 == 0o600


def test_run_holds_before_node_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rest = _Rest()
    rest.orders.append(_order_row())
    monkeypatch.setattr(canary, "BitfinexV1RestClient", lambda **_: rest)
    monkeypatch.setattr(
        canary,
        "build_paper_node",
        lambda *_args, **_kwargs: pytest.fail("HOLD must not build or connect a node"),
    )

    result = canary.run_paper_canary(
        "KEY",
        "SECRET",
        tmp_path / "hold.jsonl",
        tmp_path / "cids.state.json",
        execute=True,
        expected_user_id=269_312,
    )

    assert result.outcome == "HOLD"
    assert result.reason == "target_has_preexisting_active_orders"


def test_execute_binds_expected_user_and_refuses_old_cid_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rest = _Rest()
    monkeypatch.setattr(canary, "BitfinexV1RestClient", lambda **_: rest)
    monkeypatch.setattr(
        canary,
        "build_paper_node",
        lambda *_args, **_kwargs: pytest.fail("identity/state HOLD must precede node build"),
    )
    mismatch = canary.run_paper_canary(
        "KEY",
        "SECRET",
        tmp_path / "mismatch.jsonl",
        tmp_path / "new.state.json",
        execute=True,
        expected_user_id=269_313,
    )
    assert mismatch.outcome == "HOLD"
    assert mismatch.reason == "paper_account_identity_mismatch"

    cid_path = tmp_path / "old.state.json"
    cid_path.write_text("operator review required", encoding="utf-8")
    old = canary.run_paper_canary(
        "KEY",
        "SECRET",
        tmp_path / "old.jsonl",
        cid_path,
        execute=True,
        expected_user_id=269_312,
    )
    assert old.outcome == "HOLD"
    assert old.reason == "cid_store_requires_operator_review"


def test_account_lock_allows_only_one_concurrent_runner() -> None:
    user_id = os.getpid() + 1_000_000_000
    first = canary._lock_canary(user_id)
    try:
        with pytest.raises(PaperCanaryError, match="another paper canary"):
            canary._lock_canary(user_id)
    finally:
        first.close()


def _snapshot(*, active: int = 0, position: str = "0") -> canary.PaperSnapshot:
    return canary.PaperSnapshot(
        user_id=269_312,
        balance=Decimal("20000"),
        available=Decimal("20000"),
        active_orders=active,
        position_quantity=Decimal(position),
        permissions_ok=True,
        withdrawal_disabled=True,
        paper_enabled=True,
    )


def _evidence(
    *,
    active: tuple[int, ...] = (),
    trades: tuple[int, ...] = (),
    fill: bool = False,
    complete: bool = True,
    exact: bool = True,
) -> canary.OwnedOrderEvidence:
    return canary.OwnedOrderEvidence(
        active_venue_ids=active,
        historical_venue_ids=(123,),
        historical_statuses=("CANCELED",),
        trade_ids=trades,
        fill_indicated=fill,
        complete=complete,
        error=None if complete else "incomplete_test_evidence",
        terminal_exact=exact,
    )


def _submitted_strategy(
    outcome: str = "CANCELED",
    *,
    filled: str = "0",
) -> PaperCanaryStrategy:
    instrument = instrument_from_config(paper_data_config(), ts_init=0)
    strategy = PaperCanaryStrategy(PaperCanaryStrategyConfig())
    order = TestExecStubs.limit_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(2),
        price=instrument.make_price(4370),
    )
    harness = cast(Any, strategy)
    harness._order = order
    harness._cancel_sent = True
    strategy.venue_order_id = VenueOrderId("123")
    strategy.outcome = cast(Any, outcome)
    strategy.reason = "strategy_terminal"
    strategy.filled = Decimal(filled)
    return strategy


def _run_orchestrated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    strategy: PaperCanaryStrategy | None = None,
    reconciled: bool = True,
    final: canary.PaperSnapshot | None = None,
    evidence: canary.OwnedOrderEvidence | None = None,
    execute_error: Exception | None = None,
    dispose_error: Exception | None = None,
) -> tuple[canary.PaperCanaryResult, int]:
    rest = _Rest()
    strategy = strategy or _submitted_strategy()
    final = final or _snapshot()
    evidence = evidence or _evidence()
    recovery_calls = 0

    async def execute(*_args: object) -> tuple[canary.PaperSnapshot, bool]:
        if execute_error is not None:
            raise execute_error
        return _snapshot(), reconciled

    def post_evidence(*_args: object) -> tuple[canary.PaperSnapshot, canary.OwnedOrderEvidence]:
        nonlocal recovery_calls
        recovery_calls += 1
        assert final is not None and evidence is not None
        return final, evidence

    class Store:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def binding_for_client(self, _client_order_id: str) -> object:
            return SimpleNamespace(cid=456)

    monkeypatch.setattr(canary, "BitfinexV1RestClient", lambda **_: rest)
    monkeypatch.setattr(canary, "build_paper_node", lambda *_args: (object(), strategy))
    monkeypatch.setattr(canary, "BitfinexV1CidStore", Store)
    monkeypatch.setattr(canary, "_execute", execute)
    monkeypatch.setattr(canary, "_post_mutation_evidence", post_evidence)
    def dispose(_node: object) -> None:
        if dispose_error is not None:
            raise dispose_error

    monkeypatch.setattr(canary, "_dispose", dispose)
    result = canary.run_paper_canary(
        "KEY",
        "SECRET",
        tmp_path / "runner.jsonl",
        tmp_path / "runner.state.json",
        execute=True,
        expected_user_id=269_312,
    )
    return result, recovery_calls


@pytest.mark.parametrize(
    ("reconciled", "final", "evidence", "expected"),
    [
        (True, _snapshot(), _evidence(), "PASSED"),
        (False, _snapshot(), _evidence(), "UNKNOWN"),
        (True, _snapshot(position="1"), _evidence(), "FILLED_HOLD"),
        (True, _snapshot(active=1), _evidence(active=(123,)), "UNKNOWN"),
        (True, _snapshot(), _evidence(trades=(789,)), "FILLED_HOLD"),
        (True, _snapshot(), _evidence(fill=True, exact=False), "FILLED_HOLD"),
        (True, _snapshot(), _evidence(complete=False, exact=False), "UNKNOWN"),
        (True, _snapshot(), _evidence(exact=False), "UNKNOWN"),
    ],
)
def test_execute_runner_passes_only_with_complete_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reconciled: bool,
    final: canary.PaperSnapshot,
    evidence: canary.OwnedOrderEvidence,
    expected: str,
) -> None:
    result, recovery_calls = _run_orchestrated(
        tmp_path,
        monkeypatch,
        reconciled=reconciled,
        final=final,
        evidence=evidence,
    )

    assert result.outcome == expected
    assert recovery_calls == 1


def test_active_order_overrides_failed_terminal_to_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _run_orchestrated(
        tmp_path,
        monkeypatch,
        strategy=_submitted_strategy("FAILED"),
        final=_snapshot(active=1),
        evidence=_evidence(active=(123,), exact=False),
    )

    assert result.outcome == "UNKNOWN"
    assert result.reason == "manual_cancel_required"


def test_shutdown_failure_preserves_known_fill_and_runs_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, recovery_calls = _run_orchestrated(
        tmp_path,
        monkeypatch,
        strategy=_submitted_strategy("FILLED_HOLD", filled="1"),
        final=_snapshot(active=1, position="1"),
        evidence=_evidence(active=(123,), trades=(789,), exact=False),
        execute_error=PaperCanaryError("TradingNode shutdown failed"),
    )

    assert result.outcome == "FILLED_HOLD"
    assert result.reason == "manual_cancel_and_position_review_required"
    assert recovery_calls == 1


@pytest.mark.parametrize(
    ("strategy", "final", "expected", "reason"),
    [
        (_submitted_strategy(), _snapshot(), "UNKNOWN", "node_cleanup_failed"),
        (
            _submitted_strategy("FILLED_HOLD", filled="1"),
            _snapshot(position="1"),
            "FILLED_HOLD",
            "manual_position_review_required",
        ),
    ],
)
def test_cleanup_failure_cannot_override_mutation_risk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy: PaperCanaryStrategy,
    final: canary.PaperSnapshot,
    expected: str,
    reason: str,
) -> None:
    result, _ = _run_orchestrated(
        tmp_path,
        monkeypatch,
        strategy=strategy,
        final=final,
        dispose_error=RuntimeError("dispose failed"),
    )

    assert result.outcome == expected
    assert result.reason == reason


def test_post_evidence_keeps_final_snapshot_when_history_read_fails() -> None:
    class BrokenHistory(_Rest):
        async def order_history_by_symbol(
            self,
            symbol: str,
            *,
            start: int | None = None,
            end: int | None = None,
            limit: int = 2_500,
        ) -> object:
            del symbol, start, end, limit
            raise ConnectionError("history unavailable")

    rest = BrokenHistory()
    rest.orders = [_order_row(remaining="1", average="4370")]
    transcript = io.StringIO()
    loop = asyncio.new_event_loop()
    try:
        final, evidence = canary._post_mutation_evidence(
            loop,
            rest,
            _submitted_strategy(),
            456,
            0,
            transcript,
        )
    finally:
        loop.close()

    assert final.active_orders == 1
    assert evidence is not None
    assert evidence.active_venue_ids == (123,)
    assert evidence.fill_indicated
    assert not evidence.complete
    assert evidence.error == "order_history_ConnectionError"


def test_order_fill_fact_survives_later_trade_history_failure() -> None:
    class BrokenTrades(_Rest):
        async def trades_by_symbol(
            self,
            symbol: str,
            *,
            start: int | None = None,
            end: int | None = None,
            limit: int = 2_500,
        ) -> object:
            del symbol, start, end, limit
            raise ConnectionError("trades unavailable")

    rest = BrokenTrades()
    rest.history = [_order_row(status="CANCELED", remaining="1", average="4370")]
    evidence = asyncio.run(
        read_owned_evidence(
            rest, cid=456, venue_order_id=123, price=Decimal("4370.0"), start_ms=0
        )
    )

    assert evidence.fill_indicated
    assert not evidence.complete
    assert evidence.error == "trade_history_ConnectionError"


@pytest.mark.parametrize("failure", ["run", "stop"])
def test_execute_rejects_trading_node_run_or_shutdown_failure(failure: str) -> None:
    async def scenario() -> None:
        class Connected:
            def check_connected(self) -> bool:
                return True

        class Execution(Connected):
            async def reconcile_execution_state(self, *, timeout_secs: float) -> bool:
                del timeout_secs
                return True

        class Node:
            def __init__(self) -> None:
                self.kernel = SimpleNamespace(data_engine=Connected(), exec_engine=Execution())

            async def run_async(self) -> None:
                if failure == "run":
                    raise RuntimeError("run failed")
                await asyncio.Event().wait()

            def is_running(self) -> bool:
                return True

            async def stop_async(self) -> None:
                if failure == "stop":
                    raise RuntimeError("stop failed")

        started, quote_ready, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        started.set()
        quote_ready.set()
        finished.set()
        strategy = SimpleNamespace(
            started=started,
            quote_ready=quote_ready,
            finished=finished,
            outcome="CANCELED",
            planned_price=lambda: Decimal("4370"),
            arm=lambda _available: None,
            mark_timeout=lambda: None,
        )
        with pytest.raises(PaperCanaryError, match="shutdown failed"):
            await canary._execute(
                cast(Any, Node()), cast(Any, strategy), _Rest(), 269_312, 1.0
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(("data_connected", "exec_connected"), [(False, True), (True, False)])
def test_execute_never_arms_while_a_client_is_disconnected(
    data_connected: bool,
    exec_connected: bool,
) -> None:
    async def scenario() -> None:
        class Connected:
            def __init__(self, connected: bool) -> None:
                self.connected = connected

            def check_connected(self) -> bool:
                return self.connected

        class Node:
            def __init__(self) -> None:
                self.kernel = SimpleNamespace(
                    data_engine=Connected(data_connected),
                    exec_engine=Connected(exec_connected),
                )

            async def run_async(self) -> None:
                await asyncio.Event().wait()

            def is_running(self) -> bool:
                return True

            async def stop_async(self) -> None:
                pass

        started, quote_ready, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        started.set()
        quote_ready.set()
        arm_calls: list[Decimal] = []
        strategy = SimpleNamespace(
            started=started,
            quote_ready=quote_ready,
            finished=finished,
            outcome="PENDING",
            planned_price=lambda: Decimal("4370"),
            arm=arm_calls.append,
            mark_timeout=lambda: None,
        )
        with pytest.raises(PaperCanaryError, match="disconnected"):
            await canary._execute(
                cast(Any, Node()), cast(Any, strategy), _Rest(), 269_312, 1.0
            )
        assert arm_calls == []

    asyncio.run(scenario())
