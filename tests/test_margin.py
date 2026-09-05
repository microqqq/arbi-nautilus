"""Authenticated callee vectors and explicit normalized migration boundaries.

Normalized vectors do not authenticate accounts; separate native-event tests
cover the bounded metadata mapping, not live admission or working-order reserves.
The old modules/ZIP are not loaded by this suite.
"""

import inspect
import json
from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import EUR, USD, USDT
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId
from nautilus_trader.model.objects import Currency

from py000_nautilus import margin as margin_module
from py000_nautilus.config import HedgeAccountRoute, SourceAccountRoute
from py000_nautilus.margin import bitfinex_margin_capacity, mt5_margin_capacity
from py000_nautilus.models import HedgeAccount, MakerAccount, SourceAccount

D = Decimal
_FIXTURE = Path(__file__).parent / "fixtures" / "legacy_margin_vectors.json"
_COLUMNS = (
    "expect", "base_ml", "vol", "price", "dur", "equity", "free_margin", "collateral",
    "pl", "avail", "bfx_first", "bfx_second", "mt5_first", "mt5_second",
)
_BFX_INPUT = {
    "margin_target": D(400), "base_margin_level": D(100), "position_ounces": D(0),
    "reference_price": D(4000), "collateral": D(0), "unrealized_pnl": D(0),
    "available_balance": D(1000), "free_margin": D(1000),
}
_MT5_INPUT = {
    "margin_target": D(400), "base_margin_level": D(60), "position_ounces": D(0),
    "ask": D(4000), "equity": D(1000),
}


@pytest.fixture(scope="module")
def oracle_vectors() -> list[dict[str, Decimal]]:
    payload = json.loads(_FIXTURE.read_text())
    metadata = payload["metadata"]
    assert metadata["schema_version"] == 1
    assert metadata["archive"]["sha256"] == (
        "3e50dcc625b1f78048622f3bb5e5ee2f94eb8e4c4635f39108fcb09f6e3d1892"
    )
    assert metadata["original_vector"] == {
        "name": "margin_golden_vectors.json",
        "sha256": "b2b084b3a1ee25db3eb998a17428ee0d92d3af5e393ebe4a5649c3dd4c6caf3f",
        "case_count": 2000,
    }
    assert [method["method_segment_sha256"] for method in metadata["methods"]] == [
        "4e22fa431dd519dde5bea5b5fd75be9502920bd56721464ca90c5d1e3d24c12e",
        "b3acf7db4ed91b044e4b0888e9da37a77c85a1f923cf4136fac5ab4ebb5952d7",
    ]
    assert metadata["absolute_tolerance"] == "0.000000001"
    assert tuple(payload["columns"]) == _COLUMNS
    assert len(payload["rows"]) == 2000
    return [dict(zip(_COLUMNS, map(D, row), strict=True)) for row in payload["rows"]]


@pytest.mark.parametrize("venue", ["bfx", "mt5"])
def test_authenticated_normalized_callee_vectors(
    oracle_vectors: list[dict[str, Decimal]], venue: str,
) -> None:
    for index, row in enumerate(oracle_vectors):
        if venue == "bfx":
            actual = bitfinex_margin_capacity(
                margin_target=row["expect"], base_margin_level=row["base_ml"],
                position_ounces=row["vol"], reference_price=row["price"],
                collateral=row["collateral"], unrealized_pnl=row["pl"],
                available_balance=row["avail"], free_margin=row["free_margin"],
            )
        else:
            actual = mt5_margin_capacity(
                margin_target=row["expect"], base_margin_level=row["base_ml"],
                position_ounces=row["vol"], ask=row["price"], equity=row["equity"],
            )
        assert row["dur"] in (D(1), D(-1))
        original = (row[f"{venue}_first"], row[f"{venue}_second"])
        expected = original if row["dur"] == 1 else original[::-1]
        assert all(isinstance(value, D) for value in actual)
        assert all(abs(a - e) <= D("1e-9") for a, e in zip(actual, expected, strict=True)), (
            index, venue, actual, expected,
        )


