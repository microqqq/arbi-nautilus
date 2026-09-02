from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import pytest

from py000_nautilus.mt5_v1_protocol import (
    ACCOUNT_ID_DOMAIN,
    CAPABILITIES,
    ERROR_CODES,
    JOURNAL_NAMESPACE_DOMAIN,
    MAX_EVENTS_LIMIT,
    MAX_WIRE_BYTES,
    OPS,
    PROTOCOL,
    UINT64_MAX,
    VERSION,
    Binding,
    ExecutionEventsRequest,
    HelloRequest,
    Identity,
    JsonObject,
    RecoveryState,
    Request,
    SnapshotRequest,
    SubmitMarketDeltaRequest,
    WireError,
    closed_object,
    decimal_string,
    decode_json_object,
    decode_pub,
    decode_response_for,
    encode_json,
    exact_int,
    normalized_account_id,
    normalized_journal_namespace,
    request_to_wire,
    uint64_string,
    validate_request,
    validated_bool,
    validated_identifier,
    validated_safe_token,
    validated_text,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "mt5_ea_v1_readonly.json"
EA_ROOT = ROOT / "mt5_ea"
MQL_SOURCES = (
    EA_ROOT / "PY000_Nautilus_MT5.mq5",
    EA_ROOT / "include" / "Py000Execution.mqh",
    EA_ROOT / "include" / "Py000Journal.mqh",
    EA_ROOT / "include" / "Py000Json.mqh",
    EA_ROOT / "include" / "Py000Protocol.mqh",
    EA_ROOT / "include" / "Py000Zmq.mqh",
)


def identity(
    *,
    boot_id: str = "boot-synthetic-001",
    execution_enabled: bool = False,
) -> Identity:
    server = "Synthetic Broker"
    login = "10000001"
    return Identity(
        account_id=normalized_account_id(server, login),
        broker_server=server,
        broker_login=login,
        server_timezone="Europe/Athens",
        server_timezone_source="configured_input",
        symbol="XAUUSD",
        terminal_build=5000,
        ea_build_id="py000-mt5-ea-v1-readonly",
        declared_source_sha256="a" * 64,
        stream_id="stream-synthetic-001",
        boot_id=boot_id,
        magic="900000001",
        execution_enabled=execution_enabled,
    )


def snapshot() -> JsonObject:
    return {
        "account": {
            "balance": "10000.00",
            "broker_login": "10000001",
            "broker_server": "Synthetic Broker",
            "currency": "USD",
            "equity": "10001.25",
            "leverage": 100,
            "margin": "10.00",
            "margin_free": "9991.25",
            "margin_level": "100012.5",
            "trade_allowed": True,
            "trade_expert": True,
        },
        "authority_flags": {
            "account_trade_allowed": True,
            "account_trade_expert": True,
            "market_open_hint": True,
            "mql_trade_allowed": False,
            "symbol_trade_mode": 4,
            "terminal_connected": True,
            "terminal_trade_allowed": True,
        },
        "positions": [],
        "session": {
            "freshness": "fresh",
            "freshness_age_ms": "1000",
            "freshness_source": "mt5_symbol_info_tick_vs_time_trade_server",
            "sample_server_time_ms": "1788282000000",
            "sample_server_time_source": "mt5_time_trade_server_calculated_second_precision",
            "scheduled_open": True,
            "session_open": True,
            "session_schedule_available": True,
            "session_source": "mt5_symbol_info_session_trade_at_time_trade_server",
        },
        "symbol_spec": {
            "contract_size": "100",
            "currency_base": "XAU",
            "currency_margin": "USD",
            "currency_profit": "USD",
            "digits": 2,
            "expiration_mode": 7,
            "filling_mode": 3,
            "freeze_level": 0,
            "margin_initial": "0",
            "margin_maintenance": "0",
            "order_mode": 127,
            "point": "0.01",
            "stops_level": 0,
            "symbol": "XAUUSD",
            "swap_long": "-1.25",
            "swap_mode": 1,
            "swap_short": "0.5",
            "tick_size": "0.01",
            "tick_value": "1",
            "tick_value_loss": "1",
            "tick_value_profit": "1",
            "trade_calc_mode": 1,
            "trade_mode": 4,
            "volume_limit": "0",
            "volume_max": "100",
            "volume_min": "0.01",
            "volume_step": "0.01",
        },
        "time": {
            "observed_utc_ms": "1788271200000",
            "observed_utc_precision_ms": "1000",
            "observed_utc_source": "mt5_time_gmt_second_precision",
            "server_quote_time_ms": "1788282000000",
            "server_quote_time_source": "mt5_time_current_last_known_quote",
            "server_timezone": "Europe/Athens",
        },
    }


def model(*, boot_id: str = "boot-synthetic-001") -> Mt5V1ReferenceModel:
    current = identity(boot_id=boot_id)
    return Mt5V1ReferenceModel(
        identity=current,
        journal=JournalState(
            stream_id=current.stream_id,
            first_retained_cursor="0",
            events=[],
        ),
        observed_utc_ms="1788271200000",
        snapshot=snapshot(),
    )


def wire(request: Request) -> str:
    return encode_json(request_to_wire(request))


def decode_request(raw: str | bytes) -> Request:
    """Test-side request decoder used to replay the shared Python/MQL corpus."""
    data = decode_json_object(raw)
    base = frozenset({"protocol", "version", "request_id", "op"})
    if data.get("protocol") != PROTOCOL or type(data.get("protocol")) is not str:
        raise WireError("SCHEMA_MISMATCH", "protocol mismatch")
    if type(data.get("version")) is not int or data.get("version") != VERSION:
        raise WireError("SCHEMA_MISMATCH", "version must be exact JSON int 1")
    request_id = validated_identifier(data.get("request_id"), "request_id", max_length=64)
    op = validated_identifier(data.get("op"), "op", max_length=64)
    if op not in OPS:
        raise WireError("UNKNOWN_OP", f"unsupported operation {op!r}")
    if op == "hello":
        closed_object(data, "hello request", base)
        return HelloRequest(request_id=request_id)
    binding = Binding.from_wire(data.get("binding"))
    if op == "get_snapshot":
        closed_object(data, "get_snapshot request", base | {"binding"})
        return SnapshotRequest(request_id=request_id, binding=binding)
    if op == "submit_market_delta":
        closed_object(
            data,
            "submit_market_delta request",
            base | {"binding", "client_request_id", "side", "quantity_lots"},
        )
        side = data["side"]
        if side not in {"buy", "sell"} or type(side) is not str:
            raise WireError("SCHEMA_MISMATCH", "side must be buy or sell")
        return SubmitMarketDeltaRequest(
            request_id=request_id,
            binding=binding,
            client_request_id=validated_safe_token(data["client_request_id"], "client_request_id"),
            side=cast(Literal["buy", "sell"], side),
            quantity_lots=decimal_string(data["quantity_lots"], "quantity_lots", positive=True),
        )
    closed_object(data, "events request", base | {"binding", "after_cursor", "limit"})
    return ExecutionEventsRequest(
        request_id=request_id,
        binding=binding,
        after_cursor=uint64_string(data["after_cursor"], "after_cursor"),
        limit=exact_int(data["limit"], "limit", minimum=1, maximum=MAX_EVENTS_LIMIT),
    )


def error_code(response: JsonObject) -> str:
    error = cast(dict[str, object], response["error"])
    return cast(str, error["code"])


def event(
    sequence: int,
    *,
    boot_id: str | None = None,
    stream_id: str = "stream-synthetic-001",
    execution_enabled: bool = False,
) -> JsonObject:
    return {
        "boot_id": boot_id or f"boot-synthetic-{sequence:03d}",
        "event_seq": str(sequence),
        "event_time_ms": str(1788282000000 + sequence),
        "event_type": "stream_started",
        "payload": {
            "ea_build_id": "py000-mt5-ea-v1-readonly",
            "execution_enabled": execution_enabled,
        },
        "stream_id": stream_id,
    }


def submission_event(
    sequence: int,
    event_type: Literal[
        "submission_reserved",
        "order_rejected",
        "order_filled",
        "order_unknown",
    ],
    *,
    boot_id: str = "boot-synthetic-001",
    stream_id: str = "stream-synthetic-001",
    client_request_id: str = "delta-1",
    side: Literal["buy", "sell"] = "buy",
    quantity_lots: str = "0.01",
) -> JsonObject:
    payload: JsonObject = {
        "client_request_id": client_request_id,
        "quantity_lots": quantity_lots,
        "side": side,
    }
    if event_type in {"order_rejected", "order_unknown"}:
        payload.update(
            {
                "broker_retcode": "10030" if event_type == "order_rejected" else "0",
                "reason": "broker_rejected" if event_type == "order_rejected" else "result_unknown",
            }
        )
    elif event_type == "order_filled":
        payload.update(
            {
                "broker_retcode": "10009",
                "commission": "-0.25",
                "fill_price": "2400.25",
                "filled_quantity_lots": quantity_lots,
                "venue_deal_id": "700000002",
                "venue_order_id": "700000001",
                "venue_position_id": "700000003",
            }
        )
    return {
        "boot_id": boot_id,
        "event_seq": str(sequence),
        "event_time_ms": str(1788282000000 + sequence),
        "event_type": event_type,
        "payload": payload,
        "stream_id": stream_id,
    }


def error_response(*, request_id: str, op: str, code: str, message: str) -> JsonObject:
    assert code in ERROR_CODES
    try:
        safe_message = validated_text(message, "error.message")
    except WireError:
        safe_message = "request rejected by v1 wire grammar"
    return {
        "error": {"code": code, "message": safe_message},
        "ok": False,
        "op": validated_identifier(op, "response.op", max_length=64),
        "protocol": PROTOCOL,
        "request_id": validated_identifier(request_id, "response.request_id", max_length=64),
        "version": VERSION,
    }


def success_response(request: Request, data: JsonObject) -> JsonObject:
    response: JsonObject = {
        "data": data,
        "ok": True,
        "op": request.op,
        "protocol": PROTOCOL,
        "request_id": request.request_id,
        "version": VERSION,
    }
    if len(encode_json(response).encode("utf-8")) > MAX_WIRE_BYTES:
        return error_response(
            request_id=request.request_id,
            op=request.op,
            code="SCHEMA_MISMATCH",
            message="response exceeds 64 KiB wire limit",
        )
    try:
        decode_response_for(request, encode_json(response))
    except WireError as exc:
        return error_response(
            request_id=request.request_id,
            op=request.op,
            code=exc.code,
            message=str(exc),
        )
    return response


@dataclass(slots=True)
class JournalState:
    stream_id: str
    first_retained_cursor: str
    events: list[JsonObject]

    def validate(self) -> None:
        validated_safe_token(self.stream_id, "journal.stream_id")
        expected = int(uint64_string(self.first_retained_cursor, "first_retained_cursor")) + 1
        boot_ids: set[str] = set()
        for item in self.events:
            event_data = closed_object(
                item,
                "execution event",
                frozenset(
                    {
                        "event_seq",
                        "event_time_ms",
                        "event_type",
                        "stream_id",
                        "boot_id",
                        "payload",
                    }
                ),
            )
            sequence = int(uint64_string(event_data["event_seq"], "event_seq", positive=True))
            if sequence != expected or event_data["stream_id"] != self.stream_id:
                raise WireError("RECOVERY_BLOCKED", "journal sequence or stream mismatch")
            uint64_string(event_data["event_time_ms"], "event_time_ms", positive=True)
            if event_data["event_type"] != "stream_started":
                raise WireError("RECOVERY_BLOCKED", "journal event_type mismatch")
            boot_id = validated_safe_token(event_data["boot_id"], "event.boot_id")
            if boot_id in boot_ids:
                raise WireError("RECOVERY_BLOCKED", "journal boot_id repeated")
            boot_ids.add(boot_id)
            payload = closed_object(
                event_data["payload"],
                "journal event payload",
                frozenset({"ea_build_id", "execution_enabled"}),
            )
            validated_safe_token(payload["ea_build_id"], "event.payload.ea_build_id")
            if validated_bool(payload["execution_enabled"], "execution_enabled"):
                raise WireError("RECOVERY_BLOCKED", "journal event enables execution")
            expected += 1

    @property
    def last_cursor(self) -> str:
        return (
            cast(str, self.events[-1]["event_seq"]) if self.events else self.first_retained_cursor
        )

    def append_stream_started(self, current: Identity, event_time_ms: str) -> None:
        self.validate()
        if any(item["boot_id"] == current.boot_id for item in self.events):
            raise WireError("RECOVERY_BLOCKED", "journal boot_id repeated")
        sequence = uint64_string(str(int(self.last_cursor) + 1), "event_seq", positive=True)
        self.events.append(
            event(
                int(sequence),
                boot_id=current.boot_id,
                stream_id=current.stream_id,
            )
        )
        self.events[-1]["event_time_ms"] = uint64_string(
            event_time_ms,
            "event_time_ms",
            positive=True,
        )
        self.events[-1]["payload"] = {
            "ea_build_id": current.ea_build_id,
            "execution_enabled": False,
        }
        self.validate()

    def selected(self, after_cursor: str) -> list[JsonObject]:
        self.validate()
        after = int(uint64_string(after_cursor, "after_cursor"))
        first = int(self.first_retained_cursor)
        last = int(self.last_cursor)
        if after > last:
            raise WireError("CURSOR_AHEAD", "after_cursor exceeds last_cursor")
        if after < first:
            raise WireError("CURSOR_EXPIRED", "after_cursor precedes retained stream")
        return [deepcopy(item) for item in self.events if int(cast(str, item["event_seq"])) > after]

    def page(self, after_cursor: str, limit: int) -> JsonObject:
        selected = self.selected(after_cursor)[:limit]
        next_cursor = cast(str, selected[-1]["event_seq"]) if selected else after_cursor
        return {
            "events": selected,
            "first_retained_cursor": self.first_retained_cursor,
            "has_more": int(next_cursor) < int(self.last_cursor),
            "last_cursor": self.last_cursor,
            "next_cursor": next_cursor,
            "stream_id": self.stream_id,
        }


class Mt5V1ReferenceModel:
    """Test-only state machine; production exposes codecs, not simulated venue state."""

    def __init__(
        self,
        *,
        identity: Identity,
        journal: JournalState,
        observed_utc_ms: str,
        snapshot: JsonObject,
        append_boot_event: bool = True,
    ) -> None:
        self.identity = identity
        self.journal = journal
        self.observed_utc_ms = uint64_string(
            observed_utc_ms,
            "observed_utc_ms",
            positive=True,
        )
        self.snapshot = deepcopy(snapshot)
        self.recovery_state: RecoveryState = "ready"
        self._last_tick_time_ms: str | None = None
        try:
            self.journal.validate()
            if self.journal.stream_id != identity.stream_id:
                raise WireError("RECOVERY_BLOCKED", "identity and journal stream differ")
            if append_boot_event:
                self.journal.append_stream_started(identity, self.observed_utc_ms)
        except WireError:
            self.recovery_state = "blocked"

    def restart(self, *, boot_id: str, event_time_ms: str) -> Mt5V1ReferenceModel:
        restarted_identity = Identity(
            account_id=self.identity.account_id,
            broker_server=self.identity.broker_server,
            broker_login=self.identity.broker_login,
            server_timezone=self.identity.server_timezone,
            server_timezone_source=self.identity.server_timezone_source,
            symbol=self.identity.symbol,
            terminal_build=self.identity.terminal_build,
            ea_build_id=self.identity.ea_build_id,
            declared_source_sha256=self.identity.declared_source_sha256,
            stream_id=self.identity.stream_id,
            boot_id=boot_id,
            magic=self.identity.magic,
        )
        return Mt5V1ReferenceModel(
            identity=restarted_identity,
            journal=deepcopy(self.journal),
            observed_utc_ms=event_time_ms,
            snapshot=self.snapshot,
        )

    def _snapshot_data(self) -> JsonObject:
        value = deepcopy(self.snapshot)
        value.update(
            {
                "execution_enabled": False,
                "identity": self.identity.to_wire(),
                "recovery_state": self.recovery_state,
            }
        )
        return value

    def _events_data(self, request: ExecutionEventsRequest) -> JsonObject:
        selected = self.journal.selected(request.after_cursor)
        last = self.journal.last_cursor
        page: list[JsonObject] = []
        for candidate in selected[: request.limit]:
            candidate_page = [*page, candidate]
            next_cursor = cast(str, candidate["event_seq"])
            candidate_data: JsonObject = {
                "events": candidate_page,
                "first_retained_cursor": self.journal.first_retained_cursor,
                "has_more": int(next_cursor) < int(last),
                "identity": self.identity.to_wire(),
                "last_cursor": last,
                "next_cursor": next_cursor,
                "stream_id": self.journal.stream_id,
            }
            raw = {
                "data": candidate_data,
                "ok": True,
                "op": request.op,
                "protocol": PROTOCOL,
                "request_id": request.request_id,
                "version": VERSION,
            }
            if len(encode_json(raw).encode("utf-8")) > MAX_WIRE_BYTES:
                break
            page = candidate_page
        next_cursor = cast(str, page[-1]["event_seq"]) if page else request.after_cursor
        if selected and not page:
            raise WireError("SCHEMA_MISMATCH", "single event exceeds wire budget")
        return {
            "events": page,
            "first_retained_cursor": self.journal.first_retained_cursor,
            "has_more": int(next_cursor) < int(last),
            "identity": self.identity.to_wire(),
            "last_cursor": last,
            "next_cursor": next_cursor,
            "stream_id": self.journal.stream_id,
        }

    def handle(self, raw: str | bytes) -> JsonObject:
        fallback_request_id = "invalid"
        fallback_op = "unknown"
        try:
            envelope = decode_json_object(raw)
            if isinstance(envelope.get("request_id"), str):
                fallback_request_id = cast(str, envelope["request_id"])
            if isinstance(envelope.get("op"), str):
                fallback_op = cast(str, envelope["op"])
            request = decode_request(raw)
            if isinstance(request, HelloRequest):
                return success_response(
                    request,
                    {
                        "capabilities": list(CAPABILITIES),
                        "identity": self.identity.to_wire(),
                        "recovery_state": self.recovery_state,
                    },
                )
            if request.binding != self.identity.binding():
                raise WireError("BINDING_MISMATCH", "request binding does not match current EA")
            if isinstance(request, SnapshotRequest):
                return success_response(request, self._snapshot_data())
            if isinstance(request, SubmitMarketDeltaRequest):
                raise WireError("EXECUTION_DISABLED", "v1 package cannot execute orders")
            if self.recovery_state == "blocked":
                raise WireError("RECOVERY_BLOCKED", "journal recovery is blocked")
            return success_response(request, self._events_data(request))
        except WireError as exc:
            try:
                request_id = validated_identifier(
                    fallback_request_id,
                    "fallback_request_id",
                    max_length=64,
                )
            except WireError:
                request_id = "invalid"
            try:
                op = validated_identifier(fallback_op, "fallback_op", max_length=64)
            except WireError:
                op = "unknown"
            return error_response(request_id=request_id, op=op, code=exc.code, message=str(exc))

    def pub_tick(
        self,
        *,
        event_time_ms: str,
        bid: str,
        ask: str,
        market_open_hint: bool,
    ) -> JsonObject:
        bid_value = decimal_string(bid, "bid", positive=True)
        ask_value = decimal_string(ask, "ask", positive=True)
        if Decimal(bid_value) > Decimal(ask_value):
            raise WireError("SCHEMA_MISMATCH", "bid exceeds ask")
        event_time = uint64_string(event_time_ms, "event_time_ms", positive=True)
        self._last_tick_time_ms = event_time
        return {
            "ask": ask_value,
            "bid": bid_value,
            "event_time_ms": event_time,
            "event_time_source": "mt5_symbol_info_tick",
            "identity": self.identity.to_wire(),
            "market_open_hint": validated_bool(market_open_hint, "market_open_hint"),
            "message_type": "tick",
            "protocol": PROTOCOL,
            "version": VERSION,
        }

    def pub_heartbeat(self, *, event_time_ms: str, market_open_hint: bool) -> JsonObject:
        return {
            "event_time_ms": uint64_string(event_time_ms, "event_time_ms", positive=True),
            "event_time_source": "mt5_time_gmt_second_precision",
            "identity": self.identity.to_wire(),
            "last_tick_time_ms": self._last_tick_time_ms,
            "market_open_hint": validated_bool(market_open_hint, "market_open_hint"),
            "message_type": "heartbeat",
            "protocol": PROTOCOL,
            "version": VERSION,
        }


def test_protocol_has_exact_four_operations() -> None:
    assert {
        "hello",
        "get_snapshot",
        "submit_market_delta",
        "get_execution_events",
    } == OPS
    assert tuple(sorted(OPS)) == CAPABILITIES


@pytest.mark.parametrize(
    "raw,code",
    [
        ('{"protocol":"py000.mt5","protocol":"py000.mt5"}', "MALFORMED"),
        (
            '{"protocol":"py000.mt5","version":1,"request_id":"r","op":"hello","x":0}',
            "SCHEMA_MISMATCH",
        ),
        (
            '{"protocol":"py000.mt5","version":true,"request_id":"r","op":"hello"}',
            "SCHEMA_MISMATCH",
        ),
        ('{"protocol":"py000.mt5","version":1.0,"request_id":"r","op":"hello"}', "SCHEMA_MISMATCH"),
        (
            '{"protocol":"py000.mt5","version":1,"request_id":"bad\\u0000id","op":"hello"}',
            "SCHEMA_MISMATCH",
        ),
        ('{"protocol":"py000.mt5","version":NaN,"request_id":"r","op":"hello"}', "MALFORMED"),
    ],
)
def test_closed_envelope_rejects_invalid_wire(raw: str, code: str) -> None:
    with pytest.raises(WireError) as caught:
        decode_request(raw)
    assert caught.value.code == code


def test_json_decoder_rejects_non_object_invalid_utf8_and_size() -> None:
    with pytest.raises(WireError, match="root"):
        decode_json_object("[]")
    with pytest.raises(WireError, match="UTF-8"):
        decode_json_object(b"{\xff}")
    with pytest.raises(WireError, match="64 KiB"):
        decode_json_object(b"{" + b" " * (64 * 1024) + b"}")


@pytest.mark.parametrize(
    "raw",
    [
        '{"value":' + "9" * 5000 + "}",
        '{"value":' + "[" * 40 + "0" + "]" * 40 + "}",
        '{"value":"\\ud800"}',
        '{"value":"\ud800"}',
    ],
)
def test_json_adversarial_failures_are_normalized_to_malformed(raw: str) -> None:
    with pytest.raises(WireError) as caught:
        decode_json_object(raw)
    assert caught.value.code == "MALFORMED"


@pytest.mark.parametrize(
    "value,valid",
    [
        ("0", True),
        (str(UINT64_MAX), True),
        ("00", False),
        ("01", False),
        ("-1", False),
        (str(UINT64_MAX + 1), False),
        (1, False),
    ],
)
def test_uint64_string_boundaries(value: object, valid: bool) -> None:
    if valid:
        assert uint64_string(value, "value") == value
    else:
        with pytest.raises(WireError):
            uint64_string(value, "value")


@pytest.mark.parametrize(
    "value,valid",
    [
        ("0.01", True),
        ("1", True),
        ("1.000", True),
        ("0", False),
        ("-0.01", False),
        ("01", False),
        ("1e-2", False),
        (0.01, False),
    ],
)
def test_positive_decimal_string_boundaries(value: object, valid: bool) -> None:
    if valid:
        assert decimal_string(value, "quantity", positive=True) == value
    else:
        with pytest.raises(WireError):
            decimal_string(value, "quantity", positive=True)


def test_round_trip_all_four_request_shapes() -> None:
    current = identity()
    requests: list[Request] = [
        HelloRequest(request_id="r-hello"),
        SnapshotRequest(request_id="r-snapshot", binding=current.binding()),
        SubmitMarketDeltaRequest(
            request_id="r-submit",
            binding=current.binding(),
            client_request_id="delta-1",
            side="sell",
            quantity_lots="0.01",
        ),
        ExecutionEventsRequest(
            request_id="r-events",
            binding=current.binding(),
            after_cursor="0",
            limit=MAX_EVENTS_LIMIT,
        ),
    ]
    for request in requests:
        assert validate_request(request) is request
        assert decode_request(wire(request)) == request


def test_constructed_request_validation_normalizes_adversarial_types() -> None:
    valid_binding = identity().binding()
    invalid_requests: list[object] = [
        object(),
        HelloRequest(request_id=""),
        HelloRequest(request_id="bad\nid"),
        HelloRequest(request_id="r", op=cast(Literal["hello"], "ping")),
        SnapshotRequest(request_id="r", binding=cast(Binding, object())),
        SnapshotRequest(
            request_id="r",
            binding=Binding("", "XAUUSD", "ea", "stream", "boot"),
        ),
        SubmitMarketDeltaRequest("r", valid_binding, "", "buy", "0.01"),
        SubmitMarketDeltaRequest("r", valid_binding, "delta|1", "buy", "0.01"),
        SubmitMarketDeltaRequest(
            "r", valid_binding, "delta", cast(Literal["buy", "sell"], "hold"), "0.01"
        ),
        SubmitMarketDeltaRequest("r", valid_binding, "delta", "buy", "0"),
        ExecutionEventsRequest("r", valid_binding, "01", 1),
        ExecutionEventsRequest("r", valid_binding, "0", cast(int, True)),
        ExecutionEventsRequest("r", valid_binding, "0", MAX_EVENTS_LIMIT + 1),
    ]
    for invalid in invalid_requests:
        request = cast(Request, invalid)
        with pytest.raises(WireError) as serialized:
            request_to_wire(request)
        assert serialized.value.code == "SCHEMA_MISMATCH"
        with pytest.raises(WireError) as decoded:
            decode_response_for(request, "{}")
        assert decoded.value.code == "SCHEMA_MISMATCH"


def test_identity_rejects_placeholder_source_hash() -> None:
    wire_identity = identity().to_wire()
    wire_identity["declared_source_sha256"] = "0" * 64
    with pytest.raises(WireError) as caught:
        Identity.from_wire(wire_identity)
    assert caught.value.code == "SCHEMA_MISMATCH"


def test_identity_execution_enabled_round_trips_and_snapshot_must_agree() -> None:
    readonly = identity()
    enabled = identity(execution_enabled=True)
    assert Identity.from_wire(readonly.to_wire()).execution_enabled is False
    assert Identity.from_wire(enabled.to_wire()) == enabled
    assert enabled.to_wire()["execution_enabled"] is True

    request = SnapshotRequest(request_id="snapshot-enabled", binding=enabled.binding())
    data = snapshot()
    data.update(
        {
            "execution_enabled": True,
            "identity": enabled.to_wire(),
            "recovery_state": "ready",
        }
    )
    response = success_response(request, data)
    assert decode_response_for(request, encode_json(response)) == response

    mismatch = deepcopy(response)
    cast(JsonObject, mismatch["data"])["execution_enabled"] = False
    with pytest.raises(WireError, match="differs from identity") as caught:
        decode_response_for(request, encode_json(mismatch))
    assert caught.value.code == "SCHEMA_MISMATCH"


def test_idempotency_conflict_is_a_closed_wire_error_code() -> None:
    assert "IDEMPOTENCY_CONFLICT" in ERROR_CODES
    response = error_response(
        request_id="submit-conflict",
        op="submit_market_delta",
        code="IDEMPOTENCY_CONFLICT",
        message="client request id already binds another payload",
    )
    request = SubmitMarketDeltaRequest(
        request_id="submit-conflict",
        binding=identity().binding(),
        client_request_id="delta-1",
        side="buy",
        quantity_lots="0.01",
    )
    assert decode_response_for(request, encode_json(response)) == response


def test_account_id_fixed_vectors_and_collision_counterexamples() -> None:
    fixture = cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))
    vectors = cast(list[dict[str, str]], fixture["identity_vectors"])
    derived: dict[str, str] = {}
    for vector in vectors:
        server_bytes = vector["broker_server"].encode("utf-8")
        canonical = (
            ACCOUNT_ID_DOMAIN
            + len(server_bytes).to_bytes(4, "big")
            + server_bytes
            + b"\x00"
            + vector["broker_login"].encode("ascii")
        )
        assert canonical.hex() == vector["canonical_hex"]
        account_id = normalized_account_id(vector["broker_server"], vector["broker_login"])
        assert account_id == vector["account_id"]
        derived[vector["broker_server"]] = account_id
    assert derived["Broker-Demo"] != derived["Broker Demo"]
    assert derived["Broker \u03b1"] != derived["Broker \u03b2"]


