"""Ordinary builders and actual adapters; all venue IO is finite synthetic evidence.

Native caches are replayed in memory, not represented as a process/Redis test.
No business progress is rewritten to manufacture a rejected or successful order.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest
import test_adapter_continuity
import test_live_maker
import test_live_taker
from continuous_mt5_wire import ContinuousMt5Wire
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId
from test_adapter_continuity import (
    _accepted_source,
    _continuous,
    _drive,
    _market,
    _settle_cycle,
    _SourceWire,
)
from test_mt5_v1_data import _tick_at_utc_ms
from test_startup_recovery import _History
from test_strategy_continuity import _OrdinaryStrategy, _pump

from py000_nautilus.app import _quote
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.models import ObligationStatus, RejectedHedgeAttempt
from py000_nautilus.mt5_v1_data import quote_from_pub
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionError
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.restart_recovery import StartupRecoveryOptions

D = Decimal


def _mapped_quote(instrument: Any, bid: str, ask: str, size: str, ts: int) -> Any:
    if instrument.id.venue.value != "MT5":
        return _quote(instrument, bid, ask, size, ts)
    message = _tick_at_utc_ms(ts // 1_000_000)
    message.update(bid=bid, ask=ask)
    quote = quote_from_pub(
        message, instrument, reference_utc_ms=ts // 1_000_000,
        now_utc_ms=ts // 1_000_000, timezone_name="Europe/Athens",
        ts_init=ts, max_tick_age_ms=60_000,
    )
    assert quote is not None and quote.bid_size == quote.ask_size == 0
    return quote


@asynccontextmanager
async def _restart(
    path: Path, monkeypatch: pytest.MonkeyPatch, *, maker: bool, history: _History,
    retry_cid: str, unknown: bool = False, stale_quote: bool = False,
) -> AsyncIterator[tuple[_OrdinaryStrategy, _SourceWire, ContinuousMt5Wire, asyncio.Event]]:
    # The common fixture still invokes the actual builder. Only its explicit
    # public startup option is supplied here; no receipt/callback is replaced.
    module = test_live_maker if maker else test_live_taker
    name = "build_live_maker_node" if maker else "build_live_taker_node"
    with monkeypatch.context() as build_patch:
        build_patch.setattr(module, name, partial(
            getattr(module, name),
            startup_recovery=StartupRecoveryOptions(True, retry_cid),
        ))
        h = _OrdinaryStrategy(
            path, monkeypatch, maker=maker, two_sided=maker,
            native_mt5_transport=True, inject_mt5_io=False,
        )
    source, wire = history.restore(h)
    dispatch = asyncio.Event()
    method = "_submit_hedge" if maker else "_submit_hedge_intent"
    submit = getattr(h.strategy, method)

    def observe_before_dispatch(*args: Any) -> None:
        assert not h.store.can_submit_source()
        if dispatch.is_set():
            submit(*args)

    monkeypatch.setattr(h.strategy, method, observe_before_dispatch)
    try:
        h.hedge.connect()
        async with asyncio.timeout(2):
            while h.hedge._poll_task is None:
                await asyncio.sleep(.005)
        # A finite PUB input uses the production mapper: MT5 carries no depth.
        # This component fixture still supplies the quote before Actor.on_start;
        # the separate process test covers actual data-client subscription timing.
        h.node.kernel.data_engine.process(_mapped_quote(
            h.hedge_instrument, "3936.7", "3936.8", "10",
            h.node.kernel.clock.timestamp_ns() - (60_000_000_000 if stale_quote else 0),
        ))
        # UNKNOWN cannot meet kernel reconciliation. Even if the component
        # fixture starts its trader, the production startup gate must retain it.
        await h.start(initial_reconciliation=not unknown)
        yield h, source, wire, dispatch
    finally:
        await h.hedge._disconnect()
        await h.close()


@pytest.mark.parametrize("maker,outcome", [
    pytest.param(maker, outcome, id=f"{'maker' if maker else 'taker'}-{outcome}")
    for maker, outcomes in (
        (False, ("filled", "unknown", "rejected-again", "session", "quote", "lot", "capacity")),
        (True, ("filled", "unknown", "rejected-again")),
    ) for outcome in outcomes
])
def test_ordinary_startup_retries_only_one_explicit_zero_fill_rejected_hedge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: bool, outcome: str,
) -> None:
    monkeypatch.setattr(test_adapter_continuity, "_quote", _mapped_quote)

    async def scenario() -> None:
        async with _continuous(
            tmp_path, monkeypatch, maker=maker, long_quantity=2, short_quantity=2,
        ) as (first, source, wire):
            cid = await _accepted_source(first, source, wire, 2)
            wire.next_outcome = "order_unknown" if outcome == "unknown" else "order_rejected"
            source.fill(cid, D(2))

            def terminal_observed() -> bool:
                if not first.store.intents() or len(wire.journal) != 3:
                    return False
                intent = first.store.intents()[0]
                if intent.hedge_client_order_id is None:
                    return False
                order = first.node.cache.order(ClientOrderId(intent.hedge_client_order_id))
                return order is not None and (
                    order.status is OrderStatus.REJECTED if outcome != "unknown"
                    else first.hedge.execution_hold_reason is not None
                )

            await _drive(first, wire, terminal_observed, direction=0)
            original = first.store.intents()[0]
            old_cid = original.hedge_client_order_id
            assert old_cid is not None and original.rejected_attempt is None
            old_order = first.node.cache.order(ClientOrderId(old_cid))
            assert old_order is not None and old_order.venue_order_id is None
            assert old_order.filled_qty == 0 and not old_order.trade_ids
            assert not any(isinstance(event, OrderFilled) for event in old_order.events)
            assert original.hedge_filled_ounces == 0 and original.hedge_leg_index == 0
            assert original.hedge_order_ids == (old_cid,) and len(original.hedge_plan) == 1
            assert len(wire.submit_calls) == 1 and not wire.close_calls
            assert wire.journal[-1]["event_type"] == (
                "order_unknown" if outcome == "unknown" else "order_rejected"
            )
            reserved = cast(JsonObject, wire.journal[1]["payload"])
            terminal = cast(JsonObject, wire.journal[2]["payload"])
            assert reserved["client_request_id"] == terminal["client_request_id"] == old_cid
            assert reserved["quantity_lots"] == terminal["quantity_lots"] == "0.02"
            # The shared snapshot originated in read-only EA fixtures. The
            # reviewed attempt requires an explicitly restored execution flag.
            cast(JsonObject, wire.current_snapshot["authority_flags"])["mql_trade_allowed"] = True
            if outcome == "session":
                cast(JsonObject, wire.current_snapshot["session"]).update(
                    session_open=False, scheduled_open=False,
                )
            elif outcome == "lot":
                cast(JsonObject, wire.current_snapshot["symbol_spec"])["volume_max"] = "0.01"
            elif outcome == "capacity":
                cast(JsonObject, wire.current_snapshot["account"]).update(
                    balance="1", equity="1", margin="0", margin_free="1", margin_level="0",
                )
            history = _History(first, source, wire)
        if outcome == "quote":
            def expired_quote(instrument: Any, bid: str, ask: str, size: str, ts: int) -> Any:
                if instrument.id.venue.value == "MT5":
                    ts -= 60_000_000_000
                return _mapped_quote(instrument, bid, ask, size, ts)

            monkeypatch.setattr(test_adapter_continuity, "_quote", expired_quote)
        old_events = {events[0].client_order_id: tuple(event.id for event in events)
                      for events, _, _ in history.orders}
        async with _restart(
            tmp_path, monkeypatch, maker=maker, history=history, retry_cid=old_cid,
            unknown=outcome == "unknown", stale_quote=outcome == "quote",
        ) as (second, source2, wire2, dispatch):
            owner = get_source_terminal_reconciler(second.node)
            await _drive(second, wire2, lambda: not owner.busy and (
                not owner.restart_pending or owner.last_failure is not None
            ), direction=0)
            current = second.store.intent(original.intent_id)
            assert {order.client_order_id: tuple(event.id for event in order.events)
                    for order in second.node.cache.orders()} == old_events
            assert not wire2.submit_calls and not wire2.close_calls
            assert not second.source_cancel_commands
            assert current.hedge_filled_ounces == current.hedge_leg_filled_ounces == 0
            assert not second.store._state.seen_hedge_fills
            assert current.hedge_leg_index == original.hedge_leg_index
            assert current.hedge_order_ids == original.hedge_order_ids
            if outcome != "unknown":
                mass = await second.hedge.generate_mass_status(None)
                assert mass is not None
                report = next(iter(mass.order_reports.values()))
                assert report.client_order_id.value == old_cid
                assert report.venue_order_id == second.hedge._synthetic_rejected_venue_order_id(
                    old_cid,
                )
                assert report.venue_order_id.value.startswith("PY000_REJ_")
                assert second.node.cache.order(ClientOrderId(old_cid)).venue_order_id is None
                assert report.order_status is OrderStatus.REJECTED and report.filled_qty == 0
                assert report.quantity == original.hedge_plan[0].quantity_ounces
                assert not any(mass.fill_reports.values())
            if outcome == "unknown":
                assert owner.restart_pending and owner.last_failure
                assert current.rejected_attempt is None and current.hedge_client_order_id == old_cid
                for direction in (1, -1):
                    await _market(second, wire2, direction)
                assert not wire2.submit_calls and not wire2.close_calls
                assert set(source2.rows) == set(history.source_facts[0])
                return
            errors = {
                "session": "current trading permission and session", "quote": "fresh account",
                "lot": "not an exact permitted MT5 lot size", "capacity": "fresh account capacity",
            }
            if outcome in errors:
                assert owner.restart_pending and owner.last_failure
                assert current.rejected_attempt is None and current.hedge_client_order_id == old_cid
                assert owner._restart_recovery is not None
                before = deepcopy(second.store._to_payload())
                error_type = Mt5V1ExecutionError if outcome == "lot" else ValueError
                with pytest.raises(error_type, match=errors[outcome]):
                    await owner._restart_recovery()
                assert second.store._to_payload() == before
                assert not wire2.submit_calls and not wire2.close_calls
                assert set(source2.rows) == set(history.source_facts[0])
                return
            if owner.restart_pending:
                assert owner._restart_recovery is not None
                await owner._restart_recovery()  # Expose a genuine gate failure, not its label.
            assert not owner.restart_pending, owner.last_failure
            assert current.status is ObligationStatus.PENDING
            assert current.hedge_client_order_id is None
            assert current.rejected_attempt == RejectedHedgeAttempt(old_cid, 0)
            assert not current.hedge_leg_order_ids and not second.store.can_submit_source()
            wire2.next_outcome = "order_rejected" if outcome == "rejected-again" else "order_filled"
            dispatch.set()
            if outcome == "filled":
                await _settle_cycle(second, source2, wire2, cid=cid, expected=1)
            else:
                await _drive(second, wire2, lambda: second.store.intent(original.intent_id).status
                             is ObligationStatus.REJECTED, direction=0)
            current = second.store.intent(original.intent_id)
            assert len(wire2.submit_calls) == 1 and not wire2.close_calls
            new_cid = wire2.submit_calls[0][0]
            assert new_cid != old_cid
            assert current.hedge_order_ids == (old_cid, new_cid)
            assert current.hedge_leg_order_ids == (new_cid,)
            assert current.rejected_attempt == RejectedHedgeAttempt(old_cid, 0)
            assert tuple(event.id for event in second.node.cache.order(
                ClientOrderId(old_cid),
            ).events) == old_events[ClientOrderId(old_cid)]
            assert second.reload_stores()[0].intent(original.intent_id) == current
            if outcome == "filled":
                assert current.status is ObligationStatus.COMPLETED
                assert current.hedge_filled_ounces == current.hedge_quantity_ounces == 2
                assert len(second.store._state.seen_hedge_fills) == 1
                assert next(iter(second.store._state.seen_hedge_fills)).startswith(f"{new_cid}|")
                next_cid = await _accepted_source(second, source2, wire2, -2)
                assert next_cid != cid
                source2.fill(next_cid, D(2))
                await _settle_cycle(second, source2, wire2, cid=next_cid, expected=2)
                assert len(wire2.close_calls) == 1
                return
            assert second.store.halt_reason and not second.store.can_submit_source()
            assert current.hedge_filled_ounces == 0 and not second.store._state.seen_hedge_fills
            for direction in (1, -1):
                await _market(second, wire2, direction)
            assert len(wire2.submit_calls) == 1
            assert set(source2.rows) == set(history.source_facts[0])
            third_history = _History(second, source2, wire2)
        async with _restart(
            tmp_path, monkeypatch, maker=maker, history=third_history, retry_cid=new_cid,
        ) as (third, source3, wire3, _dispatch):
            owner = get_source_terminal_reconciler(third.node)
            before = deepcopy(third.store._to_payload())
            await _drive(third, wire3, lambda: not owner.busy and owner.last_failure is not None,
                         direction=0)
            assert owner.restart_pending
            current = third.store.intent(original.intent_id)
            assert current.rejected_attempt == RejectedHedgeAttempt(old_cid, 0)
            assert current.hedge_order_ids == (old_cid, new_cid)
            assert current.hedge_filled_ounces == 0 and not third.store._state.seen_hedge_fills
            assert third.store._to_payload() == before
            assert not wire3.submit_calls and not wire3.close_calls
            assert set(source3.rows) == set(third_history.source_facts[0])
            await _pump()
    asyncio.run(scenario())