def test_explicit_normalized_flat_is_not_account_authentication() -> None:
    # The original BFX flat account producer divides by zero. These fully
    # supplied values are the migration input, NOT evidence that an account is flat.
    assert bitfinex_margin_capacity(**_BFX_INPUT) == (D(5), D(5))
    assert mt5_margin_capacity(**_MT5_INPUT) == (D(5), D(5))


def test_reverse_positions_return_physical_buy_sell_not_legacy_duration_order() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "position_ounces": D(-3), "collateral": D(600),
        "available_balance": D(400), "free_margin": D(400),
    }) == (D(8), D(2))
    # Original MT5 duration=-1 returns (8,2); physical BUY can add only 2 oz.
    assert mt5_margin_capacity(**{**_MT5_INPUT, "position_ounces": D(3)}) == (D(2), D(8))


def test_near_margin_target_preserves_reducing_direction() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "position_ounces": D(4), "collateral": D(800),
        "available_balance": D(30), "free_margin": D(30),
    }) == (D(0), D(8))
    assert mt5_margin_capacity(**{
        **_MT5_INPUT, "position_ounces": D(-4), "equity": D(830),
    }) == (D(8), D(0))


def test_bitfinex_free_margin_bound_is_not_replaced_by_total_equity() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "position_ounces": D(3), "collateral": D(1000),
        "available_balance": D(0), "free_margin": D(0),
    }) == (D(0), D(6))


def test_mt5_keeps_fractional_leverage_while_bitfinex_floors_it() -> None:
    assert mt5_margin_capacity(**{
        **_MT5_INPUT, "margin_target": D(500), "base_margin_level": D(100),
        "equity": D(240),
    }) == (D(1), D(1))
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "margin_target": D(500),
        "available_balance": D(240), "free_margin": D(240),
    }) == (D(0), D(0))


def test_mt5_exact_one_ounce_does_not_round_repeating_leverage_first() -> None:
    assert mt5_margin_capacity(**{**_MT5_INPUT, "equity": D(184)}) == (D(1), D(1))


@pytest.mark.parametrize("target", [100, 150, 200, 300, 400, 500, 1000])
@pytest.mark.parametrize("ask", [2000, 4000, 4100])
def test_mt5_realistic_integer_holding_boundaries(target: int, ask: int) -> None:
    for holding_limit in range(1, 13):
        equity = D(holding_limit) * (D(target) + D(60)) * D(ask) / D(10000)
        for position in (D("-0.4"), D(0), D("0.4"), D(3)):
            actual = mt5_margin_capacity(**{
                **_MT5_INPUT, "margin_target": D(target), "ask": D(ask),
                "equity": equity, "position_ounces": position,
            })
            expected = (
                max(D(holding_limit) - position, D(0)),
                max(D(holding_limit) + position, D(0)),
            )
            assert actual == expected, (target, ask, holding_limit, position, actual)


@pytest.mark.parametrize(
    ("equity", "holding_limit"),
    [("183.999999999999", 0), ("184.000000000001", 1)],
)
def test_mt5_realistic_integer_boundary_both_sides(equity: str, holding_limit: int) -> None:
    for position in (D("-0.4"), D(0), D("0.4"), D(3)):
        assert mt5_margin_capacity(**{
            **_MT5_INPUT, "equity": D(equity), "position_ounces": position,
        }) == (
            max(D(holding_limit) - position, D(0)),
            max(D(holding_limit) + position, D(0)),
        )


def test_bitfinex_zero_leverage_is_not_raised_to_one() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "margin_target": D(10000), "position_ounces": D("2.25"),
        "available_balance": D(100000), "free_margin": D(100000),
    }) == (D(0), D("2.25"))


