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
class HedgeIntent:
    intent_id: str
    fill_key: str
    source_client_order_id: str
    source_trade_id: str
    source_side: BusinessOrderSide
    source_fill_ounces: Decimal
    hedge_side: BusinessOrderSide
    hedge_quantity_ounces: Decimal
    status: ObligationStatus = ObligationStatus.PENDING
    hedge_client_order_id: str | None = None
    hedge_filled_ounces: Decimal = Decimal(0)