def test_journal_namespace_fixed_vectors_preserve_raw_symbol_identity() -> None:
    fixture = cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))
    vectors = cast(list[dict[str, str]], fixture["journal_namespace_vectors"])
    namespaces: set[str] = set()
    for vector in vectors:
        canonical = (
            JOURNAL_NAMESPACE_DOMAIN
            + vector["account_id"].encode("ascii")
            + b"\x00"
            + vector["symbol"].encode("utf-8")
            + b"\x00"
            + vector["magic"].encode("ascii")
        )
        assert canonical.hex() == vector["canonical_hex"]
        namespace = normalized_journal_namespace(
            vector["account_id"], vector["symbol"], vector["magic"]
        )
        assert namespace == vector["namespace"]
        namespaces.add(namespace)
    assert len(namespaces) == len(vectors) == 4


def test_shared_request_corpus_has_python_and_mql_error_contract() -> None:
    fixture = cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))
    corpus = cast(list[dict[str, str]], fixture["request_corpus"])
    for case in corpus:
        raw = case.get("raw")
        if case.get("recipe") == "integer_5000_digits":
            raw = (
                '{"protocol":"py000.mt5","version":'
                + "9" * 5000
                + ',"request_id":"r","op":"hello"}'
            )
        elif case.get("recipe") == "array_depth_40":
            raw = (
                '{"protocol":"py000.mt5","version":1,"request_id":"r","op":"hello","x":'
                + "[" * 40
                + "0"
                + "]" * 40
                + "}"
            )
        assert raw is not None
        if case["expected"] == "OK":
            assert isinstance(decode_request(raw), HelloRequest)
        else:
            with pytest.raises(WireError) as caught:
                decode_request(raw)
            assert caught.value.code == case["expected"], case["name"]

    mql = (EA_ROOT / "include" / "Py000Json.mqh").read_text(encoding="utf-8")
    assert "Py000JsonPreflight" in mql
    assert "JSON nesting exceeds v1 limit" in mql
    assert "JSON integer exceeds digit limit" in mql
    assert "SCHEMA: exact JSON integer required" in mql
    assert "SCHEMA: binding must be an object" in mql


