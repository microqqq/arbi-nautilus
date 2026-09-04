"""Persisted string ``ClientOrderId`` to Bitfinex int45 CID bindings."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent

MAX_CID = 2**45 - 1


class BitfinexV1CidError(ValueError):
    """A CID allocation or persisted binding is invalid."""


@dataclass(frozen=True, slots=True)
class BitfinexCidBinding:
    client_order_id: str
    cid: int
    allocated_utc_date: str


class BitfinexV1CidStore:
    """One account CID namespace whose execution event loop is the sole writer."""

    def __init__(self, path: str | Path, *, account_id: str) -> None:
        self.path = Path(path)
        self.account_id = _text(account_id, "account_id")
        self._by_client: dict[str, BitfinexCidBinding] = {}
        self._by_cid: dict[int, BitfinexCidBinding] = {}
        self._last_cid = 0
        if self.path.exists():
            self._load()

    def binding_for_client(self, client_order_id: str) -> BitfinexCidBinding | None:
        return self._by_client.get(_text(client_order_id, "client_order_id"))

    def binding_for_cid(self, cid: int) -> BitfinexCidBinding | None:
        return self._by_cid.get(_cid(cid))

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
            "schema_version": 1,
            "account_id": self.account_id,
            "last_cid": last_cid,
            "bindings": [asdict(item) for item in sorted(bindings, key=lambda item: item.cid)],
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
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            replace_and_sync_parent(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if type(raw) is not dict or raw.get("schema_version") != 1:
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
