"""Read-only realized-trade/commission bridge, not an account cash-flow ledger.

The scope is all owned cached order history, including native Position snapshots.
Funding, swap, other broker fees and unrealized PnL are deliberately not estimated.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.model.enums import OmsType, OrderStatus
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TradeId,
    TraderId,
)
from nautilus_trader.model.position import Position

from py000_nautilus.bitfinex_v1_execution import (
    BitfinexV1ExecutionClient,
    _native_fill_evidence,
)
from py000_nautilus.bitfinex_v1_reports import usd_commission
from py000_nautilus.config import FxConfig
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionClient, Mt5V1ExecutionError


@dataclass(frozen=True, slots=True)
class AccountingCurrency:
    """Observed subtotals remain visible when final history coverage is incomplete."""

    native_observed_realized_pnl: Decimal
    native_booked_cost: Decimal
    native_pnl_embedded_cost: Decimal
    venue_raw_cost: Decimal
    venue_quantized_cost: Decimal
    rounding_delta: Decimal
    provisional_correction: Decimal | None
    final_realized_pnl: Decimal | None


@dataclass(frozen=True, slots=True)
class RunAccountingReport:
    status: Literal["FINAL", "PENDING"]
    pending_reasons: tuple[str, ...]
    pending_trades: int
    unknown_orders: int
    currencies: dict[str, AccountingCurrency]
    usd_usdt_bid: Decimal
    usd_usdt_ask: Decimal
    final_realized_pnl_usdt: Decimal | None
    scope: str = "owned_cached_history:realized_trading_pnl_and_commission"
    fx_method: str = "per_currency_net:positive_USD_bid_negative_USD_ask;valuation_not_exchange"
    excluded_cashflows: tuple[str, ...] = (
        "funding", "swap", "other_broker_fees", "unrealized_pnl",
    )
    reconstructed_closed_cycles: int = 0


@dataclass
class _Amounts:
    realized: Decimal = Decimal(0)
    booked: Decimal = Decimal(0)
    embedded: Decimal = Decimal(0)
    raw: Decimal = Decimal(0)
    quantized: Decimal = Decimal(0)


def _fill_identity(fill: OrderFilled) -> tuple[object, ...]:
    # Native NETTING flips split quantity/commission and create another event UUID.
    return (
        fill.trader_id, fill.strategy_id, fill.account_id, fill.instrument_id,
        fill.client_order_id, fill.venue_order_id, fill.trade_id, fill.position_id,
        fill.order_side, fill.order_type, fill.last_px, fill.currency,
        fill.commission.currency, fill.liquidity_side, fill.ts_event,
    )


def _same_position_cycle(
    actual: Position, expected: Position,
    fills: dict[tuple[ClientOrderId, TradeId], OrderFilled],
    *, snapshot: bool = False,
) -> bool:
    # Only the second half of a native flip gets a freshly generated event UUID.
    left_state, right_state = actual.to_dict(), expected.to_dict()
    if snapshot:
        canonical = expected.events[0].position_id.value
        for state in (left_state, right_state):
            suffix = state["position_id"].removeprefix(f"{canonical}-")
            if str(UUID(suffix, version=4)) != suffix or not actual.is_closed:
                return False
            state["position_id"] = canonical
    # Position.to_dict omits these native calculation-contract fields.
    contract = ("multiplier", "is_inverse", "price_precision", "size_precision",
                "instrument_class", "is_spot_currency")
    if (actual.adjustments or left_state != right_state
            or any(getattr(actual, field) != getattr(expected, field) for field in contract)
            or len(actual.events) != len(expected.events)):
        return False
    for retained, rebuilt in zip(actual.events, expected.events, strict=True):
        left, right = OrderFilled.to_dict(retained), OrderFilled.to_dict(rebuilt)
        original = fills[rebuilt.client_order_id, rebuilt.trade_id]
        if rebuilt.id != original.id:
            left.pop("event_id")
            right.pop("event_id")
        if left != right:
            return False
    return True


def _missing_netting_cycles(
    cache: Cache, fills: dict[tuple[ClientOrderId, TradeId], OrderFilled],
    positions: list[Position], instrument_id: InstrumentId, trader_id: TraderId,
) -> list[Position]:
    """Recover only missing closed cycles using pinned NT 1.231.0 position math.

    Native Redis restores current Positions, not volatile NETTING snapshots.
    This disposable context has no backing, adapters or live bus, and never
    starts or processes orders. An NT upgrade requires hot/cold differential tests.
    """
    source_fills = {key: fill for key, fill in fills.items()
                    if fill.instrument_id == instrument_id}
    observed: defaultdict[tuple[ClientOrderId, TradeId], Decimal] = defaultdict(Decimal)
    for position in positions:
        if position.instrument_id == instrument_id:
            for fill in position.events:
                observed[fill.client_order_id, fill.trade_id] += fill.last_qty.as_decimal()
    if dict(observed) == {key: fill.last_qty.as_decimal() for key, fill in source_fills.items()}:
        return []
    instrument = cache.instrument(instrument_id)
    if instrument is None:
        raise ValueError("missing NETTING instrument")
    groups: defaultdict[PositionId, list[OrderFilled]] = defaultdict(list)
    for fill in source_fills.values():
        groups[fill.position_id].append(fill)
    current = {position.id: position for position in cache.positions()
               if position in positions and position.instrument_id == instrument_id}
    if set(current) != set(groups):
        raise ValueError("NETTING current position coverage differs")
    clock = TestClock()
    local = Cache()
    bus = MessageBus(trader_id=trader_id, clock=clock)
    bus.register("Portfolio.update_position", lambda event: None)
    engine = ExecutionEngine(msgbus=bus, cache=local, clock=clock)
    missing: list[Position] = []
    try:
        for pid, events in groups.items():
            identity = {(fill.trader_id, fill.strategy_id, fill.account_id, fill.instrument_id)
                        for fill in events}
            receipts: dict[int, ClientOrderId] = {}
            last: dict[ClientOrderId, int] = {}
            for fill in events:  # Dict insertion preserves each retained order's event sequence.
                if (len(identity) != 1 or fill.ts_init <= 0
                        or fill.ts_init < last.get(fill.client_order_id, 0)
                        or receipts.get(fill.ts_init, fill.client_order_id)
                        != fill.client_order_id):
                    raise ValueError("NETTING fill reception order is ambiguous")
                receipts[fill.ts_init] = fill.client_order_id
                last[fill.client_order_id] = fill.ts_init
            for original in sorted(events, key=lambda fill: fill.ts_init):
                fill = OrderFilled.from_dict(OrderFilled.to_dict(original))
                position = local.position(pid)
                if position is None or position.is_closed:
                    engine._open_position(instrument, position, fill, OmsType.NETTING)
                elif engine._will_flip_position(position, fill):
                    engine._flip_position(instrument, position, fill, OmsType.NETTING)
                else:
                    engine._update_position(instrument, position, fill, OmsType.NETTING)
            if not _same_position_cycle(current[pid], local.position(pid), source_fills):
                raise ValueError("NETTING current position differs from retained fills")
            closed = local.position_snapshots(pid)
            for existing in cache.position_snapshots(pid):
                matches = [index for index, candidate in enumerate(closed)
                           if _same_position_cycle(
                               existing, candidate, source_fills, snapshot=True,
                           )]
                if len(matches) != 1:
                    raise ValueError("NETTING snapshot differs or is duplicated")
                closed.pop(matches[0])
            if any(not position.is_closed for position in closed):
                raise ValueError("NETTING missing cycle is not closed")
            missing.extend(closed)
        return missing
    finally:
        engine.dispose()
        bus.dispose()


def build_run_accounting_report(
    cache: Cache,
    source: BitfinexV1ExecutionClient,
    hedge: Mt5V1ExecutionClient,
    *,
    trader_id: TraderId,
    strategy_id: StrategyId,
    fx: FxConfig,
    strategy_ids: tuple[StrategyId, ...] | None = None,
) -> RunAccountingReport:
    """Observe retained native/fee facts synchronously, without I/O or state changes.

    A FINAL result is confined to this report's stated scope. It is not a claim
    that unknown funding/swap cash flows or a whole account's net profit are final.
    """
    if any(not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0
           for rate in (fx.usd_usdt_bid, fx.usd_usdt_ask)) or fx.usd_usdt_bid > fx.usd_usdt_ask:
        raise ValueError("accounting FX requires finite positive bid <= ask")
    routes = {
        source._bfx_config.instrument_id: source.account_id,
        hedge._mt5_config.instrument_id: hedge.account_id,
    }
    amounts: defaultdict[str, _Amounts] = defaultdict(_Amounts)
    issues: list[str] = []
    fills: dict[tuple[ClientOrderId, TradeId], OrderFilled] = {}
    owners = {strategy_id} if strategy_ids is None else set(strategy_ids)
    if (not owners or strategy_id not in owners
            or (strategy_ids is not None and len(owners) != len(strategy_ids))):
        raise ValueError("accounting requires distinct explicit strategy owners")
    orders = [order for order in cache.orders() if order.strategy_id in owners]
    for order in orders:
        account = routes.get(order.instrument_id)
        if (account is None or order.trader_id != trader_id
                or order.account_id not in {None, account}):
            issues.append(f"native_order_identity:{order.client_order_id}")
            continue
        if not order.is_closed:
            issues.append(f"unresolved_order:{order.client_order_id}")
        events = [event for event in order.events if isinstance(event, OrderFilled)]
        if (order.trade_ids != [event.trade_id for event in events]
                or sum((event.last_qty.as_decimal() for event in events), Decimal(0))
                != order.filled_qty.as_decimal()):
            issues.append(f"native_order_fill_coverage:{order.client_order_id}")
        for event in events:
            key = event.client_order_id, event.trade_id
            if (key in fills or event.client_order_id != order.client_order_id
                    or event.account_id != account or event.instrument_id != order.instrument_id
                    or event.trader_id != trader_id or event.strategy_id != order.strategy_id
                    or event.position_id is None or event.position_id != order.position_id
                    or event.venue_order_id != order.venue_order_id
                    or event.order_side != order.side or event.last_qty.as_decimal() <= 0):
                issues.append(f"native_fill_identity:{order.client_order_id}")
                continue
            fills[key] = event
            amounts[event.commission.currency.code].booked += event.commission.as_decimal()

    quantities: defaultdict[tuple[ClientOrderId, TradeId], Decimal] = defaultdict(Decimal)
    commissions: defaultdict[tuple[ClientOrderId, TradeId], Decimal] = defaultdict(Decimal)
    positions = [position for position in cache.positions() + cache.position_snapshots()
                 if position.strategy_id in owners]
    reconstructed: list[Position] = []
    if not issues:
        try:
            reconstructed = _missing_netting_cycles(
                cache, fills, positions, source._bfx_config.instrument_id, trader_id,
            )
            positions += reconstructed
        except (ValueError, ArithmeticError, KeyError, RuntimeError):
            issues.append("native_position_history_incomplete")
    for position in positions:
        if (position.trader_id != trader_id
                or position.account_id != routes.get(position.instrument_id)):
            issues.append(f"native_position_identity:{position.id}")
            continue
        if position.adjustments or position.realized_pnl is None:
            issues.append(f"native_position_adjustment_or_unknown_pnl:{position.id}")
        elif position.realized_pnl.currency != position.settlement_currency:
            issues.append(f"native_position_pnl_currency:{position.id}")
        else:
            amounts[position.settlement_currency.code].realized += (
                position.realized_pnl.as_decimal()
            )
        position_fees: defaultdict[str, Decimal] = defaultdict(Decimal)
        for event in position.events:
            key = event.client_order_id, event.trade_id
            original = fills.get(key)
            if (original is None or _fill_identity(event) != _fill_identity(original)
                    or event.last_qty.as_decimal() <= 0):
                issues.append(f"native_position_fill_identity:{position.id}")
                continue
            quantities[key] += event.last_qty.as_decimal()
            commissions[key] += event.commission.as_decimal()
            position_fees[event.commission.currency.code] += event.commission.as_decimal()
        actual_fees = {fee.currency.code: fee.as_decimal() for fee in position.commissions()}
        if dict(position_fees) != actual_fees:
            issues.append(f"native_position_commission_coverage:{position.id}")
        amounts[position.settlement_currency.code].embedded += actual_fees.get(
            position.settlement_currency.code, Decimal(0),
        )
    if set(quantities) != set(fills) or any(
        quantities[key] != event.last_qty.as_decimal()
        or commissions[key] != event.commission.as_decimal() for key, event in fills.items()
    ):
        issues.append("native_position_history_incomplete")

    pending = unknown = 0
    if source.accounting_incomplete:
        issues.append("source_fee_recording_incomplete")
    source_cids = {
        order.client_order_id for order in orders
        if order.instrument_id == source._bfx_config.instrument_id
    }
    for known_binding in source._cid_store.bindings:
        if cache.order(ClientOrderId(known_binding.client_order_id)) is None:
            issues.append(f"source_unattributed_cid:{known_binding.client_order_id}")
            unknown += 1
    for cid in sorted(source_cids, key=str):
        order = cache.order(cid)
        binding = source._cid_store.binding_for_client(cid.value)
        if (binding is None and order is not None and order.status == OrderStatus.DENIED
                and order.filled_qty == 0):
            continue  # An admission denial need not have allocated a venue CID.
        try:
            summary = source.fee_summary(cid)
        except (ValueError, ArithmeticError):
            issues.append(f"source_fee_facts_invalid:{cid}")
            unknown += 1
            continue
        pending += summary.pending_trades
        unknown += summary.unknown_orders
        if not summary.complete:
            issues.append(f"source_fees_incomplete:{cid}")
        for currency, value in summary.currencies.items():
            amounts[currency].raw += value.raw_cost
            amounts[currency].quantized += value.quantized_cost
        metadata = None if binding is None else source._cid_store.fee_metadata_for_cid(binding.cid)
        if metadata is not None and (
            source._cid_store.account_id != source.account_id.value
            or metadata.instrument_id != source._bfx_config.instrument_id.value
            or metadata.raw_symbol != source._bfx_config.raw_symbol
            or order is None or str(metadata.venue_order_id) != str(order.venue_order_id)
        ):
            issues.append(f"source_fee_scope_differs:{cid}")
        native = {str(key[1]): fill for key, fill in fills.items() if key[0] == cid}
        if metadata is not None and (
            set(native) != {fill.trade_id for fill in metadata.native_fills}
            or any(fill.trade_id not in native or _native_fill_evidence(
                native[fill.trade_id], fill.native_fill_origin,
            ) != fill for fill in metadata.native_fills)
        ):
            issues.append(f"source_native_fee_evidence_differs:{cid}")

    hedge_keys = {key for key, fill in fills.items()
                  if fill.instrument_id == hedge._mt5_config.instrument_id}
    try:
        journal_fees = hedge.raw_commission_cashflows()
    except (ValueError, ArithmeticError, KeyError, Mt5V1ExecutionError):
        journal_fees = {}
        issues.append("hedge_commission_history_unresolved")
    owned_journal_keys = set()
    for key, cashflow in journal_fees.items():
        order = cache.order(key[0])
        if order is None:
            issues.append(f"hedge_unattributed_cid:{key[0]}")
            unknown += 1
            continue
        if order.strategy_id not in owners:
            continue
        owned_journal_keys.add(key)
        cost = usd_commission(cashflow)
        amounts["USD"].raw -= cashflow
        amounts["USD"].quantized += cost.as_decimal()
        if key not in fills or fills[key].commission != cost:
            issues.append(f"hedge_native_commission_differs:{key[0]}")
    if owned_journal_keys != hedge_keys:
        issues.append("hedge_commission_coverage_incomplete")
    if set(amounts) - {"USD", "USDT"}:
        issues.append("unsupported_accounting_currency")
    complete = not issues and pending == unknown == 0
    currencies = {
        currency: AccountingCurrency(
            value.realized, value.booked, value.embedded, value.raw, value.quantized,
            value.raw - value.quantized,
            value.quantized - value.booked if complete else None,
            value.realized + value.embedded - value.raw if complete else None,
        ) for currency, value in sorted(amounts.items())
    }
    final = None
    if complete:
        usd = currencies.get("USD")
        net_usd = Decimal(0) if usd is None else usd.final_realized_pnl
        assert net_usd is not None
        usdt = currencies.get("USDT")
        net_usdt = Decimal(0) if usdt is None else usdt.final_realized_pnl
        assert net_usdt is not None
        final = net_usdt + net_usd * (fx.usd_usdt_bid if net_usd >= 0 else fx.usd_usdt_ask)
    return RunAccountingReport(
        "FINAL" if complete else "PENDING", tuple(dict.fromkeys(issues)), pending, unknown,
        currencies, fx.usd_usdt_bid, fx.usd_usdt_ask, final,
        scope=("shared_owned_cached_history:native_virtual_realized_trading_pnl_and_commission;"
               "not_venue_realized_cashflow" if len(owners) > 1
               else "owned_cached_history:realized_trading_pnl_and_commission"),
        reconstructed_closed_cycles=len(reconstructed),
    )
