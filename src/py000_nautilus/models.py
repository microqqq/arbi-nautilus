"""Small business records NautilusTrader does not provide."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from nautilus_trader.model.identifiers import AccountId, ClientId


class SourceDirection(StrEnum):
    """Legacy order-side names: bid buys source, ask sells source."""

    LONG = "bid"
    SHORT = "ask"


class BusinessOrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ObligationStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BookTop:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal


@dataclass(frozen=True, slots=True)
class SourceAccount:
    account_id: AccountId
    client_id: ClientId | None
    position_ounces: Decimal
    max_long_ounces: Decimal
    max_short_ounces: Decimal
    base_margin_level: Decimal


@dataclass(frozen=True, slots=True)
class HedgeAccount:
    position_ounces: Decimal
    max_long_ounces: Decimal
    max_short_ounces: Decimal


@dataclass(frozen=True, slots=True)
class MakerAccount:
    account_id: AccountId
    client_id: ClientId | None
    position_ounces: Decimal
    max_long_ounces: Decimal
    max_short_ounces: Decimal


@dataclass(frozen=True, slots=True)
class MakerQuote:
    direction: SourceDirection
    source_account: SourceAccount
    hedge_account: MakerAccount
    source_price_usdt: Decimal
    hedge_reference_price_usd: Decimal
    quantity_ounces: Decimal
    adjusted_spread: Decimal
    leverage: int


@dataclass(frozen=True, slots=True)
class Opportunity:
    direction: SourceDirection
    source_account: SourceAccount
    source_price_usdt: Decimal
    hedge_reference_price_usd: Decimal
    source_quantity_ounces: Decimal
    net_return: Decimal
    leverage: int


@dataclass(frozen=True, slots=True)
class HedgeLeg:
    """One deterministic MT5 HEDGING action within a signed hedge delta."""

    side: BusinessOrderSide
    quantity_ounces: Decimal
    position_id: str | None = None
    expected_position_side: BusinessOrderSide | None = None
    expected_position_quantity_ounces: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.side, BusinessOrderSide):
            raise TypeError("hedge leg side must be BusinessOrderSide")
        if (
            not isinstance(self.quantity_ounces, Decimal)
            or not self.quantity_ounces.is_finite()
            or self.quantity_ounces <= 0
        ):
            raise ValueError("hedge leg quantity must be a positive finite Decimal")
        is_close = self.position_id is not None
        if is_close:
            if not self.position_id:
                raise ValueError("hedge close leg position ID must not be empty")
            if not isinstance(self.expected_position_side, BusinessOrderSide):
                raise TypeError("hedge close leg requires the expected position side")
            expected = self.expected_position_quantity_ounces
            if (
                not isinstance(expected, Decimal)
                or not expected.is_finite()
                or expected <= 0
            ):
                raise ValueError(
                    "hedge close leg requires a positive expected position quantity"
                )
            if self.expected_position_side is self.side:
                raise ValueError("hedge close leg side must oppose the position side")
            if self.quantity_ounces > expected:
                raise ValueError("hedge close leg exceeds the expected position quantity")
            return
        if self.expected_position_side is not None:
            raise ValueError("hedge open leg cannot bind an expected position side")
        if self.expected_position_quantity_ounces is not None:
            raise ValueError("hedge open leg cannot bind an expected position quantity")

    @property
    def is_close(self) -> bool:
        return self.position_id is not None


@dataclass(frozen=True, slots=True)
class HedgeIntent:
    intent_id: str
    fill_key: str
    source_client_order_id: str
    source_trade_id: str
    source_side: BusinessOrderSide
    source_fill_ounces: Decimal
    hedge_side: BusinessOrderSide
    hedge_quantity_ounces: Decimal
    hedge_position_id: str | None = None
    hedge_position_quantity_ounces: Decimal | None = None
    status: ObligationStatus = ObligationStatus.PENDING
    hedge_client_order_id: str | None = None
    hedge_filled_ounces: Decimal = Decimal(0)
    hedge_plan: tuple[HedgeLeg, ...] = ()
    hedge_leg_index: int = 0
    hedge_leg_filled_ounces: Decimal = Decimal(0)
    hedge_order_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Schema-1 runtime history can contain an ID without the later quantity
        # binding. Preserve that evidence for compatibility; current writers still
        # require the pair, and an ID-only active intent fails closed in planning.
        if self.hedge_position_id is None and self.hedge_position_quantity_ounces is not None:
            raise ValueError("bound hedge position quantity requires a position ID")
        if self.hedge_position_quantity_ounces is not None and (
            not self.hedge_position_quantity_ounces.is_finite()
            or self.hedge_position_quantity_ounces <= 0
        ):
            raise ValueError("bound hedge position quantity must be positive and finite")
        if (
            not isinstance(self.hedge_quantity_ounces, Decimal)
            or not self.hedge_quantity_ounces.is_finite()
            or self.hedge_quantity_ounces <= 0
        ):
            raise ValueError("hedge intent quantity must be a positive finite Decimal")
        if (
            not isinstance(self.hedge_filled_ounces, Decimal)
            or not self.hedge_filled_ounces.is_finite()
            or self.hedge_filled_ounces < 0
        ):
            raise ValueError("hedge filled quantity must be a non-negative finite Decimal")
        if type(self.hedge_leg_index) is not int or not 0 <= self.hedge_leg_index <= len(
            self.hedge_plan
        ):
            raise ValueError("hedge plan index is out of bounds")
        if (
            not isinstance(self.hedge_leg_filled_ounces, Decimal)
            or not self.hedge_leg_filled_ounces.is_finite()
            or self.hedge_leg_filled_ounces < 0
        ):
            raise ValueError("hedge leg fill must be a non-negative finite Decimal")
        if any(not value for value in self.hedge_order_ids) or len(
            set(self.hedge_order_ids)
        ) != len(self.hedge_order_ids):
            raise ValueError("hedge order history must contain unique non-empty IDs")
        if not self.hedge_plan:
            if self.hedge_leg_index != 0 or self.hedge_leg_filled_ounces != 0:
                raise ValueError("unplanned hedge intent cannot have leg progress")
            return
        if any(leg.side is not self.hedge_side for leg in self.hedge_plan):
            raise ValueError("hedge plan side does not match its intent")
        if sum((leg.quantity_ounces for leg in self.hedge_plan), Decimal(0)) != (
            self.hedge_quantity_ounces
        ):
            raise ValueError("hedge plan quantity does not match its intent")
        if self.hedge_leg_index == len(self.hedge_plan):
            if self.hedge_leg_filled_ounces != 0:
                raise ValueError("completed hedge plan cannot retain a partial leg fill")
        elif (
            self.status is not ObligationStatus.BLOCKED
            and self.hedge_leg_filled_ounces
            >= self.hedge_plan[self.hedge_leg_index].quantity_ounces
        ):
            raise ValueError("hedge leg progress must advance at the planned quantity")
        if self.status is not ObligationStatus.BLOCKED:
            expected_filled = sum(
                (
                    leg.quantity_ounces
                    for leg in self.hedge_plan[: self.hedge_leg_index]
                ),
                self.hedge_leg_filled_ounces,
            )
            if self.hedge_filled_ounces != expected_filled:
                raise ValueError("hedge aggregate fill does not match its leg progress")
        if self.status is ObligationStatus.COMPLETED and (
            self.hedge_leg_index != len(self.hedge_plan)
            or self.hedge_filled_ounces != self.hedge_quantity_ounces
        ):
            raise ValueError("completed hedge intent has incomplete plan progress")
