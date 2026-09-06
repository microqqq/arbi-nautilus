"""One startup check over native/venue facts and the existing business stores.

This never resends existing requests. A receipt captured
before startup can finalize completed bound legs and, for ordinary Maker/Taker,
release proven unbound remainders to its existing dispatcher. Explicit operator
review can qualify an old HOLD or one zero-fill rejection for a new request ID.
The choice alone proves no execution facts. Its caller owns
the timeout and keeps strategy callbacks gated until it returns.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import partial
from math import isclose
from typing import cast

from nautilus_trader.cache.cache import Cache
from nautilus_trader.execution.reports import ExecutionMassStatus, PositionStatusReport
from nautilus_trader.model.enums import OrderStatus, PositionSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TraderId,
)
from nautilus_trader.model.orders import Order

from py000_nautilus.bitfinex_v1_execution import BitfinexV1ExecutionClient
from py000_nautilus.config import HedgeAccountRoute, MakerStrategyConfig, TakerStrategyConfig
from py000_nautilus.durability import ParentDirectorySyncError
from py000_nautilus.hedge_projection import project_hedge_fills
from py000_nautilus.live_cache import validate_native_cache
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.margin import mt5_hedge_account
from py000_nautilus.models import (
    BusinessOrderSide,
    HedgeIntent,
    ObligationStatus,
    RejectedHedgeAttempt,
)
from py000_nautilus.mt5_v1_data import Mt5V1DataClient, status_from_snapshot
from py000_nautilus.mt5_v1_execution import Mt5V1ExecutionClient
from py000_nautilus.mt5_v1_protocol import JsonObject
from py000_nautilus.source_projection import project_source_fills
from py000_nautilus.store import JsonStateStore

_HELD = "startup facts projected; business recovery remains held"


@dataclass(frozen=True, slots=True)
class StartupRecoveryOptions:
    """An operator's explicit choice for this startup, never saved as configuration."""

    resume_held: bool = False
    rejected_hedge_order_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.resume_held) is not bool:
            raise TypeError("resume_held must be a bool")
        cid = self.rejected_hedge_order_id
        if cid is not None and (
            not self.resume_held or not isinstance(cid, str) or not cid.strip() or "|" in cid
        ):
            raise ValueError("rejected hedge retry requires explicit review and one order ID")


def _views(store: JsonStateStore | MakerStateStore) -> tuple[JsonStateStore, ...]:
    return tuple(store.stores.values()) if isinstance(store, MakerStateStore) else (store,)


@dataclass(slots=True)
class _StartupReceipt:
    store: JsonStateStore | MakerStateStore
    eligible: bool
    halts: dict[JsonStateStore, str] = field(default_factory=dict)
    freezes: dict[JsonStateStore, str] = field(default_factory=dict)
    publication_failed: bool = False
    maker_cycle_freeze: bool = False
    options: StartupRecoveryOptions = field(default_factory=StartupRecoveryOptions)
    review_revoked: bool = False
    retry_deadline: float | None = None

    def revoke_review(self) -> None:
        self.review_revoked = True

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
        if self.review_revoked:
            raise RuntimeError("startup review revoked by a new pause or failure")
        if isinstance(store, MakerStateStore):
            if store._freeze_publication_failed:
                raise RuntimeError("startup receipt invalid after Maker freeze publication failure")
            if self.eligible and store.cycle_freeze_only != self.maker_cycle_freeze:
                raise ValueError("Maker cycle freeze is no longer owned by this startup receipt")
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