@pytest.mark.parametrize(
    "field",
    ["account_id", "symbol", "ea_build_id", "stream_id", "boot_id"],
)
def test_each_binding_field_mismatch_fails_closed(field: str) -> None:
    current = model()
    changed = current.identity.binding().to_wire()
    changed[field] = f"wrong-{field}"
    request = {
        "binding": changed,
        "op": "get_snapshot",
        "protocol": "py000.mt5",
        "request_id": f"mismatch-{field}",
        "version": 1,
    }
    response = current.handle(encode_json(request))
    assert response["ok"] is False
    assert error_code(response) == "BINDING_MISMATCH"
    message = cast(str, cast(dict[str, object], response["error"])["message"])
    assert field not in message


def test_snapshot_is_same_turn_reference_state_with_identity() -> None:
    current = model()
    request = SnapshotRequest(request_id="snapshot-1", binding=current.identity.binding())
    response = current.handle(wire(request))
    data = cast(dict[str, object], response["data"])
    assert response["ok"] is True
    assert cast(dict[str, object], data["time"])["observed_utc_ms"] == "1788271200000"
    assert data["identity"] == current.identity.to_wire()
    assert data["execution_enabled"] is False
    assert data["positions"] == []


def test_event_paging_is_exclusive_empty_single_and_multi_page() -> None:
    current = identity()
    journal = JournalState(
        stream_id=current.stream_id,
        first_retained_cursor="0",
        events=[event(1), event(2), event(3)],
    )
    empty = journal.page("3", 10)
    assert empty == {
        "events": [],
        "first_retained_cursor": "0",
        "has_more": False,
        "last_cursor": "3",
        "next_cursor": "3",
        "stream_id": current.stream_id,
    }
    single = journal.page("1", 1)
    assert [item["event_seq"] for item in cast(list[JsonObject], single["events"])] == ["2"]
    assert single["next_cursor"] == "2"
    assert single["has_more"] is True
    final = journal.page("2", 10)
    assert [item["event_seq"] for item in cast(list[JsonObject], final["events"])] == ["3"]
    assert final["next_cursor"] == "3"
    assert final["has_more"] is False


