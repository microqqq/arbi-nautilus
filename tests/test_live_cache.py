"""Offline native-cache identity and builder wiring, not backend recovery evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import test_live_maker as maker
import test_live_taker as taker
from msgspec.structs import replace as struct_replace
from nautilus_trader.cache.cache import Cache
from nautilus_trader.config import DatabaseConfig, TradingNodeConfig
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TradeId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.mocks.exec_clients import MockExecutionClient
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from py000_nautilus import live_maker, live_taker
from py000_nautilus.app import _hedge_instrument, _source_instrument
from py000_nautilus.bitfinex_v1_cids import BitfinexV1CidStore
from py000_nautilus.bitfinex_v1_data import BitfinexV1DataClient
from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionClient
from py000_nautilus.live_cache import (
    _filled_bitfinex_position_id,
    native_cache_config,
    validate_native_cache,
)
from py000_nautilus.live_lifecycle import DrainingTradingNode
from py000_nautilus.live_runtime import SourceTerminalReconciler
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import BusinessOrderSide, SourceDirection
from py000_nautilus.mt5_v1_data import Mt5V1DataClient
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionClient
from py000_nautilus.store import JsonStateStore

_TRADER = TraderId("TESTER-000")
_OWNER = StrategyId("CACHE-001")
_SOURCE_ACCOUNT = AccountId("BITFINEX-269312")
_HEDGE_ACCOUNT = AccountId("MT5-12345678")
_ROUTES = {
    taker.SOURCE_ID: (_SOURCE_ACCOUNT, taker.BITFINEX_ID),
    taker.HEDGE_ID: (_HEDGE_ACCOUNT, taker.MT5_ID),
}


def _cache() -> Cache:
    cache = Cache()
    for instrument in (_source_instrument(), _hedge_instrument()):
        cache.add_instrument(instrument)
    for account_id, _ in _ROUTES.values():
        cache.add_account(TestExecStubs.margin_account(account_id))
    return cache


def _order(
    instrument: Instrument | None = None,
    *,
    owner: StrategyId = _OWNER,
    trader: TraderId = _TRADER,
    cid: str = "CACHE-ORDER-1",
    reduce_only: bool = False,
) -> Order:
    return TestExecStubs.limit_order(
        instrument=instrument or _source_instrument(), strategy_id=owner,
        trader_id=trader, client_order_id=ClientOrderId(cid), reduce_only=reduce_only,
    )


def _validate(cache: Cache) -> bool:
    return validate_native_cache(cache, trader_id=_TRADER, strategy_id=_OWNER, routes=_ROUTES)


def _add_position(
    cache: Cache, *, cid: str = "CACHE-ORDER-1", ticket: str = "900000101", **identity: object,
) -> tuple[Order, Position]:
    instrument = cache.instrument(taker.HEDGE_ID)
    order = _order(instrument, cid=cid)
    order.apply(TestEventStubs.order_submitted(order, _HEDGE_ACCOUNT))
    order.apply(TestEventStubs.order_accepted(order, _HEDGE_ACCOUNT, VenueOrderId(cid)))
    fill = TestEventStubs.order_filled(
        order, instrument, account_id=_HEDGE_ACCOUNT, position_id=PositionId(ticket),
        commission=Money(0, USD),
    )
    order.apply(fill)
    cache.add_order(order, client_id=taker.MT5_ID)
    cache.update_order(order)
    values = OrderFilled.to_dict(fill)
    values.update(identity)
    position = Position(instrument, OrderFilled.from_dict(values))
    cache.add_position(position, OmsType.HEDGING)
    return order, position


def test_native_config_is_opt_in_stable_and_non_flushing() -> None:
    assert native_cache_config(None) is None
    database = DatabaseConfig(host="offline.invalid", port=6379)
    config = native_cache_config(database)
    assert config is not None and config.database is database
    assert config.use_trader_prefix is True
    assert config.use_instance_id is False
    assert config.flush_on_start is False
    assert config.persist_account_events is True
    with pytest.raises(ValueError, match="only redis"):
        native_cache_config(DatabaseConfig(type="sqlite"))


def test_empty_cache_has_no_pending_history_and_unsubmitted_order_does() -> None:
    cache = _cache()
    assert _validate(cache) is False
    order = _order()
    cache.add_order(order, client_id=taker.BITFINEX_ID)
    assert order.account_id is None
    assert _validate(cache) is True
    assert order.account_id is None  # Identity checks must not invent an account binding.


def test_native_integrity_failure_is_not_ignored() -> None:
    cache = _cache()
    cache.add_order(_order(), client_id=taker.BITFINEX_ID)
    cache.clear_index()
    assert cache.check_integrity() is False
    with pytest.raises(ValueError, match="integrity check failed"):
        _validate(cache)


@pytest.mark.parametrize("foreign", ["account", "instrument"])
def test_foreign_cached_entities_are_rejected(foreign: str) -> None:
    cache = _cache()
    if foreign == "account":
        cache.add_account(TestExecStubs.margin_account(AccountId("OTHER-001")))
    else:
        cache.add_instrument(_foreign_instrument())
    assert cache.check_integrity()
    with pytest.raises(ValueError, match=f"contains an {foreign} outside"):
        _validate(cache)


def _foreign_instrument() -> Instrument:
    return TestInstrumentProvider.default_fx_ccy("EUR/USD")


@pytest.mark.parametrize("mismatch", ["owner", "trader", "instrument", "account", "client"])
def test_order_identity_and_explicit_client_route_are_required(mismatch: str) -> None:
    cache = _cache()
    order = _order(
        _foreign_instrument() if mismatch == "instrument" else None,
        owner=StrategyId("OTHER-001") if mismatch == "owner" else _OWNER,
        trader=TraderId("OTHER-001") if mismatch == "trader" else _TRADER,
    )
    if mismatch == "account":
        order.apply(TestEventStubs.order_submitted(order, _HEDGE_ACCOUNT))
    cache.add_order(order, client_id=(taker.MT5_ID if mismatch == "client" else taker.BITFINEX_ID))
    cache.update_order(order)
    assert cache.check_integrity()
    with pytest.raises(ValueError, match="native cache order"):
        _validate(cache)


def test_missing_client_index_fails_despite_native_integrity_passing() -> None:
    cache = _cache()
    cache.add_order(_order())
    assert cache.check_integrity()
    with pytest.raises(ValueError, match="client index mismatch"):
        _validate(cache)


@pytest.mark.parametrize("missing", ["instrument", "account"])
def test_incomplete_order_dependencies_fail_despite_native_integrity(missing: str) -> None:
    cache = Cache()
    order = _order()
    if missing == "account":
        cache.add_instrument(_source_instrument())
        order.apply(TestEventStubs.order_submitted(order, _SOURCE_ACCOUNT))
    cache.add_order(order, client_id=taker.BITFINEX_ID)
    cache.update_order(order)
    assert cache.check_integrity()
    with pytest.raises(ValueError, match="native cache order"):
        _validate(cache)


def test_filled_position_and_unsubmitted_exact_close_mapping_are_preserved() -> None:
    cache = _cache()
    filled, position = _add_position(cache)
    close = _order(cache.instrument(taker.HEDGE_ID), cid="CACHE-CLOSE-1", reduce_only=True)
    cache.add_order(close, client_id=taker.MT5_ID, position_id=position.id)
    assert close.account_id is None and close.position_id is None
    assert _validate(cache) is True
    assert cache.position_id(filled.client_order_id) == position.id
    assert cache.position_id(close.client_order_id) == position.id
    assert cache.client_id(close.client_order_id) == taker.MT5_ID


@pytest.mark.parametrize("state", ["INITIALIZED", "ACCEPTED"])
@pytest.mark.parametrize("client_id", [taker.MT5_ID, taker.BITFINEX_ID], ids=["mt5", "bitfinex"])
def test_only_mt5_reduce_only_requires_explicit_position_index(
    state: str, client_id: ClientId,
) -> None:
    cache = _cache()
    instrument_id = taker.HEDGE_ID if client_id == taker.MT5_ID else taker.SOURCE_ID
    account_id, _ = _ROUTES[instrument_id]
    order = _order(cache.instrument(instrument_id), reduce_only=True)
    if state == "ACCEPTED":
        order.apply(TestEventStubs.order_submitted(order, account_id))
        order.apply(TestEventStubs.order_accepted(order, account_id))
    cache.add_order(order, client_id=client_id)
    cache.update_order(order)
    assert order.status.name == state
    assert cache.check_integrity()
    assert order.position_id is None and cache.position_id(order.client_order_id) is None

    if client_id == taker.MT5_ID:
        with pytest.raises(ValueError, match="MT5 reduce-only order requires a position index"):
            _validate(cache)
    else:
        assert _validate(cache) is True  # NETTING source reduction does not name an MT5 ticket.
    assert cache.position_id(order.client_order_id) is None  # Never invent a close target.


@pytest.mark.parametrize("mismatch", ["owner", "trader", "account"])
def test_position_identity_is_independently_checked(mismatch: str) -> None:
    cache = _cache()
    field = {"owner": "strategy_id", "trader": "trader_id", "account": "account_id"}[mismatch]
    _add_position(cache, **{field: "OTHER-001"})
    assert cache.check_integrity()
    with pytest.raises(ValueError, match="position identity mismatch"):
        _validate(cache)


def test_conflicting_position_index_fails_without_mutating_cache() -> None:
    cache = _cache()
    order, _ = _add_position(cache)
    _, other = _add_position(cache, cid="CACHE-ORDER-2", ticket="900000102")
    cache.add_position_id(other.id, taker.HEDGE_ID.venue, order.client_order_id, _OWNER)
    assert cache.check_integrity()  # Native integrity does not compare the order's PositionId.
    with pytest.raises(ValueError, match="position index mismatch"):
        _validate(cache)
    assert cache.position_id(order.client_order_id) == other.id


def _netting_history(count: int) -> tuple[Cache, list[Order]]:
    """Run real native NETTING fills; no venue transport or synthetic index repair."""
    cache = _cache()
    instrument = _source_instrument()
    clock, msgbus = TestComponentStubs.clock(), TestComponentStubs.msgbus()
    engine = ExecutionEngine(msgbus=msgbus, cache=cache, clock=clock)
    client = MockExecutionClient(
        client_id=taker.BITFINEX_ID, venue=instrument.id.venue,
        account_type=AccountType.MARGIN, base_currency=USD,
        msgbus=msgbus, cache=cache, clock=clock, oms_type=OmsType.NETTING,
    )
    engine.register_client(client)
    orders = []
    sequence = [(OrderSide.BUY, 2), (OrderSide.BUY, 2),
                (OrderSide.SELL, 4), (OrderSide.BUY, 2)]
    try:
        for index, (side, quantity) in enumerate(sequence[:count]):
            order = TestExecStubs.limit_order(
                instrument=instrument, strategy_id=_OWNER, trader_id=_TRADER,
                client_order_id=ClientOrderId(f"NETTING-{index}"), order_side=side,
                quantity=instrument.make_qty(quantity),
            )
            cache.add_order(order, client_id=taker.BITFINEX_ID)
            engine.process(TestEventStubs.order_submitted(order, _SOURCE_ACCOUNT))
            engine.process(TestEventStubs.order_accepted(
                order, _SOURCE_ACCOUNT, VenueOrderId(order.client_order_id.value),
            ))
            fill = TestEventStubs.order_filled(
                order, instrument, account_id=_SOURCE_ACCOUNT, commission=Money(0, USD),
            )
            assert fill.position_id is None  # The NETTING engine determines the native PID.
            engine.process(fill)
            assert order.filled_qty == order.quantity and cache.check_integrity()
            orders.append(order)
        assert not client.commands and not client.is_connected
    finally:
        engine.dispose()
    return cache, orders


@pytest.mark.parametrize("count", [2, 3, 4], ids=["same-side", "flat", "reopened"])
def test_native_netting_optional_indexes_are_valid_without_cache_mutation(count: int) -> None:
    cache, orders = _netting_history(count)
    pid = PositionId(f"{taker.SOURCE_ID}-{_OWNER}")
    expected = [pid, None, None, pid][:count]
    assert [order.position_id for order in orders] == [pid] * count
    assert [cache.position_id(order.client_order_id) for order in orders] == expected
    position = cache.position(pid)
    assert position is not None
    assert position.signed_decimal_qty() == [Decimal(4), Decimal(0), Decimal(2)][count - 2]
    assert len(position.events) == [2, 3, 1][count - 2]
    if count == 4:
        assert orders[0].last_trade_id not in position.trade_ids
    events = [tuple(order.events) for order in orders]
    before = Position.to_dict(position)

    assert _validate(cache) is True

    assert [tuple(order.events) for order in orders] == events
    assert Position.to_dict(position) == before
    assert [cache.position_id(order.client_order_id) for order in orders] == expected


def test_filled_source_without_native_position_id_is_not_certified_by_integrity() -> None:
    cache = _cache()
    order = _order()
    order.apply(TestEventStubs.order_submitted(order, _SOURCE_ACCOUNT))
    order.apply(TestEventStubs.order_accepted(order, _SOURCE_ACCOUNT))
    fill = TestEventStubs.order_filled(
        order, _source_instrument(), account_id=_SOURCE_ACCOUNT, commission=Money(0, USD),
    )
    order.apply(fill)
    cache.add_order(order, client_id=taker.BITFINEX_ID)
    cache.update_order(order)
    assert order.filled_qty.as_decimal() > 0
    assert order.position_id is None and cache.position_id(order.client_order_id) is None
    assert cache.check_integrity()
    with pytest.raises(ValueError, match="native cache.*position"):
        _validate(cache)


def _source_fill_cache(
    first_changes: dict[str, str] | None = None,
    *,
    position_changes: dict[str, str] | None = None,
    native_pid: str | None = None,
    add_position: bool = True,
) -> tuple[Cache, Order]:
    """Build native objects, including deliberately conflicting historical facts."""
    cache, instrument = _cache(), _source_instrument()
    order = _order()
    order.apply(TestEventStubs.order_submitted(order, _SOURCE_ACCOUNT))
    order.apply(TestEventStubs.order_accepted(order, _SOURCE_ACCOUNT))
    fills = []
    for index in range(2):
        fill = TestEventStubs.order_filled(
            order, instrument, strategy_id=_OWNER,
            account_id=_SOURCE_ACCOUNT, commission=Money(0, USD),
            position_id=PositionId(native_pid or f"{instrument.id}-{_OWNER}"),
            trade_id=TradeId(f"SOURCE-{index}"), last_qty=instrument.make_qty(50),
        )
        fills.append(fill)
        values = OrderFilled.to_dict(fill)
        if index == 0:
            values.update(first_changes or {})
        order.apply(OrderFilled.from_dict(values))
    cache.add_order(order, client_id=taker.BITFINEX_ID)
    cache.update_order(order)
    if add_position:
        values = OrderFilled.to_dict(fills[0])
        values.update(position_changes or {})
        position_instrument = cache.instrument(InstrumentId.from_str(values["instrument_id"]))
        position = Position(position_instrument, OrderFilled.from_dict(values))
        if not position_changes:
            position.apply(fills[1])
        cache.add_position(position, OmsType.NETTING)
    return cache, order


@pytest.mark.parametrize("field,value", [
    ("trader_id", "OTHER-001"), ("strategy_id", "OTHER-001"),
    ("instrument_id", str(taker.HEDGE_ID)), ("account_id", str(_HEDGE_ACCOUNT)),
    ("position_id", "WRONG-POSITION"), ("order_side", "SELL"),
])
def test_earlier_fill_identity_cannot_hide_behind_latest_order_fields(
    field: str, value: str,
) -> None:
    cache, order = _source_fill_cache({field: value})
    assert cache.check_integrity()
    assert order.position_id == PositionId(f"{taker.SOURCE_ID}-{_OWNER}")
    assert order.strategy_id == _OWNER and order.account_id == _SOURCE_ACCOUNT
    events = tuple(order.events)
    with pytest.raises(ValueError, match="filled order facts mismatch"):
        _validate(cache)
    assert tuple(order.events) == events


def test_noncanonical_source_pid_is_rejected_even_with_matching_index_and_position() -> None:
    cache, order = _source_fill_cache(native_pid="WRONG-POSITION")
    assert cache.check_integrity()
    assert cache.position_id(order.client_order_id) == order.position_id
    with pytest.raises(ValueError, match="filled order position mismatch"):
        _validate(cache)


def test_optional_source_index_does_not_excuse_missing_actual_position() -> None:
    cache, order = _source_fill_cache(add_position=False)
    assert cache.check_integrity()
    assert cache.position_id(order.client_order_id) is None and order.position_id is not None
    with pytest.raises(ValueError, match="order position missing"):
        _validate(cache)


@pytest.mark.parametrize("field,value", [
    ("trader_id", "OTHER-001"), ("strategy_id", "OTHER-001"),
    ("account_id", str(_HEDGE_ACCOUNT)),
])
def test_source_actual_position_must_still_match_route_and_owner(field: str, value: str) -> None:
    cache, _ = _source_fill_cache(position_changes={field: value})
    assert cache.check_integrity()
    with pytest.raises(ValueError, match="native cache.*position.*mismatch"):
        _validate(cache)


def test_native_integrity_already_rejects_source_position_index_on_wrong_instrument() -> None:
    cache, _ = _source_fill_cache(position_changes={"instrument_id": str(taker.HEDGE_ID)})
    assert not cache.check_integrity()
    with pytest.raises(ValueError, match="integrity check failed"):
        _validate(cache)


def test_nonempty_conflicting_source_index_is_never_replaced_by_native_pid() -> None:
    cache, order = _source_fill_cache()
    _, hedge_position = _add_position(cache, cid="HEDGE-OTHER")
    cache.add_position_id(hedge_position.id, taker.SOURCE_ID.venue, order.client_order_id, _OWNER)
    assert cache.check_integrity()
    with pytest.raises(ValueError, match="position index mismatch"):
        _validate(cache)
    assert cache.position_id(order.client_order_id) == hedge_position.id


@pytest.mark.parametrize("mismatch", ["no-fills", "trade-order", "duplicate", "quantity",
                                     "client_order_id", "venue_order_id"])
def test_source_fill_proof_requires_complete_order_evidence(mismatch: str) -> None:
    # Predicate-only views exercise inconsistencies normal native Order.apply prevents.
    # They are not represented as states proven loadable from a backend.
    _, order = _source_fill_cache()
    values = {field: getattr(order, field) for field in (
        "trader_id", "strategy_id", "instrument_id", "client_order_id", "account_id",
        "venue_order_id", "position_id", "side", "filled_qty", "events", "trade_ids",
    )}
    if mismatch == "no-fills":
        values["events"] = order.events[:3]
    elif mismatch == "trade-order":
        values["trade_ids"] = order.trade_ids[::-1]
    elif mismatch == "duplicate":
        values["events"] = [*order.events, order.events[-1]]
        values["trade_ids"] = [*order.trade_ids, order.trade_ids[-1]]
    elif mismatch == "quantity":
        values["filled_qty"] = _source_instrument().make_qty(101)
    else:
        fill = OrderFilled.to_dict(order.events[-1])
        fill[mismatch] = "WRONG-ID"
        values["events"] = [*order.events[:-1], OrderFilled.from_dict(fill)]
    view = cast(Order, SimpleNamespace(**values))
    with pytest.raises(ValueError, match="filled order facts mismatch"):
        _filled_bitfinex_position_id(view)


def _ready_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    for cls, name, value in (
        (BitfinexV1DataClient, "is_connected", True),
        (BitfinexV1DataClient, "book_is_actionable", True),
        (BitfinexV1ExecutionClient, "execution_hold_reason", None),
        (BitfinexV1ExecutionClient, "accounting_ready", True),
        (Mt5V1DataClient, "is_connected", True),
        (Mt5V1DataClient, "snapshot_refresh_healthy", True),
        (Mt5V1ExecutionClient, "execution_admitted", True),
        (SourceTerminalReconciler, "source_submission_ready", True),
    ):
        def get_ready(_self: object, result: bool | None = value) -> bool | None:
            return result

        monkeypatch.setattr(cls, name, property(get_ready))
    for cls in (BitfinexV1ExecutionClient, Mt5V1ExecutionClient):
        monkeypatch.setattr(cls, "get_account", lambda _self: object())


@pytest.mark.parametrize("kind", ["taker", "maker"])
@pytest.mark.parametrize("history", [False, True], ids=["empty-ready", "history-pending"])
def test_builder_forwards_native_config_and_gates_only_new_source(
    kind: str, history: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strip DB at construction and inject memory data only to test thin wiring."""
    module: Any = live_taker if kind == "taker" else live_maker
    configs: Any = taker._configs(tmp_path) if kind == "taker" else maker._configs(tmp_path)
    requested = struct_replace(configs.strategy, order_id_tag="CACHE")
    database = DatabaseConfig(host="offline.invalid")
    captured: list[TradingNodeConfig] = []
    built: list[TradingNode] = []

    def offline_node(
        *, config: TradingNodeConfig, loop: asyncio.AbstractEventLoop,
    ) -> DrainingTradingNode:
        captured.append(config)
        node = DrainingTradingNode(config=struct_replace(config, cache=None), loop=loop)
        built.append(node)
        return node

    def verify(
        cache: Cache, *, trader_id: TraderId, strategy_id: StrategyId,
        routes: Mapping[InstrumentId, tuple[AccountId, ClientId]],
    ) -> bool:
        assert strategy_id == built[0].trader.strategies()[0].id
        # Use the actual registered ID, not config None.
        assert strategy_id.value.endswith("-CACHE")
        assert routes == _ROUTES
        if history:
            instrument = _source_instrument()
            cache.add_instrument(instrument)
            cache.add_order(_order(instrument, owner=strategy_id, trader=trader_id),
                            client_id=taker.BITFINEX_ID)
        return validate_native_cache(cache, trader_id=trader_id, strategy_id=strategy_id,
                                     routes=routes)

    monkeypatch.setattr(module, "DrainingTradingNode", offline_node)
    monkeypatch.setattr(module, "validate_native_cache", verify)
    loop = asyncio.new_event_loop()
    builder = module.build_live_taker_node if kind == "taker" else module.build_live_maker_node
    node, strategy = builder(
        bitfinex_data_config=configs.bitfinex_data, bitfinex_exec_config=configs.bitfinex_exec,
        mt5_data_config=configs.mt5_data, mt5_exec_config=configs.mt5_exec,
        strategy_config=requested, cache_database=database, loop=loop,
    )
    try:
        assert captured[0].cache == native_cache_config(database)
        expected_trader = (live_taker.LIVE_TAKER_TRADER_ID if kind == "taker"
                           else live_maker.LIVE_MAKER_TRADER_ID)
        assert captured[0].trader_id == expected_trader
        _ready_dependencies(monkeypatch)
        assert cast(Any, strategy)._live_submission_ready() is not history
        hedge_gate = cast(Any, strategy)._hedge_quantity_ready
        clients = cast(dict[ClientId, Any], node.kernel.exec_engine._clients)
        assert hedge_gate.__self__ is clients[taker.MT5_ID]
        assert hedge_gate.__func__ is Mt5V1ExecutionClient.can_execute_quantity
        assert clients[taker.BITFINEX_ID].oms_type is OmsType.NETTING
        assert clients[taker.MT5_ID].oms_type is OmsType.HEDGING
        assert not list(tmp_path.iterdir())
    finally:
        node.dispose()
    assert loop.is_closed()


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_default_builder_never_validates_or_connects_native_cache(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("default offline build must not enter native cache validation")

    module = live_taker if kind == "taker" else live_maker
    monkeypatch.setattr(module, "validate_native_cache", forbidden)
    loop = asyncio.new_event_loop()
    if kind == "taker":
        node, _ = taker._build(taker._configs(tmp_path), loop=loop)
    else:
        node, _ = maker._build(maker._configs(tmp_path), loop=loop)
    node.dispose()
    assert loop.is_closed()


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_orphan_source_cid_history_holds_empty_native_and_business_startup(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs: Any = taker._configs(tmp_path) if kind == "taker" else maker._configs(tmp_path)
    cids = BitfinexV1CidStore(
        configs.bitfinex_exec.cid_store_path,
        account_id=configs.bitfinex_exec.account_id.value,
    )
    cids.allocate("ORPHAN-OLD-CID", epoch_ms=1000)
    before = cids.path.read_bytes()
    loop = asyncio.new_event_loop()
    node, strategy = (taker._build(configs, loop=loop) if kind == "taker"
                      else maker._build(configs, loop=loop))
    try:
        runtime = next(actor for actor in node.trader.actors()
                       if isinstance(actor, SourceTerminalReconciler))
        assert not node.cache.orders() and not node.cache.positions()
        _ready_dependencies(monkeypatch)
        assert runtime.restart_pending
        assert not strategy._live_submission_ready()
        assert not runtime._source._tasks and runtime._root is None
        assert cids.path.read_bytes() == before
    finally:
        node.dispose()
    assert loop.is_closed()


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_business_history_with_empty_native_cache_binds_startup_gate_without_database(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module: Any = live_taker if kind == "taker" else live_maker
    configs: Any = taker._configs(tmp_path) if kind == "taker" else maker._configs(tmp_path)
    owner: JsonStateStore | MakerStateStore
    if kind == "taker":
        owner = JsonStateStore(configs.strategy.store_path)
        view = owner
    else:
        owner = MakerStateStore(configs.strategy.store_path_prefix,
                                str(taker.SOURCE_ID), str(taker.HEDGE_ID))
        view = owner.stores[SourceDirection.LONG]
    view.begin_source("OLD", BusinessOrderSide.BUY, Decimal(1))
    view.update_source_status("OLD", "REJECTED")
    assert view.can_submit_source()  # Business readiness cannot bypass missing native facts.
    before = owner.path.read_bytes()
    calls: list[tuple[Cache, object, dict[str, object]]] = []

    async def recovery(cache: Cache, store: object, **kwargs: object) -> None:
        calls.append((cache, store, kwargs))
        raise ValueError("native history missing")

    monkeypatch.setattr(module, "reconcile_startup", recovery)
    loop = asyncio.new_event_loop()
    builder = module.build_live_taker_node if kind == "taker" else module.build_live_maker_node
    node, strategy = builder(
        bitfinex_data_config=configs.bitfinex_data, bitfinex_exec_config=configs.bitfinex_exec,
        mt5_data_config=configs.mt5_data, mt5_exec_config=configs.mt5_exec,
        strategy_config=configs.strategy, connection_timeout_seconds=0.03, loop=loop,
    )
    runtime = next(actor for actor in node.trader.actors()
                   if isinstance(actor, SourceTerminalReconciler))
    try:
        assert not node.cache.orders() and not node.cache.positions()
        assert runtime.restart_pending and not calls
        assert strategy._restart_gate() is True
        _ready_dependencies(monkeypatch)
        assert not strategy._live_submission_ready()
        engine = node.kernel.exec_engine
        monkeypatch.setattr(engine, "check_connected", lambda: True)

        async def reconciled(*, timeout_secs: float) -> bool:
            return True

        monkeypatch.setattr(engine, "reconcile_execution_state", reconciled)

        async def start() -> None:
            runtime.start()
            assert runtime._root is not None and not await runtime._root
            assert runtime.restart_pending
            assert len(calls) == 2  # Existing Actor attempt budget, not a new polling loop.
            assert calls[0][0] is node.cache
            store = strategy.state_store if kind == "taker" else strategy._state_store
            assert calls[0][1] is store
            assert calls[0][2]["strategy_id"] == strategy.id
            assert owner.path.read_bytes() == before
            runtime.stop()

        loop.run_until_complete(start())
    finally:
        node.dispose()
    assert loop.is_closed()


@pytest.mark.parametrize("kind", ["taker", "maker"])
def test_loaded_identity_failure_disposes_builder_without_creating_business_state(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module: Any = live_taker if kind == "taker" else live_maker
    configs: Any = taker._configs(tmp_path) if kind == "taker" else maker._configs(tmp_path)

    def reject(*_args: object, **_kwargs: object) -> bool:
        raise ValueError("synthetic native identity mismatch")

    # This is cleanup wiring only: no backend is constructed or represented as restored.
    monkeypatch.setattr(module, "native_cache_config", lambda _database: None)
    monkeypatch.setattr(module, "validate_native_cache", reject)
    loop = asyncio.new_event_loop()
    builder = module.build_live_taker_node if kind == "taker" else module.build_live_maker_node
    with pytest.raises(ValueError, match="synthetic native identity mismatch"):
        builder(
            bitfinex_data_config=configs.bitfinex_data,
            bitfinex_exec_config=configs.bitfinex_exec,
            mt5_data_config=configs.mt5_data, mt5_exec_config=configs.mt5_exec,
            strategy_config=configs.strategy, cache_database=DatabaseConfig(), loop=loop,
        )
    assert loop.is_closed()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("close_existing", [False, True])
def test_one_shot_rejects_database_before_any_node_is_constructed(
    close_existing: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(**_kwargs: object) -> None:
        pytest.fail("one-shot database rejection must precede node construction")

    monkeypatch.setattr(live_taker, "DrainingTradingNode", forbidden)
    configs = taker._configs(tmp_path)
    with pytest.raises(ValueError, match="one_shot execution cannot use"):
        live_taker.build_live_taker_node(
            bitfinex_data_config=configs.bitfinex_data,
            bitfinex_exec_config=configs.bitfinex_exec,
            mt5_data_config=configs.mt5_data, mt5_exec_config=configs.mt5_exec,
            strategy_config=configs.strategy, cache_database=DatabaseConfig(),
            one_shot=True, one_shot_close_existing=close_existing,
            one_shot_expected_source_position=Decimal(0),
        )