@pytest.mark.parametrize("position", ["0.4", "-0.4"])
def test_fractional_net_position_is_not_rounded_by_account_producer(position: str) -> None:
    q = D(position)
    expected = (D(5) - q, D(5) + q)
    assert bitfinex_margin_capacity(**{**_BFX_INPUT, "position_ounces": q}) == expected
    assert mt5_margin_capacity(**{**_MT5_INPUT, "position_ounces": q}) == expected


@pytest.mark.parametrize(
    ("available", "expected"),
    [("0.999999999999999999", "0"), ("1", "1"), ("1.000000000000000001", "1")],
)
def test_decimal_hold_floor_does_not_round_through_float(available: str, expected: str) -> None:
    common = {"margin_target": D(9900), "base_margin_level": D(100)}
    result = (D(expected), D(expected))
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, **common, "reference_price": D(1),
        "available_balance": D(available), "free_margin": D(available),
    }) == result
    assert mt5_margin_capacity(**{
        **_MT5_INPUT, **common, "ask": D(1), "equity": D(available),
    }) == result


@pytest.mark.parametrize("field", ["collateral", "unrealized_pnl", "available_balance"])
def test_finite_negative_bitfinex_value_keeps_original_floor_and_final_clamp(field: str) -> None:
    values = {
        **_BFX_INPUT, "position_ounces": D(3), "collateral": D(0),
        "unrealized_pnl": D(0), "available_balance": D(0), "free_margin": D(0),
        field: D(-1),
    }
    assert bitfinex_margin_capacity(**values) == (D(0), D(2))


def test_finite_negative_free_margin_is_not_silently_clamped_to_zero() -> None:
    assert bitfinex_margin_capacity(**{
        **_BFX_INPUT, "position_ounces": D(3), "free_margin": D(-1),
    }) == (D(0), D(5))


def test_finite_negative_mt5_equity_keeps_original_floor_and_final_clamp() -> None:
    assert mt5_margin_capacity(**{
        **_MT5_INPUT, "position_ounces": D(3), "equity": D(-1),
    }) == (D(0), D(2))


@pytest.mark.parametrize(
    ("target", "base"), [("-400", "900"), ("600", "-100")],
)
def test_finite_margin_components_require_positive_sum_not_individual_risk_policy(
    target: str, base: str,
) -> None:
    values = {"margin_target": D(target), "base_margin_level": D(base)}
    assert bitfinex_margin_capacity(**{**_BFX_INPUT, **values}) == (D(5), D(5))
    assert mt5_margin_capacity(**{**_MT5_INPUT, **values}) == (D(5), D(5))


_FUNCTIONS: tuple[
    tuple[Callable[..., tuple[Decimal, Decimal]], dict[str, Decimal]], ...
] = ((bitfinex_margin_capacity, _BFX_INPUT), (mt5_margin_capacity, _MT5_INPUT))


