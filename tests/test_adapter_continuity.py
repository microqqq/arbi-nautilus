"""Ordinary continuous strategies through both actual execution adapters.

Venue IO is finite synthetic WS/REST/MT5 journal data. Native execution,
position accounting, strategy decisions and terminal reconciliation are real.
This is not an EA, network, live-account or full PnL simulator.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from continuous_mt5_wire import ContinuousMt5Wire
from nautilus_trader.model.enums import OrderSide, OrderStatus, PositionSide
from nautilus_trader.model.events import OrderModifyRejected
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.orders import Order
from test_bitfinex_v1_execution import _position_row
from test_maker_migration import _v1_payload
from test_mt5_v1_execution import _identity, _snapshot
from test_strategy_continuity import _OrdinaryStrategy, _pump

from py000_nautilus.app import _book_snapshot, _quote
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.models import BusinessOrderSide, ObligationStatus, SourceDirection
from py000_nautilus.mt5_v1_data import instrument_from_snapshot
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.store import JsonStateStore

D = Decimal


class _SourceWire:
    """Finite fixture response to actual on/ou/oc; REST reads the same facts."""

    def __init__(self, harness: _OrdinaryStrategy) -> None:
        self.harness = harness
        self.rows: dict[int, list[Any]] = {}
        self.trades: list[list[Any]] = []
        self.net = D(0)
        self.average_price = D(0)
        harness.transport.after_send = self.respond
        self.publish()

    def order(self, cid: int) -> Order:
        binding = self.harness.source._cid_store.binding_for_cid(cid)
        assert binding is not None
        order = self.harness.node.cache.order(ClientOrderId(binding.client_order_id))
        assert order is not None
        return cast(Order, order)

    def publish(self) -> None:
        h = self.harness
        h.rest.active = deepcopy([row for row in self.rows.values() if self._active(row)])
        h.rest.history = deepcopy([row for row in self.rows.values() if not self._active(row)])
        h.rest.trades = [deepcopy(trade) for trade in self.trades]
        if self.net:
            row = [*_position_row(
                self.net, avg_px=self.average_price, raw_symbol=h.wire.raw_symbol,
            ), None, D(1000), D(200), None]
            # A reopened venue position is a new lifecycle, not resurrection of
            # the closed raw ID. Derive only from this fixture's executed trades.
            balance, position_id = D(0), 43
            for trade in self.trades:
                if balance == 0:
                    position_id += 1
                balance += D(trade[4])
            row[11] = position_id
            row[13] = h.node.kernel.clock.timestamp_ns() // 1_000_000
            h.rest.position_rows = [row]
        else:
            h.rest.position_rows = []

    @staticmethod
    def _active(row: list[Any]) -> bool:
        status = str(row[13])
        return status == "ACTIVE" or status.startswith("PARTIALLY FILLED")

    def emit(self, operation: str, row: list[Any]) -> None:
        self.harness.transport.queue.put_nowait([0, operation, deepcopy(row)])

    def respond(self, message: dict[str, object] | list[object]) -> None:
        if not isinstance(message, list) or len(message) != 4:
            return  # Original authentication handshake is supplied by the shared fixture.
        operation, data = message[1], cast(dict[str, Any], message[3])
        now = self.harness.node.kernel.clock.timestamp_ns() // 1_000_000
        if operation == "on":
            cid = int(data["cid"])
            assert cid not in self.rows
            row = cast(list[Any], self.harness.wire.order_frame("on", cid, self.order(cid))[2])
            row[0] = 900000000 + len(self.rows) + 1
            row[4] = row[5] = now
            # Responses follow the actual wire amount/price, not an expected test delta.
            row[6] = row[7] = D(data["amount"])
            row[16] = D(data["price"])
            row[12] = data.get("flags", 0)
            self.rows[cid] = row
        else:
            row = next(item for item in self.rows.values() if item[0] == data["id"])
            if operation == "oc" and not self._active(row):
                # A cancel racing an IOC terminal only echoes that same venue
                # fact; it must not invent a later terminal update timestamp.
                self.emit("oc", row)
                return
            row[5] = now
            if operation == "ou":
                assert self._active(row)
                row[16] = D(data["price"])
            elif operation == "oc":
                if self._active(row):
                    row[13] = "CANCELED"
            else:
                raise AssertionError(f"unexpected source operation {operation}")
        self.publish()
        self.emit(cast(str, operation), row)

    def fill(self, cid: int, quantity: Decimal) -> None:
        row = self.rows[cid]
        assert self._active(row) and 0 < quantity <= abs(row[6])
        signed = quantity if row[7] > 0 else -quantity
        now = self.harness.node.kernel.clock.timestamp_ns() // 1_000_000
        trade = cast(list[Any], self.harness.wire.trade_frame(
            cid, self.order(cid), trade_id=910000000 + len(self.trades) + 1,
            quantity=str(quantity), price=str(row[16]), fee="0",
            maker=1 if self.harness.maker else -1,
        )[2])
        trade[2], trade[3], trade[4] = now, row[0], signed
        self.trades.append(trade)
        row[6] -= signed
        row[5], row[17] = now, row[16]
        row[13] = (f"EXECUTED @ {row[16]}({abs(row[7])})" if row[6] == 0 else
                   f"PARTIALLY FILLED @ {row[16]}({quantity})" if self.harness.maker else
                   "IOC CANCELED")
        price = D(row[16])
        if self.net == 0 or self.net * signed > 0:
            self.average_price = (
                abs(self.net) * self.average_price + quantity * price
            ) / (abs(self.net) + quantity)
        elif quantity >= abs(self.net):
            self.average_price = price if quantity > abs(self.net) else D(0)
        # A partial reduction retains the entry price of the surviving net position.
        self.net += signed
        self.publish()  # Venue commits reports/positions before delivering its events.
        self.emit("tu", trade)
        self.emit("ou" if self._active(row) else "oc", row)


@asynccontextmanager
async def _continuous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, maker: bool,
    long_quantity: int, short_quantity: int,
    maker_config: dict[str, object] | None = None,
) -> AsyncIterator[tuple[_OrdinaryStrategy, _SourceWire, ContinuousMt5Wire]]:
    h = _OrdinaryStrategy(
        tmp_path, monkeypatch, maker=maker, two_sided=maker, native_mt5_transport=True,
        source_quantity=long_quantity, source_short_quantity=short_quantity, inject_mt5_io=False,
        maker_config=maker_config,
    )
    snapshot = _snapshot(_identity())
    snapshot["positions"] = []
    cast(dict[str, Any], snapshot["execution_limits"])["max_order_lots"] = "0.02"
    wire = ContinuousMt5Wire(_identity(), snapshot, now_ns=h.node.kernel.clock.timestamp_ns)
    h.hedge._transport = wire

    async def verify_serial(payload: JsonObject) -> None:
        for event in wire.journal:
            if event["event_type"] != "submission_reserved":
                continue
            previous = cast(JsonObject, event["payload"])["client_request_id"]
            if previous != payload["client_request_id"]:
                order = h.node.cache.order(ClientOrderId(str(previous)))
                assert order is not None and order.status == OrderStatus.FILLED

    wire.before_mutation = verify_serial  # Observe only; actual native fills must precede each leg.
    source = _SourceWire(h)
    try:
        # Public connect also starts the original journal/snapshot poller. Calling
        # only _connect would make later report assertions accidentally supply refreshes.
        h.hedge.connect()
        async with asyncio.timeout(2):
            while h.hedge._poll_task is None:
                await asyncio.sleep(.005)
        await h.start()
        yield h, source, wire
    finally:
        await h.hedge._disconnect()
        await h.close()


async def _market(h: _OrdinaryStrategy, wire: ContinuousMt5Wire, direction: int) -> None:
    now = h.node.kernel.clock.timestamp_ns()
    # A new legitimate instrument observation keeps the existing cost gate fresh.
    snapshot = await wire.snapshot(wire.identity.binding())
    h.node.kernel.data_engine.process(instrument_from_snapshot(
        snapshot, h.hedge_instrument.id, ts_init=now,
    ))
    assert h.strategy.update_cost_snapshot(h.strategy._carry, h.strategy._fx, now)
    h.strategy.update_hedge_session(True, now)
    bid, ask = ({1: ("3926.6", "3926.7"), -1: ("3946.8", "3946.9"),
                 0: ("3936.7", "3936.8")})[direction]
    h.source_data._book.apply_snapshot(
        [[D(bid) - D(i) / 10, 1, D(10)] for i in range(25)]
        + [[D(ask) + D(i) / 10, 1, D(-10)] for i in range(25)],
    )
    # Neutralize/change the source first; do not trigger from the previous opportunity.
    h.node.kernel.data_engine.process(_book_snapshot(h.source_instrument, bid, ask, "10", now))
    if h.maker:
        h.node.kernel.data_engine.process(_quote(h.source_instrument, bid, ask, "10", now))
    h.node.kernel.data_engine.process(_quote(h.hedge_instrument, "3936.7", "3936.8", "10", now))
    await _pump()


async def _drive(
    h: _OrdinaryStrategy, wire: ContinuousMt5Wire, ready: Callable[[], bool], *, direction: int,
) -> None:
    async with asyncio.timeout(8):
        while not ready():
            await _market(h, wire, direction)
            await asyncio.sleep(0.05)
    await _pump()


def _stores(h: _OrdinaryStrategy) -> tuple[JsonStateStore, ...]:
    return tuple(h.strategy._stores.values()) if h.maker else (h.store,)


async def _accepted_source(
    h: _OrdinaryStrategy, source: _SourceWire, wire: ContinuousMt5Wire, delta: int,
) -> int:
    side = OrderSide.BUY if delta > 0 else OrderSide.SELL

    def candidates() -> list[int]:
        return [cid for cid, row in source.rows.items() if source._active(row)
                and source.order(cid).status == OrderStatus.ACCEPTED
                and source.order(cid).filled_qty.as_decimal() == 0
                and source.order(cid).side == side]

    try:
        await _drive(h, wire, lambda: bool(candidates()), direction=1 if delta > 0 else -1)
    except TimeoutError as exc:
        raise AssertionError({
            "delta": delta, "source_hold": h.source.execution_hold_reason,
            "hedge_hold": h.hedge.execution_hold_reason,
            "owner_failure": get_source_terminal_reconciler(h.node).last_failure,
            "stores": [store._state for store in _stores(h)],
        }) from exc
    assert len(candidates()) == 1
    return candidates()[0]


async def _settle_cycle(
    h: _OrdinaryStrategy, source: _SourceWire, wire: ContinuousMt5Wire, *,
    cid: int, expected: int,
) -> None:
    stores = _stores(h)
    owner = get_source_terminal_reconciler(h.node)

    def settled() -> bool:
        intents = [intent for store in stores for intent in store.intents()]
        return (len(intents) == expected
                and all(intent.status is ObligationStatus.COMPLETED for intent in intents)
                and h.hedge.account_capacity_ready(h.strategy._config.max_cost_age_ns)
                and not owner.busy and source.order(cid).is_closed
                and all(store.active_source_order_id != source.order(cid).client_order_id.value
                        and store.halt_reason is None and store.source_freeze_reason is None
                        for store in stores))

    try:
        await _drive(h, wire, settled, direction=0)
    except TimeoutError as exc:
        raise AssertionError({
            "cycle": expected, "source_status": str(source.order(cid).status),
            "source_events": [(type(event).__name__, getattr(event, "reason", None))
                              for event in source.order(cid).events],
            "owner_busy": owner.busy, "owner_failure": owner.last_failure,
            "stores": [store._state for store in stores],
        }) from exc


def _ticket_facts(wire: ContinuousMt5Wire) -> dict[str, Decimal]:
    return {str(row["identifier"]): D(str(row["volume_lots"])) * 100
            * (1 if row["side"] == "buy" else -1)
            for row in cast(list[JsonObject], wire.current_snapshot["positions"])}


async def _assert_reports(
    h: _OrdinaryStrategy, source: _SourceWire, wire: ContinuousMt5Wire,
    expected_tickets: dict[str, Decimal],
) -> None:
    mass = await h.hedge.generate_mass_status()
    assert mass is not None
    reports = [report for group in mass.position_reports.values() for report in group]
    assert {str(report.venue_position_id): report.quantity.as_decimal()
            * (1 if report.position_side == PositionSide.LONG else -1)
            for report in reports} == expected_tickets == _ticket_facts(wire)
    assert {position.id.value: D(str(position.signed_qty)) for position in
            h.node.cache.positions_open(instrument_id=h.hedge_instrument.id)} == expected_tickets
    events = [cast(JsonObject, event["payload"]) for event in wire.journal
              if event["event_type"] == "order_filled"]
    assert len(mass.order_reports) == len(events)
    assert all(report.order_status == OrderStatus.FILLED for report in mass.order_reports.values())
    assert {(report.venue_order_id.value, report.trade_id.value, report.last_qty.as_decimal(),
             report.order_side == OrderSide.BUY)
            for group in mass.fill_reports.values() for report in group} == {
                (event["venue_order_id"], event["venue_deal_id"],
                 D(str(event["filled_quantity_lots"])) * 100, event["side"] == "buy")
                for event in events}
    source_mass = await h.source.generate_mass_status()
    assert source_mass is not None
    assert len(source_mass.order_reports) == len(source.rows)
    assert {(report.venue_order_id.value, report.trade_id.value, report.last_qty.as_decimal(),
             report.order_side == OrderSide.BUY)
            for group in source_mass.fill_reports.values() for report in group} == {
                (str(trade[3]), str(trade[0]), abs(trade[4]), trade[4] > 0)
                for trade in source.trades}
    positions = [report for group in source_mass.position_reports.values() for report in group]
    assert len(positions) == 1  # The actual mapper emits an explicit FLAT report.
    if source.net == 0:
        assert positions[0].position_side == PositionSide.FLAT
    assert sum((report.quantity.as_decimal()
                * (1 if report.position_side == PositionSide.LONG else -1)
                for report in positions), D(0)) == source.net
    native = h.node.cache.positions_open(instrument_id=h.source_instrument.id)
    assert len(native) == int(source.net != 0)
    if native:
        assert native[0].avg_px_open == pytest.approx(float(source.average_price), abs=1e-7)


@pytest.mark.parametrize("legacy_schema", [1, 2], ids=["dual-v1", "single-v2"])
def test_both_adapters_maker_hold_legacy_checkpoint_without_native_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_schema: int,
) -> None:
    from py000_nautilus.maker_migration import migrate_maker_state

    old_prefix, new_prefix = tmp_path / "old-maker", tmp_path / "maker-state"
    legacy: dict[str, JsonStateStore] = {}
    for direction, side in ((SourceDirection.LONG, BusinessOrderSide.BUY),
                            (SourceDirection.SHORT, BusinessOrderSide.SELL)):
        cid = f"LEGACY-{direction.value}"
        store = JsonStateStore(Path(f"{old_prefix}.{direction.value}.json"))
        store.begin_source(
            cid, side, D(2), source_account_id="BITFINEX-269312", source_client_id="BITFINEX",
            hedge_account_id=f"MT5-{_identity().account_id}", hedge_client_id="MT5",
        )
        assert store.reserve_source_fill(
            fill_key=f"{cid}|V-{cid}|T-{cid}", client_order_id=cid, trade_id=f"T-{cid}",
            source_side=side, fill_ounces=D("0.5"),
        ) is None
        store.update_source_status(cid, "CANCELED")
        store.confirm_source_reconciled(cid)
        store.freeze_source_submissions("legacy matched source fills")
        store.path.write_text(json.dumps(_v1_payload(store)))
        legacy[direction.value] = store
    if legacy_schema == 2:
        Path(f"{old_prefix}.maker.json").write_text(json.dumps({
            "schema_version": 2, "kind": "maker",
            "source_instrument_id": "XAUTUSDT-PERP.BITFINEX",
            "hedge_instrument_id": "XAUUSD.MT5",
            "directions": {key: _v1_payload(store) for key, store in legacy.items()},
        }))
    originals = {path: path.read_bytes() for path in tmp_path.glob("old-maker*.json")}
    migrated = migrate_maker_state(
        old_prefix, new_prefix, "XAUTUSDT-PERP.BITFINEX", "XAUUSD.MT5", stopped=True,
    )
    checkpoint = json.loads(migrated.path.read_text())["legacy_checkpoint"]
    migrated_bytes = migrated.path.read_bytes()
    assert all(view.source_freeze_reason == "legacy matched source fills"
               for view in migrated.stores.values())

    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True, long_quantity=2, short_quantity=0,
        ) as (h, source, wire):
            # Conversion preserves business facts, not missing native/venue
            # history or authority to clear the original source freeze.
            owner = get_source_terminal_reconciler(h.node)
            await _drive(
                h, wire, lambda: not owner.busy and owner.last_failure is not None, direction=1,
            )
            assert owner.restart_pending and not owner.source_submission_ready
            assert not source.rows and not source.trades
            assert not wire.submit_calls and not wire.close_calls
            assert not h.source_cancel_commands
            assert not h.node.cache.orders() and not h.node.cache.positions()
            for store, restored in zip(_stores(h), h.reload_stores(), strict=True):
                old = next(record for record in restored.source_orders()
                           if record.client_order_id.startswith("LEGACY-"))
                assert old.filled_ounces == D("0.5") and old.status == "CANCELED"
                assert restored.rounding_residual_ounces == store.rounding_residual_ounces == 0
                assert store.source_freeze_reason == "legacy matched source fills"
                assert restored._state == store._state
            payload = json.loads(h.store.path.read_text())
            assert payload["schema_version"] == 8
            assert payload["legacy_checkpoint"] == checkpoint
            assert not payload["allocations"]
            assert migrated.path.read_bytes() == migrated_bytes
            assert all(path.read_bytes() == original for path, original in originals.items())

    asyncio.run(run())


@pytest.mark.parametrize("first_sign", [1, -1], ids=["bid-first", "ask-first"])
@pytest.mark.parametrize("second_quantity", [D("0.5"), D("0.6")], ids=["net-zero", "dust"])
def test_both_adapters_maker_net_actual_dual_fills_then_obey_strict_next_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    first_sign: int, second_quantity: Decimal,
) -> None:
    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True, long_quantity=2, short_quantity=2,
        ) as (h, source, wire):
            def accepted() -> dict[OrderSide, int]:
                return {source.order(cid).side: cid for cid, row in source.rows.items()
                        if source._active(row)
                        and source.order(cid).status == OrderStatus.ACCEPTED}

            # Ordinary economics and both actual adapters admit the original pair.
            # No direct begin_source, private reserve call, or canary subclass.
            await _drive(h, wire, lambda: len(accepted()) == 2, direction=0)
            pair = accepted()
            first = pair[OrderSide.BUY if first_sign > 0 else OrderSide.SELL]
            second = pair[OrderSide.SELL if first_sign > 0 else OrderSide.BUY]
            original_ids = set(source.rows)
            assert len(original_ids) == 2
            initial_journal = deepcopy(wire.journal)
            assert [item["event_type"] for item in initial_journal] == ["stream_started"]
            # The venue fills both already-live orders before protective cancels
            # arrive; enqueue the actual WS facts without yielding between fills.
            source.fill(first, D("0.5"))
            source.fill(second, second_quantity)
            owner = get_source_terminal_reconciler(h.node)
            stores = _stores(h)

            def reconciled() -> bool:
                return (not owner.busy and all(source.order(cid).is_closed for cid in pair.values())
                        and all(store.active_source_order_id not in {
                            source.order(cid).client_order_id.value for cid in pair.values()
                        } and store.halt_reason is None for store in stores))

            await _drive(h, wire, reconciled, direction=0)
            expected = (D("0.5") - second_quantity) * first_sign
            assert source.net == h.node.portfolio.net_position(h.source_instrument.id) == expected
            assert h.node.portfolio.net_position(h.hedge_instrument.id) == 0
            assert wire.journal == initial_journal and all(not store.intents() for store in stores)
            assert sum((store.rounding_residual_ounces for store in stores), D(0)) == expected
            assert h.strategy._state_store.has_residuals() is (expected != 0)
            assert owner.last_failure is None
            assert {cid.value for cid in h.source_cancel_commands} == {
                source.order(cid).client_order_id.value for cid in original_ids
            }
            assert len(h.source_cancel_commands) == 2
            for store, restored in zip(stores, h.reload_stores(), strict=True):
                assert restored.rounding_residual_ounces == store.rounding_residual_ounces
                assert restored.source_freeze_reason == store.source_freeze_reason

            if expected:
                # Valid future market events still cannot admit a strict dust cycle.
                for direction in (1, -1):
                    await _market(h, wire, direction)
                assert set(source.rows) == original_ids
                assert h.strategy._global_obligation_block()
                assert all(not store.can_submit_source() for store in stores)
            else:
                # A genuinely new economic source order proceeds through the
                # unmodified strategy, native events, planner and MT5 journal.
                cid = await _accepted_source(h, source, wire, first_sign)
                assert cid not in original_ids
                order = source.order(cid)
                assert order.quantity.as_decimal() == 2 and order.is_post_only
                source.fill(cid, D(2))
                await _settle_cycle(h, source, wire, cid=cid, expected=1)
                assert source.net == h.node.portfolio.net_position(h.source_instrument.id) == (
                    D(2) * first_sign
                )
                assert h.node.portfolio.net_position(h.hedge_instrument.id) == -D(2) * first_sign
                assert [item["event_type"] for item in wire.journal[len(initial_journal):]] == [
                    "submission_reserved", "order_filled",
                ]
                assert all(store.rounding_residual_ounces == 0 for store in stores)

    asyncio.run(run())


@pytest.mark.parametrize("first_sign", [1, -1], ids=["buy-first", "sell-first"])
@pytest.mark.parametrize("limit", [D("0.5"), D("0.49")], ids=["boundary", "over-limit"])
def test_bounded_maker_carries_actual_dust_into_next_economic_dual_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_sign: int, limit: Decimal,
) -> None:
    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True, long_quantity=2, short_quantity=2,
            maker_config={"residual_mode": "bounded-carry", "residual_limit_ounces": limit,
                          "max_unhedged_ounces": D("2.5")},
        ) as (h, source, wire):
            first = await _accepted_source(h, source, wire, first_sign)
            await _accepted_source(h, source, wire, -first_sign)
            original_ids = set(source.rows)
            assert len(original_ids) == 2  # The ordinary second side remains admissible.
            source.fill(first, D("0.5"))
            stores = _stores(h)
            owner = get_source_terminal_reconciler(h.node)
            await _drive(h, wire, lambda: not owner.busy and all(
                source.order(cid).is_closed for cid in original_ids
            ) and all(store.active_source_order_id not in {
                source.order(cid).client_order_id.value for cid in original_ids
            } for store in stores), direction=0)
            assert source.net == D("0.5") * first_sign
            assert all(not store.intents() for store in stores)
            assert wire.submit_calls == [] and wire.close_calls == []

            if limit < D("0.5"):
                for direction in (1, -1, 0):
                    await _market(h, wire, direction)
                assert set(source.rows) == original_ids
                assert h.strategy._global_obligation_block()
                assert all(store.source_freeze_reason is not None for store in stores)
                return

            await _settle_cycle(h, source, wire, cid=first, expected=0)
            cid = await _accepted_source(h, source, wire, first_sign)
            opposite = await _accepted_source(h, source, wire, -first_sign)
            assert {cid, opposite}.isdisjoint(original_ids)
            assert source.order(cid).quantity.as_decimal() == 2
            assert source.order(opposite).quantity.as_decimal() == 2
            source.fill(cid, D(2))
            await _settle_cycle(h, source, wire, cid=cid, expected=1)
            residual = D("0.5") * first_sign
            assert source.net == h.node.portfolio.net_position(h.source_instrument.id) == (
                D("2.5") * first_sign
            )
            assert h.node.portfolio.net_position(h.hedge_instrument.id) == -D(2) * first_sign
            assert sum((store.net_unhedged_ounces for store in stores), D(0)) == residual
            assert sum((store.rounding_residual_ounces for store in h.reload_stores()), D(0)) == (
                residual
            )
            await _assert_reports(h, source, wire, _ticket_facts(wire))
            before = (len(source.trades), deepcopy(wire.journal))
            # Normal stop retains actual signed exposure; it never manufactures
            # a dust-flattening trade. Full stop/drain recovery is a later W6 test.
            h.node.trader.stop()
            await _pump()
            assert (len(source.trades), wire.journal) == before
            assert sum((store.net_unhedged_ounces for store in h.reload_stores()), D(0)) == residual
            assert source.net + sum(_ticket_facts(wire).values(), D(0)) == residual

    asyncio.run(run())


@pytest.mark.parametrize("first_sign", [1, -1], ids=["buy-first", "sell-first"])
def test_bounded_maker_budget_blocks_next_actual_single_side_with_dust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_sign: int,
) -> None:
    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True,
            long_quantity=2 if first_sign > 0 else 0,
            short_quantity=2 if first_sign < 0 else 0,
            maker_config={"residual_mode": "bounded-carry", "residual_limit_ounces": D("0.5"),
                          "max_unhedged_ounces": D(2)},
        ) as (h, source, wire):
            cid = await _accepted_source(h, source, wire, first_sign)
            source.fill(cid, D("0.5"))
            await _settle_cycle(h, source, wire, cid=cid, expected=0)
            for direction in (first_sign, 0, first_sign):
                await _market(h, wire, direction)
            assert set(source.rows) == {cid}  # .5 + 2 is above the explicit budget of 2.
            assert all(store.source_freeze_reason is None for store in _stores(h))
            assert source.net == D("0.5") * first_sign
            assert wire.submit_calls == [] and wire.close_calls == []

    asyncio.run(run())


@pytest.mark.parametrize("first_sign", [1, -1], ids=["buy-first", "sell-first"])
def test_bounded_maker_split_fill_carries_without_confusing_total_and_per_leg_lots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_sign: int,
) -> None:
    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True,
            long_quantity=2 if first_sign > 0 else 0,
            short_quantity=2 if first_sign < 0 else 0,
            maker_config={"residual_mode": "bounded-carry", "residual_limit_ounces": D("0.5"),
                          "max_unhedged_ounces": D("2.5")},
        ) as (h, source, wire):
            first = await _accepted_source(h, source, wire, first_sign)
            source.fill(first, D("0.5"))
            await _settle_cycle(h, source, wire, cid=first, expected=0)
            cid = await _accepted_source(h, source, wire, first_sign)
            assert cid != first
            # Both genuine venue fills precede the protective cancel. Starting
            # from .5, the unchanged rounding allocates 1 and then 2, not 2 total.
            source.fill(cid, D("0.2"))
            source.fill(cid, D("1.8"))
            await _settle_cycle(h, source, wire, cid=cid, expected=2)
            fills = [cast(JsonObject, event["payload"]) for event in wire.journal
                     if event["event_type"] == "order_filled"]
            assert [D(str(fill["filled_quantity_lots"])) for fill in fills] == [D(".01"), D(".02")]
            assert [fill["side"] for fill in fills] == ["sell" if first_sign > 0 else "buy"] * 2
            assert source.net == D("2.5") * first_sign
            assert h.node.portfolio.net_position(h.hedge_instrument.id) == -D(3) * first_sign
            residual = D("-0.5") * first_sign
            assert sum((store.net_unhedged_ounces for store in _stores(h)), D(0)) == residual
            assert sum((store.rounding_residual_ounces for store in h.reload_stores()), D(0)) == (
                residual
            )
            assert all(store.source_freeze_reason is None for store in _stores(h))
            await _assert_reports(h, source, wire, _ticket_facts(wire))

    asyncio.run(run())


@pytest.mark.parametrize("first_sign", [1, -1], ids=["buy-first", "sell-first"])
def test_bounded_maker_rechecks_future_open_lot_after_real_ticket_and_dust_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_sign: int,
) -> None:
    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True,
            long_quantity=4 if first_sign > 0 else 2,
            short_quantity=2 if first_sign > 0 else 4,
            maker_config={"residual_mode": "bounded-carry", "residual_limit_ounces": D("0.5"),
                          "max_unhedged_ounces": D("4.5")},
        ) as (h, source, wire):
            first = await _accepted_source(h, source, wire, -first_sign)
            source.fill(first, D(2))
            await _settle_cycle(h, source, wire, cid=first, expected=1)
            assert sum(_ticket_facts(wire).values(), D(0)) == D(2) * first_sign
            # From zero residual this ordinary quote can close 2 and open 2.
            cid = await _accepted_source(h, source, wire, first_sign)
            assert source.order(cid).quantity.as_decimal() == 4
            source.fill(cid, D("0.5"))
            await _settle_cycle(h, source, wire, cid=cid, expected=1)
            for direction in (first_sign, 0, first_sign):
                await _market(h, wire, direction)
            # With .5 carried, a future .2+3.8 split could close 1, close 1,
            # then open 3. The unchanged .02lot ceiling blocks this new quote.
            assert [other for other in source.rows
                    if source.order(other).side is source.order(cid).side] == [cid]
            assert len(wire.submit_calls) == 1 and wire.close_calls == []
            assert source.net == D("-1.5") * first_sign
            assert sum((store.rounding_residual_ounces for store in h.reload_stores()), D(0)) == (
                D("0.5") * first_sign
            )
            await _assert_reports(h, source, wire, _ticket_facts(wire))

    asyncio.run(run())


@pytest.mark.parametrize("first_sign", [1, -1], ids=["buy-first", "sell-first"])
def test_bounded_maker_cancels_carried_quote_when_fresh_mt5_capacity_shrinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_sign: int,
) -> None:
    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True,
            long_quantity=2 if first_sign > 0 else 0,
            short_quantity=2 if first_sign < 0 else 0,
            maker_config={"residual_mode": "bounded-carry", "residual_limit_ounces": D("0.5"),
                          "max_unhedged_ounces": D("2.5")},
        ) as (h, source, wire):
            first = await _accepted_source(h, source, wire, first_sign)
            source.fill(first, D("0.5"))
            await _settle_cycle(h, source, wire, cid=first, expected=0)
            cid = await _accepted_source(h, source, wire, first_sign)
            # Publish changed venue facts through the real MT5 poller. With
            # margin target500/base60 and ask3936.8, equity500 permits 2oz.
            # The quote's carried/split exposure needs a cumulative hedge of3.
            cast(JsonObject, wire.current_snapshot["account"]).update({
                "balance": "500", "equity": "500", "margin": "10",
                "margin_free": "490", "margin_level": "5000",
            })
            await _drive(h, wire, lambda: source.order(cid).is_closed, direction=first_sign)
            event = h.node.cache.account(h.hedge.account_id).last_event
            assert event.info["mt5_equity"] == "500"
            assert h.hedge.account_capacity_ready(h.strategy._config.max_cost_age_ns)
            assert source.order(cid).status is OrderStatus.CANCELED
            assert source.order(cid).filled_qty.as_decimal() == 0
            assert set(source.rows) == {first, cid}
            assert len(source.trades) == 1 and wire.submit_calls == [] and wire.close_calls == []
            assert source.net == D("0.5") * first_sign

    asyncio.run(run())


@pytest.mark.parametrize("first_sign", [1, -1], ids=["buy-first", "sell-first"])
def test_maker_executes_interleaved_hedges_in_real_allocation_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_sign: int,
) -> None:
    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=True, long_quantity=2, short_quantity=2,
        ) as (h, source, wire):
            await _accepted_source(h, source, wire, first_sign)
            await _accepted_source(h, source, wire, -first_sign)
            by_side = {1 if source.order(cid).side is OrderSide.BUY else -1: cid
                       for cid, row in source.rows.items() if source._active(row)}
            assert len(by_side) == 2
            original_ids = set(source.rows)
            cancels: list[dict[str, object] | list[object]] = []

            def delay_cancel_delivery(message: dict[str, object] | list[object]) -> None:
                if isinstance(message, list) and message[1] == "oc":
                    cancels.append(deepcopy(message))
                else:
                    source.respond(message)

            h.transport.after_send = delay_cancel_delivery
            entered, release = asyncio.Event(), asyncio.Event()
            verify_serial = wire.before_mutation
            assert verify_serial is not None

            async def delay_first_hedge(payload: JsonObject) -> None:
                await verify_serial(payload)
                if not entered.is_set():
                    entered.set()
                    await release.wait()

            wire.before_mutation = delay_first_hedge
            actual: list[tuple[str, Decimal]] = []

            def record_execution(outcome: JsonObject) -> None:
                payload = cast(JsonObject, outcome["payload"])
                actual.append((str(payload["side"]), sum(_ticket_facts(wire).values(), D(0))))

            wire.after_mutation = record_execution
            try:
                source.fill(by_side[first_sign], D("0.6"))
                async with asyncio.timeout(2):
                    await entered.wait()
                # Venue receives these genuine fills before the protective
                # cancels arrive; the first native MT5 order remains in flight.
                for sign in (-first_sign, first_sign) * 3:
                    source.fill(by_side[sign], D("0.2"))
                await _pump()
                stores = _stores(h)
                assert sum(len(store.intents()) for store in stores) == 7
                assert len(wire.submit_calls) == 1 and wire.close_calls == []
                assert len(cancels) == 2
                h.transport.after_send = source.respond
                for message in cancels:
                    source.respond(message)
            finally:
                release.set()

            owner = get_source_terminal_reconciler(h.node)

            def completed() -> bool:
                return (all(intent.status is ObligationStatus.COMPLETED
                            for store in stores for intent in store.intents())
                        and all(source.order(cid).is_closed for cid in original_ids)
                        and not owner.busy
                        and h.hedge.account_capacity_ready(h.strategy._config.max_cost_age_ns))

            await _drive(h, wire, completed, direction=0)
            first, second = ("sell", "buy") if first_sign == 1 else ("buy", "sell")
            assert [side for side, _ in actual] == [first, second] * 3 + [first]
            assert [net for _, net in actual] == [-D(first_sign), D(0)] * 3 + [-D(first_sign)]
            assert source.net == D("0.6") * first_sign
            assert h.node.portfolio.net_position(h.hedge_instrument.id) == -D(first_sign)
            assert sum((store.net_unhedged_ounces for store in stores), D(0)) == (
                D("-0.4") * first_sign
            )
            assert all(not store.can_submit_source() for store in stores)
            assert set(source.rows) == original_ids  # strict dust remains HOLD.
            assert owner.last_failure is None
            assert len(h.node.cache.orders(instrument_id=h.hedge_instrument.id)) == 7
            await _assert_reports(h, source, wire, _ticket_facts(wire))

    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("sign", [1, -1], ids=["long-first", "short-first"])
@pytest.mark.parametrize("deltas", [(2, 2, -2, -1, -2), (1, 2, -4)],
                         ids=["add-reduce-partial-cross", "two-ticket-cross"])
def test_both_execution_adapters_complete_continuous_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, sign: int,
    deltas: tuple[int, ...],
) -> None:
    async def run() -> None:
        changes = tuple(sign * value for value in deltas)
        async with _continuous(
            tmp_path, monkeypatch, maker=maker,
            long_quantity=max(changes), short_quantity=-min(changes),
        ) as (h, source, wire):
            owner = get_source_terminal_reconciler(h.node)
            expected_net = D(0)
            expected_tickets: dict[str, Decimal] = {}
            ticket_handles: dict[str, str] = {}
            request_ids: set[str] = set()
            for index, delta in enumerate(changes):
                cid = await _accepted_source(h, source, wire, delta)
                cursor = len(wire.journal)
                source.fill(cid, D(abs(delta)))
                await _settle_cycle(h, source, wire, cid=cid, expected=index + 1)
                expected_net += D(delta)
                assert source.net == expected_net
                assert h.node.portfolio.net_position(h.source_instrument.id) == expected_net
                assert h.node.portfolio.net_position(h.hedge_instrument.id) == -expected_net
                assert [trade[4] for trade in source.trades] == list(changes[:index + 1])
                assert source.order(cid).filled_qty.as_decimal() == abs(delta)
                if maker and sign == 1 and ((len(changes) == 5 and index == 3)
                                            or (len(changes) == 3 and index == 2)):
                    # Keep the real queued-modify race in the regression, including
                    # partial fill -> protective cancel and fully filled branches.
                    assert any(isinstance(event, OrderModifyRejected)
                               for event in source.order(cid).events)
                remaining = D(-delta)
                targets = [(key, value) for key, value in expected_tickets.items()
                           if value * remaining < 0]
                events = wire.journal[cursor:]
                assert len(events) % 2 == 0
                for reserved, terminal in zip(events[::2], events[1::2], strict=True):
                    assert reserved["event_type"] == "submission_reserved"
                    assert terminal["event_type"] == "order_filled"
                    payload = cast(JsonObject, reserved["payload"])
                    fill = cast(JsonObject, terminal["payload"])
                    request_id = str(payload["client_request_id"])
                    assert request_id == fill["client_request_id"] and request_id not in request_ids
                    request_ids.add(request_id)
                    order = h.node.cache.order(ClientOrderId(request_id))
                    assert order is not None and order.status == OrderStatus.FILLED
                    quantity = D(str(payload["quantity_lots"])) * 100
                    signed = quantity * (1 if payload["side"] == "buy" else -1)
                    assert order.quantity.as_decimal() == order.filled_qty.as_decimal() == quantity
                    assert (order.side == OrderSide.BUY) == (signed > 0)
                    assert 0 < quantity <= 2 and signed * remaining > 0
                    position_id = h.node.cache.position_id(order.client_order_id)
                    assert position_id is not None
                    assert position_id.value == fill["venue_position_id"]
                    if targets:
                        target, before = targets.pop(0)
                        assert order.is_reduce_only and position_id.value == target
                        assert payload["position_identifier"] == target
                        assert payload["position_ticket"] == ticket_handles[target]
                        assert quantity == min(abs(remaining), abs(before))
                        expected_tickets[target] += signed
                        if expected_tickets[target] == 0:
                            del expected_tickets[target]
                    else:
                        assert not order.is_reduce_only and "position_identifier" not in payload
                        assert position_id.value not in ticket_handles and signed == remaining
                        expected_tickets[position_id.value] = signed
                        rows = cast(list[JsonObject], wire.current_snapshot["positions"])
                        ticket_handles[position_id.value] = str(next(
                            row["ticket"] for row in rows if row["identifier"] == position_id.value
                        ))
                    remaining -= signed
                assert remaining == 0
                assert len(h.node.cache.orders(
                    instrument_id=h.hedge_instrument.id,
                )) == len(request_ids)
                assert owner.last_failure is None
                assert h.source.is_connected and h.hedge.is_connected
                await _assert_reports(h, source, wire, expected_tickets)
            assert expected_net != 0  # No test-only flatten or restart between cycles.
            assert len({trade[0] for trade in source.trades}) == len(changes)
            assert len({row[0] for row in source.rows.values()}) == len(source.rows)
            next_cid = await _accepted_source(h, source, wire, sign)
            assert source.order(next_cid).filled_qty.as_decimal() == 0
            assert source.net == expected_net  # Next source admitted without clearing inventory.
    asyncio.run(run())


@pytest.mark.parametrize("maker", [False, True], ids=["taker", "maker"])
@pytest.mark.parametrize("sign", [1, -1], ids=["long-first", "short-first"])
@pytest.mark.parametrize("mode", ["pending", "drift", "rejected", "unknown"])
def test_both_adapters_stop_later_legs_and_source_at_between_leg_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, sign: int, mode: str,
) -> None:
    async def run() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=maker,
            long_quantity=2 if sign == 1 else 4, short_quantity=4 if sign == 1 else 2,
        ) as (h, source, wire):
            for index, delta in enumerate((sign, 2 * sign)):
                cid = await _accepted_source(h, source, wire, delta)
                source.fill(cid, D(abs(delta)))
                await _settle_cycle(h, source, wire, cid=cid, expected=index + 1)
            before = deepcopy(cast(list[JsonObject], wire.current_snapshot["positions"]))
            assert [D(str(row["volume_lots"])) for row in before] == [D(".01"), D(".02")]
            assert source.net == 3 * sign
            assert h.node.portfolio.net_position(h.hedge_instrument.id) == -3 * sign
            cid = await _accepted_source(h, source, wire, -4 * sign)
            entered, release = asyncio.Event(), asyncio.Event()
            verify_serial = wire.before_mutation
            assert verify_serial is not None

            async def pause(payload: JsonObject) -> None:
                await verify_serial(payload)
                assert payload["position_identifier"] == before[0]["identifier"]
                wire.before_mutation = verify_serial
                entered.set()
                await release.wait()

            def after_first_close(outcome: JsonObject) -> None:
                wire.after_mutation = None
                payload = cast(JsonObject, outcome["payload"])
                assert payload["position_identifier"] == before[0]["identifier"]
                assert outcome["event_type"] == "order_filled"
                if mode == "drift":
                    # New venue fact only; deliberately do not alter the native planner cache.
                    rows = cast(list[JsonObject], wire.current_snapshot["positions"])
                    assert len(rows) == 1 and rows[0]["identifier"] == before[1]["identifier"]
                    rows[0]["volume_lots"] = "0.01"
                elif mode == "unknown":
                    wire.next_outcome = "order_unknown"
                elif mode == "rejected":
                    wire.next_outcome = "order_rejected"

            wire.before_mutation = pause
            wire.after_mutation = after_first_close
            source.fill(cid, D(4))
            try:
                await _drive(h, wire, entered.is_set, direction=0)
                assert len(wire.close_calls) == 1 and len(wire.submit_calls) == 2
                first_id = wire.close_calls[0][0]
                first_order = h.node.cache.order(ClientOrderId(first_id))
                assert first_order is not None and first_order.status == OrderStatus.SUBMITTED
                assert h.hedge.pending_client_order_ids == (first_id,)
                assert wire.current_snapshot["positions"] == before
                source_count = len(source.rows)
                for _ in range(3):
                    await _market(h, wire, -sign)
                    await asyncio.sleep(.05)
                assert len(source.rows) == source_count
                assert len(wire.close_calls) == 1 and len(wire.submit_calls) == 2
            finally:
                release.set()
            if mode == "pending":
                await _settle_cycle(h, source, wire, cid=cid, expected=3)
                assert len(wire.close_calls) == 2 and len(wire.submit_calls) == 3
                assert [D(call[2]) for call in wire.close_calls] == [D(".01"), D(".02")]
                assert D(wire.submit_calls[-1][2]) == D(".01")
                assert source.net == -sign
                assert h.node.portfolio.net_position(h.hedge_instrument.id) == sign
                await _assert_reports(h, source, wire, _ticket_facts(wire))
                return

            def fault_visible() -> bool:
                if mode == "unknown":
                    return any(pending.unknown for pending in h.hedge._pending.values())
                return any(order.status == (OrderStatus.DENIED if mode == "drift"
                                             else OrderStatus.REJECTED)
                           for order in h.node.cache.orders(instrument_id=h.hedge_instrument.id))

            await _drive(h, wire, fault_visible, direction=0)
            assert first_order.status == OrderStatus.FILLED
            source_count = len(source.rows)
            for _ in range(3):
                await _market(h, wire, -sign)
                await asyncio.sleep(.05)
            assert len(source.rows) == source_count
            assert len(wire.close_calls) == (1 if mode == "drift" else 2)
            assert len(wire.submit_calls) == 2  # No open residual, retry or replacement.
            assert len(h.node.cache.orders(instrument_id=h.hedge_instrument.id)) == 4
            intents = [intent for store in _stores(h) for intent in store.intents()]
            unresolved = [intent for intent in intents
                          if intent.status is not ObligationStatus.COMPLETED]
            assert len(intents) == 3 and len(unresolved) == 1
            intent = unresolved[0]
            assert intent.hedge_filled_ounces == 1 and intent.hedge_quantity_ounces == 4
            assert intent.hedge_leg_index == 1 and len(intent.hedge_plan) == 3
            assert sum((store.net_unhedged_ounces for store in _stores(h)), D(0)) == -3 * sign
            assert all(not store.can_submit_source() for store in _stores(h))
            assert _ticket_facts(wire) == {str(before[1]["identifier"]):
                                           D(-sign if mode == "drift" else -2 * sign)}
            # Reloading business files must retain the uncompleted obligation; never clear to flat.
            for store, reloaded in zip(_stores(h), h.reload_stores(), strict=True):
                assert {item.intent_id: item for item in reloaded.intents()} == {
                    item.intent_id: item for item in store.intents()
                }
                assert reloaded.net_unhedged_ounces == store.net_unhedged_ounces
                assert reloaded.halt_reason == store.halt_reason
                assert reloaded.source_freeze_reason == store.source_freeze_reason
            if mode == "unknown":
                pending_id = wire.close_calls[-1][0]
                assert h.hedge.pending_client_order_ids == (pending_id,)
                assert intent.status is ObligationStatus.SUBMITTED
                assert await h.hedge.generate_mass_status() is None
                assert h.hedge.pending_client_order_ids == (pending_id,)
            else:
                assert intent.status is ObligationStatus.REJECTED
                assert not h.hedge.pending_client_order_ids
    asyncio.run(run())
