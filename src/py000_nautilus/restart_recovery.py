"""One startup check over native/venue facts and the existing business stores.

This does not resume pending requests or clear old HOLDs. A receipt captured
before startup can authorize final settlement of already completed bound legs.
Its caller owns the timeout and keeps strategy callbacks gated until it returns.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import partial
from math import isclose
from typing import cast

from nautilus_trader.cache.cache import Cache
from nautilus_trader.execution.reports import ExecutionMassStatus, PositionStatusReport
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId, StrategyId, TraderId
from nautilus_trader.model.orders import Order

from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionClient
from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.hedge_projection import project_hedge_fills
from py000_nautilus.live_cache import validate_native_cache
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.models import BusinessOrderSide, ObligationStatus
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionClient
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.source_projection import project_source_fills
from py000_nautilus.store import JsonStateStore

_HELD = "startup facts projected; business recovery remains held"


def _views(store: JsonStateStore | MakerStateStore) -> tuple[JsonStateStore, ...]:
    return tuple(store.stores.values()) if isinstance(store, MakerStateStore) else (store,)


@dataclass(slots=True)
class _StartupReceipt:
    store: JsonStateStore | MakerStateStore
    eligible: bool
    halts: dict[JsonStateStore, str] = field(default_factory=dict)
    freezes: dict[JsonStateStore, str] = field(default_factory=dict)
    publication_failed: bool = False

    def fail_publication(self) -> None:
        self.publication_failed = True

    def record_halt(self, view: JsonStateStore, reason: str) -> None:
        if self.eligible:
            self.halts[view] = reason

    def check(self, store: JsonStateStore | MakerStateStore) -> None:
        if self.store is not store:
            raise ValueError("startup receipt belongs to another business store")
        if self.publication_failed:
            raise RuntimeError("startup receipt invalid after publication sync failure")
        if self.eligible and any(
            view.halt_reason != self.halts.get(view)
            or view.source_freeze_reason != self.freezes.get(view)
            for view in _views(store)
        ):
            raise ValueError("startup pause is not owned by this startup receipt")

    def claim_projected(self, before: tuple[tuple[str | None, str | None], ...]) -> None:
        if not self.eligible:
            return
        for view, (halt, freeze) in zip(_views(self.store), before, strict=True):
            if halt is None and view.halt_reason == _HELD:
                self.halts[view] = _HELD
            if freeze is None and view.source_freeze_reason == _HELD:
                self.freezes[view] = _HELD


def capture_startup_receipt(store: JsonStateStore | MakerStateStore) -> _StartupReceipt:
    """Capture before on_start; old text/status never grants recovery permission."""
    if type(store) is not JsonStateStore and not isinstance(store, MakerStateStore):
        raise TypeError("startup receipt requires a whole Maker owner or JsonStateStore")
    forbidden = {"UNKNOWN", "BLOCKED", "REJECTED"}
    receipt = _StartupReceipt(store, all(
        view.halt_reason is None and view.source_freeze_reason is None
        and all(record.status not in forbidden for record in view.source_orders())
        and all(intent.status.value not in forbidden for intent in view.intents())
        for view in _views(store)
    ))
    for view in _views(store):
        view._restart_halt_recorder = partial(receipt.record_halt, view)
        view._restart_failure_recorder = receipt.fail_publication
    return receipt


def has_business_history(store: JsonStateStore | MakerStateStore) -> bool:
    return any(
        view.source_orders() or view.intents() or view.halt_reason or view.source_freeze_reason
        or view.rounding_residual_ounces or view._state.seen_source_fills
        or view._state.seen_hedge_fills
        for view in _views(store)
    )


def _observed(cache: Cache, source: BitfinexV1ExecutionClient,
              hedge: Mt5V1ExecutionClient) -> object:
    # Immutable event identities detect even net-zero native changes. Fee evidence
    # also changes on retired-order TU, which emits no native event/action revision.
    orders = frozenset((
        order.client_order_id, tuple(event.id for event in order.events),
        order.status, order.quantity, order.filled_qty, order.venue_order_id,
        cache.client_id(order.client_order_id), cache.position_id(order.client_order_id),
    ) for order in cache.orders())
    positions = frozenset((
        position.id, tuple(event.id for event in position.events),
        position.signed_decimal_qty(), position.avg_px_open,
    ) for position in cache.positions())
    current = source._margin_position
    source_position = None if current is None else (
        current.symbol, current.status, current.quantity, current.base_price, current.position_id,
    )
    snapshot = hedge._snapshot
    tickets = [] if snapshot is None else cast(list[JsonObject], snapshot["positions"])
    # REST/poll observation timestamps and mark-to-market PnL are not executions.
    hedge_positions = sorted((tuple(row.get(field) for field in (
        "identifier", "ticket", "symbol", "side", "volume_lots", "price_open", "magic",
    )) for row in tickets), key=repr)
    return (orders, positions, source._cid_store.fee_metadata,
            source._account_action_revision, source._last_auth_nonce, source_position,
            hedge._identity, hedge._cursor, deepcopy(hedge_positions),
            hedge.pending_client_order_ids)


def _complete_orders(mass: ExecutionMassStatus, orders: list[Order],
                     instrument_id: InstrumentId) -> None:
    reported = list(mass.order_reports.values())
    expected = {order.client_order_id: order for order in orders}
    if (len(reported) != len(expected)
            or {report.client_order_id for report in reported} != set(expected)):
        raise ValueError("startup order reports do not cover the complete native history")
    for report in reported:
        order = expected[report.client_order_id]
        if (not order.is_closed or order.venue_order_id is None
                or report.instrument_id != instrument_id or report.account_id != order.account_id
                or report.venue_order_id != order.venue_order_id
                or report.order_status != order.status or report.quantity != order.quantity
                or report.filled_qty != order.filled_qty):
            raise ValueError("startup order terminal facts do not match native history")
        fills = mass.fill_reports.get(report.venue_order_id, [])
        native = [event for event in order.events if isinstance(event, OrderFilled)]
        if (len(fills) != len(native)
                or {fill.trade_id for fill in fills} != {event.trade_id for event in native}
                or sum((fill.last_qty.as_decimal() for fill in fills), Decimal(0))
                != order.filled_qty.as_decimal()):
            raise ValueError("startup order lacks its complete real trade evidence")
    if any(venue_id not in {order.venue_order_id for order in orders}
           for venue_id, fills in mass.fill_reports.items() if fills):
        raise ValueError("startup fill reports contain an unbound order")


def _positions(cache: Cache, mass: ExecutionMassStatus, instrument_id: InstrumentId,
               *, netting: bool) -> Decimal:
    reports = mass.position_reports.get(instrument_id, [])
    if any(key != instrument_id and rows for key, rows in mass.position_reports.items()):
        raise ValueError("startup position reports contain another instrument")
    if any(report.account_id != mass.account_id for report in reports):
        raise ValueError("startup position report account differs")
    positions = cache.positions_open(instrument_id=instrument_id, account_id=mass.account_id)
    total = sum((position.signed_decimal_qty() for position in positions), Decimal(0))

    def check_price(report: PositionStatusReport, price: float) -> None:
        if report.signed_decimal_qty != 0 and (
            report.avg_px_open is None or not isclose(price, float(report.avg_px_open))
        ):
            raise ValueError("startup position average price differs")

    if netting:
        if (len(reports) != int(total != 0)
                or sum((report.signed_decimal_qty for report in reports), Decimal(0)) != total):
            raise ValueError("startup NETTING position differs")
        if total:
            weight = sum((abs(position.signed_decimal_qty()) for position in positions), Decimal(0))
            price = sum((Decimal(str(position.avg_px_open)) * abs(position.signed_decimal_qty())
                         for position in positions), Decimal(0)) / weight
            check_price(reports[0], float(price))
    else:
        by_id = {report.venue_position_id: report for report in reports}
        if None in by_id or len(by_id) != len(reports):
            raise ValueError("startup HEDGING position identity is missing or duplicated")
        if {key for key, report in by_id.items() if report.signed_decimal_qty} != {
            position.id for position in positions
        }:
            raise ValueError("startup HEDGING ticket set differs")
        for position in positions:
            report = by_id[position.id]
            if report.signed_decimal_qty != position.signed_decimal_qty():
                raise ValueError("startup HEDGING ticket quantity differs")
            check_price(report, position.avg_px_open)
    return total


def _settle_completed(
    store: JsonStateStore | MakerStateStore, source_orders: list[Order],
    hedge_orders: list[Order], receipt: _StartupReceipt,
) -> None:
    """Finalize only fully proven facts; the caller already checked both projectors."""
    receipt.check(store)
    if not receipt.eligible:
        raise ValueError("startup receipt does not authorize business settlement")
    views = _views(store)
    sources = {order.client_order_id.value: order for order in source_orders}
    hedges = {order.client_order_id.value: order for order in hedge_orders}
    for view in views:
        if any(not sources[record.client_order_id].is_closed
               or record.filled_ounces != sources[record.client_order_id].filled_qty.as_decimal()
               for record in view.source_orders()):
            raise ValueError("startup source facts are not fully terminal")
        for intent in view.intents():
            ids = intent.hedge_order_ids or (
                (intent.hedge_client_order_id,) if intent.hedge_client_order_id else ()
            )
            if (intent.hedge_filled_ounces != intent.hedge_quantity_ounces
                    or len(ids) != (len(intent.hedge_plan) if intent.hedge_plan else 1)
                    or any(hedges[cid].status != OrderStatus.FILLED for cid in ids)):
                raise ValueError("startup hedge still has an incomplete or unbound leg")
    previous = deepcopy(views[0]._state)
    maker_previous = store._snapshot() if isinstance(store, MakerStateStore) else None
    before = store._to_payload()
    try:
        for view in views:
            view._state.source_orders = {
                cid: replace(record, status=sources[cid].status.name)
                for cid, record in view._state.source_orders.items()
            }
            view._state.active_source_order_id = None
            view._state.hedge_intents = {
                identity: (
                    replace(intent, status=ObligationStatus.COMPLETED,
                            hedge_client_order_id=None, hedge_leg_index=len(intent.hedge_plan),
                            hedge_leg_filled_ounces=Decimal(0))
                    if intent.hedge_plan else replace(intent, status=ObligationStatus.COMPLETED)
                ) for identity, intent in view._state.hedge_intents.items()
            }
            view._state.halt_reason = None
            view._state.source_freeze_reason = None
        if isinstance(store, MakerStateStore):
            store._validate()
        if any(not view.can_submit_source() or not view.cycle_evidence_complete()
               for view in views):
            raise ValueError("startup settled candidate has inadmissible business residuals")
        if store._to_payload() != before:
            views[0]._persist()
    except ParentDirectorySyncError:
        receipt.fail_publication()
        raise  # The finalized candidate is published, but this process must stay gated.
    except Exception:
        if isinstance(store, MakerStateStore) and maker_previous is not None:
            store._restore(maker_previous)
        else:
            views[0]._state = previous
        raise
    receipt.halts.clear()
    receipt.freezes.clear()


async def reconcile_startup(
    cache: Cache, store: JsonStateStore | MakerStateStore, *,
    trader_id: TraderId, strategy_id: StrategyId,
    source: BitfinexV1ExecutionClient, hedge: Mt5V1ExecutionClient,
    source_instrument_id: InstrumentId, hedge_instrument_id: InstrumentId,
    receipt: _StartupReceipt | None = None,
) -> None:
    """Prove complete history; only a fresh eligible receipt can finalize held facts."""
    if receipt is not None:
        receipt.check(store)
    business_before = store._to_payload()
    before = _observed(cache, source, hedge)
    async with asyncio.TaskGroup() as group:
        source_task = group.create_task(source.generate_mass_status(None))
        hedge_task = group.create_task(hedge.generate_mass_status(None))
    source_mass, hedge_mass = source_task.result(), hedge_task.result()
    if before != _observed(cache, source, hedge):
        raise ValueError("startup execution facts changed during report collection")
    if business_before != store._to_payload():
        raise ValueError("startup business facts changed during report collection")
    if (source_mass is None or hedge_mass is None or not source.is_connected
            or not hedge.is_connected or source.execution_hold_reason is not None
            or not source.accounting_ready or not hedge.execution_admitted
            or not hedge._snapshot_refresh_healthy):
        raise RuntimeError("startup execution facts are not ready")
    if (source_mass.account_id != source.account_id or source_mass.client_id != source.id
            or hedge_mass.account_id != hedge.account_id or hedge_mass.client_id != hedge.id):
        raise ValueError("startup report route differs")
    validate_native_cache(cache, trader_id=trader_id, strategy_id=strategy_id, routes={
        source_instrument_id: (source.account_id, source.id),
        hedge_instrument_id: (hedge.account_id, hedge.id),
    })
    source_orders = cast(list[Order], cache.orders(instrument_id=source_instrument_id))
    hedge_orders = cast(list[Order], cache.orders(instrument_id=hedge_instrument_id))
    if {binding.client_order_id for binding in source._cid_store.bindings} != {
        order.client_order_id.value for order in source_orders
    } or not source.fee_summary().complete:
        raise ValueError("startup source CID history or final fee evidence is incomplete")
    _complete_orders(source_mass, source_orders, source_instrument_id)
    _complete_orders(hedge_mass, hedge_orders, hedge_instrument_id)
    source_position = _positions(cache, source_mass, source_instrument_id, netting=True)
    hedge_position = _positions(cache, hedge_mass, hedge_instrument_id, netting=False)
    # A newer private observation may survive a rejected REST enrichment. A
    # stable pre/post snapshot alone does not certify the returned older report.
    current = source._margin_position
    if (not source._margin_positions_current or not source._margin_positions_complete
            or (current.quantity if current is not None else Decimal(0)) != source_position
            or (current is not None and current.base_price !=
                source_mass.position_reports[source_instrument_id][0].avg_px_open)):
        raise ValueError("startup source position report is not the current private observation")
    snapshot = hedge._require_snapshot()
    contract_size = Decimal(str(hedge._report_instrument().lot_size))
    current_tickets = {
        str(row["identifier"]): (
            Decimal(str(row["volume_lots"])) * contract_size
            * (1 if row["side"] == "buy" else -1), Decimal(str(row["price_open"])),
        ) for row in cast(list[JsonObject], snapshot["positions"])
    }
    if current_tickets != {
        str(report.venue_position_id): (report.signed_decimal_qty, report.avg_px_open)
        for report in hedge_mass.position_reports.get(hedge_instrument_id, [])
        if report.signed_decimal_qty
    }:
        raise ValueError("startup hedge reports differ from current tickets")
    views = _views(store)
    try:
        pauses = tuple((view.halt_reason, view.source_freeze_reason) for view in views)
        added_source = project_source_fills(
            store, source_orders, source_instrument_id=source_instrument_id,
            trader_id=trader_id, strategy_id=strategy_id, reason=_HELD,
        )
        if receipt is not None:
            receipt.claim_projected(pauses)
        pauses = tuple((view.halt_reason, view.source_freeze_reason) for view in views)
        added_hedge = project_hedge_fills(
            store, hedge_orders, hedge_instrument_id=hedge_instrument_id,
            trader_id=trader_id, strategy_id=strategy_id, reason=_HELD,
        )
        if receipt is not None:
            receipt.claim_projected(pauses)
    except ParentDirectorySyncError:
        if receipt is not None:
            receipt.fail_publication()
        raise
    source_filled = sum((record.filled_ounces * (
        1 if record.side is BusinessOrderSide.BUY else -1
    ) for view in views for record in view.source_orders()), Decimal(0))
    hedge_filled = sum((intent.hedge_filled_ounces * (
        1 if intent.hedge_side is BusinessOrderSide.BUY else -1
    ) for view in views for intent in view.intents()), Decimal(0))
    if source_position != source_filled or hedge_position != hedge_filled:
        raise ValueError("startup positions differ from complete business fills")
    if receipt is not None and receipt.eligible:
        _settle_completed(store, source_orders, hedge_orders, receipt)
        return
    native = {order.client_order_id.value: order for order in source_orders}
    if added_source or added_hedge or any(
        not view.can_submit_source() or not view.cycle_evidence_complete()
        or any(record.status != native[record.client_order_id].status.name
               for record in view.source_orders())
        or any(intent.status is not ObligationStatus.COMPLETED
               or intent.hedge_filled_ounces != intent.hedge_quantity_ounces
               for intent in view.intents())
        for view in views
    ):
        raise ValueError("startup business history remains held or unsettled")