@pytest.mark.parametrize(("calculate", "values"), _FUNCTIONS, ids=["bitfinex", "mt5"])
@pytest.mark.parametrize(
    "invalid", [None, True, 1, 1.0, "1", D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity")],
)
def test_every_input_requires_a_finite_decimal(
    calculate: Callable[..., tuple[Decimal, Decimal]], values: dict[str, Decimal], invalid: object,
) -> None:
    for field in values:
        with pytest.raises(ValueError, match=field):
            calculate(**{**values, field: cast(Decimal, invalid)})


@pytest.mark.parametrize(("calculate", "values"), _FUNCTIONS, ids=["bitfinex", "mt5"])
@pytest.mark.parametrize("target", ["-100", "-101"])
def test_nonpositive_combined_margin_is_rejected(
    calculate: Callable[..., tuple[Decimal, Decimal]], values: dict[str, Decimal], target: str,
) -> None:
    with pytest.raises(ValueError, match="combined margin"):
        calculate(**{**values, "margin_target": D(target), "base_margin_level": D(100)})


@pytest.mark.parametrize("price", ["0", "-1"])
def test_nonpositive_price_is_rejected(price: str) -> None:
    with pytest.raises(ValueError, match="reference_price"):
        bitfinex_margin_capacity(**{**_BFX_INPUT, "reference_price": D(price)})
    with pytest.raises(ValueError, match="ask"):
        mt5_margin_capacity(**{**_MT5_INPUT, "ask": D(price)})


@pytest.mark.parametrize(("calculate", "values"), _FUNCTIONS, ids=["bitfinex", "mt5"])
def test_all_normalized_values_are_required_arguments(
    calculate: Callable[..., tuple[Decimal, Decimal]], values: dict[str, Decimal],
) -> None:
    for field in values:
        with pytest.raises(TypeError, match=field):
            calculate(**{name: value for name, value in values.items() if name != field})


_SOURCE_ROUTE = SourceAccountRoute(
    account_id=AccountId("BITFINEX-001"), client_id=ClientId("BITFINEX"),
    max_long_ounces=D(100), max_short_ounces=D(100), base_margin_level=D(100),
)
_HEDGE_ROUTE = HedgeAccountRoute(
    account_id=AccountId("MT5-001"), client_id=ClientId("MT5"),
    max_long_ounces=D(100), max_short_ounces=D(100),
)
_SOURCE_ID = InstrumentId.from_str("XAUTUSDT.BITFINEX")
_VIEW_INPUT = {
    "margin_target": D(400), "max_abs_ounces": D(100), "now_ns": 100,
    "max_account_age_ns": 20, "client_ready": True, "ask": D(4000),
    "ask_ts_ns": 90, "max_quote_age_ns": 10, "ask_actionable": True,
}


def _account_event(*, bfx: bool, info: dict[str, Any] | None = None) -> AccountState:
    if info is None:
        info = ({
            "bitfinex_wallet_currency": "USTF0",
            "bitfinex_margin": {
                "instrument_id": _SOURCE_ID.value,
                "wallet": {
                    "balance": "1000", "available_balance": "1000",
                    "observed_ns": 85, "current": True,
                },
                "positions": {
                    "complete": True, "current": True, "observed_ns": 90, "position": None,
                },
            },
        } if bfx else {
            "mt5_equity": "1000", "mt5_leverage": 999,
            "mt5_positions_complete": True, "mt5_net_position_ounces": "0",
            "mt5_position_count": 0, "mt5_symbol": "XAUUSD", "mt5_stream_id": "stream-1",
            "mt5_account_observed_ns": 80, "mt5_account_sample_valid": True,
        })
    return AccountState(
        account_id=(_SOURCE_ROUTE if bfx else _HEDGE_ROUTE).account_id,
        account_type=AccountType.MARGIN, base_currency=USDT if bfx else USD,
        balances=[], margins=[], reported=True, info=info,
        event_id=UUID4(), ts_event=100, ts_init=100,
    )


def _position(quantity: str = "3") -> dict[str, Any]:
    return {
        "position_id": 44, "status": "ACTIVE", "quantity": quantity,
        "base_price": "4000", "profit_loss": "0", "leverage": "10",
        "collateral": "1000", "collateral_min": "200", "type": 1,
        "venue_update_ms": 1,
    }


def _source_view(event: AccountState | None, **changes: Any) -> SourceAccount | None:
    return margin_module.bitfinex_source_account(event, **{
        **_VIEW_INPUT, "route": _SOURCE_ROUTE, "instrument_id": _SOURCE_ID,
        "wallet_currency": "USTF0", **changes,
    })


def _mt5_view(
    event: AccountState | None, *, maker: bool = False, **changes: Any,
) -> HedgeAccount | MakerAccount | None:
    mapper = margin_module.mt5_maker_account if maker else margin_module.mt5_hedge_account
    return mapper(event, **{
        **_VIEW_INPUT, "route": _HEDGE_ROUTE, "symbol": "XAUUSD", "stream_id": "stream-1",
        **changes,
    })


def test_native_flat_account_mapping_and_mt5_shared_view_do_not_mutate_info() -> None:
    source, hedge = _account_event(bfx=True), _account_event(bfx=False)
    original = deepcopy((source.info, hedge.info))
    assert _source_view(source) == SourceAccount(
        _SOURCE_ROUTE.account_id, _SOURCE_ROUTE.client_id, D(0), D(5), D(5), D(100),
    )
    assert _mt5_view(hedge) == HedgeAccount(D(0), D(5), D(5))
    assert _mt5_view(hedge, maker=True) == MakerAccount(
        _HEDGE_ROUTE.account_id, _HEDGE_ROUTE.client_id, D(0), D(5), D(5),
    )
    assert (source.info, hedge.info) == original


def test_native_held_source_uses_dynamic_base_and_entry_price_not_flat_defaults() -> None:
    event = _account_event(bfx=True)
    event.info["bitfinex_margin"]["positions"]["position"] = _position()
    # B=10000*200/(1000*10)=200, floor leverage=16, H=min(8,4+3)=7.
    view = _source_view(event, ask=D("NaN"), ask_ts_ns=101, ask_actionable=False)
    assert view == SourceAccount(
        _SOURCE_ROUTE.account_id, _SOURCE_ROUTE.client_id, D(3), D(4), D(10), D(200),
    )


@pytest.mark.parametrize("quantity", ["0.4", "-0.4"])
def test_native_mapping_preserves_fractional_net_and_physical_sides(quantity: str) -> None:
    q = D(quantity)
    source, hedge = _account_event(bfx=True), _account_event(bfx=False)
    position = _position(quantity)
    position.update(collateral="200", collateral_min="20")
    source.info["bitfinex_margin"]["positions"]["position"] = position
    hedge.info.update(mt5_net_position_ounces=quantity, mt5_position_count=1)
    assert _source_view(source) == SourceAccount(
        _SOURCE_ROUTE.account_id, _SOURCE_ROUTE.client_id, q,
        D("5.4") - q, D("5.4") + q, D(100),
    )
    assert _mt5_view(hedge) == HedgeAccount(q, D(5) - q, D(5) + q)


@pytest.mark.parametrize("quantity", ["12", "-12"])
@pytest.mark.parametrize("bfx", [False, True])
def test_view_directional_risk_space_allows_reduction_but_not_opposite_over_limit(
    quantity: str, bfx: bool,
) -> None:
    event, q = _account_event(bfx=bfx), D(quantity)
    view: SourceAccount | HedgeAccount | MakerAccount | None
    if bfx:
        event.info["bitfinex_margin"]["positions"]["position"] = _position(quantity)
        event.info["bitfinex_margin"]["wallet"]["available_balance"] = "100000"
        view = _source_view(event, max_abs_ounces=D(10))
    else:
        event.info.update(mt5_net_position_ounces=quantity, mt5_position_count=1)
        event.info["mt5_equity"] = "100000"
        view = _mt5_view(event, max_abs_ounces=D(10))
    assert view is not None
    assert (view.max_long_ounces, view.max_short_ounces) == (
        (D(0), D(22)) if q > 0 else (D(22), D(0))
    )
    reducing_capacity = view.max_short_ounces if q > 0 else view.max_long_ounces
    assert D(1) <= reducing_capacity  # 12 -> 11 remains permitted.
    assert D(22) <= reducing_capacity < D(23)  # Cross to -10, never -11.


@pytest.mark.parametrize("bfx", [False, True])
def test_route_capacity_is_trade_amount_not_another_position_limit(bfx: bool) -> None:
    event = _account_event(bfx=bfx)
    view: SourceAccount | HedgeAccount | MakerAccount | None
    if bfx:
        event.info["bitfinex_margin"]["positions"]["position"] = _position()
        route = SourceAccountRoute(
            account_id=_SOURCE_ROUTE.account_id, client_id=_SOURCE_ROUTE.client_id,
            max_long_ounces=D(2), max_short_ounces=D("1.5"), base_margin_level=D(100),
        )
        view = _source_view(event, route=route)
    else:
        event.info.update(mt5_net_position_ounces="3", mt5_position_count=1)
        hedge_route = HedgeAccountRoute(
            account_id=_HEDGE_ROUTE.account_id, client_id=_HEDGE_ROUTE.client_id,
            max_long_ounces=D(2), max_short_ounces=D("1.5"),
        )
        view = _mt5_view(event, route=hedge_route)
    assert view is not None
    assert (view.max_long_ounces, view.max_short_ounces) == (D(2), D("1.5"))


def test_mapping_retains_zero_bfx_leverage_and_zero_base_from_real_held_fields() -> None:
    event = _account_event(bfx=True)
    position = _position("2.25")
    event.info["bitfinex_margin"]["positions"]["position"] = position
    view = _source_view(event, margin_target=D(10000))
    assert view is not None
    assert (view.max_long_ounces, view.max_short_ounces) == (D(0), D("2.25"))
    position["collateral_min"] = "0"
    view = _source_view(event)
    assert view is not None and view.base_margin_level == 0


@pytest.mark.parametrize("maker", [False, True])
def test_mt5_zero_net_with_multiple_tickets_is_valid_and_leverage_is_not_base(maker: bool) -> None:
    event = _account_event(bfx=False)
    event.info.update(mt5_position_count=2, mt5_equity="184", mt5_leverage=1)
    view = _mt5_view(event, maker=maker)
    assert view is not None and view.position_ounces == 0
    assert (view.max_long_ounces, view.max_short_ounces) == (D(1), D(1))


@pytest.mark.parametrize("maker", [False, True])
def test_mt5_single_positive_volume_ticket_cannot_have_zero_net(maker: bool) -> None:
    event = _account_event(bfx=False)
    event.info["mt5_position_count"] = 1
    assert _mt5_view(event, maker=maker) is None


@pytest.mark.parametrize("component", ["wallet", "positions"])
@pytest.mark.parametrize("observed", [None, True, "90", -1, 79, 101])
def test_bfx_component_age_is_independent_of_publish_time(component: str, observed: object) -> None:
    event = _account_event(bfx=True)
    event.info["bitfinex_margin"][component]["observed_ns"] = observed
    assert _source_view(event) is None


@pytest.mark.parametrize("field", ["balance", "available_balance"])
@pytest.mark.parametrize("invalid", [None, True, "bad", "NaN", "Infinity"])
def test_missing_or_invalid_wallet_value_is_not_zero(field: str, invalid: object) -> None:
    event = _account_event(bfx=True)
    event.info["bitfinex_margin"]["wallet"][field] = invalid
    assert _source_view(event) is None


@pytest.mark.parametrize("field", [
    "quantity", "base_price", "profit_loss", "leverage", "collateral", "collateral_min",
])
@pytest.mark.parametrize("invalid", [None, "bad", "NaN", "Infinity"])
def test_incomplete_held_fields_never_fall_back_to_flat(field: str, invalid: object) -> None:
    event = _account_event(bfx=True)
    event.info["bitfinex_margin"]["positions"]["position"] = _position()
    event.info["bitfinex_margin"]["positions"]["position"][field] = invalid
    assert _source_view(event) is None


@pytest.mark.parametrize(("field", "invalid"), [
    ("quantity", "0"), ("base_price", "0"), ("collateral", "0"),
    ("leverage", "0"), ("collateral_min", "-1"), ("status", "CLOSED"),
    ("type", 0), ("type", True), ("position_id", 0), ("position_id", True),
])
def test_held_position_requires_valid_derivative_position(field: str, invalid: object) -> None:
    event = _account_event(bfx=True)
    event.info["bitfinex_margin"]["positions"]["position"] = _position()
    event.info["bitfinex_margin"]["positions"]["position"][field] = invalid
    assert _source_view(event) is None


@pytest.mark.parametrize("case", [
    "missing_position", "missing_wallet", "incomplete", "old_positions", "old_wallet",
    "instrument", "currency", "account", "no_flat_base",
])
def test_bfx_identity_and_completeness_cannot_invent_flat(case: str) -> None:
    event, changes = _account_event(bfx=True), {}
    facts = event.info["bitfinex_margin"]
    if case == "missing_position":
        del facts["positions"]["position"]
    elif case == "missing_wallet":
        del facts["wallet"]
    elif case == "incomplete":
        facts["positions"]["complete"] = False
    elif case.startswith("old_"):
        facts[case.removeprefix("old_")]["current"] = False
    elif case == "instrument":
        facts["instrument_id"] = "OTHER.BITFINEX"
    elif case == "currency":
        event.info["bitfinex_wallet_currency"] = "USD"
    else:
        changes["route"] = SourceAccountRoute(
            account_id=(
                AccountId("BITFINEX-OTHER") if case == "account" else _SOURCE_ROUTE.account_id
            ),
            max_long_ounces=D(100), max_short_ounces=D(100),
        )
    assert _source_view(event, **changes) is None


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize(("field", "invalid"), [
    ("mt5_positions_complete", False), ("mt5_account_sample_valid", False),
    ("mt5_account_observed_ns", 79), ("mt5_account_observed_ns", 101),
    ("mt5_account_observed_ns", True), ("mt5_symbol", "OTHER"), ("mt5_stream_id", "old"),
    ("mt5_net_position_ounces", None), ("mt5_net_position_ounces", "NaN"),
    ("mt5_equity", None), ("mt5_equity", "Infinity"),
    ("mt5_position_count", None), ("mt5_position_count", -1), ("mt5_position_count", True),
    ("mt5_net_position_ounces", "1"),  # Contradicts the complete zero-ticket count.
])
def test_mt5_invalid_facts_never_fall_back_to_native_net_zero(
    maker: bool, field: str, invalid: object,
) -> None:
    event = _account_event(bfx=False)
    event.info[field] = invalid
    assert _mt5_view(event, maker=maker) is None


@pytest.mark.parametrize("bfx", [False, True])
@pytest.mark.parametrize(("field", "invalid"), [
    ("client_ready", False), ("client_ready", 1), ("ask_actionable", False),
    ("ask_ts_ns", 89), ("ask_ts_ns", 101), ("ask", D(0)), ("ask", D("NaN")),
    ("now_ns", True), ("max_account_age_ns", -1), ("max_quote_age_ns", -1),
    ("max_abs_ounces", D(-1)), ("max_abs_ounces", D("NaN")),
    ("margin_target", D("NaN")),
])
def test_flat_mapping_requires_current_qualification_and_actionable_fresh_ask(
    bfx: bool, field: str, invalid: object,
) -> None:
    event = _account_event(bfx=bfx)
    mapper = _source_view if bfx else _mt5_view
    changes: dict[str, Any] = {field: invalid}
    assert mapper(event, **changes) is None


@pytest.mark.parametrize("bfx", [False, True])
def test_mapping_converts_numeric_overflow_to_unavailable_without_mutating_event(bfx: bool) -> None:
    event = _account_event(bfx=bfx)
    if bfx:
        event.info["bitfinex_margin"]["wallet"]["available_balance"] = "1e1000000"
    else:
        event.info["mt5_equity"] = "1e1000000"
    before = deepcopy(event.info)
    assert (_source_view(event) if bfx else _mt5_view(event)) is None
    assert event.info == before


def test_mapping_missing_event_and_wrong_mt5_account_are_unavailable() -> None:
    assert _source_view(None) is None
    assert _mt5_view(None) is None
    assert _mt5_view(None, maker=True) is None
    assert _mt5_view(_account_event(bfx=True)) is None
    assert _source_view(_account_event(bfx=True), route=None) is None
    assert _mt5_view(_account_event(bfx=False), route=None) is None


def test_current_client_qualification_has_no_implicit_true_default() -> None:
    for mapper in (
        margin_module.bitfinex_source_account,
        margin_module.mt5_hedge_account,
        margin_module.mt5_maker_account,
    ):
        parameter = inspect.signature(mapper).parameters["client_ready"]
        assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("venue", ["bfx", "mt5", "maker"])
@pytest.mark.parametrize("currency", [EUR, None])
def test_native_base_currency_must_match_fixed_price_units(
    venue: str, currency: Currency | None,
) -> None:
    correct = _account_event(bfx=venue == "bfx")
    event = AccountState(
        account_id=correct.account_id, account_type=AccountType.MARGIN,
        base_currency=currency, balances=[], margins=[], reported=True, info=correct.info,
        event_id=UUID4(), ts_event=100, ts_init=100,
    )
    if venue == "bfx":
        assert _source_view(event) is None
    else:
        assert _mt5_view(event, maker=venue == "maker") is None


def test_held_base_does_not_require_a_flat_route_default() -> None:
    event = _account_event(bfx=True)
    event.info["bitfinex_margin"]["positions"]["position"] = _position()
    route = SourceAccountRoute(
        account_id=_SOURCE_ROUTE.account_id, max_long_ounces=D(100), max_short_ounces=D(100),
    )
    view = _source_view(event, route=route)
    assert view is not None and view.base_margin_level == D(200)


@pytest.mark.parametrize("bfx", [False, True])
def test_mapper_preserves_finite_negative_balance_formula(bfx: bool) -> None:
    event = _account_event(bfx=bfx)
    view: SourceAccount | HedgeAccount | MakerAccount | None
    if bfx:
        event.info["bitfinex_margin"]["positions"]["position"] = _position()
        event.info["bitfinex_margin"]["wallet"]["available_balance"] = "-1"
        view = _source_view(event)
        expected = D(5)
    else:
        event.info.update(mt5_equity="-1", mt5_net_position_ounces="3", mt5_position_count=1)
        view = _mt5_view(event)
        expected = D(2)
    assert view is not None
    assert (view.max_long_ounces, view.max_short_ounces) == (D(0), expected)


@pytest.mark.parametrize("invalid", [None, [], "bad"])
def test_mapper_rejects_wrong_metadata_shapes(invalid: object) -> None:
    event = _account_event(bfx=True)
    event.info["bitfinex_margin"] = invalid
    assert _source_view(event) is None


def test_mapper_rejects_missing_mt5_equity_key() -> None:
    event = _account_event(bfx=False)
    del event.info["mt5_equity"]
    assert _mt5_view(event) is None


@pytest.mark.parametrize("component", ["wallet", "positions"])
def test_bfx_current_is_exact_boolean(component: str) -> None:
    event = _account_event(bfx=True)
    event.info["bitfinex_margin"][component]["current"] = 1
    assert _source_view(event) is None


@pytest.mark.parametrize("bfx", [False, True])
@pytest.mark.parametrize("invalid", [D(-1), D("NaN"), D("Infinity")])
def test_route_trade_capacity_must_be_finite_and_nonnegative(bfx: bool, invalid: Decimal) -> None:
    event = _account_event(bfx=bfx)
    if bfx:
        route = SourceAccountRoute(
            account_id=_SOURCE_ROUTE.account_id, max_long_ounces=invalid,
            max_short_ounces=D(100), base_margin_level=D(100),
        )
        assert _source_view(event, route=route) is None
    else:
        hedge_route = HedgeAccountRoute(
            account_id=_HEDGE_ROUTE.account_id, max_long_ounces=invalid,
            max_short_ounces=D(100),
        )
        assert _mt5_view(event, route=hedge_route) is None
