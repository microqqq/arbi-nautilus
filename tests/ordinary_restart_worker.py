"""Ordinary entry process with finite venue IO and externally killed checkpoints.

No native orders/accounts/positions are seeded. The venue fixture contains only
received wire requests and their raw remote results; native history comes from
the ordinary builder's Redis load. A checkpoint never stops/disposes the node.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import traceback
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import msgspec
import test_adapter_continuity
from continuous_mt5_wire import ContinuousMt5Wire
from msgspec.structs import replace
from nautilus_trader.cache.database import CacheDatabaseAdapter
from nautilus_trader.config import DatabaseConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import AccountId, ClientId, ClientOrderId, TraderId, Venue
from nautilus_trader.serialization.serializer import MsgSpecSerializer
from test_adapter_continuity import _accepted_source, _market, _SourceWire
from test_bitfinex_v1_execution import _FakeRest, _FakeTransport, _Harness
from test_live_maker_entry import _maker_profile
from test_live_taker_entry import _paper_profile
from test_mt5_v1_data import _tick_at_utc_ms
from test_mt5_v1_execution import _identity, _snapshot
from test_startup_retry import _mapped_quote
from test_strategy_continuity import _OrdinaryStrategy

import py000_nautilus
from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport
from py000_nautilus.hedge import HedgeCoordinator
from py000_nautilus.live_cache import native_cache_config
from py000_nautilus.live_maker import build_live_maker_node
from py000_nautilus.live_runtime import get_source_terminal_reconciler
from py000_nautilus.live_taker import build_live_taker_node
from py000_nautilus.live_taker_entry import main, maker_main
from py000_nautilus.models import ObligationStatus
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.mt5_v1_transport import Mt5V1Transport
from py000_nautilus.store import JsonStateStore


def _write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, sort_keys=True, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _facts(orders: Any, positions: Any, clients: Any, indexes: Any) -> dict[str, Any]:
    return {
        "orders": {order.client_order_id.value: {
            "owner": str(order.strategy_id), "trader": str(order.trader_id),
            "account": str(order.account_id), "instrument": str(order.instrument_id),
            "side": order.side.name, "status": order.status.name,
            "quantity": str(order.quantity.as_decimal()),
            "filled": str(order.filled_qty.as_decimal()),
            "venue": str(order.venue_order_id), "position": str(order.position_id),
            "client": str(clients.get(order.client_order_id)),
            "index": str(indexes.get(order.client_order_id)),
            "events": [str(event.id) for event in order.events],
            "trades": [str(trade) for trade in order.trade_ids],
            "fills": [{"quantity": str(event.last_qty.as_decimal()),
                       "price": str(event.last_px.as_decimal()),
                       "commission": str(event.commission), "time": event.ts_event}
                      for event in order.events if isinstance(event, OrderFilled)],
        } for order in orders},
        "positions": {str(position.id): {
            "owner": str(position.strategy_id), "account": str(position.account_id),
            "instrument": str(position.instrument_id),
            "quantity": str(position.signed_decimal_qty()),
            "avg_px_open": str(position.avg_px_open),
            "realized_pnl": str(position.realized_pnl),
            "events": [str(event.id) for event in position.events],
            "trades": [str(trade) for trade in position.trade_ids],
        } for position in positions},
    }


def _native(node: Any) -> dict[str, Any]:
    orders = node.cache.orders()
    return _facts(orders, node.cache.positions(), {
        order.client_order_id: node.cache.client_id(order.client_order_id) for order in orders
    }, {order.client_order_id: node.cache.position_id(order.client_order_id) for order in orders})


def _database(port: int) -> DatabaseConfig:
    return DatabaseConfig(host="127.0.0.1", port=port, connection_timeout=1,
                          response_timeout=1, number_of_retries=0)


def probe(kind: str, port: int) -> dict[str, Any]:
    """Independent native reads only; close this reader, never the producing node."""
    backend = CacheDatabaseAdapter(
        trader_id=TraderId(f"PY000-{kind.upper()}-LIVE-001"), instance_id=UUID4(),
        serializer=MsgSpecSerializer(msgspec.msgpack, timestamps_as_str=True),
        config=native_cache_config(_database(port)),
    )
    try:
        return _facts(backend.load_orders().values(), backend.load_positions().values(),
                      backend.load_index_order_client(), backend.load_index_order_position())
    finally:
        backend.close()


def _profile(kind: str, directory: Path, port: int, cut: str) -> Any:
    profile: Any = _maker_profile(directory) if kind == "maker" else _paper_profile(directory)
    identity = _identity()
    binding = dict(expected_account_id=identity.account_id,
                   expected_ea_build_id=identity.ea_build_id,
                   expected_source_sha256=identity.declared_source_sha256)
    strategy = profile.strategy_config
    account = AccountId(f"MT5-{identity.account_id}")
    long_quantity = Decimal(4 if cut == "between-legs" else 2)
    if kind == "maker":
        strategy = replace(strategy, hedge_accounts=(
            replace(strategy.hedge_accounts[0], account_id=account),
        ), economics=replace(
            strategy.economics,
            bid=replace(strategy.economics.bid, open_quantity_ounces=long_quantity),
            ask=replace(strategy.economics.ask, open_quantity_ounces=Decimal(
                2 if cut == "between-legs" else 0,
            )),
        ))
    else:
        strategy = replace(strategy, hedge_account_id=account,
                           economics=replace(strategy.economics,
                                             open_quantity_long=long_quantity))
    return replace(
        profile, cache_database=_database(port), strategy_config=strategy,
        mt5_data_config=replace(profile.mt5_data_config, **binding),
        mt5_exec_config=replace(profile.mt5_exec_config, **binding,
                               expected_stream_id=identity.stream_id),
        connection_timeout_seconds=3.0, stop_timeout_seconds=1.0,
    )


def run(kind: str, cut: str, phase: str, port: int, directory: Path) -> int:
    profile_path = directory / "profile.json"
    if phase == "produce":
        profile_path.write_bytes(_profile(kind, directory, port, cut).json())
    package = Path(py000_nautilus.__file__).resolve().parent
    production = {str(path.relative_to(package)): sha256(path.read_bytes()).hexdigest()
                  for path in sorted(package.rglob("*.py"))}
    loaded: dict[str, Any] = {}
    driver_errors: list[str] = []
    builder = build_live_maker_node if kind == "maker" else build_live_taker_node

    def wire_only_builder(**kwargs: Any) -> Any:
        node, strategy = builder(**kwargs)
        loaded.update(native=_native(node), event_count=node.kernel.exec_engine.event_count)
        h: Any = SimpleNamespace(
            node=node, strategy=strategy, maker=kind == "maker",
            source_quantity=Decimal(4 if cut == "between-legs" else 2),
            wallet_currency=kwargs["bitfinex_exec_config"].wallet_currency,
            source=node.kernel.exec_engine._clients[ClientId("BITFINEX")],
            hedge=node.kernel.exec_engine._clients[ClientId("MT5")],
            source_data=node.kernel.data_engine.routing_map[Venue("BITFINEX")],
            hedge_data=node.kernel.data_engine.routing_map[Venue("MT5")],
            source_cancel_commands=[], transport=_FakeTransport(),
            rest=_FakeRest(kwargs["bitfinex_exec_config"].raw_symbol),
        )
        h.store = (strategy._stores[next(iter(strategy._stores))]
                   if h.maker else strategy.state_store)
        h.reload_stores = MethodType(_OrdinaryStrategy.reload_stores, h)
        h.wire = object.__new__(_Harness)
        h.wire.raw_symbol = kwargs["bitfinex_exec_config"].raw_symbol
        h.wire.fee_currency = "USD"
        h.rest.wallet_rows = [["margin", h.wallet_currency, Decimal(10000), Decimal(0),
                              Decimal(10000)]]
        h.source._transport, h.source._rest = h.transport, h.rest
        h.source_data._transport = _FakeTransport()
        snapshot = _snapshot(_identity())
        snapshot["positions"] = []
        cast(JsonObject, snapshot["execution_limits"])["max_order_lots"] = "0.02"
        wire = ContinuousMt5Wire(_identity(), snapshot, now_ns=node.kernel.clock.timestamp_ns)
        h.hedge._transport = wire
        h.hedge_data._transport = wire
        wire.topic = kwargs["mt5_data_config"].expected_symbol.encode()  # type: ignore[attr-defined]
        pub_count = 0

        async def finite_pub() -> Any:
            nonlocal pub_count
            if (phase != "produce" and cut in {"hedge-rejected-held", "hedge-rejected-retry"}
                    and pub_count == 0):
                while not h.hedge_data._quote_subscribed:
                    await asyncio.sleep(.001)
                # A normal EA timer can publish after Actor's first two rounds.
                # Deliver through the actual data reader, never cache a startup
                # quote directly or fabricate an MT5 depth measurement.
                await asyncio.sleep(.3)
                _write(directory / "before-pub.json", observation())
                message = _tick_at_utc_ms(node.kernel.clock.timestamp_ns() // 1_000_000)
                message.update(identity=wire.identity.to_wire(), bid="3936.7", ask="3936.8")
                pub_count += 1
                return wire.topic, message  # type: ignore[attr-defined]
            await asyncio.Event().wait()

        wire.recv_pub = finite_pub  # type: ignore[attr-defined]
        source = _SourceWire(h)
        venue_path = directory / "venue.json"
        source_requests: list[Any] = []
        source_while_unresolved: list[int] = []
        source_attempt_states: dict[str, dict[str, str]] = {}
        if phase != "produce":
            venue = json.loads(venue_path.read_text())
            source.rows = {int(cid): row for cid, row in venue["source_rows"].items()}
            for row in source.rows.values():
                for index in (6, 7, 16, 17):
                    row[index] = Decimal(str(row[index]))
            source.trades = venue["source_trades"]
            for trade in source.trades:
                for index in (4, 5, 7, 9):
                    if trade[index] is not None:
                        trade[index] = Decimal(str(trade[index]))
            source.net, source.average_price = map(
                Decimal, (venue["source_net"], venue["source_avg"]),
            )
            source_requests = venue["source_requests"]
            source_while_unresolved = venue["source_while_unresolved"]
            source_attempt_states = venue["source_attempt_states"]
            wire.current_snapshot, wire.journal = venue["mt5_snapshot"], venue["mt5_journal"]
            wire._serial = venue["mt5_serial"]
            wire.submit_calls, wire.close_calls = venue["mt5_submits"], venue["mt5_closes"]
            source.publish()

        def save_venue() -> None:
            _write(venue_path, {
                "source_rows": source.rows, "source_trades": source.trades,
                "source_net": source.net, "source_avg": source.average_price,
                "source_requests": source_requests, "mt5_snapshot": wire.current_snapshot,
                "source_while_unresolved": source_while_unresolved,
                "source_attempt_states": source_attempt_states,
                "mt5_journal": wire.journal, "mt5_serial": wire._serial,
                "mt5_submits": wire.submit_calls, "mt5_closes": wire.close_calls,
            })

        original_publish, original_response = source.publish, source.respond

        def publish() -> None:
            original_publish()
            save_venue()  # Commit remote facts before the fake WS delivers any event.

        def respond(message: Any) -> None:
            if isinstance(message, list):
                source_requests.append(message)
                views = strategy._stores.values() if h.maker else (h.store,)
                if message[1] == "on":
                    states = {intent.intent_id: intent.status.value
                              for view in views for intent in view.intents()}
                    source_attempt_states[str(message[3]["cid"])] = states
                    if any(status != ObligationStatus.COMPLETED.value
                           for status in states.values()):
                        source_while_unresolved.append(message[3]["cid"])
                save_venue()  # Observe attempted sends even if the fake venue rejects them.
            original_response(message)

        source.publish = publish  # type: ignore[method-assign]
        h.transport.after_send = respond
        wire.after_mutation = lambda _outcome: save_venue()
        submit, close = wire.submit_market_delta, wire.close_position

        async def submit_observed(*args: Any, **kwargs: Any) -> Any:
            try:
                return await submit(*args, **kwargs)
            finally:
                save_venue()  # Includes duplicate attempts, not just new venue effects.

        async def close_observed(*args: Any, **kwargs: Any) -> Any:
            try:
                return await close(*args, **kwargs)
            finally:
                save_venue()

        wire.submit_market_delta = submit_observed  # type: ignore[method-assign]
        wire.close_position = close_observed  # type: ignore[method-assign]
        save_venue()
        h.transport.queue.put_nowait(
            {"event": "auth", "status": "OK", "chanId": 0, "userId": 269_312},
        )
        h.transport.queue.put_nowait([0, "ws", h.rest.wallet_rows])
        original_cancel = h.source.cancel_order

        def cancel(command: Any) -> None:
            h.source_cancel_commands.append(command.client_order_id)
            original_cancel(command)

        h.source.cancel_order = cancel

        def observation() -> dict[str, Any]:
            owner = strategy._state_store if h.maker else h.store
            quote = node.cache.quote_tick(strategy._config.hedge_instrument_id)
            return {
                "pid": os.getpid(), "package": str(Path(py000_nautilus.__file__).resolve()),
                "production": production,
                "native": _native(node), "business": owner._to_payload(),
                "business_bytes": owner.path.read_text() if owner.path.exists() else None,
                "source_can_submit": [view.can_submit_source() for view in (
                    strategy._stores.values() if h.maker else (h.store,)
                )],
                "source_requests": len(source_requests), "hedge_requests": len(wire.submit_calls),
                "close_requests": len(wire.close_calls), "source_net": str(source.net),
                "source_request_ids": [message[3]["cid"] for message in source_requests
                                       if message[1] == "on"],
                "source_while_unresolved": source_while_unresolved,
                "source_attempt_states": source_attempt_states,
                "hedge_request_ids": [call[0] for call in wire.submit_calls],
                "close_request_ids": [call[0] for call in wire.close_calls],
                "pending_hedge_ids": sorted(h.hedge.pending_client_order_ids),
                "hedge_admitted": h.hedge.execution_admitted,
                "hedge_hold": h.hedge.execution_hold_reason,
                "journal_types": [event["event_type"] for event in wire.journal],
                "restart_pending": get_source_terminal_reconciler(node).restart_pending,
                "reconciler_busy": get_source_terminal_reconciler(node).busy,
                "failure": get_source_terminal_reconciler(node).last_failure,
                "node_running": node.is_running(), "strategy_running": strategy.is_running,
                "connected": h.source.is_connected and h.hedge.is_connected,
                "mt5_pub_count": pub_count,
                "mt5_quote": None if quote is None else {
                    "bid": str(quote.bid_price), "ask": str(quote.ask_price),
                    "bid_size": str(quote.bid_size), "ask_size": str(quote.ask_size),
                    "ts_event": quote.ts_event,
                },
            }

        def checkpoint() -> None:
            name = "checkpoint-interrupt.json" if phase == "interrupt" else "checkpoint.json"
            _write(directory / name, observation())
            threading.Event().wait()  # Parent SIGKILL; no finally/stop/cache.close runs.

        async def settle(expected: int) -> None:
            # Stop the finite feed after this opportunity. Repeated neutral ticks
            # can legitimately create Maker's next passive quote after settlement.
            views = tuple(strategy._stores.values()) if h.maker else (h.store,)
            async with asyncio.timeout(8):
                while True:
                    intents = [intent for view in views for intent in view.intents()]
                    if (len(intents) == expected
                            and all(intent.status is ObligationStatus.COMPLETED
                                    for intent in intents)
                            and all(view.active_source_order_id is None
                                    and view.halt_reason is None
                                    and view.source_freeze_reason is None for view in views)
                            and h.hedge.account_capacity_ready(strategy._config.max_cost_age_ns)
                            and not get_source_terminal_reconciler(node).busy):
                        return
                    await asyncio.sleep(.01)

        if phase == "produce" and cut == "source-reserved":
            original_begin = JsonStateStore.begin_source

            def begin(store: JsonStateStore, *args: Any, **values: Any) -> None:
                original_begin(store, *args, **values)
                checkpoint()  # Actual fsync returned; submit_order has not been called.

            patch.object(JsonStateStore, "begin_source", begin).start()
        elif phase == "produce" and cut in {"hedge-callback", "source-callback", "recovery-killed"}:
            method = "on_source_filled" if cut == "source-callback" else "on_hedge_filled"
            original_fill = getattr(HedgeCoordinator, method)

            def filled(coordinator: HedgeCoordinator, event: Any) -> Any:
                checkpoint()  # Native Order/Position updated, business reducer not entered.
                return original_fill(coordinator, event)

            patch.object(HedgeCoordinator, method, filled).start()
        elif phase == "produce" and cut == "between-legs":
            original_bind = HedgeCoordinator.bind_hedge_leg

            def bind(coordinator: HedgeCoordinator, intent_id: str, cid: str) -> None:
                if coordinator._store.intent(intent_id).hedge_leg_index == 1:
                    checkpoint()  # Old close was applied; next CID has never been bound/sent.
                original_bind(coordinator, intent_id, cid)

            patch.object(HedgeCoordinator, "bind_hedge_leg", bind).start()
        elif phase == "produce" and cut == "request-pending":
            async def pending(payload: JsonObject) -> None:
                save_venue()  # Actual received request and submission_reserved, no outcome.
                async with asyncio.timeout(2):
                    order = node.cache.order(ClientOrderId(str(payload["client_request_id"])))
                    while order.status not in {OrderStatus.SUBMITTED, OrderStatus.ACCEPTED}:
                        await asyncio.sleep(0)
                checkpoint()

            wire.before_mutation = pending
        elif phase == "interrupt":
            original_recover = JsonStateStore.recover_for_start

            def recover(store: JsonStateStore) -> str | None:
                reason = original_recover(store)
                if reason is not None:
                    checkpoint()  # Existing startup pause really reached the original file.
                return reason

            patch.object(JsonStateStore, "recover_for_start", recover).start()

        actor = get_source_terminal_reconciler(node)
        recovery = actor._restart_recovery
        if phase != "produce" and cut.startswith("hedge-rejected-") and recovery is not None:
            async def observe_recovery() -> None:
                try:
                    await recovery()
                except Exception:
                    _write(directory / "startup-diagnostic.json", {"error": traceback.format_exc()})
                    raise  # Observation only: preserve the original admission/failure result.

            actor._restart_recovery = observe_recovery

        async def driver() -> None:
            try:
                async with asyncio.timeout(8):
                    while not (node.is_running() if phase != "produce" and cut == "request-pending"
                               else node.trader.is_running and strategy.is_running):
                        await asyncio.sleep(.01)
                h.source_instrument = node.cache.instrument(strategy._config.source_instrument_id)
                h.hedge_instrument = node.cache.instrument(strategy._config.hedge_instrument_id)
                if phase != "produce":
                    actor = get_source_terminal_reconciler(node)
                    if cut != "request-pending":
                        async with asyncio.timeout(8):
                            while actor.restart_pending and (
                                actor.last_failure is None or actor.busy
                            ):
                                await asyncio.sleep(.01)
                    if (cut in {"hedge-rejected-held", "hedge-rejected-retry"}
                            and actor.last_failure is not None):
                        async with asyncio.timeout(1):
                            while pub_count == 0:
                                await asyncio.sleep(.01)
                        await asyncio.sleep(.01)  # Let the real data queue consume that PUB.
                    _write(directory / "recovered.json", observation() | {"loaded": loaded})
                    if cut in {"source-reserved", "request-pending", "old-hold", "recovery-killed",
                               "hedge-rejected-held", "hedge-rejected-no-quote"}:
                        if cut != "hedge-rejected-no-quote":
                            for _ in range(3):
                                await _market(h, wire, 1)
                        else:
                            # No PUB and no direct tick input: the ordinary round
                            # deadline must finish without inventing a first quote.
                            await asyncio.sleep(.1)
                        _write(directory / "final.json", observation())
                        return
                    assert not actor.restart_pending, actor.last_failure
                    async with asyncio.timeout(8):
                        while not (directory / "advance").exists():
                            await asyncio.sleep(.01)
                    if cut in {"source-callback", "between-legs", "hedge-rejected-retry"}:
                        await _market(h, wire, 0)  # Original dispatcher, not a direct submit.
                        await settle(2 if cut == "between-legs" else 1)
                        _write(directory / "continued.json", observation())
                elif cut == "between-legs":
                    initial = await _accepted_source(h, source, wire, -2)
                    source.fill(initial, Decimal(2))
                    await settle(1)
                delta = (4 if phase == "produce" else -2) if cut == "between-legs" else 2
                cid = await _accepted_source(h, source, wire, delta)
                if phase == "produce" and cut.startswith("hedge-rejected-"):
                    # Actual finite EA response, never a native/business state seed.
                    wire.next_outcome = "order_rejected"
                source.fill(cid, Decimal(abs(delta)))
                if phase == "produce" and cut.startswith("hedge-rejected-"):
                    views = tuple(strategy._stores.values()) if h.maker else (h.store,)
                    async with asyncio.timeout(8):
                        while True:
                            intents = [intent for view in views for intent in view.intents()]
                            if (len(intents) == 1 and intents[0].status is ObligationStatus.REJECTED
                                    and all(view.active_source_order_id is None for view in views)
                                    and not h.hedge.pending_client_order_ids
                                    and not get_source_terminal_reconciler(node).busy):
                                old = node.cache.order(ClientOrderId(wire.submit_calls[0][0]))
                                assert old.status is OrderStatus.REJECTED and old.filled_qty == 0
                                # The inherited snapshot is read-only. Model the
                                # remote permission recovery before the next run;
                                # do not alter native events, business HOLD, or gates.
                                cast(JsonObject, wire.current_snapshot["authority_flags"])[
                                    "mql_trade_allowed"
                                ] = True
                                save_venue()
                                checkpoint()  # Native rejection and normal business HOLD persisted.
                            await asyncio.sleep(.01)
                await settle(1 if phase == "produce" else 3 if cut == "between-legs" else 2)
                if phase == "produce":
                    if cut == "old-hold":
                        if h.maker:
                            strategy._state_store.freeze_sources("operator review required")
                        else:
                            h.store.freeze_source_submissions("operator review required")
                    checkpoint()
                else:
                    _write(directory / "final.json", observation())
            except Exception:
                driver_errors.append(traceback.format_exc())
                _write(directory / f"{phase}-error.json", {
                    "error": driver_errors[-1], "observation": observation(),
                })
                await node.stop_async()

        node.kernel.loop.create_task(driver())
        return node, strategy

    def observed_builder(**kwargs: Any) -> Any:
        try:
            return wire_only_builder(**kwargs)
        except Exception:
            _write(directory / f"{phase}-error.json", {"error": traceback.format_exc()})
            raise

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("restart test tried to open an actual trading transport")

    entry = maker_main if kind == "maker" else main
    with patch.object(BitfinexV1Transport, "open", forbidden), patch.object(
        Mt5V1Transport, "open", forbidden,
    ), patch.object(
        test_adapter_continuity, "_quote", _mapped_quote,
    ):
        options = ["--profile", str(profile_path), "--run-paper", "--env-file",
                   str(directory / "empty.env")]
        if phase == "recover" and cut in {"hedge-rejected-retry", "hedge-rejected-no-quote"}:
            original = json.loads((directory / "checkpoint.json").read_text())
            old_cid, = original["hedge_request_ids"]
            options.extend(["--resume-held", "--retry-rejected-hedge", old_cid])
        result = entry(
            options,
            node_builder=observed_builder,
            environment={"BFX_TEST_API_KEY": "SYNTHETIC", "BFX_TEST_API_SECRET": "SYNTHETIC",
                         "BFX_TEST_USER_ID": "269312"},
        )
    return 1 if driver_errors else result


if __name__ == "__main__":
    kind, cut, phase, raw_port, raw_directory = sys.argv[1:]
    if phase == "probe":
        print("RESTART_JSON=" + json.dumps(probe(kind, int(raw_port)), sort_keys=True))
    else:
        raise SystemExit(run(kind, cut, phase, int(raw_port), Path(raw_directory)))