def test_cursor_ahead_expired_and_gap_are_distinct() -> None:
    journal = JournalState(
        stream_id="stream-synthetic-001",
        first_retained_cursor="2",
        events=[event(3)],
    )
    with pytest.raises(WireError) as ahead:
        journal.page("4", 1)
    assert ahead.value.code == "CURSOR_AHEAD"
    with pytest.raises(WireError) as expired:
        journal.page("1", 1)
    assert expired.value.code == "CURSOR_EXPIRED"
    journal.events.append(event(5))
    with pytest.raises(WireError) as gap:
        journal.validate()
    assert gap.value.code == "RECOVERY_BLOCKED"


def test_restart_preserves_stream_changes_boot_and_appends_stream_started() -> None:
    first = model()
    restarted = first.restart(
        boot_id="boot-synthetic-002",
        event_time_ms="1788282001000",
    )
    assert restarted.identity.stream_id == first.identity.stream_id
    assert restarted.identity.boot_id != first.identity.boot_id
    assert [entry["event_seq"] for entry in restarted.journal.events] == ["1", "2"]
    assert [entry["boot_id"] for entry in restarted.journal.events] == [
        "boot-synthetic-001",
        "boot-synthetic-002",
    ]
    assert all(entry["event_type"] == "stream_started" for entry in restarted.journal.events)

    duplicate = first.restart(
        boot_id="boot-synthetic-001",
        event_time_ms="1788282002000",
    )
    assert duplicate.recovery_state == "blocked"
    assert [entry["event_seq"] for entry in duplicate.journal.events] == ["1"]