def capture_startup_receipt(
    store: JsonStateStore | MakerStateStore,
    options: StartupRecoveryOptions | None = None,
) -> _StartupReceipt:
    """Capture before on_start; old text/status never grants recovery permission."""
    if type(store) is not JsonStateStore and not isinstance(store, MakerStateStore):
        raise TypeError("startup receipt requires a whole Maker owner or JsonStateStore")
    options = options or StartupRecoveryOptions()
    reviewed = options.resume_held
    forbidden = {"UNKNOWN", "BLOCKED", "REJECTED"}
    bound_statuses = {ObligationStatus.SUBMITTING, ObligationStatus.SUBMITTED,
                      ObligationStatus.ACCEPTED}
    cycle_freeze = (
        isinstance(store, MakerStateStore) and store.cycle_freeze_only
        and all(view.source_freeze_reason for view in _views(store))
        and len({view.source_freeze_reason for view in _views(store)}) == 1
    )
    receipt = _StartupReceipt(store, all(
        (reviewed or (view.halt_reason is None
                     and (view.source_freeze_reason is None or cycle_freeze)))
        and all(reviewed or record.status not in forbidden for record in view.source_orders())
        and all(
            (reviewed or intent.status.value not in forbidden)
            and (intent.status is not ObligationStatus.PENDING
                 or intent.hedge_client_order_id is None)
            and (intent.status not in bound_statuses or intent.hedge_client_order_id is not None)
            for intent in view.intents()
        )
        for view in _views(store)
    ) and not (isinstance(store, MakerStateStore) and (
        store._freeze_publication_failed or (store.cycle_freeze_only and not cycle_freeze)
    )),
        maker_cycle_freeze=cycle_freeze, options=options,
        retry_deadline=time.monotonic() + 30 if options.rejected_hedge_order_id else None)
    if receipt.eligible:
        receipt.halts = {view: view.halt_reason for view in _views(store) if view.halt_reason}
        receipt.freezes = {view: view.source_freeze_reason for view in _views(store)
                          if view.source_freeze_reason}
    for view in _views(store):
        view._revoke_restart_permission()
        view._restart_halt_recorder = partial(receipt.record_halt, view)
        view._restart_failure_recorder = receipt.fail_publication
        view._restart_pause_revoker = receipt.revoke_review if reviewed else None
    return receipt


def has_business_history(store: JsonStateStore | MakerStateStore) -> bool:
    return any(
        view.source_orders() or view.intents() or view.halt_reason or view.source_freeze_reason
        or view.rounding_residual_ounces or view._state.seen_source_fills
        or view._state.seen_hedge_fills
        for view in _views(store)
    )


def describe_business_recovery(store: JsonStateStore | MakerStateStore) -> dict[str, object]:
    """Inspect existing local custody only; no venue or native-cache claim is made."""
    return {
        "outcome": "RECOVERY_INSPECTED", "path": str(store.path),
        "native_and_venue_checked": False,
        "views": [{
            "halt": view.halt_reason, "source_freeze": view.source_freeze_reason,
            "active_source_order_id": view.active_source_order_id,
            "source_orders": [{"client_order_id": record.client_order_id,
                               "status": record.status, "filled_ounces": record.filled_ounces}
                              for record in view.source_orders()],
            "unfinished_hedges": [{
                "intent_id": intent.intent_id, "status": intent.status.value,
                "current_order_id": intent.hedge_client_order_id,
                "all_order_ids": intent.hedge_order_ids,
                "remaining_ounces": intent.hedge_quantity_ounces - intent.hedge_filled_ounces,
                "remaining_legs": len(intent.hedge_plan) - intent.hedge_leg_index,
                "rejected_retry_consumed": intent.rejected_attempt is not None,
            } for intent in view.intents() if intent.status is not ObligationStatus.COMPLETED],
        } for view in _views(store)],
        "required_proof": [
            "complete native order, fill, position and source CID history",
            "matching current venue reports and final source commissions",
            "explicit --resume-held to review an old pause in this run",
            "retry additionally requires exact zero-fill rejection and current execution capacity",
        ],
    }


