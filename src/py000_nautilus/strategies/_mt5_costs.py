"""Pure MT5 Instrument facts shared by the two strategy consumers."""

from decimal import Decimal, InvalidOperation
from typing import cast

from nautilus_trader.model.instruments import Instrument

from py000_nautilus.economics import normalize_mt5_points_swap
from py000_nautilus.mt5_v1_protocol import MAX_OBSERVATION_FUTURE_NS

type Mt5SwapSpec = tuple[Decimal, Decimal, Decimal, int, tuple[Decimal, ...], str]


def mt5_swap_spec(instrument: Instrument) -> Mt5SwapSpec:
    info = instrument.info
    if not isinstance(info, dict):
        raise TypeError("MT5 instrument info must be a mapping")
    swap_long = _decimal(info["swap_long"])
    swap_short = _decimal(info["swap_short"])
    point = _decimal(info["point"])
    mode, rates, timezone = info["swap_mode"], info["swap_rates"], info["server_timezone"]
    if (
        type(mode) is not int or not isinstance(rates, list | tuple)
        or not isinstance(timezone, str)
    ):
        raise TypeError("MT5 swap mode, rates, or timezone has the wrong type")
    swap_rates = tuple(_decimal(rate) for rate in rates)
    normalize_mt5_points_swap(
        swap_long=swap_long, swap_short=swap_short, point=point, ask=Decimal(1),
        native_swap_mode=mode, swap_rates=swap_rates, now_ns=0, server_timezone=timezone,
    )
    return swap_long, swap_short, point, mode, swap_rates, timezone


def mt5_instrument_structure(instrument: Instrument) -> dict[str, object]:
    """Only swap values/mode/multipliers and observation times may change live."""
    values = cast(dict[str, object], type(instrument).to_dict(instrument))
    values.pop("ts_event")
    values.pop("ts_init")
    info = dict(cast(dict[str, object], values["info"]))
    for key in ("swap_long", "swap_short", "swap_mode", "swap_rates"):
        info.pop(key)
    for key in ("point", "mt5_contract_size_ounces", "mt5_volume_step_lots"):
        info[key] = _decimal(info[key])
    values["info"] = info
    return values


def mt5_instrument_is_fresh(instrument: Instrument, now_ns: int, max_age_ns: int) -> bool:
    return bool(
        instrument.ts_event > 0
        and -MAX_OBSERVATION_FUTURE_NS <= now_ns - instrument.ts_event <= max_age_ns
    )


def validate_mt5_instrument_update(
    previous: Instrument, instrument: Instrument, now_ns: int, max_age_ns: int,
) -> bool:
    """Reject invalid updates before replacing last-good; return economic change only."""
    changed = mt5_swap_spec(instrument) != mt5_swap_spec(previous)
    if (
        not mt5_instrument_is_fresh(instrument, now_ns, max_age_ns)
        or instrument.ts_event < previous.ts_event
        or mt5_instrument_structure(instrument) != mt5_instrument_structure(previous)
        or (instrument.ts_event == previous.ts_event and changed)
    ):
        raise ValueError("MT5 instrument changed structure or has invalid observation time")
    return changed


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("MT5 cost metadata has an invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("MT5 cost metadata must be finite")
    return result