def test_limit_500_pages_by_wire_budget_without_nonprogress_error() -> None:
    current_identity = identity()
    journal = JournalState(
        stream_id=current_identity.stream_id,
        first_retained_cursor="0",
        events=[event(index) for index in range(1, 501)],
    )
    current = Mt5V1ReferenceModel(
        identity=current_identity,
        journal=journal,
        observed_utc_ms="1788271200000",
        snapshot=snapshot(),
        append_boot_event=False,
    )
    request = ExecutionEventsRequest(
        request_id="budget-500",
        binding=current_identity.binding(),
        after_cursor="0",
        limit=500,
    )
    response = current.handle(wire(request))
    data = cast(dict[str, object], response["data"])
    returned = cast(list[JsonObject], data["events"])
    assert response["ok"] is True
    assert 0 < len(returned) < 500
    assert data["has_more"] is True
    assert int(cast(str, data["next_cursor"])) > 0
    assert len(encode_json(response).encode("utf-8")) <= MAX_WIRE_BYTES
    assert decode_response_for(request, encode_json(response)) == response


def test_corrupt_journal_keeps_hello_and_snapshot_but_blocks_events() -> None:
    current_identity = identity()
    corrupt = JournalState(
        stream_id=current_identity.stream_id,
        first_retained_cursor="0",
        events=[event(2)],
    )
    current = Mt5V1ReferenceModel(
        identity=current_identity,
        journal=corrupt,
        observed_utc_ms="1788271200000",
        snapshot=snapshot(),
    )
    hello = current.handle(wire(HelloRequest(request_id="blocked-hello")))
    hello_data = cast(dict[str, object], hello["data"])
    assert hello_data["recovery_state"] == "blocked"
    snap = current.handle(
        wire(SnapshotRequest(request_id="blocked-snapshot", binding=current_identity.binding()))
    )
    assert cast(dict[str, object], snap["data"])["recovery_state"] == "blocked"
    events = current.handle(
        wire(
            ExecutionEventsRequest(
                request_id="blocked-events",
                binding=current_identity.binding(),
                after_cursor="0",
                limit=10,
            )
        )
    )
    assert error_code(events) == "RECOVERY_BLOCKED"


def test_submit_is_deterministically_disabled() -> None:
    current = model()
    request = SubmitMarketDeltaRequest(
        request_id="submit-1",
        binding=current.identity.binding(),
        client_request_id="delta-1",
        side="buy",
        quantity_lots="0.01",
    )
    first = current.handle(wire(request))
    second = current.handle(wire(request))
    assert first == second
    assert error_code(first) == "EXECUTION_DISABLED"
    assert current.journal.last_cursor == "1"


@pytest.mark.parametrize("event_type", ["order_rejected", "order_filled", "order_unknown"])
def test_submit_accepts_each_strict_terminal_outcome(
    event_type: Literal["order_rejected", "order_filled", "order_unknown"],
) -> None:
    current = identity(execution_enabled=True)
    request = SubmitMarketDeltaRequest(
        request_id=f"submit-{event_type}",
        binding=current.binding(),
        client_request_id="delta-1",
        side="buy",
        quantity_lots="0.01",
    )
    outcome = submission_event(2, event_type)
    response = success_response(
        request,
        {"identity": current.to_wire(), "outcome": outcome},
    )
    assert response["ok"] is True
    assert decode_response_for(request, encode_json(response)) == response


