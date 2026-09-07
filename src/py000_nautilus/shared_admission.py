"""Synchronous worst-fill admission over native orders and the shared allocation owner."""

from collections.abc import Callable
from decimal import Decimal

from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.enums import OrderSide

from py000_nautilus.config import MakerStrategyConfig
from py000_nautilus.maker_economics import maker_carry_bounds
from py000_nautilus.maker_store import MakerStateStore
from py000_nautilus.margin import LiveAccountReader
from py000_nautilus.models import BookTop, BusinessOrderSide
from py000_nautilus.store import JsonStateStore


def _within_bounds(
    net: Decimal, buy: Decimal, sell: Decimal, maximum: Decimal,
    minimum: Decimal, only_long: bool,
) -> bool:
    upper, lower = net + buy, net - sell
    if upper > max(net, maximum) or lower < min(net, -maximum):
        return False
    if not only_long:
        return True
    return all(net * after >= 0 and not (abs(after) < minimum and abs(after) < abs(net))
               for after in (upper, lower))


class SharedSourceAdmission:
    """No reservation ledger: begin_source plus native INITIALIZED reserve the same turn."""

    def __init__(self, cache: Cache, owner: MakerStateStore, config: MakerStrategyConfig) -> None:
        self.cache, self.owner, self.config = cache, owner, config
        self.reader: LiveAccountReader | None = None
        self.positions_ready: Callable[[], bool] | None = None
        self.on_position_mismatch: Callable[[], None] | None = None

    def __call__(self, view: JsonStateStore, side: BusinessOrderSide, quantity: Decimal) -> bool:
        owner, config = self.owner, self.config
        if (view not in owner.all_views() or not quantity.is_finite() or quantity <= 0
                or self.reader is None or not view.can_submit_source()
                or owner._freeze_publication_failed
                or any(target.halt_reason is not None or target.source_freeze_reason is not None
                       for target in owner.all_views())
                or owner.first_unfinished_hedge() is not None
                or not owner.source_balance_is_admissible()):
            return False
        source_tick = self.cache.quote_tick(config.source_instrument_id)
        hedge_tick = self.cache.quote_tick(config.hedge_instrument_id)
        if source_tick is None or hedge_tick is None:
            return False
        accounts = self.reader(
            BookTop(source_tick.bid_price.as_decimal(), source_tick.ask_price.as_decimal(),
                    source_tick.bid_size.as_decimal(), source_tick.ask_size.as_decimal()),
            source_tick.ts_event,
            BookTop(hedge_tick.bid_price.as_decimal(), hedge_tick.ask_price.as_decimal(),
                    hedge_tick.bid_size.as_decimal(), hedge_tick.ask_size.as_decimal()),
            hedge_tick.ts_event, True,
        )
        if accounts is None or not accounts[3]:
            return False
        if self.positions_ready is not None and not self.positions_ready():
            owner.freeze_sources("shared current venue/native positions differ")
            if self.on_position_mismatch is not None:
                self.on_position_mismatch()
            return False
        source, hedge = accounts[:2]
        records = {record.client_order_id: (target, record)
                   for target in owner.all_views() for record in target.source_orders()}
        native = {order.client_order_id.value: order for order in self.cache.orders(
            instrument_id=config.source_instrument_id,
        )}
        if records.keys() != native.keys():
            owner.freeze_sources("shared source/native ownership differs")
            return False
        buy = quantity if side is BusinessOrderSide.BUY else Decimal(0)
        sell = quantity if side is BusinessOrderSide.SELL else Decimal(0)
        for cid, (target, record) in records.items():
            order = native[cid]
            expected = OrderSide.BUY if record.side is BusinessOrderSide.BUY else OrderSide.SELL
            if (order.strategy_id.value != owner.strategy_id_for(target)
                    or order.side != expected
                    or order.quantity.as_decimal() != record.quantity_ounces
                    or order.filled_qty.as_decimal() != record.filled_ounces
                    or order.account_id not in {None, source.account_id}):
                owner.freeze_sources("shared source/native facts differ")
                return False
            if not order.is_closed:
                # Pending cancel and price-only update still have the original live quantity.
                if record.side is BusinessOrderSide.BUY:
                    buy += order.leaves_qty.as_decimal()
                else:
                    sell += order.leaves_qty.as_decimal()
        risk = config.economics.risk
        if (buy > source.max_long_ounces or sell > source.max_short_ounces
                or not _within_bounds(source.position_ounces, buy, sell, risk.source_max_abs,
                                      risk.source_min_keep_abs, risk.only_long)):
            return False
        exposure, hedge_buy, hedge_sell = maker_carry_bounds(owner.carry_residual_ounces, buy, sell)
        return (
            (config.max_unhedged_ounces is None or exposure <= config.max_unhedged_ounces)
            and hedge_buy <= hedge.max_long_ounces and hedge_sell <= hedge.max_short_ounces
            and _within_bounds(hedge.position_ounces, hedge_buy, hedge_sell, risk.hedge_max_abs,
                               risk.hedge_min_keep_abs, risk.only_long)
        )