def check_rejected_retry_execution(
    cache: Cache, hedge: Mt5V1ExecutionClient, intent: HedgeIntent, order: Order,
    *, config: MakerStrategyConfig | TakerStrategyConfig, data: Mt5V1DataClient,
) -> None:
    """Read current adapter facts; the ordinary dispatcher still rechecks before send."""
    if not data.is_connected or not data.snapshot_refresh_healthy:
        raise ValueError("rejected hedge retry requires a healthy market data channel")
    now = hedge._clock.timestamp_ns()
    snapshot = hedge._require_snapshot()
    flags = cast(JsonObject, snapshot["authority_flags"])
    status = status_from_snapshot(snapshot, config.hedge_instrument_id, ts_init=now)
    if (status.is_trading is not True or not 0 <= now - status.ts_event <= config.max_cost_age_ns
            or any(flags[name] is not True for name in (
                "account_trade_allowed", "account_trade_expert", "mql_trade_allowed",
                "terminal_connected", "terminal_trade_allowed",
            ))):
        raise ValueError("rejected hedge retry requires current trading permission and session")
    if not intent.hedge_plan or intent.hedge_leg_index >= len(intent.hedge_plan):
        raise ValueError("rejected hedge retry requires its explicit unfinished plan")
    leg = intent.hedge_plan[intent.hedge_leg_index]
    trade_mode = flags["symbol_trade_mode"]
    if not leg.is_close and (
        trade_mode == 3 or (trade_mode == 1 and leg.side is BusinessOrderSide.SELL)
        or (trade_mode == 2 and leg.side is BusinessOrderSide.BUY)
    ):
        raise ValueError("rejected hedge retry side is disabled by the current symbol mode")
    hedge._prepare(
        order, position_id=PositionId(leg.position_id) if leg.position_id else None,
        planned=True, expected_position_ounces=leg.expected_position_quantity_ounces,
    )
    quote = cache.quote_tick(config.hedge_instrument_id)
    native = hedge.get_account()
    if quote is None or native is None:
        raise ValueError("rejected hedge retry lacks a current quote or account")
    # MT5 PUB carries prices, not depth; its native mapper correctly reports
    # unknown sizes as zero. Capacity comes from account facts and the plan.
    if not 0 < quote.bid_price.as_decimal() <= quote.ask_price.as_decimal():
        raise ValueError("rejected hedge retry lacks an actionable bid")
    route = (config.hedge_accounts[0] if isinstance(config, MakerStrategyConfig)
             else HedgeAccountRoute(
                 account_id=config.hedge_account_id, client_id=config.hedge_client_id,
                 max_long_ounces=config.hedge_max_long_ounces,
                 max_short_ounces=config.hedge_max_short_ounces,
             ))
    account = mt5_hedge_account(
        native.last_event, route=route, symbol=hedge._mt5_config.expected_symbol,
        stream_id=hedge._mt5_config.expected_stream_id,
        margin_target=config.economics.margin_level,
        max_abs_ounces=config.economics.risk.hedge_max_abs, now_ns=now,
        max_account_age_ns=config.max_cost_age_ns,
        client_ready=(hedge.execution_admitted
                      and hedge.account_capacity_ready(config.max_cost_age_ns)),
        ask=quote.ask_price.as_decimal(), ask_ts_ns=quote.ts_event,
        max_quote_age_ns=config.max_quote_age_ns, ask_actionable=True,
    )
    if account is None or (not leg.is_close and leg.quantity_ounces > (
        account.max_long_ounces if leg.side is BusinessOrderSide.BUY else account.max_short_ounces
    )):
        raise ValueError("rejected hedge retry lacks fresh account capacity")


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
                     instrument_id: InstrumentId, *,
                     rejected_venue_ids: dict[str, str] | None = None) -> None:
    reported = list(mass.order_reports.values())
    expected = {order.client_order_id: order for order in orders}
    if (len(reported) != len(expected)
            or {report.client_order_id for report in reported} != set(expected)):
        raise ValueError("startup order reports do not cover the complete native history")
    for report in reported:
        order = expected[report.client_order_id]
        rejected_id = (rejected_venue_ids or {}).get(order.client_order_id.value)
        venue_matches = report.venue_order_id == order.venue_order_id
        if (order.venue_order_id is None and order.status == OrderStatus.REJECTED
                and order.filled_qty.as_decimal() == 0 and not order.trade_ids
                and rejected_id is not None):
            # A native rejection has no accepted venue order. The MT5 adapter's
            # complete EA journal authenticates its deterministic report-only ID.
            venue_matches = report.venue_order_id.value == rejected_id
        if (not order.is_closed or not venue_matches
                or report.instrument_id != instrument_id or report.account_id != order.account_id
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
        side = (PositionSide.LONG if total > 0 else
                PositionSide.SHORT if total < 0 else PositionSide.FLAT)
        # The BFX mapper emits one explicit FLAT report even for an empty REST result.
        if (len(reports) != 1 or reports[0].position_side != side
                or reports[0].quantity.as_decimal() != abs(total)
                or reports[0].signed_decimal_qty != total):
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
    hedge_orders: list[Order], receipt: _StartupReceipt, *, resume_unbound: bool = False,
    rejected_retry_ready: bool = False,
) -> None:
    """Finalize proven facts, optionally releasing the existing unbound remainder.

    Both projectors and the complete native/venue history must already have passed.
    """
    receipt.check(store)
    if not receipt.eligible:
        raise ValueError("startup receipt does not authorize business settlement")
    views = _views(store)
    sources = {order.client_order_id.value: order for order in source_orders}
    hedges = {order.client_order_id.value: order for order in hedge_orders}
    candidates: dict[str, HedgeIntent] = {}
    target = receipt.options.rejected_hedge_order_id
    targets = [intent for view in views for intent in view.intents()
               if target is not None and target in intent.hedge_order_ids]
    if target is not None and len(targets) != 1:
        raise ValueError("reviewed rejected order does not identify exactly one obligation")
    for view in views:
        if any(not sources[record.client_order_id].is_closed
               or record.filled_ounces != sources[record.client_order_id].filled_qty.as_decimal()
               for record in view.source_orders()):
            raise ValueError("startup source facts are not fully terminal")
        for intent in view.intents():
            if target is not None and target in intent.hedge_order_ids:
                if intent.rejected_attempt is None:
                    rejected = hedges.get(target)
                    if (not resume_unbound or not rejected_retry_ready
                            or receipt.retry_deadline is None
                            or time.monotonic() > receipt.retry_deadline
                            or not intent.hedge_plan
                            or intent.hedge_client_order_id != target
                            or intent.hedge_leg_filled_ounces != 0
                            or rejected is None or rejected.status != OrderStatus.REJECTED
                            or rejected.filled_qty.as_decimal() != 0 or rejected.trade_ids
                            or any(isinstance(event, OrderFilled) for event in rejected.events)):
                        raise ValueError("reviewed hedge is not a qualified zero-fill rejection")
                    intent = replace(
                        intent,
                        rejected_attempt=RejectedHedgeAttempt(target, intent.hedge_leg_index),
                        hedge_client_order_id=None, status=ObligationStatus.PENDING,
                    )
                elif intent.rejected_attempt.client_order_id != target:
                    raise ValueError("hedge obligation already consumed its one rejected retry")
            if intent.rejected_attempt is not None:
                rejected = hedges.get(intent.rejected_attempt.client_order_id)
                if (rejected is None or rejected.status != OrderStatus.REJECTED
                        or rejected.filled_qty.as_decimal() != 0 or rejected.trade_ids
                        or any(isinstance(event, OrderFilled) for event in rejected.events)):
                    raise ValueError("archived rejected hedge has contradictory execution facts")
            ids = intent.hedge_leg_order_ids
            if not intent.hedge_plan and not ids and intent.hedge_client_order_id:
                ids = (intent.hedge_client_order_id,)  # Original schema-1 single bound order.
            if any(cid not in hedges or hedges[cid].status != OrderStatus.FILLED for cid in ids):
                raise ValueError("startup hedge still has an incomplete or unbound leg")
            completed = intent.hedge_filled_ounces == intent.hedge_quantity_ounces
            if resume_unbound and intent.hedge_plan:
                if (len(ids) > len(intent.hedge_plan)
                        or len(ids) != intent.hedge_leg_index
                        + (intent.hedge_client_order_id is not None)
                        or (intent.hedge_client_order_id is not None
                            and intent.hedge_client_order_id != ids[-1])
                        or intent.hedge_filled_ounces != sum((leg.quantity_ounces for leg
                            in intent.hedge_plan[:len(ids)]), Decimal(0))
                        or intent.hedge_filled_ounces != sum((hedges[cid].filled_qty.as_decimal()
                            for cid in ids), Decimal(0))):
                    raise ValueError("startup hedge completed prefix differs from its plan")
            elif (not (resume_unbound and not ids and intent.hedge_filled_ounces == 0)
                  and (not completed
                       or len(ids) != (len(intent.hedge_plan) if intent.hedge_plan else 1))):
                raise ValueError("startup hedge still has an incomplete or unbound leg")
            status = ObligationStatus.COMPLETED if completed else ObligationStatus.PENDING
            candidates[intent.intent_id] = (
                replace(intent, status=status, hedge_client_order_id=None,
                        hedge_leg_index=len(ids), hedge_leg_filled_ounces=Decimal(0))
                if intent.hedge_plan else replace(intent, status=status)
            )
    pending = any(intent.status is ObligationStatus.PENDING for intent in candidates.values())
    # Maker's normal dispatcher releases this freeze after the last obligation.
    # Clearing it while pending would strand the strategy's in-memory source hold.
    cycle_reason = (
        next(iter(receipt.freezes.values()), "verified pending Maker hedge obligations")
        if pending and isinstance(store, MakerStateStore) else None
    )
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
                identity: candidates[identity] for identity in view._state.hedge_intents
            }
            view._state.halt_reason = None
            view._state.source_freeze_reason = cycle_reason
        if isinstance(store, MakerStateStore):
            store._cycle_freeze_only = pending
            store._validate()
        admissible_pending = (
            store.source_balance_is_admissible() and store.next_pending_hedge() is not None
            if isinstance(store, MakerStateStore)
            else all(view.rounding_residual_ounces == 0 for view in views)
        )
        if ((pending and not admissible_pending)
                or (not pending and any(not view.can_submit_source()
                                       or not view.cycle_evidence_complete() for view in views))):
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
    receipt.freezes = {view: cycle_reason for view in views} if cycle_reason else {}
    receipt.maker_cycle_freeze = cycle_reason is not None