def test_submit_accepts_idempotent_terminal_outcome_from_an_older_boot() -> None:
    current = identity(execution_enabled=True)
    request = SubmitMarketDeltaRequest(
        request_id="submit-replay-after-restart",
        binding=current.binding(),
        client_request_id="delta-1",
        side="buy",
        quantity_lots="0.01",
    )
    outcome = submission_event(2, "order_filled", boot_id="boot-prior-001")
    response = success_response(
        request,
        {"identity": current.to_wire(), "outcome": outcome},
    )

    assert decode_response_for(request, encode_json(response)) == response


@pytest.mark.parametrize(
    ("event_type", "field", "invalid"),
    [
        ("order_rejected", "reason", "not safe"),
        ("order_unknown", "broker_retcode", "01"),
        ("order_filled", "venue_order_id", "0"),
        ("order_filled", "commission", "1e-2"),
        ("order_filled", "unexpected", "closed"),
    ],
)
def test_submit_terminal_payloads_are_closed_and_canonical(
    event_type: Literal["order_rejected", "order_filled", "order_unknown"],
    field: str,
    invalid: str,
) -> None:
    current = identity(execution_enabled=True)
    request = SubmitMarketDeltaRequest(
        request_id=f"invalid-{event_type}-{field}",
        binding=current.binding(),
        client_request_id="delta-1",
        side="buy",
        quantity_lots="0.01",
    )
    outcome = submission_event(2, event_type)
    cast(JsonObject, outcome["payload"])[field] = invalid
    response: JsonObject = {
        "data": {"identity": current.to_wire(), "outcome": outcome},
        "ok": True,
        "op": request.op,
        "protocol": PROTOCOL,
        "request_id": request.request_id,
        "version": VERSION,
    }
    with pytest.raises(WireError) as caught:
        decode_response_for(request, encode_json(response))
    assert caught.value.code == "SCHEMA_MISMATCH"


def test_submit_rejects_nonterminal_reserved_outcome() -> None:
    current = identity(execution_enabled=True)
    request = SubmitMarketDeltaRequest(
        request_id="submit-reserved",
        binding=current.binding(),
        client_request_id="delta-1",
        side="buy",
        quantity_lots="0.01",
    )
    response: JsonObject = {
        "data": {
            "identity": current.to_wire(),
            "outcome": submission_event(2, "submission_reserved"),
        },
        "ok": True,
        "op": request.op,
        "protocol": PROTOCOL,
        "request_id": request.request_id,
        "version": VERSION,
    }
    with pytest.raises(WireError, match="terminal execution event") as caught:
        decode_response_for(request, encode_json(response))
    assert caught.value.code == "SCHEMA_MISMATCH"


@pytest.mark.parametrize(
    ("field", "mismatched"),
    [
        ("client_request_id", "delta-other"),
        ("side", "sell"),
        ("quantity_lots", "0.02"),
    ],
)
def test_submit_outcome_is_bound_to_exact_request(field: str, mismatched: str) -> None:
    current = identity(execution_enabled=True)
    request = SubmitMarketDeltaRequest(
        request_id=f"submit-mismatch-{field}",
        binding=current.binding(),
        client_request_id="delta-1",
        side="buy",
        quantity_lots="0.01",
    )
    outcome = submission_event(2, "order_filled")
    cast(JsonObject, outcome["payload"])[field] = mismatched
    response: JsonObject = {
        "data": {"identity": current.to_wire(), "outcome": outcome},
        "ok": True,
        "op": request.op,
        "protocol": PROTOCOL,
        "request_id": request.request_id,
        "version": VERSION,
    }
    with pytest.raises(WireError) as caught:
        decode_response_for(request, encode_json(response))
    assert caught.value.code == "BINDING_MISMATCH"


def test_execution_page_allows_same_boot_for_non_start_events() -> None:
    current = identity(execution_enabled=True)
    request = ExecutionEventsRequest(
        request_id="events-shared-boot",
        binding=current.binding(),
        after_cursor="0",
        limit=3,
    )
    response = success_response(
        request,
        {
            "events": [
                event(1, boot_id=current.boot_id, execution_enabled=True),
                submission_event(2, "submission_reserved", boot_id=current.boot_id),
                submission_event(3, "order_filled", boot_id=current.boot_id),
            ],
            "first_retained_cursor": "0",
            "has_more": False,
            "identity": current.to_wire(),
            "last_cursor": "3",
            "next_cursor": "3",
            "stream_id": current.stream_id,
        },
    )
    assert response["ok"] is True
    assert decode_response_for(request, encode_json(response)) == response


@pytest.mark.parametrize(
    "legacy_op",
    [
        "account",
        "adjust_net_position",
        "cancel_pending_order",
        "get_attempt_results_since",
        "get_trades_since",
        "is_open",
        "modify_pending_order",
        "order_by_id",
        "pending_orders",
        "ping",
        "place_pending_order",
        "positions",
        "reduce_position_only",
        "spec",
    ],
)
def test_authenticated_legacy_operation_names_are_unknown(legacy_op: str) -> None:
    request = encode_json(
        {
            "op": legacy_op,
            "protocol": "py000.mt5",
            "request_id": f"legacy-{legacy_op}",
            "version": 1,
        }
    )
    response = model().handle(request)
    assert error_code(response) == "UNKNOWN_OP"


def test_tick_and_heartbeat_are_distinct_hints_with_identical_identity() -> None:
    current = model()
    tick = current.pub_tick(
        event_time_ms="1788282000100",
        bid="2400.10",
        ask="2400.20",
        market_open_hint=True,
    )
    heartbeat = current.pub_heartbeat(
        event_time_ms="1788282001100",
        market_open_hint=False,
    )
    later = current.pub_heartbeat(
        event_time_ms="1788282002100",
        market_open_hint=True,
    )
    assert tick["message_type"] == "tick"
    assert heartbeat["message_type"] == "heartbeat"
    assert tick["identity"] == heartbeat["identity"]
    assert heartbeat["last_tick_time_ms"] == "1788282000100"
    assert later["last_tick_time_ms"] == "1788282000100"
    assert "bid" not in heartbeat and "ask" not in heartbeat


def test_response_envelope_is_closed() -> None:
    hello_request = HelloRequest(request_id="hello-response")
    valid = model().handle(wire(hello_request))
    assert decode_response_for(hello_request, encode_json(valid)) == valid
    unknown = deepcopy(valid)
    unknown["extra"] = True
    with pytest.raises(WireError) as caught:
        decode_response_for(hello_request, encode_json(unknown))
    assert caught.value.code == "SCHEMA_MISMATCH"

    events = model()
    events_request = ExecutionEventsRequest(
        request_id="events-response",
        binding=events.identity.binding(),
        after_cursor="0",
        limit=10,
    )
    events_response = events.handle(wire(events_request))
    assert decode_response_for(events_request, encode_json(events_response)) == events_response
    broken = deepcopy(events_response)
    broken_data = cast(dict[str, object], broken["data"])
    broken_events = cast(list[dict[str, object]], broken_data["events"])
    broken_events[0]["event_seq"] = "2"
    with pytest.raises(WireError) as event_error:
        decode_response_for(events_request, encode_json(broken))
    assert event_error.value.code == "SCHEMA_MISMATCH"


