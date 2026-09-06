"""Explicit offline legacy Maker checkpoint conversion to a different prefix."""

import argparse
import json
import sys
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.maker_store import (
    MakerStateStore,
    _LegacyOrder,
    _route,
    maker_legacy_paths,
    maker_state_path,
)
from py000_nautilus.models import BusinessOrderSide, SourceDirection
from py000_nautilus.store import JsonStateStore, StoreState, _persist_payload


def _input_paths(prefix: Path) -> tuple[Path, ...]:
    new = maker_state_path(prefix)
    if new.exists() or new.is_symlink():
        return (new,)
    paths = maker_legacy_paths(prefix)
    if not all(path.is_file() for path in paths):
        raise ValueError("legacy migration requires both old direction files; one is missing")
    return paths


def _require_new_output(prefix: Path) -> None:
    if any(path.exists() or path.is_symlink()
           for path in (maker_state_path(prefix), *maker_legacy_paths(prefix))):
        raise ValueError("migration output state already exists")


def _read_states(
    contents: tuple[bytes, ...], source_instrument_id: str, hedge_instrument_id: str,
) -> dict[SourceDirection, StoreState]:
    raw = [json.loads(content) for content in contents]
    if len(raw) == 1:
        header = raw[0]
        if not isinstance(header, dict) or set(header) != {
            "schema_version", "kind", "source_instrument_id", "hedge_instrument_id", "directions",
        } or type(header["schema_version"]) is not int or header["schema_version"] != 2:
            raise ValueError("migration accepts only legacy Maker schema v2 or both v1 files")
        if (header["kind"] != "maker" or header["source_instrument_id"] != source_instrument_id
                or header["hedge_instrument_id"] != hedge_instrument_id):
            raise ValueError("legacy Maker instrument binding differs")
        directions = header["directions"]
        if not isinstance(directions, dict) or set(directions) != {"bid", "ask"}:
            raise ValueError("legacy Maker requires both directions")
        raw = [directions["bid"], directions["ask"]]
    if any(not isinstance(item, dict) or type(item.get("schema_version")) is not int
           or item["schema_version"] != 1 for item in raw):
        raise ValueError("invalid legacy direction schema")
    try:
        return {direction: JsonStateStore._from_payload(item)
                for direction, item in zip((SourceDirection.LONG, SourceDirection.SHORT), raw,
                                           strict=True)}
    except (KeyError, TypeError, AttributeError, ArithmeticError) as exc:
        raise ValueError("invalid legacy direction state") from exc


def _checkpoint_orders(states: dict[SourceDirection, StoreState]) -> tuple[_LegacyOrder, ...]:
    orders: list[_LegacyOrder] = []
    for direction, state in states.items():
        direction_residual = Decimal(0)
        for cid, record in sorted(state.source_orders.items()):
            intents = [intent for intent in state.hedge_intents.values()
                       if intent.source_client_order_id == cid]
            allocated = sum((intent.hedge_quantity_ounces * (
                1 if intent.hedge_side is BusinessOrderSide.SELL else -1
            ) for intent in intents), Decimal(0))
            signed = record.filled_ounces * (1 if direction is SourceDirection.LONG else -1)
            direction_residual += signed - allocated
            orders.append(_LegacyOrder(
                cid, direction, _route(record), record.filled_ounces,
                tuple(sorted(key for key in state.seen_source_fills if key.split("|")[0] == cid)),
                tuple(sorted(intent.intent_id for intent in intents)), allocated,
            ))
        if (not state.net_unhedged_ounces.is_finite()
                or direction_residual != state.net_unhedged_ounces):
            raise ValueError("legacy direction cumulative/allocation conservation differs")
    return tuple(orders)


def migrate_maker_state(
    input_prefix: str | Path, output_prefix: str | Path,
    source_instrument_id: str, hedge_instrument_id: str, *, stopped: bool = False,
) -> MakerStateStore:
    """Convert only explicitly stopped legacy input; this declaration is not a process lock."""
    if not stopped:
        raise ValueError("migration requires an explicit stopped declaration")
    source, destination = Path(input_prefix).resolve(), Path(output_prefix).resolve()
    if source == destination:
        raise ValueError("migration requires a different output prefix")
    _require_new_output(destination)
    paths = _input_paths(source)
    contents = tuple(path.read_bytes() for path in paths)
    states = _read_states(contents, source_instrument_id, hedge_instrument_id)
    owner = MakerStateStore(destination, source_instrument_id, hedge_instrument_id)
    owner._legacy_orders = _checkpoint_orders(states)
    owner._legacy_sources = tuple((str(path), sha256(content).hexdigest())
                                  for path, content in zip(paths, contents, strict=True))
    for direction, state in states.items():
        owner.stores[direction]._state = replace(state, net_unhedged_ounces=Decimal(0))
    owner._validate()
    owner._committed = owner._snapshot()
    if paths != _input_paths(source) or contents != tuple(path.read_bytes() for path in paths):
        raise ValueError("legacy input changed during migration; stable stopped input is required")
    _require_new_output(destination)
    _persist_payload(owner.path, owner._to_payload(), overwrite=False)
    return owner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-prefix", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--source-instrument", required=True)
    parser.add_argument("--hedge-instrument", required=True)
    parser.add_argument("--stopped", action="store_true",
                        help="declare the old program stopped; does not acquire a process lock")
    args = parser.parse_args(argv)
    try:
        owner = migrate_maker_state(args.input_prefix, args.output_prefix, args.source_instrument,
                                    args.hedge_instrument, stopped=args.stopped)
    except ParentDirectorySyncError as exc:
        print(f"Complete new target retained; parent durability unconfirmed: {exc}",
              file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"Maker migration refused: {exc}", file=sys.stderr)
        return 1
    print(f"Maker state migrated to {owner.path}; original files retained, no strategy started")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
