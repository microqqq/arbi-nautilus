"""Bounded MT5 test wire: one journal and its actual HEDGING ticket changes.

No sockets, adapter overrides, native-cache writes, or account/PnL simulation.
The caller supplies fixed account facts and the existing node clock. Mutation
hooks are only explicit test barriers/faults; normal execution needs neither.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from decimal import Decimal
from typing import Literal, cast

from test_mt5_v1_execution import _event, _FakeTransport, _page, _snapshot, _stream_started

from py000_nautilus.mt5_v1_protocol import Binding, Identity, JsonObject
from py000_nautilus.mt5_v1_transport import Mt5V1RemoteError


class ContinuousMt5Wire(_FakeTransport):
    def __init__(
        self, identity: Identity, snapshot: JsonObject, *, now_ns: Callable[[], int],
        fill_price: Decimal = Decimal("3936.7"),
    ) -> None:
        super().__init__(identity, deepcopy(snapshot))
        assert fill_price.is_finite() and fill_price > 0
        self.now_ns = now_ns
        self.fill_price = fill_price
        self.journal = [_stream_started(identity)]
        self.journal[0]["event_time_ms"] = str(now_ns() // 1_000_000)
        self.next_outcome: Literal["order_filled", "order_rejected", "order_unknown"] = (
            "order_filled"
        )
        self.before_mutation: Callable[[JsonObject], Awaitable[None]] | None = None
        self.after_mutation: Callable[[JsonObject], None] | None = None
        self._serial = max([
            1_000_000_000,
            *[int(cast(str, position[key])) for position in self._positions()
              for key in ("identifier", "ticket")],
        ])

    def _positions(self) -> list[JsonObject]:
        return cast(list[JsonObject], self.current_snapshot["positions"])

    def _new_id(self) -> str:
        self._serial += 1
        return str(self._serial)

    def _append(self, event_type: str, payload: JsonObject) -> JsonObject:
        event = _event(self.identity, len(self.journal) + 1, event_type, deepcopy(payload))
        event["event_time_ms"] = str(self.now_ns() // 1_000_000)
        self.journal.append(event)
        return event

    async def snapshot(self, binding: Binding) -> JsonObject:
        sample = deepcopy(await super().snapshot(binding))
        cast(JsonObject, sample["time"])["observed_utc_ms"] = str(self.now_ns() // 1_000_000)
        return sample

    async def execution_events(
        self, binding: Binding, *, after_cursor: str, limit: int,
    ) -> JsonObject:
        assert binding == self.identity.binding()
        assert 1 <= limit <= 500 and int(after_cursor) >= 0
        self.event_calls.append((after_cursor, limit))
        if int(after_cursor) > len(self.journal):
            raise Mt5V1RemoteError("CURSOR_AHEAD", "test cursor exceeds retained journal")
        return _page(
            self.identity, after_cursor=after_cursor, last_cursor=str(len(self.journal)),
            events=deepcopy(self.journal[int(after_cursor):int(after_cursor) + limit]),
        )

    async def submit_market_delta(
        self, binding: Binding, *, client_request_id: str, side: str, quantity_lots: str,
    ) -> JsonObject:
        assert binding == self.identity.binding()
        self.submit_calls.append((client_request_id, side, quantity_lots))
        return await self._mutate({
            "client_request_id": client_request_id, "side": side, "quantity_lots": quantity_lots,
        })

    async def close_position(
        self, binding: Binding, *, client_request_id: str, side: str, quantity_lots: str,
        position_ticket: str, position_identifier: str,
    ) -> JsonObject:
        assert binding == self.identity.binding()
        self.close_calls.append(
            (client_request_id, side, quantity_lots, position_ticket, position_identifier),
        )
        return await self._mutate({
            "client_request_id": client_request_id, "side": side, "quantity_lots": quantity_lots,
            "position_ticket": position_ticket, "position_identifier": position_identifier,
        })

    async def _mutate(self, payload: JsonObject) -> JsonObject:
        prior = [event for event in self.journal if event["event_type"] != "stream_started"
                 and cast(JsonObject, event["payload"])["client_request_id"]
                 == payload["client_request_id"]]
        if prior:
            if prior[0]["payload"] != payload:
                raise Mt5V1RemoteError("IDEMPOTENCY_CONFLICT", "test request changed")
            assert len(prior) == 2, "duplicate pending request is outside this finite fixture"
            return {"identity": self.identity.to_wire(), "outcome": deepcopy(prior[-1])}
        assert len(self.journal) + 2 <= 201, "bounded test journal exhausted"
        lots = Decimal(cast(str, payload["quantity_lots"]))
        spec = cast(JsonObject, self.current_snapshot["symbol_spec"])
        limits = cast(JsonObject, self.current_snapshot["execution_limits"])
        assert lots.is_finite() and Decimal(cast(str, spec["volume_min"])) <= lots <= Decimal(
            cast(str, limits["max_order_lots"]),
        )
        assert lots % Decimal(cast(str, spec["volume_step"])) == 0
        assert payload["side"] in {"buy", "sell"}
        self._append("submission_reserved", payload)
        if self.before_mutation is not None:
            await self.before_mutation(deepcopy(payload))
        result = self.next_outcome
        assert result in {"order_filled", "order_rejected", "order_unknown"}
        self.next_outcome = "order_filled"
        terminal = deepcopy(payload)
        if result == "order_filled":
            position_id = self._fill_position(payload, lots)
            terminal.update({
                "broker_retcode": "10009", "commission": "0",
                "fill_price": str(self.fill_price), "filled_quantity_lots": str(lots),
                "venue_order_id": self._new_id(), "venue_deal_id": self._new_id(),
                "venue_position_id": position_id,
            })
        else:
            terminal.update({"broker_retcode": "10013", "reason": f"synthetic_{result}"})
        outcome = self._append(result, terminal)
        if self.after_mutation is not None:
            self.after_mutation(deepcopy(outcome))
        return {"identity": self.identity.to_wire(), "outcome": deepcopy(outcome)}

    def _fill_position(self, payload: JsonObject, lots: Decimal) -> str:
        positions = self._positions()
        identifier = cast(str | None, payload.get("position_identifier"))
        if identifier is not None:
            targets = [position for position in positions if position["identifier"] == identifier]
            assert len(targets) == 1, "wire close must target one existing ticket"
            target = targets[0]
            assert target["ticket"] == payload["position_ticket"]
            assert target["magic"] == self.identity.magic and target["side"] != payload["side"]
            remaining = Decimal(cast(str, target["volume_lots"])) - lots
            assert remaining >= 0, "wire close cannot exceed the ticket"
            if remaining:
                target["volume_lots"] = str(remaining)
                target["time_msc"] = str(self.now_ns() // 1_000_000)
            else:
                positions.remove(target)
            return identifier
        identifier = self._new_id()
        position = deepcopy(cast(list[JsonObject], _snapshot(self.identity)["positions"])[0])
        position.update({
            "identifier": identifier, "ticket": self._new_id(), "magic": self.identity.magic,
            "side": payload["side"], "volume_lots": str(lots), "price_open": str(self.fill_price),
            "price_current": str(self.fill_price), "profit": "0", "swap": "0",
            "time_msc": str(self.now_ns() // 1_000_000),
        })
        positions.append(position)
        return identifier