def test_response_decoder_binds_envelope_identity_limit_and_progress() -> None:
    current = model()
    request = ExecutionEventsRequest(
        request_id="bound-events",
        binding=current.identity.binding(),
        after_cursor="0",
        limit=1,
    )
    valid = current.handle(wire(request))
    assert decode_response_for(request, encode_json(valid)) == valid

    wrong_envelope = deepcopy(valid)
    wrong_envelope["request_id"] = "other-request"
    with pytest.raises(WireError) as envelope_error:
        decode_response_for(request, encode_json(wrong_envelope))
    assert envelope_error.value.code == "BINDING_MISMATCH"

    wrong_identity = deepcopy(valid)
    wrong_data = cast(dict[str, object], wrong_identity["data"])
    identity_wire = cast(dict[str, object], wrong_data["identity"])
    identity_wire["boot_id"] = "boot-other"
    with pytest.raises(WireError) as identity_error:
        decode_response_for(request, encode_json(wrong_identity))
    assert identity_error.value.code == "BINDING_MISMATCH"

    too_many = deepcopy(valid)
    too_many_data = cast(dict[str, object], too_many["data"])
    first_event = deepcopy(cast(list[JsonObject], too_many_data["events"])[0])
    second_event = deepcopy(first_event)
    second_event["event_seq"] = "2"
    second_event["boot_id"] = "boot-synthetic-002"
    too_many_data["events"] = [first_event, second_event]
    too_many_data["next_cursor"] = "2"
    too_many_data["last_cursor"] = "2"
    with pytest.raises(WireError, match="requested limit"):
        decode_response_for(request, encode_json(too_many))

    nonprogress = deepcopy(valid)
    nonprogress_data = cast(dict[str, object], nonprogress["data"])
    nonprogress_data["events"] = []
    nonprogress_data["has_more"] = True
    nonprogress_data["next_cursor"] = "0"
    nonprogress_data["last_cursor"] = "1"
    with pytest.raises(WireError, match="empty event page"):
        decode_response_for(request, encode_json(nonprogress))

    skipped = deepcopy(valid)
    skipped_data = cast(dict[str, object], skipped["data"])
    skipped_event = cast(list[dict[str, object]], skipped_data["events"])[0]
    skipped_event["event_seq"] = "2"
    skipped_data["next_cursor"] = "2"
    skipped_data["last_cursor"] = "2"
    with pytest.raises(WireError, match="after_cursor"):
        decode_response_for(request, encode_json(skipped))


def test_response_decoder_rejects_expired_context_and_duplicate_page_boot() -> None:
    current = model()
    request = ExecutionEventsRequest(
        request_id="retained-context",
        binding=current.identity.binding(),
        after_cursor="0",
        limit=2,
    )
    valid = current.handle(wire(request))
    expired = deepcopy(valid)
    expired_data = cast(JsonObject, expired["data"])
    expired_data["first_retained_cursor"] = "1"
    with pytest.raises(WireError, match="after_cursor"):
        decode_response_for(request, encode_json(expired))

    duplicate = deepcopy(valid)
    duplicate_data = cast(JsonObject, duplicate["data"])
    first_event = deepcopy(cast(list[JsonObject], duplicate_data["events"])[0])
    second_event = deepcopy(first_event)
    second_event["event_seq"] = "2"
    duplicate_data["events"] = [first_event, second_event]
    duplicate_data["last_cursor"] = "2"
    duplicate_data["next_cursor"] = "2"
    duplicate_data["has_more"] = False
    with pytest.raises(WireError, match="boot_id"):
        decode_response_for(request, encode_json(duplicate))


def test_snapshot_session_open_requires_schedule_connection_and_fresh_tick() -> None:
    current = model()
    request = SnapshotRequest(request_id="session-facts", binding=current.identity.binding())
    valid = current.handle(wire(request))

    stale = deepcopy(valid)
    stale_session = cast(JsonObject, cast(JsonObject, stale["data"])["session"])
    stale_session["freshness"] = "stale"
    with pytest.raises(WireError, match="overstates"):
        decode_response_for(request, encode_json(stale))

    disconnected = deepcopy(valid)
    disconnected_flags = cast(
        JsonObject, cast(JsonObject, disconnected["data"])["authority_flags"]
    )
    disconnected_flags["terminal_connected"] = False
    with pytest.raises(WireError, match="overstates"):
        decode_response_for(request, encode_json(disconnected))

    unknown = deepcopy(valid)
    unknown_session = cast(JsonObject, cast(JsonObject, unknown["data"])["session"])
    unknown_session.update(
        {
            "freshness": "unknown",
            "freshness_age_ms": None,
            "sample_server_time_ms": None,
            "scheduled_open": False,
            "session_open": False,
            "session_schedule_available": False,
        }
    )
    assert decode_response_for(request, encode_json(unknown)) == unknown


def test_snapshot_and_pub_nested_objects_are_closed() -> None:
    current = model()
    snapshot_request = SnapshotRequest(
        request_id="nested-snapshot",
        binding=current.identity.binding(),
    )
    snapshot_response = current.handle(wire(snapshot_request))
    assert (
        decode_response_for(snapshot_request, encode_json(snapshot_response)) == snapshot_response
    )
    changed = deepcopy(snapshot_response)
    changed_data = cast(dict[str, object], changed["data"])
    changed_account = cast(dict[str, object], changed_data["account"])
    changed_account["unknown"] = "rejected"
    with pytest.raises(WireError) as snapshot_error:
        decode_response_for(snapshot_request, encode_json(changed))
    assert snapshot_error.value.code == "SCHEMA_MISMATCH"

    tick = current.pub_tick(
        event_time_ms="1788282000100",
        bid="2400.10",
        ask="2400.20",
        market_open_hint=True,
    )
    heartbeat = current.pub_heartbeat(
        event_time_ms="1788282001100",
        market_open_hint=True,
    )
    assert decode_pub(encode_json(tick)) == tick
    assert decode_pub(encode_json(heartbeat)) == heartbeat
    heartbeat["bid"] = "2400.10"
    with pytest.raises(WireError) as pub_error:
        decode_pub(encode_json(heartbeat))
    assert pub_error.value.code == "SCHEMA_MISMATCH"


def test_adversarial_schema_error_is_still_a_bounded_wire_response() -> None:
    request: JsonObject = {
        "op": "hello",
        "protocol": "py000.mt5",
        "request_id": "many-unknown-fields",
        "version": 1,
    }
    request.update({f"unknown_{index}": index for index in range(200)})
    response = model().handle(encode_json(request))
    assert response["ok"] is False
    assert error_code(response) == "SCHEMA_MISMATCH"
    assert len(cast(str, cast(dict[str, object], response["error"])["message"])) <= 256


def test_oversized_success_is_replaced_by_bounded_error() -> None:
    current_identity = identity()
    oversized_snapshot = snapshot()
    oversized_snapshot["oversized_synthetic_field"] = "x" * (64 * 1024)
    current = Mt5V1ReferenceModel(
        identity=current_identity,
        journal=JournalState(
            stream_id=current_identity.stream_id,
            first_retained_cursor="0",
            events=[],
        ),
        observed_utc_ms="1788271200000",
        snapshot=oversized_snapshot,
    )
    response = current.handle(
        wire(SnapshotRequest(request_id="oversized", binding=current_identity.binding()))
    )
    assert error_code(response) == "SCHEMA_MISMATCH"
    assert len(encode_json(response).encode("utf-8")) <= 64 * 1024


