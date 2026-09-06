"""Small authenticated caller vectors; CI never loads the original ZIP/modules.

Inputs are normalized shared-market values. FX conversion is an explicit migration
mapping, and Maker final price/clamp remain separately tested Owner-frozen contracts.
"""

import json
import math
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model.identifiers import AccountId

from py000_nautilus.config import (
    CarryConfig,
    FxConfig,
    MakerEconomicsConfig,
    MakerSideConfig,
    RiskConfig,
    TakerEconomicsConfig,
)
from py000_nautilus.economics import (
    evaluate_taker,
    long_net_return,
    risk_allows,
    select_source_account,
    short_net_return,
)
from py000_nautilus.maker_economics import maker_quote
from py000_nautilus.models import (
    BookTop,
    HedgeAccount,
    MakerAccount,
    Opportunity,
    SourceAccount,
    SourceDirection,
)

D = Decimal
_PAYLOAD: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures/legacy_caller_vectors.json").read_text(),
)
_OUTPUT_SHA = "bb12dd4872ed170b972d45fa9708059644830a6f5528eaf6924500fd592a1273"
_TOLERANCE = D("1e-12")


def _values(group: str, case: dict[str, Any]) -> dict[str, Any]:
    return {**_PAYLOAD["common"], **_PAYLOAD[group]["defaults"], **case["input"]}


def _book(values: dict[str, str]) -> BookTop:
    return BookTop(**{key: D(value) for key, value in values.items()})


def _source(values: dict[str, str]) -> SourceAccount:
    return SourceAccount(
        account_id=AccountId(values["name"]), client_id=None,
        position_ounces=D(values["position"]), max_long_ounces=D(values["buy"]),
        max_short_ounces=D(values["sell"]), base_margin_level=D(values["base_margin"]),
    )


def _hedge(values: dict[str, str]) -> MakerAccount:
    return MakerAccount(
        account_id=AccountId(values["name"]), client_id=None,
        position_ounces=D(values["position"]), max_long_ounces=D(values["buy"]),
        max_short_ounces=D(values["sell"]),
    )


def _risk(values: dict[str, Any]) -> RiskConfig:
    return RiskConfig(
        source_max_abs=D(values["source_max_abs"]), hedge_max_abs=D(values["hedge_max_abs"]),
        source_min_keep_abs=D(values["source_min_keep_abs"]),
        hedge_min_keep_abs=D(values["hedge_min_keep_abs"]), only_long=values["only_long"],
    )


def _close(actual: Decimal, original: str) -> None:
    assert abs(actual - D(original)) <= _TOLERANCE, (actual, original)


