"""Strict codec for the PY000 MT5 EA v1 wire."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from itertools import pairwise
from typing import Literal, cast
from unicodedata import category

PROTOCOL = "py000.mt5"
VERSION = 1
MAX_WIRE_BYTES = 64 * 1024
MAX_TEXT_LENGTH = 256
MAX_IDENTIFIER_LENGTH = 128
MAX_EVENTS_LIMIT = 500
MAX_JSON_DEPTH = 32
MAX_JSON_INT_DIGITS = 20
UINT64_MAX = 2**64 - 1
ACCOUNT_ID_DOMAIN = b"py000.mt5/account-id/v1\x00"
JOURNAL_NAMESPACE_DOMAIN = b"py000.mt5/journal-namespace/v1\x00"
JOURNAL_NAMESPACE_PREFIX = "PY000_MT5_V1_"

OPS = frozenset(
    {
        "close_position",
        "hello",
        "get_snapshot",
        "submit_market_delta",
        "get_execution_events",
    }
)
CAPABILITIES = tuple(sorted(OPS))
ERROR_CODES = frozenset(
    {
        "BINDING_MISMATCH",
        "CURSOR_AHEAD",
        "CURSOR_EXPIRED",
        "EXECUTION_DISABLED",
        "IDEMPOTENCY_CONFLICT",
        "MALFORMED",
        "RECOVERY_BLOCKED",
        "SCHEMA_MISMATCH",
        "UNKNOWN_OP",
    }
)

_UINT_RE = re.compile(r"0|[1-9][0-9]*\Z")
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_ID_RE = re.compile(r"[!-~]+\Z")
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+\Z")

type JsonObject = dict[str, object]
type RecoveryState = Literal["ready", "blocked"]


class WireError(ValueError):
    """A closed-wire failure with a stable public error code."""

    def __init__(self, code: str, message: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown v1 wire error code {code!r}")
        super().__init__(message)
        self.code = code


def _validate_json_tree(value: object) -> None:
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise WireError("MALFORMED", "JSON nesting exceeds v1 limit")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise WireError("MALFORMED", "JSON object key is not text")
                try:
                    key.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise WireError("MALFORMED", "JSON contains a surrogate") from exc
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise WireError("MALFORMED", "JSON contains a surrogate") from exc
        elif isinstance(current, float) and not math.isfinite(current):
            raise WireError("MALFORMED", "JSON number is not finite")


def _bounded_json_int(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > MAX_JSON_INT_DIGITS:
        raise WireError("MALFORMED", "JSON integer exceeds digit limit")
    return int(token)


def decode_json_object(raw: str | bytes) -> JsonObject:
    """Decode one bounded JSON object without duplicate-member last-wins behavior."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_WIRE_BYTES:
            raise WireError("MALFORMED", "wire payload exceeds 64 KiB")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WireError("MALFORMED", "wire payload is not UTF-8") from exc
    else:
        text = raw
        try:
            encoded = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise WireError("MALFORMED", "wire payload contains a surrogate") from exc
        if len(encoded) > MAX_WIRE_BYTES:
            raise WireError("MALFORMED", "wire payload exceeds 64 KiB")

    def unique_members(pairs: list[tuple[str, object]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in pairs:
            if key in result:
                raise WireError("MALFORMED", f"duplicate JSON member {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise WireError("MALFORMED", f"non-finite JSON constant {value!r}")

    try:
        decoded = json.loads(
            text,
            object_pairs_hook=unique_members,
            parse_constant=reject_constant,
            parse_int=_bounded_json_int,
        )
    except WireError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError, OverflowError) as exc:
        raise WireError("MALFORMED", "malformed JSON") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise WireError("MALFORMED", "wire root must be an object")
    _validate_json_tree(decoded)
    return cast(JsonObject, decoded)


def encode_json(value: JsonObject) -> str:
    """Return canonical UTF-8 JSON for hashes, fixtures, and wire comparison."""
    _validate_json_tree(value)
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        encoded.encode("utf-8", errors="strict")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise WireError("MALFORMED", "value cannot be encoded as v1 JSON") from exc
    return encoded


def validated_text(value: object, label: str, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise WireError("SCHEMA_MISMATCH", f"{label} must be a string")
    if not value or value != value.strip() or len(value) > max_length:
        raise WireError("SCHEMA_MISMATCH", f"{label} is empty, untrimmed, or too long")
    if any(character == "\x00" or category(character) in {"Cc", "Cs"} for character in value):
        raise WireError("SCHEMA_MISMATCH", f"{label} contains forbidden text")
    return value


def validated_optional_text(
    value: object,
    label: str,
    *,
    max_length: int = MAX_TEXT_LENGTH,
) -> str:
    if value == "":
        return ""
    return validated_text(value, label, max_length=max_length)


def validated_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise WireError("SCHEMA_MISMATCH", f"{label} must be a JSON bool")
    return value


def validated_identifier(
    value: object, label: str, *, max_length: int = MAX_IDENTIFIER_LENGTH
) -> str:
    text = validated_text(value, label, max_length=max_length)
    if _ID_RE.fullmatch(text) is None:
        raise WireError("SCHEMA_MISMATCH", f"{label} must be visible ASCII")
    return text


def validated_safe_token(value: object, label: str) -> str:
    text = validated_identifier(value, label)
    if _SAFE_TOKEN_RE.fullmatch(text) is None:
        raise WireError("SCHEMA_MISMATCH", f"{label} must use safe token characters")
    return text


def uint64_string(value: object, label: str, *, positive: bool = False) -> str:
    if not isinstance(value, str) or _UINT_RE.fullmatch(value) is None:
        raise WireError("SCHEMA_MISMATCH", f"{label} must be a canonical uint64 string")
    parsed = int(value)
    if parsed > UINT64_MAX or (positive and parsed == 0):
        raise WireError("SCHEMA_MISMATCH", f"{label} is outside uint64 range")
    return value


def decimal_string(value: object, label: str, *, positive: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 64 or _DECIMAL_RE.fullmatch(value) is None:
        raise WireError("SCHEMA_MISMATCH", f"{label} must be a non-exponent decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise WireError("SCHEMA_MISMATCH", f"{label} is not finite decimal") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise WireError("SCHEMA_MISMATCH", f"{label} must be positive finite decimal")
    return value


def exact_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WireError("SCHEMA_MISMATCH", f"{label} must be an exact bounded JSON int")
    return value


def closed_object(value: object, label: str, keys: frozenset[str]) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WireError("SCHEMA_MISMATCH", f"{label} must be an object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unknown = sorted(actual - keys)
        raise WireError(
            "SCHEMA_MISMATCH",
            f"{label} keys differ; missing={missing} unknown={unknown}",
        )
    return cast(JsonObject, value)


def normalized_account_id(broker_server: object, broker_login: object) -> str:
    """Derive a collision-resistant account binding from canonical raw identity bytes."""
    server = validated_text(broker_server, "broker_server", max_length=128)
    login = uint64_string(broker_login, "broker_login", positive=True)
    server_bytes = server.encode("utf-8", errors="strict")
    canonical = (
        ACCOUNT_ID_DOMAIN
        + len(server_bytes).to_bytes(4, "big")
        + server_bytes
        + b"\x00"
        + login.encode("ascii")
    )
    return f"mt5-sha256-{sha256(canonical).hexdigest()}"


def normalized_journal_namespace(account_id: object, symbol: object, magic: object) -> str:
    """Bind the full journal-custody tuple without lossy filesystem slugs."""
    account = validated_identifier(account_id, "account_id")
    raw_symbol = validated_text(symbol, "symbol", max_length=128)
    canonical_magic = uint64_string(magic, "magic")
    canonical = (
        JOURNAL_NAMESPACE_DOMAIN
        + account.encode("ascii")
        + b"\x00"
        + raw_symbol.encode("utf-8")
        + b"\x00"
        + canonical_magic.encode("ascii")
    )
    return JOURNAL_NAMESPACE_PREFIX + sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class Identity:
    account_id: str
    broker_server: str
    broker_login: str
    server_timezone: str
    server_timezone_source: Literal["configured_input"]
    symbol: str
    terminal_build: int
    ea_build_id: str
    declared_source_sha256: str
    stream_id: str
    boot_id: str
    magic: str
    execution_enabled: bool = False

    @classmethod
    def from_wire(cls, value: object) -> Identity:
        data = closed_object(
            value,
            "identity",
            frozenset(
                {
                    "account_id",
                    "broker_login",
                    "broker_server",
                    "server_timezone",
                    "server_timezone_source",
                    "boot_id",
                    "ea_build_id",
                    "declared_source_sha256",
                    "execution_enabled",
                    "magic",
                    "stream_id",
                    "symbol",
                    "terminal_build",
                }
            ),
        )
        execution_enabled = validated_bool(
            data["execution_enabled"],
            "identity.execution_enabled",
        )
        return cls(
            account_id=validated_identifier(data["account_id"], "identity.account_id"),
            broker_server=validated_text(
                data["broker_server"],
                "identity.broker_server",
                max_length=128,
            ),
            broker_login=uint64_string(
                data["broker_login"],
                "identity.broker_login",
                positive=True,
            ),
            server_timezone=validated_text(
                data["server_timezone"],
                "identity.server_timezone",
                max_length=64,
            ),
            server_timezone_source=cast(
                Literal["configured_input"],
                data["server_timezone_source"],
            ),
            symbol=validated_identifier(data["symbol"], "identity.symbol"),
            terminal_build=exact_int(
                data["terminal_build"],
                "identity.terminal_build",
                minimum=1,
                maximum=2**31 - 1,
            ),
            ea_build_id=validated_safe_token(data["ea_build_id"], "identity.ea_build_id"),
            declared_source_sha256=validated_identifier(
                data["declared_source_sha256"],
                "identity.declared_source_sha256",
                max_length=64,
            ),
            stream_id=validated_safe_token(data["stream_id"], "identity.stream_id"),
            boot_id=validated_safe_token(data["boot_id"], "identity.boot_id"),
            magic=uint64_string(data["magic"], "identity.magic"),
            execution_enabled=execution_enabled,
        )

    def __post_init__(self) -> None:
        expected = normalized_account_id(self.broker_server, self.broker_login)
        if validated_identifier(self.account_id, "account_id") != expected:
            raise WireError("SCHEMA_MISMATCH", "account_id is not derived from server+login")
        validated_identifier(self.symbol, "symbol")
        validated_text(self.server_timezone, "server_timezone", max_length=64)
        if self.server_timezone_source != "configured_input":
            raise WireError("SCHEMA_MISMATCH", "server_timezone_source mismatch")
        exact_int(self.terminal_build, "terminal_build", minimum=1, maximum=2**31 - 1)
        validated_safe_token(self.ea_build_id, "ea_build_id")
        sha = validated_identifier(
            self.declared_source_sha256,
            "declared_source_sha256",
            max_length=64,
        )
        if (
            len(sha) != 64
            or any(character not in "0123456789abcdef" for character in sha)
            or set(sha) == {"0"}
        ):
            raise WireError(
                "SCHEMA_MISMATCH",
                "declared_source_sha256 must be non-placeholder lowercase SHA-256",
            )
        validated_safe_token(self.stream_id, "stream_id")
        validated_safe_token(self.boot_id, "boot_id")
        uint64_string(self.magic, "magic")
        validated_bool(self.execution_enabled, "execution_enabled")

    def binding(self) -> Binding:
        return Binding(
            account_id=self.account_id,
            symbol=self.symbol,
            ea_build_id=self.ea_build_id,
            stream_id=self.stream_id,
            boot_id=self.boot_id,
        )

    def to_wire(self) -> JsonObject:
        return {
            "account_id": self.account_id,
            "broker_login": self.broker_login,
            "broker_server": self.broker_server,
            "server_timezone": self.server_timezone,
            "server_timezone_source": self.server_timezone_source,
            "boot_id": self.boot_id,
            "ea_build_id": self.ea_build_id,
            "declared_source_sha256": self.declared_source_sha256,
            "execution_enabled": self.execution_enabled,
            "magic": self.magic,
            "stream_id": self.stream_id,
            "symbol": self.symbol,
            "terminal_build": self.terminal_build,
        }


@dataclass(frozen=True, slots=True)
class Binding:
    account_id: str
    symbol: str
    ea_build_id: str
    stream_id: str
    boot_id: str

    @classmethod
    def from_wire(cls, value: object) -> Binding:
        data = closed_object(
            value,
            "binding",
            frozenset({"account_id", "symbol", "ea_build_id", "stream_id", "boot_id"}),
        )
        return cls(
            account_id=validated_identifier(data["account_id"], "binding.account_id"),
            symbol=validated_identifier(data["symbol"], "binding.symbol"),
            ea_build_id=validated_safe_token(data["ea_build_id"], "binding.ea_build_id"),
            stream_id=validated_safe_token(data["stream_id"], "binding.stream_id"),
            boot_id=validated_safe_token(data["boot_id"], "binding.boot_id"),
        )

    def to_wire(self) -> JsonObject:
        return {
            "account_id": self.account_id,
            "boot_id": self.boot_id,
            "ea_build_id": self.ea_build_id,
            "stream_id": self.stream_id,
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class HelloRequest:
    request_id: str
    op: Literal["hello"] = "hello"


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    request_id: str
    binding: Binding
    op: Literal["get_snapshot"] = "get_snapshot"


@dataclass(frozen=True, slots=True)
class SubmitMarketDeltaRequest:
    request_id: str
    binding: Binding
    client_request_id: str
    side: Literal["buy", "sell"]
    quantity_lots: str
    op: Literal["submit_market_delta"] = "submit_market_delta"


@dataclass(frozen=True, slots=True)
class ClosePositionRequest:
    request_id: str
    binding: Binding
    client_request_id: str
    side: Literal["buy", "sell"]
    quantity_lots: str
    position_ticket: str
    position_identifier: str
    op: Literal["close_position"] = "close_position"


@dataclass(frozen=True, slots=True)
class ExecutionEventsRequest:
    request_id: str
    binding: Binding
    after_cursor: str
    limit: int
    op: Literal["get_execution_events"] = "get_execution_events"


type Request = (
    HelloRequest
    | SnapshotRequest
    | SubmitMarketDeltaRequest
    | ClosePositionRequest
    | ExecutionEventsRequest
)


def validate_request(request: object) -> Request:
    """Validate one constructed request and normalize all failures to WireError."""
    if not isinstance(
        request,
        HelloRequest
        | SnapshotRequest
        | SubmitMarketDeltaRequest
        | ClosePositionRequest
        | ExecutionEventsRequest,
    ):
        raise WireError("SCHEMA_MISMATCH", "request has an unsupported dataclass type")
    validated_identifier(request.request_id, "request.request_id", max_length=64)
    expected_op = {
        HelloRequest: "hello",
        SnapshotRequest: "get_snapshot",
        SubmitMarketDeltaRequest: "submit_market_delta",
        ClosePositionRequest: "close_position",
        ExecutionEventsRequest: "get_execution_events",
    }.get(type(request))
    if expected_op is None or type(request.op) is not str or request.op != expected_op:
        raise WireError("SCHEMA_MISMATCH", "request op differs from its dataclass")
    if isinstance(request, HelloRequest):
        return request
    if type(request.binding) is not Binding:
        raise WireError("SCHEMA_MISMATCH", "request binding has an invalid type")
    Binding.from_wire(request.binding.to_wire())
    if isinstance(request, SubmitMarketDeltaRequest | ClosePositionRequest):
        validated_safe_token(request.client_request_id, "request.client_request_id")
        if type(request.side) is not str or request.side not in {"buy", "sell"}:
            raise WireError("SCHEMA_MISMATCH", "request side must be buy or sell")
        decimal_string(request.quantity_lots, "request.quantity_lots", positive=True)
        if isinstance(request, ClosePositionRequest):
            uint64_string(request.position_ticket, "request.position_ticket", positive=True)
            uint64_string(
                request.position_identifier,
                "request.position_identifier",
                positive=True,
            )
    elif isinstance(request, ExecutionEventsRequest):
        uint64_string(request.after_cursor, "request.after_cursor")
        exact_int(request.limit, "request.limit", minimum=1, maximum=MAX_EVENTS_LIMIT)
    return request


def request_to_wire(request: Request) -> JsonObject:
    request = validate_request(request)
    value: JsonObject = {
        "op": request.op,
        "protocol": PROTOCOL,
        "request_id": request.request_id,
        "version": VERSION,
    }
    if isinstance(request, HelloRequest):
        return value
    value["binding"] = request.binding.to_wire()
    if isinstance(request, SubmitMarketDeltaRequest | ClosePositionRequest):
        value.update(
            {
                "client_request_id": request.client_request_id,
                "quantity_lots": request.quantity_lots,
                "side": request.side,
            }
        )
        if isinstance(request, ClosePositionRequest):
            value.update(
                {
                    "position_identifier": request.position_identifier,
                    "position_ticket": request.position_ticket,
                }
            )
    elif isinstance(request, ExecutionEventsRequest):
        value.update({"after_cursor": request.after_cursor, "limit": request.limit})
    return value


def _validated_recovery_state(value: object, label: str) -> RecoveryState:
    if value not in {"ready", "blocked"} or type(value) is not str:
        raise WireError("SCHEMA_MISMATCH", f"{label} must be ready or blocked")
    return cast(RecoveryState, value)


def _validate_account(value: object) -> None:
    data = closed_object(
        value,
        "snapshot.account",
        frozenset(
            {
                "balance",
                "broker_login",
                "broker_server",
                "currency",
                "equity",
                "leverage",
                "margin",
                "margin_free",
                "margin_level",
                "trade_allowed",
                "trade_expert",
            }
        ),
    )
    decimal_string(data["balance"], "snapshot.account.balance")
    uint64_string(data["broker_login"], "snapshot.account.broker_login", positive=True)
    validated_text(data["broker_server"], "snapshot.account.broker_server", max_length=128)
    validated_identifier(data["currency"], "snapshot.account.currency", max_length=16)
    for field in ("equity", "margin", "margin_free", "margin_level"):
        decimal_string(data[field], f"snapshot.account.{field}")
    exact_int(data["leverage"], "snapshot.account.leverage", minimum=1, maximum=2**31 - 1)
    validated_bool(data["trade_allowed"], "snapshot.account.trade_allowed")
    validated_bool(data["trade_expert"], "snapshot.account.trade_expert")


def _validate_authority_flags(value: object) -> None:
    data = closed_object(
        value,
        "snapshot.authority_flags",
        frozenset(
            {
                "account_trade_allowed",
                "account_trade_expert",
                "market_open_hint",
                "mql_trade_allowed",
                "symbol_trade_mode",
                "terminal_connected",
                "terminal_trade_allowed",
            }
        ),
    )
    for field in (
        "account_trade_allowed",
        "account_trade_expert",
        "market_open_hint",
        "mql_trade_allowed",
        "terminal_connected",
        "terminal_trade_allowed",
    ):
        validated_bool(data[field], f"snapshot.authority_flags.{field}")
    exact_int(
        data["symbol_trade_mode"],
        "snapshot.authority_flags.symbol_trade_mode",
        minimum=0,
        maximum=2**31 - 1,
    )


def _validate_symbol_spec(value: object) -> None:
    data = closed_object(
        value,
        "snapshot.symbol_spec",
        frozenset(
            {
                "contract_size",
                "currency_base",
                "currency_margin",
                "currency_profit",
                "digits",
                "expiration_mode",
                "filling_mode",
                "freeze_level",
                "margin_initial",
                "margin_maintenance",
                "order_mode",
                "point",
                "stops_level",
                "symbol",
                "swap_long",
                "swap_mode",
                "swap_rates",
                "swap_short",
                "tick_size",
                "tick_value",
                "tick_value_loss",
                "tick_value_profit",
                "trade_calc_mode",
                "trade_mode",
                "volume_limit",
                "volume_max",
                "volume_min",
                "volume_step",
            }
        ),
    )
    for field in (
        "contract_size",
        "margin_initial",
        "margin_maintenance",
        "point",
        "swap_long",
        "swap_short",
        "tick_size",
        "tick_value",
        "tick_value_loss",
        "tick_value_profit",
        "volume_limit",
        "volume_max",
        "volume_min",
        "volume_step",
    ):
        decimal_string(data[field], f"snapshot.symbol_spec.{field}")
    validated_identifier(data["symbol"], "snapshot.symbol_spec.symbol")
    for field in ("currency_base", "currency_margin", "currency_profit"):
        validated_identifier(
            data[field],
            f"snapshot.symbol_spec.{field}",
            max_length=16,
        )
    swap_rates = data["swap_rates"]
    if not isinstance(swap_rates, list) or len(swap_rates) != 7:
        raise WireError(
            "SCHEMA_MISMATCH",
            "snapshot.symbol_spec.swap_rates must be a seven-item array",
        )
    for index, value in enumerate(swap_rates):
        rate = decimal_string(value, f"snapshot.symbol_spec.swap_rates[{index}]")
        if Decimal(rate) not in {Decimal(0), Decimal(1), Decimal(3)}:
            raise WireError(
                "SCHEMA_MISMATCH",
                f"snapshot.symbol_spec.swap_rates[{index}] must be 0, 1, or 3",
            )
    exact_int(data["digits"], "snapshot.symbol_spec.digits", minimum=0, maximum=16)
    for field in (
        "expiration_mode",
        "filling_mode",
        "freeze_level",
        "order_mode",
        "stops_level",
        "swap_mode",
        "trade_calc_mode",
        "trade_mode",
    ):
        exact_int(
            data[field],
            f"snapshot.symbol_spec.{field}",
            minimum=0,
            maximum=2**31 - 1,
        )


def _validate_execution_limits(value: object) -> None:
    data = closed_object(
        value,
        "snapshot.execution_limits",
        frozenset({"max_order_lots"}),
    )
    max_order_lots = decimal_string(
        data["max_order_lots"],
        "snapshot.execution_limits.max_order_lots",
        positive=True,
    )
    if "." in max_order_lots and len(max_order_lots.rsplit(".", maxsplit=1)[1]) > 8:
        raise WireError(
            "SCHEMA_MISMATCH",
            "snapshot.execution_limits.max_order_lots exceeds 8 decimal places",
        )


def _validate_position(value: object) -> None:
    data = closed_object(
        value,
        "snapshot.position",
        frozenset(
            {
                "comment",
                "identifier",
                "magic",
                "price_current",
                "price_open",
                "profit",
                "side",
                "stop_loss",
                "swap",
                "take_profit",
                "ticket",
                "time_msc",
                "volume_lots",
            }
        ),
    )
    validated_optional_text(data["comment"], "snapshot.position.comment", max_length=128)
    for field in ("identifier", "magic", "ticket", "time_msc"):
        uint64_string(data[field], f"snapshot.position.{field}", positive=field != "magic")
    for field in (
        "price_current",
        "price_open",
        "profit",
        "stop_loss",
        "swap",
        "take_profit",
    ):
        decimal_string(data[field], f"snapshot.position.{field}")
    decimal_string(data["volume_lots"], "snapshot.position.volume_lots", positive=True)
    if data["side"] not in {"buy", "sell"} or type(data["side"]) is not str:
        raise WireError("SCHEMA_MISMATCH", "snapshot.position.side must be buy or sell")


def _validate_time_provenance(value: object, identity: Identity) -> None:
    data = closed_object(
        value,
        "snapshot.time",
        frozenset(
            {
                "observed_utc_ms",
                "observed_utc_precision_ms",
                "observed_utc_source",
                "server_quote_time_ms",
                "server_quote_time_source",
                "server_timezone",
            }
        ),
    )
    uint64_string(data["observed_utc_ms"], "snapshot.time.observed_utc_ms", positive=True)
    if data["observed_utc_precision_ms"] != "1000":
        raise WireError("SCHEMA_MISMATCH", "UTC observation precision mismatch")
    if data["observed_utc_source"] != "mt5_time_gmt_second_precision":
        raise WireError("SCHEMA_MISMATCH", "UTC observation source mismatch")
    uint64_string(
        data["server_quote_time_ms"],
        "snapshot.time.server_quote_time_ms",
        positive=True,
    )
    if data["server_quote_time_source"] != "mt5_time_current_last_known_quote":
        raise WireError("SCHEMA_MISMATCH", "server quote time source mismatch")
    if data["server_timezone"] != identity.server_timezone:
        raise WireError("SCHEMA_MISMATCH", "snapshot timezone differs from identity")


def _validate_session(value: object, *, terminal_connected: bool) -> None:
    data = closed_object(
        value,
        "snapshot.session",
        frozenset(
            {
                "freshness",
                "freshness_age_ms",
                "freshness_source",
                "sample_server_time_ms",
                "sample_server_time_source",
                "scheduled_open",
                "session_schedule_available",
                "session_open",
                "session_source",
            }
        ),
    )
    validated_bool(data["session_open"], "snapshot.session.session_open")
    validated_bool(data["scheduled_open"], "snapshot.session.scheduled_open")
    validated_bool(
        data["session_schedule_available"],
        "snapshot.session.session_schedule_available",
    )
    sample_time = data["sample_server_time_ms"]
    if sample_time is not None:
        uint64_string(sample_time, "snapshot.session.sample_server_time_ms", positive=True)
    if data["sample_server_time_source"] != "mt5_time_trade_server_calculated_second_precision":
        raise WireError("SCHEMA_MISMATCH", "session sample time source mismatch")
    if data["session_source"] != "mt5_symbol_info_session_trade_at_time_trade_server":
        raise WireError("SCHEMA_MISMATCH", "session source mismatch")
    if data["freshness"] not in {"fresh", "stale", "unknown"}:
        raise WireError("SCHEMA_MISMATCH", "session freshness mismatch")
    age = data["freshness_age_ms"]
    if age is not None:
        uint64_string(age, "snapshot.session.freshness_age_ms")
    if (data["freshness"] == "unknown") != (age is None):
        raise WireError("SCHEMA_MISMATCH", "session freshness and age disagree")
    if data["freshness_source"] != "mt5_symbol_info_tick_vs_time_trade_server":
        raise WireError("SCHEMA_MISMATCH", "session freshness source mismatch")
    if sample_time is None and (
        data["freshness"] != "unknown"
        or data["session_schedule_available"] is not False
        or data["scheduled_open"] is not False
    ):
        raise WireError("SCHEMA_MISMATCH", "unknown session time cannot establish schedule")
    if data["session_schedule_available"] is False and data["scheduled_open"] is not False:
        raise WireError("SCHEMA_MISMATCH", "unavailable schedule cannot be open")
    available = (
        data["scheduled_open"] is True
        and terminal_connected
        and data["freshness"] == "fresh"
    )
    if data["session_open"] is not available:
        raise WireError("SCHEMA_MISMATCH", "session_open overstates current availability")


def _validate_snapshot_data(value: object) -> None:
    data = closed_object(
        value,
        "snapshot data",
        frozenset(
            {
                "account",
                "authority_flags",
                "execution_enabled",
                "execution_limits",
                "identity",
                "positions",
                "recovery_state",
                "session",
                "symbol_spec",
                "time",
            }
        ),
    )
    identity = Identity.from_wire(data["identity"])
    execution_enabled = validated_bool(data["execution_enabled"], "snapshot.execution_enabled")
    if execution_enabled != identity.execution_enabled:
        raise WireError(
            "SCHEMA_MISMATCH",
            "snapshot execution_enabled differs from identity",
        )
    _validate_execution_limits(data["execution_limits"])
    _validated_recovery_state(data["recovery_state"], "snapshot.recovery_state")
    _validate_time_provenance(data["time"], identity)
    _validate_authority_flags(data["authority_flags"])
    flags = cast(JsonObject, data["authority_flags"])
    _validate_session(
        data["session"],
        terminal_connected=cast(bool, flags["terminal_connected"]),
    )
    _validate_account(data["account"])
    account = cast(JsonObject, data["account"])
    if account["broker_login"] != identity.broker_login:
        raise WireError("SCHEMA_MISMATCH", "snapshot account login differs from identity")
    if account["broker_server"] != identity.broker_server:
        raise WireError("SCHEMA_MISMATCH", "snapshot account server differs from identity")
    _validate_symbol_spec(data["symbol_spec"])
    symbol_spec = cast(JsonObject, data["symbol_spec"])
    if symbol_spec["symbol"] != identity.symbol:
        raise WireError("SCHEMA_MISMATCH", "snapshot symbol differs from identity")
    positions = data["positions"]
    if not isinstance(positions, list):
        raise WireError("SCHEMA_MISMATCH", "snapshot.positions must be an array")
    for position in positions:
        _validate_position(position)


def _validate_submission_payload(
    value: object,
    *,
    event_type: str,
) -> JsonObject:
    base = frozenset({"client_request_id", "side", "quantity_lots"})
    close_target = frozenset({"position_identifier", "position_ticket"})
    if not isinstance(value, dict):
        raise WireError("SCHEMA_MISMATCH", f"{event_type} payload must be an object")
    supplied_target = close_target & value.keys()
    if supplied_target and supplied_target != close_target:
        raise WireError(
            "SCHEMA_MISMATCH",
            f"{event_type} payload must carry both close target fields",
        )
    target_keys = close_target if supplied_target else frozenset()
    if event_type == "submission_reserved":
        keys = base | target_keys
    elif event_type in {"order_rejected", "order_unknown"}:
        keys = base | target_keys | {"reason", "broker_retcode"}
    else:
        keys = base | target_keys | {
            "broker_retcode",
            "commission",
            "fill_price",
            "filled_quantity_lots",
            "venue_deal_id",
            "venue_order_id",
            "venue_position_id",
        }
    payload = closed_object(value, f"{event_type} payload", keys)
    validated_safe_token(payload["client_request_id"], "event.payload.client_request_id")
    if type(payload["side"]) is not str or payload["side"] not in {"buy", "sell"}:
        raise WireError("SCHEMA_MISMATCH", "event.payload.side must be buy or sell")
    decimal_string(payload["quantity_lots"], "event.payload.quantity_lots", positive=True)
    if target_keys:
        uint64_string(
            payload["position_identifier"],
            "event.payload.position_identifier",
            positive=True,
        )
        uint64_string(
            payload["position_ticket"],
            "event.payload.position_ticket",
            positive=True,
        )
    if event_type in {"order_rejected", "order_unknown"}:
        validated_safe_token(payload["reason"], "event.payload.reason")
        uint64_string(payload["broker_retcode"], "event.payload.broker_retcode")
    elif event_type == "order_filled":
        decimal_string(
            payload["filled_quantity_lots"],
            "event.payload.filled_quantity_lots",
            positive=True,
        )
        decimal_string(payload["fill_price"], "event.payload.fill_price", positive=True)
        decimal_string(payload["commission"], "event.payload.commission")
        for field in ("venue_order_id", "venue_deal_id", "venue_position_id"):
            uint64_string(payload[field], f"event.payload.{field}", positive=True)
        uint64_string(payload["broker_retcode"], "event.payload.broker_retcode")
        if target_keys and payload["venue_position_id"] != payload["position_identifier"]:
            raise WireError(
                "SCHEMA_MISMATCH",
                "close fill venue_position_id differs from position_identifier",
            )
    return payload


def _validate_event(value: object, *, stream_id: str) -> tuple[str, str]:
    data = closed_object(
        value,
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
    sequence = uint64_string(data["event_seq"], "event.event_seq", positive=True)
    uint64_string(data["event_time_ms"], "event.event_time_ms", positive=True)
    validated_safe_token(data["boot_id"], "event.boot_id")
    event_type = validated_safe_token(data["event_type"], "event.event_type")
    if event_type not in {
        "stream_started",
        "submission_reserved",
        "order_rejected",
        "order_filled",
        "order_unknown",
    }:
        raise WireError("SCHEMA_MISMATCH", "v1 event_type is unsupported")
    if data["stream_id"] != stream_id:
        raise WireError("SCHEMA_MISMATCH", "event stream_id differs from page")
    if event_type == "stream_started":
        payload = closed_object(
            data["payload"],
            "stream_started payload",
            frozenset({"ea_build_id", "execution_enabled"}),
        )
        validated_safe_token(payload["ea_build_id"], "event.payload.ea_build_id")
        validated_bool(payload["execution_enabled"], "event.payload.execution_enabled")
    else:
        _validate_submission_payload(data["payload"], event_type=event_type)
    return sequence, event_type


def _validate_events_data(value: object) -> None:
    data = closed_object(
        value,
        "events data",
        frozenset(
            {
                "events",
                "first_retained_cursor",
                "has_more",
                "identity",
                "last_cursor",
                "next_cursor",
                "stream_id",
            }
        ),
    )
    identity = Identity.from_wire(data["identity"])
    stream_id = validated_safe_token(data["stream_id"], "events.stream_id")
    if stream_id != identity.stream_id:
        raise WireError("SCHEMA_MISMATCH", "event page stream differs from identity")
    first = int(uint64_string(data["first_retained_cursor"], "first_retained_cursor"))
    last = int(uint64_string(data["last_cursor"], "last_cursor"))
    next_cursor = int(uint64_string(data["next_cursor"], "next_cursor"))
    if not first <= next_cursor <= last:
        raise WireError("SCHEMA_MISMATCH", "event page cursors are not ordered")
    has_more = validated_bool(data["has_more"], "events.has_more")
    if has_more != (next_cursor < last):
        raise WireError("SCHEMA_MISMATCH", "events.has_more differs from cursors")
    events = data["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS_LIMIT:
        raise WireError("SCHEMA_MISMATCH", "events must be a bounded array")
    validated_events = [_validate_event(event, stream_id=stream_id) for event in events]
    sequences = [int(sequence) for sequence, _ in validated_events]
    stream_started_boot_ids = [
        cast(JsonObject, event)["boot_id"]
        for event, (_, event_type) in zip(events, validated_events, strict=True)
        if event_type == "stream_started"
    ]
    if len(set(stream_started_boot_ids)) != len(stream_started_boot_ids):
        raise WireError("SCHEMA_MISMATCH", "event page repeats a stream_started boot_id")
    if any(right != left + 1 for left, right in pairwise(sequences)):
        raise WireError("SCHEMA_MISMATCH", "event page sequence is not contiguous")
    if sequences and sequences[-1] != next_cursor:
        raise WireError("SCHEMA_MISMATCH", "next_cursor differs from final event")
    if not sequences and has_more:
        raise WireError("SCHEMA_MISMATCH", "empty event page cannot have_more")


def _validate_success_data(op: str, value: object) -> None:
    if op not in OPS:
        raise WireError("SCHEMA_MISMATCH", "successful response op is unknown")
    if op == "hello":
        data = closed_object(
            value,
            "hello data",
            frozenset({"capabilities", "identity", "recovery_state"}),
        )
        if data["capabilities"] != list(CAPABILITIES):
            raise WireError("SCHEMA_MISMATCH", "hello capabilities mismatch")
        Identity.from_wire(data["identity"])
        _validated_recovery_state(data["recovery_state"], "hello.recovery_state")
        return
    if op == "get_snapshot":
        _validate_snapshot_data(value)
        return
    if op == "get_execution_events":
        _validate_events_data(value)
        return
    data = closed_object(value, "submit data", frozenset({"identity", "outcome"}))
    identity = Identity.from_wire(data["identity"])
    outcome = closed_object(
        data["outcome"],
        "submit outcome",
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
    _, event_type = _validate_event(outcome, stream_id=identity.stream_id)
    if event_type not in {"order_rejected", "order_filled", "order_unknown"}:
        raise WireError("SCHEMA_MISMATCH", "submit outcome must be a terminal execution event")


def _decode_response(raw: str | bytes) -> JsonObject:
    data = decode_json_object(raw)
    common = frozenset({"protocol", "version", "request_id", "op", "ok"})
    if data.get("protocol") != PROTOCOL or type(data.get("protocol")) is not str:
        raise WireError("SCHEMA_MISMATCH", "response protocol mismatch")
    if type(data.get("version")) is not int or data.get("version") != VERSION:
        raise WireError("SCHEMA_MISMATCH", "response version mismatch")
    validated_identifier(data.get("request_id"), "response.request_id", max_length=64)
    op = validated_identifier(data.get("op"), "response.op", max_length=64)
    if type(data.get("ok")) is not bool:
        raise WireError("SCHEMA_MISMATCH", "response ok must be bool")
    if data["ok"] is True:
        closed_object(data, "success response", common | {"data"})
        _validate_success_data(op, data["data"])
    else:
        closed_object(data, "error response", common | {"error"})
        error = closed_object(data["error"], "error", frozenset({"code", "message"}))
        code = validated_identifier(error["code"], "error.code", max_length=64)
        if code not in ERROR_CODES:
            raise WireError("SCHEMA_MISMATCH", "unknown response error code")
        validated_text(error["message"], "error.message")
    return data


def decode_response_for(request: Request, raw: str | bytes) -> JsonObject:
    """Decode a response while binding it to the exact originating request."""
    request = validate_request(request)
    response = _decode_response(raw)
    if response["request_id"] != request.request_id or response["op"] != request.op:
        raise WireError("BINDING_MISMATCH", "response envelope differs from request")
    if response["ok"] is not True or isinstance(request, HelloRequest):
        return response

    data = cast(JsonObject, response["data"])
    identity = Identity.from_wire(data["identity"])
    if identity.binding() != request.binding:
        raise WireError("BINDING_MISMATCH", "response identity differs from request binding")
    if isinstance(request, SubmitMarketDeltaRequest | ClosePositionRequest):
        outcome = cast(JsonObject, data["outcome"])
        payload = cast(JsonObject, outcome["payload"])
        expected = {
            "client_request_id": request.client_request_id,
            "quantity_lots": request.quantity_lots,
            "side": request.side,
        }
        if isinstance(request, ClosePositionRequest):
            if "position_identifier" not in payload or "position_ticket" not in payload:
                raise WireError("BINDING_MISMATCH", "close outcome omits its target position")
            if (
                outcome["event_type"] == "order_filled"
                and payload["venue_position_id"] != request.position_identifier
            ):
                raise WireError(
                    "BINDING_MISMATCH",
                    "close fill differs from its target position",
                )
            expected.update(
                {
                    "position_identifier": request.position_identifier,
                    "position_ticket": request.position_ticket,
                }
            )
        elif "position_identifier" in payload or "position_ticket" in payload:
            raise WireError("BINDING_MISMATCH", "market delta outcome contains a close target")
        if any(payload[field] != value for field, value in expected.items()):
            raise WireError("BINDING_MISMATCH", "submit outcome differs from request")
    elif isinstance(request, ExecutionEventsRequest):
        events = cast(list[JsonObject], data["events"])
        if len(events) > request.limit:
            raise WireError("SCHEMA_MISMATCH", "event page exceeds requested limit")
        after = int(request.after_cursor)
        first_retained = int(cast(str, data["first_retained_cursor"]))
        last_cursor = int(cast(str, data["last_cursor"]))
        if not first_retained <= after <= last_cursor:
            raise WireError("SCHEMA_MISMATCH", "event page does not contain after_cursor")
        next_cursor = int(cast(str, data["next_cursor"]))
        has_more = cast(bool, data["has_more"])
        if events:
            first = int(cast(str, events[0]["event_seq"]))
            if first != after + 1:
                raise WireError("SCHEMA_MISMATCH", "first event does not follow after_cursor")
        elif next_cursor != after:
            raise WireError("SCHEMA_MISMATCH", "empty page advanced its cursor")
        if has_more and (not events or next_cursor <= after):
            raise WireError("SCHEMA_MISMATCH", "event page cannot make forward progress")
    return response


def decode_pub(raw: str | bytes) -> JsonObject:
    data = decode_json_object(raw)
    common = frozenset(
        {
            "protocol",
            "version",
            "message_type",
            "event_time_ms",
            "event_time_source",
            "identity",
            "market_open_hint",
        }
    )
    if data.get("protocol") != PROTOCOL or type(data.get("protocol")) is not str:
        raise WireError("SCHEMA_MISMATCH", "PUB protocol mismatch")
    if type(data.get("version")) is not int or data.get("version") != VERSION:
        raise WireError("SCHEMA_MISMATCH", "PUB version mismatch")
    message_type = data.get("message_type")
    Identity.from_wire(data.get("identity"))
    uint64_string(data.get("event_time_ms"), "PUB event_time_ms", positive=True)
    validated_bool(data.get("market_open_hint"), "PUB market_open_hint")
    if message_type == "tick" and type(message_type) is str:
        closed_object(data, "tick PUB", common | {"bid", "ask"})
        if data["event_time_source"] != "mt5_symbol_info_tick":
            raise WireError("SCHEMA_MISMATCH", "tick time source mismatch")
        bid = decimal_string(data["bid"], "tick.bid", positive=True)
        ask = decimal_string(data["ask"], "tick.ask", positive=True)
        if Decimal(bid) > Decimal(ask):
            raise WireError("SCHEMA_MISMATCH", "tick bid exceeds ask")
    elif message_type == "heartbeat" and type(message_type) is str:
        closed_object(data, "heartbeat PUB", common | {"last_tick_time_ms"})
        if data["event_time_source"] != "mt5_time_gmt_second_precision":
            raise WireError("SCHEMA_MISMATCH", "heartbeat time source mismatch")
        last_tick = data["last_tick_time_ms"]
        if last_tick is not None:
            uint64_string(last_tick, "heartbeat.last_tick_time_ms", positive=True)
    else:
        raise WireError("SCHEMA_MISMATCH", "PUB message_type mismatch")
    return data
