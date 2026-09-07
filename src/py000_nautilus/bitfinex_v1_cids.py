"""Persisted string ``ClientOrderId`` to Bitfinex int45 CID bindings."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent

MAX_CID = 2**45 - 1


class BitfinexV1CidError(ValueError):
    """A CID allocation or persisted binding is invalid."""


class BitfinexV1FeeConflict(BitfinexV1CidError):
    """The same execution has contradictory final fee facts."""


@dataclass(frozen=True, slots=True)
class BitfinexCidBinding:
    client_order_id: str
    cid: int
    allocated_utc_date: str


@dataclass(frozen=True, slots=True)
class BitfinexNativeFill:
    """Evidence of a fill already applied to the native cache, never a send intent."""

    trade_id: str
    native_fill_origin: str
    signed_quantity: Decimal
    price: Decimal
    ts_event_ns: int
    liquidity_side: str
    commission: Decimal
    commission_currency: str


@dataclass(frozen=True, slots=True)
class BitfinexFeeTrade:
    """Venue trade facts; absent final fee means pending, not a zero fee."""

    trade_id: int
    ts_event_ms: int
    execution_qty: Decimal
    execution_price: Decimal
    order_type: str
    order_price: Decimal
    maker: bool
    raw_fee: Decimal | None
    fee_currency: str | None

    @property
    def fee_finality(self) -> str:
        return "pending" if self.raw_fee is None else "final"


@dataclass(frozen=True, slots=True)
class BitfinexFeeMetadata:
    cid: int
    venue_order_id: int
    instrument_id: str
    raw_symbol: str
    native_fills: tuple[BitfinexNativeFill, ...] = ()
    venue_trades: tuple[BitfinexFeeTrade, ...] = ()


class BitfinexV1CidStore:
    """One account CID namespace whose execution event loop is the sole writer."""

    def __init__(self, path: str | Path, *, account_id: str) -> None:
        self.path = Path(path)
        self.account_id = _text(account_id, "account_id")
        self._by_client: dict[str, BitfinexCidBinding] = {}
        self._by_cid: dict[int, BitfinexCidBinding] = {}
        self._last_cid = 0
        self._fee_metadata: dict[int, BitfinexFeeMetadata] = {}
        self._accounting_conflict = False
        self.fees_durable = True
        if self.path.exists():
            self._load()

    def binding_for_client(self, client_order_id: str) -> BitfinexCidBinding | None:
        return self._by_client.get(_text(client_order_id, "client_order_id"))

    def binding_for_cid(self, cid: int) -> BitfinexCidBinding | None:
        return self._by_cid.get(_cid(cid))

    @property
    def bindings(self) -> tuple[BitfinexCidBinding, ...]:
        return tuple(self._by_client.values())

    @property
    def fee_metadata(self) -> tuple[BitfinexFeeMetadata, ...]:
        return tuple(self._fee_metadata.values())

    def fee_metadata_for_cid(self, cid: int) -> BitfinexFeeMetadata | None:
        return self._fee_metadata.get(_cid(cid))

    @property
    def accounting_conflict(self) -> bool:
        return self._accounting_conflict

    def mark_accounting_conflict(self) -> None:
        """Latch known contradictory evidence; successful I/O never resolves it."""
        if self._accounting_conflict and self.fees_durable:
            return
        self._accounting_conflict = True
        self.fees_durable = False
        self._persist(self._last_cid, tuple(self._by_client.values()))

    def record_native_fill(
        self, scope: BitfinexFeeMetadata, fill: BitfinexNativeFill,
    ) -> None:
        current = self._fee_scope(scope)
        previous = next(
            (row for row in current.native_fills if row.trade_id == fill.trade_id), None,
        )
        if previous is not None and previous != fill:
            raise BitfinexV1CidError("native fill changed its applied facts or origin")
        updated = current if previous is not None else replace(
            current, native_fills=(*current.native_fills, fill),
        )
        self._save_fee_metadata(updated)

    def record_venue_trade(
        self, scope: BitfinexFeeMetadata, trade: BitfinexFeeTrade,
    ) -> None:
        current = self._fee_scope(scope)
        previous = next(
            (row for row in current.venue_trades if row.trade_id == trade.trade_id), None,
        )
        if previous is not None:
            if replace(previous, raw_fee=None, fee_currency=None) != replace(
                trade, raw_fee=None, fee_currency=None,
            ):
                raise BitfinexV1CidError("Bitfinex trade ID changed its execution facts or fee")
            if previous.raw_fee is not None and trade.raw_fee is not None and previous != trade:
                raise BitfinexV1FeeConflict("Bitfinex trade ID changed its final fee facts")
            if previous.raw_fee is not None:
                trade = previous  # A repeated TE must never erase a final TU/REST fee.
        rows = {row.trade_id: row for row in current.venue_trades}
        rows[trade.trade_id] = trade
        self._save_fee_metadata(replace(current, venue_trades=tuple(rows.values())))

    def _fee_scope(self, scope: BitfinexFeeMetadata) -> BitfinexFeeMetadata:
        if scope.cid not in self._by_cid or scope.native_fills or scope.venue_trades:
            raise BitfinexV1CidError("fee scope must reference an existing CID without fill rows")
        current = self._fee_metadata.get(scope.cid, scope)
        if replace(current, native_fills=(), venue_trades=()) != scope or any(
            row.cid != scope.cid and row.venue_order_id == scope.venue_order_id
            for row in self._fee_metadata.values()
        ):
            raise BitfinexV1CidError("fee metadata changes CID/order identity")
        return current

    def _save_fee_metadata(self, metadata: BitfinexFeeMetadata) -> None:
        # Keep observed facts on I/O failure so replay can retry without accepting
        # conflicting observations. They are not advertised as durable until synced.
        if self._fee_metadata.get(metadata.cid) == metadata and self.fees_durable:
            return
        self._fee_metadata[metadata.cid] = metadata
        self.fees_durable = False
        self._persist(self._last_cid, tuple(self._by_client.values()))

    def allocate(self, client_order_id: str, *, epoch_ms: int) -> BitfinexCidBinding:
        client_id = _text(client_order_id, "client_order_id")
        if client_id in self._by_client:
            raise BitfinexV1CidError(f"client_order_id {client_id!r} is already bound")
        now_ms = _nonnegative_int(epoch_ms, "epoch_ms")
        cid = max(now_ms, self._last_cid + 1)
        if cid > MAX_CID:
            raise BitfinexV1CidError(f"CID exceeds int45 maximum {MAX_CID}")
        binding = BitfinexCidBinding(
            client_order_id=client_id,
            cid=cid,
            allocated_utc_date=datetime.fromtimestamp(now_ms // 1000, UTC).date().isoformat(),
        )
        bindings = (*self._by_client.values(), binding)
        try:
            self._persist(cid, bindings)
        except ParentDirectorySyncError:
            self._remember(binding)
            raise
        self._remember(binding)
        return binding

    def _remember(self, binding: BitfinexCidBinding) -> None:
        self._by_client[binding.client_order_id] = binding
        self._by_cid[binding.cid] = binding
        self._last_cid = binding.cid

    def _persist(self, last_cid: int, bindings: tuple[BitfinexCidBinding, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "account_id": self.account_id,
            "last_cid": last_cid,
            "accounting_conflict": self._accounting_conflict,
            "bindings": [asdict(item) for item in sorted(bindings, key=lambda item: item.cid)],
            "fee_metadata": [
                asdict(item)
                for item in sorted(self._fee_metadata.values(), key=lambda item: item.cid)
            ],
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(
                    payload, handle, sort_keys=True, separators=(",", ":"), default=_decimal_json,
                )
                handle.flush()
                os.fsync(handle.fileno())
            replace_and_sync_parent(temporary_path, self.path)
            self.fees_durable = True
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                type(raw) is not dict or type(raw.get("schema_version")) is not int
                or raw.get("schema_version") not in {1, 2}
            ):
                raise BitfinexV1CidError("unsupported CID state schema")
            if raw.get("account_id") != self.account_id:
                raise BitfinexV1CidError("CID state account_id does not match configuration")
            last_cid = _nonnegative_int(raw.get("last_cid"), "last_cid")
            rows = raw.get("bindings")
            if not isinstance(rows, list):
                raise BitfinexV1CidError("bindings must be an array")
            for raw_binding in rows:
                binding = _binding(raw_binding)
                if binding.client_order_id in self._by_client or binding.cid in self._by_cid:
                    raise BitfinexV1CidError("duplicate persisted binding")
                self._by_client[binding.client_order_id] = binding
                self._by_cid[binding.cid] = binding
            if last_cid != max(self._by_cid, default=0):
                raise BitfinexV1CidError("last_cid does not match persisted bindings")
            self._last_cid = last_cid
            if raw["schema_version"] == 2:
                conflict = raw.get("accounting_conflict")
                if type(conflict) is not bool:
                    raise BitfinexV1CidError("accounting_conflict must be a boolean")
                self._accounting_conflict = conflict
                metadata_rows = raw.get("fee_metadata")
                if not isinstance(metadata_rows, list):
                    raise BitfinexV1CidError("fee_metadata must be an array")
                for value in metadata_rows:
                    metadata = _fee_metadata(value)
                    if metadata.cid in self._fee_metadata:
                        raise BitfinexV1CidError("duplicate fee metadata CID")
                    self._fee_scope(replace(metadata, native_fills=(), venue_trades=()))
                    self._fee_metadata[metadata.cid] = metadata
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BitfinexV1CidError(str(exc)) from exc


def _binding(value: object) -> BitfinexCidBinding:
    if type(value) is not dict:
        raise BitfinexV1CidError("binding must be an object")
    row = value
    if set(row) != {"client_order_id", "cid", "allocated_utc_date"}:
        raise BitfinexV1CidError("binding fields are invalid")
    allocated = _text(row["allocated_utc_date"], "allocated_utc_date")
    if date.fromisoformat(allocated).isoformat() != allocated:
        raise BitfinexV1CidError("allocated_utc_date is not canonical")
    return BitfinexCidBinding(
        _text(row["client_order_id"], "client_order_id"), _cid(row["cid"]), allocated
    )


def _fee_metadata(value: object) -> BitfinexFeeMetadata:
    row = _fields(value, set(BitfinexFeeMetadata.__dataclass_fields__))
    native = tuple(_native_fill(item) for item in _array(row["native_fills"]))
    trades = tuple(_fee_trade(item) for item in _array(row["venue_trades"]))
    if len({item.trade_id for item in native}) != len(native) or len(
        {item.trade_id for item in trades}
    ) != len(trades):
        raise BitfinexV1CidError("duplicate fee trade identity")
    venue_id = _nonnegative_int(row["venue_order_id"], "venue_order_id")
    if venue_id == 0:
        raise BitfinexV1CidError("fee venue_order_id must be positive")
    return BitfinexFeeMetadata(
        _cid(row["cid"]), venue_id, _text(row["instrument_id"], "instrument_id"),
        _text(row["raw_symbol"], "raw_symbol"), native, trades,
    )


def _native_fill(value: object) -> BitfinexNativeFill:
    row = _fields(value, set(BitfinexNativeFill.__dataclass_fields__))
    origin = _text(row["native_fill_origin"], "native_fill_origin")
    liquidity = _text(row["liquidity_side"], "liquidity_side")
    if origin not in {"te_paper", "tu", "rest", "inferred", "unknown"} or liquidity not in {
        "MAKER", "TAKER", "NO_LIQUIDITY_SIDE",
    }:
        raise BitfinexV1CidError("invalid native fill origin or liquidity")
    return BitfinexNativeFill(
        _text(row["trade_id"], "trade_id"), origin,
        _decimal(row["signed_quantity"], nonzero=True),
        _decimal(row["price"], positive=True),
        _nonnegative_int(row["ts_event_ns"], "ts_event_ns"), liquidity,
        _decimal(row["commission"]), _text(row["commission_currency"], "commission_currency"),
    )


def _fee_trade(value: object) -> BitfinexFeeTrade:
    row = _fields(value, set(BitfinexFeeTrade.__dataclass_fields__))
    trade_id = _nonnegative_int(row["trade_id"], "trade_id")
    order_type = _text(row["order_type"], "order_type")
    if trade_id == 0 or order_type not in {"IOC", "LIMIT"} or type(row["maker"]) is not bool:
        raise BitfinexV1CidError("invalid fee trade identity/type/maker")
    if (row["raw_fee"] is None) != (row["fee_currency"] is None):
        raise BitfinexV1CidError("final fee needs both amount and currency")
    return BitfinexFeeTrade(
        trade_id, _nonnegative_int(row["ts_event_ms"], "ts_event_ms"),
        _decimal(row["execution_qty"], nonzero=True),
        _decimal(row["execution_price"], positive=True), order_type,
        _decimal(row["order_price"], positive=True), row["maker"],
        None if row["raw_fee"] is None else _decimal(row["raw_fee"]),
        None if row["fee_currency"] is None else _text(row["fee_currency"], "fee_currency"),
    )


def _fields(value: object, expected: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise BitfinexV1CidError("fee metadata fields are invalid")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise BitfinexV1CidError("fee metadata rows must be arrays")
    return value


def _decimal(value: object, *, nonzero: bool = False, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise BitfinexV1CidError("fee amounts must be decimal strings")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BitfinexV1CidError("invalid fee decimal") from exc
    if not parsed.is_finite() or (nonzero and parsed == 0) or (positive and parsed <= 0):
        raise BitfinexV1CidError("invalid finite fee quantity/price/amount")
    return parsed


def _decimal_json(value: object) -> str:
    if isinstance(value, Decimal) and value.is_finite():
        return format(value, "f")
    raise TypeError("fee metadata contains an unsupported value")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BitfinexV1CidError(f"{label} must be a non-empty string")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise BitfinexV1CidError(f"{label} must be a non-negative exact integer")
    return value


def _cid(value: object) -> int:
    parsed = _nonnegative_int(value, "cid")
    if not 1 <= parsed <= MAX_CID:
        raise BitfinexV1CidError(f"cid must be between 1 and {MAX_CID}")
    return parsed