def test_caller_fixture_provenance_and_output_integrity() -> None:
    metadata = _PAYLOAD["metadata"]
    assert metadata["schema_version"] == 1
    assert metadata["archive"]["sha256"] == (
        "3e50dcc625b1f78048622f3bb5e5ee2f94eb8e4c4635f39108fcb09f6e3d1892"
    )
    assert metadata["member"]["sha256"] == (
        "741e264c28bde67d59583b9fa49c626436e04066125e3015737add77e122d3bd"
    )
    assert [method["method_segment_sha256"] for method in metadata["methods"]] == [
        "eb1cc8a6668db38a0c37044f42dbeac418a1e27803ce66f361cf80e5c8b2763f",
        "5a1bea4888e1ab08446183b6b8ba4aceece46549b4129d1d4f226e84294d8b9f",
        "e9ad9f6448a18f3d50578276a3bd0f6495897a6a9cb94c3cd9d687cd92309bb7",
        "26a6fbf458dc66715139b74b3d59de2192d4ea17094de19300806f46ea4d7612",
        "10b7a670d0b4b14d29483ea21099bd1048df19d99a4f6b6e0a9b23d2c5fe3063",
        "30c134e82ddf7918945b94a1566eb2a1f225f7eb827f9e0255763fa311cb8fa9",
        "a52be69134898032136833858c7c6939efc65a41b52a258ef7ce2e8778203882",
        "1a17559add82f3fdfeba94080fcfd55a715e6172ee90ed5109c4ddda7b0f2b02",
    ]
    outputs = {
        group: [[case["id"], case["expected"]] for case in _PAYLOAD[group]["cases"]]
        for group in ("taker", "maker")
    }
    digest = sha256(json.dumps(outputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == metadata["expected_outputs_sha256"] == _OUTPUT_SHA
    assert sum(map(len, outputs.values())) == metadata["case_count"] == 59
    assert D(metadata["absolute_tolerance"]) == _TOLERANCE
    for group in ("taker", "maker"):
        assert len({case["id"] for case in _PAYLOAD[group]["cases"]}) == len(outputs[group])


def test_strict_threshold_fixture_uses_binary_exact_ratios_and_adjacent_thresholds() -> None:
    for side, indices in (("ask", (0, 1, 2)), ("bid", (3, 4, 5))):
        rows = [_PAYLOAD["taker"]["cases"][index] for index in indices]
        assert [float(_values("taker", row)["open_spread"][side]) for row in rows] == [
            math.nextafter(0.125, -math.inf), 0.125, math.nextafter(0.125, math.inf),
        ]
        assert all(row["expected"]["net_return"][side] == "0.125" for row in rows)
        assert rows[0]["expected"]["candidate"]["side"] == side
        assert rows[1]["expected"]["candidate"] is None
        assert rows[2]["expected"]["candidate"] is None


@pytest.mark.parametrize("case", _PAYLOAD["taker"]["cases"], ids=lambda case: case["id"])
def test_authenticated_taker_caller(case: dict[str, Any]) -> None:
    values = _values("taker", case)
    config = TakerEconomicsConfig(
        base_book_quantity=D(values["base_book_quantity"]),
        open_quantity_long=D(values["open_amount"]["bid"]),
        open_quantity_short=D(values["open_amount"]["ask"]),
        threshold_long=D(values["open_spread"]["bid"]),
        threshold_short=D(values["open_spread"]["ask"]), margin_level=D(values["margin_level"]),
        carry=CarryConfig(**{key: D(value) for key, value in values["carry"].items()}),
        fx=FxConfig(**{key: D(value) for key, value in values["fx"].items()}),
        risk=_risk(values["risk"]),
    )
    source_book, hedge_book = _book(values["source_book"]), _book(values["hedge_book"])
    assert min(source_book.bid_size, source_book.ask_size) >= config.base_book_quantity
    accounts = tuple(_source(value) for value in values["sources"])
    assert len(values["hedges"]) == 1
    raw_hedge = values["hedges"][0]
    hedge = HedgeAccount(D(raw_hedge["position"]), D(raw_hedge["buy"]), D(raw_hedge["sell"]))
    original = case["expected"]
    _close(long_net_return(source_book.ask, hedge_book.bid, config), original["net_return"]["bid"])
    _close(short_net_return(source_book.bid, hedge_book.ask, config), original["net_return"]["ask"])
    actual = evaluate_taker(source_book, hedge_book, accounts, hedge, config)
    candidate = original["candidate"]
    if candidate is None:
        assert actual is None
        return

    direction = SourceDirection(candidate["side"])
    selected = select_source_account(accounts, direction)
    assert str(selected.account_id) == candidate["source"]
    assert raw_hedge["name"] == candidate["hedge"]
    # Compare the existing risk helper directly to the original validator's bool,
    # including rejected pre-risk candidates which evaluate_taker does not return.
    original_candidate = Opportunity(
        direction=direction, source_account=selected,
        source_price_usdt=D(candidate["source_price"]),
        hedge_reference_price_usd=D(candidate["comparable_hedge_price"]),
        source_quantity_ounces=D(candidate["quantity"]),
        net_return=D(original["net_return"][candidate["side"]]), leverage=1,
    )
    assert risk_allows(original_candidate, hedge, config) is candidate["risk_allows"]
    if not candidate["risk_allows"]:
        assert actual is None  # Especially SHORT rejection when LONG also qualifies.
        return

    assert actual is not None
    assert actual.direction is direction
    assert actual.source_account == selected
    _close(actual.source_quantity_ounces, candidate["quantity"])
    _close(actual.source_price_usdt, candidate["source_price"])
    fx_side = (
        config.fx.usd_usdt_bid if direction is SourceDirection.LONG else config.fx.usd_usdt_ask
    )
    _close(actual.hedge_reference_price_usd * fx_side, candidate["comparable_hedge_price"])
    _close(actual.net_return, original["net_return"][candidate["side"]])
    sign = D(1) if direction is SourceDirection.LONG else D(-1)
    _close(selected.position_ounces, candidate["before"][0])
    _close(hedge.position_ounces, candidate["before"][1])
    _close(selected.position_ounces + sign * actual.source_quantity_ounces, candidate["after"][0])
    _close(hedge.position_ounces - sign * actual.source_quantity_ounces, candidate["after"][1])


@pytest.mark.parametrize("case", _PAYLOAD["maker"]["cases"], ids=lambda case: case["id"])
def test_authenticated_maker_caller(case: dict[str, Any]) -> None:
    values = _values("maker", case)
    sides = {
        side: MakerSideConfig(
            open_quantity_ounces=D(values["open_amount"][side]),
            open_spread=D(values["open_spread"][side]), delta=D(values["delta"][side]),
        ) for side in ("bid", "ask")
    }
    config = MakerEconomicsConfig(
        bid=sides["bid"], ask=sides["ask"], margin_level=D(values["margin_level"]),
        carry=CarryConfig(**{key: D(value) for key, value in values["carry"].items()}),
        fx=FxConfig(**{key: D(value) for key, value in values["fx"].items()}),
        risk=_risk(values["risk"]),
    )
    direction = SourceDirection(values["side"])
    actual = maker_quote(
        direction, _book(values["hedge_book"]),
        tuple(_source(value) for value in values["sources"]),
        tuple(_hedge(value) for value in values["hedges"]), config,
    )
    original = case["expected"]
    if original is None:
        assert actual is None
        return
    assert actual is not None
    assert actual.direction is direction
    assert str(actual.source_account.account_id) == original["source"]
    assert str(actual.hedge_account.account_id) == original["hedge"]
    params = original["params"]
    sign = D(1) if direction is SourceDirection.LONG else D(-1)
    _close(sign * actual.quantity_ounces, params["amount"])
    _close(actual.adjusted_spread, params["spread"])
    fx_side = (
        config.fx.usd_usdt_bid if direction is SourceDirection.LONG else config.fx.usd_usdt_ask
    )
    _close(actual.hedge_reference_price_usd * fx_side, params["base_price"])
    assert D(actual.leverage) == D(params["lev"])
    # Deliberately do not label source_price_usdt or passive clamp as legacy callee outputs.