def test_fixture_is_synthetic_sanitized_and_network_free() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    value = cast(dict[str, object], json.loads(raw))
    provenance = cast(dict[str, object], value["_provenance"])
    assert provenance == {
        "source": "synthetic-only",
        "live_connected": False,
        "credentials_present": False,
        "redactions": {
            "account": "SYNTHETIC_ACCOUNT",
            "balances": "SYNTHETIC_MONEY",
            "tickets": "SYNTHETIC_TICKETS",
        },
    }
    assert "tcp://" not in raw and "ipc://" not in raw
    assert "password" not in raw.lower() and "secret" not in raw.lower()
    assert "Synthetic" in raw or "synthetic" in raw

    interactions = cast(list[dict[str, object]], value["interactions"])
    for interaction in interactions:
        request = cast(JsonObject, interaction["request"])
        response = cast(JsonObject, interaction["response"])
        parsed_request = decode_request(encode_json(request))
        assert decode_response_for(parsed_request, encode_json(response)) == response

    fixture_identity = cast(JsonObject, value["identity"])
    snapshot_data = deepcopy(cast(JsonObject, value["snapshot"]))
    snapshot_data.update(
        {
            "execution_enabled": False,
            "identity": fixture_identity,
            "recovery_state": "ready",
        }
    )
    snapshot_response: JsonObject = {
        "data": snapshot_data,
        "ok": True,
        "op": "get_snapshot",
        "protocol": "py000.mt5",
        "request_id": "fixture-snapshot-1",
        "version": 1,
    }
    fixture_identity_value = Identity.from_wire(fixture_identity)
    fixture_request = SnapshotRequest(
        request_id="fixture-snapshot-1",
        binding=fixture_identity_value.binding(),
    )
    assert decode_response_for(fixture_request, encode_json(snapshot_response)) == snapshot_response

    pub_fragments = cast(list[JsonObject], value["pub"])
    for fragment in pub_fragments:
        message = deepcopy(fragment)
        message.update(
            {
                "identity": fixture_identity,
                "protocol": "py000.mt5",
                "version": 1,
            }
        )
        assert decode_pub(encode_json(message)) == message


def test_ea_execution_surface_is_demo_only_and_bounded() -> None:
    source = "\n".join(file.read_text(encoding="utf-8") for file in MQL_SOURCES)
    forbidden = (
        "C" + "Trade",
        "." + "Buy",
        "." + "Sell",
        "Position" + "Close",
        "StringTo" + "Double",
        "tcp://127.0.0.1:" + "6001",
        "tcp://127.0.0.1:" + "6002",
    )
    assert all(token not in source for token in forbidden)
    assert source.count("OrderSend(") == 1
    assert "OrderCheck(" in source
    assert "PY000_EXECUTION_DISABLED = 0" in source
    assert "PY000_EXECUTION_DEMO = 1" in source
    assert "ACCOUNT_TRADE_MODE_DEMO" in source
    assert "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING" in source
    assert "ORDER_FILLING_FOK" in source
    assert "submission_reserved" in source
    assert source.index("Py000JournalAppendReserved(") < source.index("OrderSend(")
    execution_source = (EA_ROOT / "include" / "Py000Execution.mqh").read_text(encoding="utf-8")
    journal_guard = execution_source.index("if(!g_py000_journal_ready)")
    replay_lookup = execution_source.index("Py000JournalReservedIndex(request.client_request_id)")
    assert journal_guard < replay_lookup
    assert "PY000_MT5_V1_" in source
    assert "EXECUTION_DISABLED" in source
    assert "py000.mt5/account-id/v1" in source
    assert "py000.mt5/journal-namespace/v1" in source
    assert "Py000NamespaceToken" not in source
    assert "CryptEncode(CRYPT_HASH_SHA256" in source
    assert "PY000_ZMQ_RCVMORE" in source
    assert "PY000_ZMQ_MAX_MULTIPART_FRAMES" in source
    assert "received == 0" in source
    assert "PY000_ZMQ_DONTWAIT" in source
    assert "ExpertRemove()" in source
    assert "SymbolInfoSessionTrade" in source
    assert "TimeTradeServer()" in source
    assert source.count("TimeCurrent()") == 1
    assert "mt5_symbol_info_tick_vs_time_trade_server" in source
    assert "mt5_time_current_last_known_quote" in source
    assert "ea_source_sha256" not in source


def test_ea_volume_validation_covers_decimal_accumulation_error() -> None:
    def accumulate(source: str) -> tuple[float, int]:
        value = 0.0
        fraction = 0.1
        after_decimal = False
        fractional_digits = 0
        for character in source:
            if character == ".":
                after_decimal = True
                continue
            digit = ord(character) - ord("0")
            if after_decimal:
                fractional_digits += 1
                value += digit * fraction
                fraction *= 0.1
            else:
                value = value * 10.0 + digit
        return value, fractional_digits

    accumulated, digits = accumulate("0.01")
    assert accumulated > 0.01
    assert round(accumulated, digits) == 0.01
    for expected in (
        "1",
        "0.1",
        "0.01",
        "0.001",
        "0.0001",
        "0.00001",
        "0.000001",
        "0.0000001",
        "0.00000001",
    ):
        value, digits = accumulate(expected)
        assert round(value, digits) == float(expected)
    value_at_cap, digits_at_cap = accumulate("0.01000000")
    value_above_cap, digits_above_cap = accumulate("0.01000001")
    assert round(value_at_cap, digits_at_cap) == 0.01
    assert round(value_above_cap, digits_above_cap) > 0.01

    source = (EA_ROOT / "include" / "Py000Execution.mqh").read_text(encoding="utf-8")
    assert "if(++fractional_digits > 8) return false;" in source
    assert "value = NormalizeDouble(value, fractional_digits);" in source


def test_zmq_import_uses_64_bit_mt5_size_t_abi() -> None:
    source = (EA_ROOT / "include" / "Py000Zmq.mqh").read_text(encoding="utf-8")
    import_block = source.split('#import "libzmq.dll"', 1)[1].split("#import", 1)[0]
    declarations = {
        " ".join(line.split())
        for line in import_block.splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    }
    assert (
        "int zmq_setsockopt(long socket, int option, uchar &value[], long value_length);"
        in declarations
    )
    assert (
        "int zmq_getsockopt(long socket, int option, uchar &value[], long &value_length);"
        in declarations
    )
    assert "int zmq_send(long socket, uchar &data[], long length, int flags);" in declarations
    assert (
        "int zmq_recv(long socket, uchar &buffer[], long maximum_length, int flags);"
        in declarations
    )
    assert not any("int value_length" in line for line in declarations)
    assert not any("int &value_length" in line for line in declarations)
    assert not any("int length, int flags" in line for line in declarations)
    assert not any("int maximum_length" in line for line in declarations)


def test_transport_fatal_diagnostics_do_not_read_stale_errno() -> None:
    ea_source = (ROOT / "mt5_ea" / "PY000_Nautilus_MT5.mq5").read_text()
    zmq_source = (ROOT / "mt5_ea" / "include" / "Py000Zmq.mqh").read_text()

    assert 'reason, zmq_errno()' not in ea_source
    assert "Py000FailTransport(fatal_reason, fatal_has_errno, fatal_errno);" in ea_source
    assert 'fatal_reason = "multipart frame budget exceeded";' in zmq_source
    assert 'fatal_reason = "multipart byte budget exceeded";' in zmq_source
    assert 'fatal_reason = "REP zmq_send was short";' in zmq_source
    assert "fatal_errno = zmq_errno();" in zmq_source


def test_production_protocol_excludes_reference_state() -> None:
    python_source = (ROOT / "src" / "py000_nautilus" / "mt5_v1_protocol.py").read_text(
        encoding="utf-8"
    )
    assert "class JournalState" not in python_source
    assert "class Mt5V1ReferenceModel" not in python_source
    assert "def decode_response(" not in python_source
    assert "def decode_response_for(" in python_source
    assert "def validate_request(" in python_source


def test_docs_bind_selective_immutable_reference_blobs() -> None:
    docs = (ROOT / "docs" / "MT5_EA_V1_PROTOCOL.md").read_text(encoding="utf-8")
    for object_id in (
        "4d2c62edf16329c7cabb9afb2a683f777b215752",
        "16332d42ab94b8c96761e43a5ce2bae874811409",
        "2270dc97b43be219117d4b8484a0d2e048336bb0",
        "d4bdab85fd5980f07584f80ad7df710a5a67186e",
        "e46f3f2ec921ce5d949cc8ee73c4e888279cda0d",
    ):
        assert object_id in docs