async def _wait_for_retry_quote(
    cache: Cache, store: JsonStateStore | MakerStateStore,
    receipt: _StartupReceipt | None, instrument_id: InstrumentId,
) -> None:
    """Wait for the first PUB before collecting facts; never retry a mutation.

    A connected MT5 data client may not have received a quote yet. Existing
    quotes (including stale/invalid ones) still face the complete preflight.
    The receipt deadline and the caller's round timeout both bound this wait.
    """
    if receipt is None or receipt.options.rejected_hedge_order_id is None:
        return
    target = receipt.options.rejected_hedge_order_id
    order = cache.order(ClientOrderId(target))
    if (order is None or order.status != OrderStatus.REJECTED or order.filled_qty != 0
            or not any(intent.hedge_client_order_id == target and intent.rejected_attempt is None
                       for view in _views(store) for intent in view.intents())):
        return
    while cache.quote_tick(instrument_id) is None:
        receipt.check(store)
        remaining = (receipt.retry_deadline or 0) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("rejected hedge retry expired while waiting for first MT5 quote")
        await asyncio.sleep(min(.025, remaining))


async def reconcile_startup(
    cache: Cache, store: JsonStateStore | MakerStateStore, *,
    trader_id: TraderId, strategy_id: StrategyId,
    source: BitfinexV1ExecutionClient, hedge: Mt5V1ExecutionClient,
    source_instrument_id: InstrumentId, hedge_instrument_id: InstrumentId,
    receipt: _StartupReceipt | None = None,
    rejected_retry_check: Callable[[HedgeIntent, Order], None] | None = None,
) -> None:
    """Prove complete history; only a fresh eligible receipt can finalize held facts."""
    if receipt is not None:
        receipt.check(store)
    await _wait_for_retry_quote(cache, store, receipt, hedge_instrument_id)
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
    _complete_orders(hedge_mass, hedge_orders, hedge_instrument_id, rejected_venue_ids={
        order.client_order_id.value:
            hedge._synthetic_rejected_venue_order_id(order.client_order_id.value).value
        for order in hedge_orders if order.status == OrderStatus.REJECTED
    })
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
        target = receipt.options.rejected_hedge_order_id
        retry_ready = False
        if target is not None:
            matches = [intent for view in views for intent in view.intents()
                       if intent.hedge_client_order_id == target
                       and intent.rejected_attempt is None]
            if matches:
                order = next((order for order in hedge_orders
                              if order.client_order_id.value == target), None)
                if len(matches) != 1 or order is None or rejected_retry_check is None:
                    raise ValueError("reviewed rejected hedge lacks execution qualification")
                rejected_retry_check(matches[0], order)
                retry_ready = True
        _settle_completed(store, source_orders, hedge_orders, receipt, resume_unbound=True,
                          rejected_retry_ready=retry_ready)
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
