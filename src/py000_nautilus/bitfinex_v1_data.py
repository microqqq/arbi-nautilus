"""Checksum-gated public Bitfinex XAUT perpetual market data for Nautilus."""

from __future__ import annotations

import asyncio
import zlib
from decimal import Decimal
from typing import Protocol, cast

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.data.messages import (
    SubscribeFundingRates,
    SubscribeOrderBook,
    SubscribeQuoteTicks,
    UnsubscribeFundingRates,
    UnsubscribeOrderBook,
    UnsubscribeQuoteTicks,
)
from nautilus_trader.live.data_client import LiveDataClient, LiveMarketDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import (
    BookOrder,
    FundingRateUpdate,
    OrderBookDelta,
    OrderBookDeltas,
    QuoteTick,
)
from nautilus_trader.model.enums import BookAction, BookType, OrderSide, RecordFlag
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Currency, Price, Quantity

from py000_nautilus.bitfinex_v1_transport import BitfinexV1Transport

BITFINEX = Venue("BITFINEX")
INSTRUMENT_ID = InstrumentId.from_str("XAUTUSDT-PERP.BITFINEX")
RAW_SYMBOL = "tXAUTF0:USTF0"
PAPER_RAW_SYMBOL = "tTESTXAUTF0:TESTUSDTF0"
SUPPORTED_RAW_SYMBOLS = frozenset({RAW_SYMBOL, PAPER_RAW_SYMBOL})
_CHECKSUM_FLAG = 131_072
_BOOK_SUB_ID = "py000-xaut-book-v1"
_DERIV_STATUS_TIMESTAMP_INDEX = 0
_DERIV_STATUS_FUNDING_INDEX = 8
_DERIV_STATUS_MIN_SIZE = _DERIV_STATUS_FUNDING_INDEX + 1


def _subscription(raw_symbol: str) -> dict[str, object]:
    return {
        "event": "subscribe",
        "channel": "book",
        "symbol": raw_symbol,
        "prec": "P0",
        "freq": "F0",
        "len": "25",
        "subId": _BOOK_SUB_ID,
    }


def _funding_subscription(raw_symbol: str) -> dict[str, object]:
    return {
        "event": "subscribe",
        "channel": "status",
        "key": f"deriv:{raw_symbol}",
    }


class BitfinexV1DataError(RuntimeError):
    pass


class BitfinexV1DataClientConfig(LiveDataClientConfig, kw_only=True, frozen=True):
    url: str
    instrument_id: InstrumentId
    raw_symbol: str
    price_precision: int
    size_precision: int
    price_increment: Decimal
    size_increment: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    margin_init: Decimal
    margin_maint: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
    open_timeout_ms: int = 10_000


class _PublicTransport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def send_json(self, payload: dict[str, object]) -> None: ...
    async def recv_json(self) -> dict[str, object] | list[object]: ...


class BitfinexP0Book:
    def __init__(self) -> None:
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._has_snapshot = False
        self._actionable = False

    @property
    def is_actionable(self) -> bool:
        return self._actionable

    def clear(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self._has_snapshot = False
        self._actionable = False

    def apply_snapshot(self, value: list[object]) -> None:
        if len(value) != 50:
            raise BitfinexV1DataError("Bitfinex len25 snapshot must contain 50 levels")
        bids: dict[Decimal, Decimal] = {}
        asks: dict[Decimal, Decimal] = {}
        for item in value:
            price, count, amount = _book_row(item)
            if count == 0 or price in bids or price in asks:
                raise BitfinexV1DataError("Bitfinex snapshot contains an invalid level")
            (bids if amount > 0 else asks)[price] = amount
        if len(bids) != 25 or len(asks) != 25:
            raise BitfinexV1DataError("Bitfinex snapshot must contain 25 levels per side")
        self._bids, self._asks = bids, asks
        self._has_snapshot = True
        # The venue delivers the initial book atomically in one WebSocket frame.
        # Any state derived from later deltas still requires the following CRC.
        self._actionable = True

    def apply_delta(self, value: list[object]) -> None:
        if not self._has_snapshot:
            raise BitfinexV1DataError("Bitfinex delta arrived before the book snapshot")
        price, count, amount = _book_row(value)
        if count == 0:
            side = self._bids if amount > 0 else self._asks
            side.pop(price, None)
        else:
            self._bids.pop(price, None)
            self._asks.pop(price, None)
            (self._bids if amount > 0 else self._asks)[price] = amount
        self._actionable = False

    def verify(self, venue_checksum: int) -> bool:
        if not self._has_snapshot or len(self._bids) < 25 or len(self._asks) < 25:
            raise BitfinexV1DataError("Bitfinex book is not complete enough for CRC validation")
        values: list[Decimal] = []
        bids = sorted(self._bids.items(), reverse=True)[:25]
        asks = sorted(self._asks.items())[:25]
        for (bid_price, bid_amount), (ask_price, ask_amount) in zip(bids, asks, strict=True):
            values.extend((bid_price, bid_amount, ask_price, ask_amount))
        payload = ":".join(_crc_token(value) for value in values).encode("utf-8")
        self._actionable = zlib.crc32(payload) == (venue_checksum & 0xFFFF_FFFF)
        return self._actionable

    def quote(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        bids, asks = self.levels()
        best_bid, bid_size = bids[0]
        best_ask, ask_size = asks[0]
        return best_bid, bid_size, best_ask, ask_size

    def levels(
        self,
    ) -> tuple[
        tuple[tuple[Decimal, Decimal], ...],
        tuple[tuple[Decimal, Decimal], ...],
    ]:
        if not self._actionable:
            raise BitfinexV1DataError("Bitfinex book has no actionable snapshot or latest CRC")
        bids = tuple(sorted(self._bids.items(), reverse=True)[:25])
        asks = tuple((price, abs(size)) for price, size in sorted(self._asks.items())[:25])
        if bids[0][0] >= asks[0][0]:
            raise BitfinexV1DataError("Bitfinex book is crossed")
        return bids, asks


def instrument_from_config(config: BitfinexV1DataClientConfig, *, ts_init: int) -> CryptoPerpetual:
    _validate_config(config)
    return CryptoPerpetual(
        instrument_id=config.instrument_id,
        raw_symbol=Symbol(config.raw_symbol),
        base_currency=Currency.from_str("XAUT"), quote_currency=USDT, settlement_currency=USDT,
        is_inverse=False,
        price_precision=config.price_precision,
        size_precision=config.size_precision,
        price_increment=Price.from_str(format(config.price_increment, "f")),
        size_increment=Quantity.from_str(format(config.size_increment, "f")),
        multiplier=Quantity.from_int(1),
        lot_size=Quantity.from_int(1),
        min_quantity=Quantity.from_str(format(config.min_quantity, "f")),
        max_quantity=Quantity.from_str(format(config.max_quantity, "f")),
        margin_init=config.margin_init, margin_maint=config.margin_maint,
        maker_fee=config.maker_fee, taker_fee=config.taker_fee,
        ts_event=0, ts_init=ts_init,
        info={
            "book_channel": "P0/F0/len25",
            "checksum_required": True,
            "canonical_quantity": "ounce",
            "bitfinex_environment": (
                "paper" if config.raw_symbol == PAPER_RAW_SYMBOL else "production"
            ),
        },
    )


class BitfinexV1DataClient(LiveMarketDataClient):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        name: str | None,
        config: BitfinexV1DataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: InstrumentProvider,
        transport: _PublicTransport | None = None,
    ) -> None:
        _validate_config(config)
        super().__init__(loop=loop, client_id=ClientId(name or "BITFINEX"), venue=BITFINEX,
                         msgbus=msgbus, cache=cache, clock=clock,
                         instrument_provider=instrument_provider, config=config)
        self._bfx_config = config
        self._instrument = instrument_from_config(config, ts_init=clock.timestamp_ns())
        self._subscription = _subscription(config.raw_symbol)
        self._funding_subscription = _funding_subscription(config.raw_symbol)
        self._transport = transport or BitfinexV1Transport(
            config.url, open_timeout_ms=config.open_timeout_ms
        )
        self._book = BitfinexP0Book()
        self._configuration_lock = asyncio.Lock()
        self._checksum_configured = False
        self._channel_id: int | None = None
        self._pending_unsubscribe_channel_id: int | None = None
        self._unsubscribe_book_on_ack = False
        self._book_unsubscribe_task: asyncio.Task[None] | None = None
        self._funding_channel_id: int | None = None
        self._pending_funding_unsubscribe_channel_id: int | None = None
        self._unsubscribe_funding_on_ack = False
        self._funding_unsubscribe_task: asyncio.Task[None] | None = None
        self._subscription_requested = False
        self._funding_subscription_requested = False
        self._pre_ack_heartbeat_channel_ids: set[int] = set()
        self._publish_quotes = False
        self._publish_deltas = False
        self._publish_funding = False
        self._running = False
        self._reader_task: asyncio.Task[None] | None = None
        self._last_failure: str | None = None

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    @property
    def book_is_actionable(self) -> bool:
        return self._book.is_actionable

    def connect(self) -> None:
        self.create_task(
            self._connect(),
            actions=self._finish_connect,
            success_msg="Connected",
            success_color=LogColor.GREEN,
        )

    async def _connect(self) -> None:
        try:
            await self._transport.open()
            self._handle_data(self._instrument)
            self._last_failure = None
        except BaseException as exc:
            await self._fail_closed(exc)
            raise

    def _finish_connect(self) -> None:
        self._set_connected(True)
        self._running = True
        self._reader_task = self.create_task(self._read_loop(), log_msg="bitfinex-v1-book")

    async def _disconnect(self) -> None:
        self._running = False
        if self._reader_task is not None and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._reader_task = None
        deferred = tuple(
            task
            for task in (self._book_unsubscribe_task, self._funding_unsubscribe_task)
            if task is not None and task is not asyncio.current_task()
        )
        for task in deferred:
            task.cancel()
        if deferred:
            await asyncio.gather(*deferred, return_exceptions=True)
        self._book_unsubscribe_task = None
        self._funding_unsubscribe_task = None
        await self._transport.close()
        self._checksum_configured = False
        self._channel_id = None
        self._pending_unsubscribe_channel_id = None
        self._unsubscribe_book_on_ack = False
        self._funding_channel_id = None
        self._pending_funding_unsubscribe_channel_id = None
        self._unsubscribe_funding_on_ack = False
        self._subscription_requested = False
        self._funding_subscription_requested = False
        self._pre_ack_heartbeat_channel_ids.clear()
        self._publish_quotes = False
        self._publish_deltas = False
        self._publish_funding = False
        self._clear_base_subscriptions()
        self._book.clear()

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        try:
            self._require_instrument(command.instrument_id)
        except Exception:
            self._remove_subscription_quote_ticks(command.instrument_id)
            raise
        self._publish_quotes = True
        await self._ensure_book_subscription()

    async def _subscribe_order_book_deltas(self, command: SubscribeOrderBook) -> None:
        try:
            self._require_instrument(command.instrument_id)
            if command.book_type != BookType.L2_MBP or command.depth != 25:
                raise BitfinexV1DataError("Bitfinex v1 requires an L2_MBP depth-25 subscription")
        except Exception:
            self._remove_subscription_order_book_deltas(command.instrument_id)
            raise
        self._publish_deltas = True
        await self._ensure_book_subscription()

    async def _ensure_book_subscription(self) -> None:
        task = self._book_unsubscribe_task
        if task is not None:
            try:
                await task
            finally:
                if self._book_unsubscribe_task is task:
                    self._book_unsubscribe_task = None
        if self._subscription_requested:
            self._unsubscribe_book_on_ack = False
            return
        self._subscription_requested = True
        self._unsubscribe_book_on_ack = False
        try:
            await self._ensure_checksum_configured()
            await self._transport.send_json(self._subscription)
        except BaseException as exc:
            await self._fail_closed(exc)
            raise

    async def _subscribe_funding_rates(self, command: SubscribeFundingRates) -> None:
        try:
            self._require_instrument(command.instrument_id)
        except Exception:
            self._remove_subscription_funding_rates(command.instrument_id)
            raise
        self._publish_funding = True
        await self._ensure_funding_subscription()

    async def _ensure_funding_subscription(self) -> None:
        task = self._funding_unsubscribe_task
        if task is not None:
            try:
                await task
            finally:
                if self._funding_unsubscribe_task is task:
                    self._funding_unsubscribe_task = None
        if self._funding_subscription_requested:
            self._unsubscribe_funding_on_ack = False
            return
        self._funding_subscription_requested = True
        self._unsubscribe_funding_on_ack = False
        try:
            await self._ensure_checksum_configured()
            await self._transport.send_json(self._funding_subscription)
        except BaseException as exc:
            await self._fail_closed(exc)
            raise

    async def _ensure_checksum_configured(self) -> None:
        async with self._configuration_lock:
            if self._checksum_configured:
                return
            await self._transport.send_json({"event": "conf", "flags": _CHECKSUM_FLAG})
            self._checksum_configured = True

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        self._require_instrument(command.instrument_id)
        self._publish_quotes = False
        await self._unsubscribe_book_if_unused()

    async def _unsubscribe_order_book_deltas(self, command: UnsubscribeOrderBook) -> None:
        self._require_instrument(command.instrument_id)
        self._publish_deltas = False
        await self._unsubscribe_book_if_unused()

    async def _unsubscribe_funding_rates(self, command: UnsubscribeFundingRates) -> None:
        self._require_instrument(command.instrument_id)
        self._publish_funding = False
        if not self._funding_subscription_requested:
            return
        channel_id = self._funding_channel_id
        if channel_id is None:
            self._unsubscribe_funding_on_ack = True
            return
        self._funding_subscription_requested = False
        self._funding_channel_id = None
        self._pending_funding_unsubscribe_channel_id = channel_id
        await self._send_unsubscribe(channel_id)

    async def _unsubscribe_book_if_unused(self) -> None:
        if self._publish_quotes or self._publish_deltas:
            return
        self._book.clear()
        if not self._subscription_requested:
            return
        channel_id = self._channel_id
        if channel_id is None:
            self._unsubscribe_book_on_ack = True
            return
        self._subscription_requested = False
        self._channel_id = None
        self._pending_unsubscribe_channel_id = channel_id
        await self._send_unsubscribe(channel_id)

    async def _send_unsubscribe(self, channel_id: int) -> None:
        try:
            await self._transport.send_json(
                {"event": "unsubscribe", "chanId": channel_id}
            )
        except BaseException as exc:
            await self._fail_closed(exc)
            raise

    async def _read_loop(self) -> None:
        try:
            while self._running:
                data = self._consume_frame(await self._transport.recv_json())
                for item in data:
                    self._handle_data(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_closed(exc)

    async def _fail_closed(self, exc: BaseException) -> None:
        self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
        self._running = False
        self._checksum_configured = False
        self._subscription_requested = False
        self._funding_subscription_requested = False
        self._pre_ack_heartbeat_channel_ids.clear()
        self._publish_quotes = False
        self._publish_deltas = False
        self._publish_funding = False
        self._channel_id = None
        self._pending_unsubscribe_channel_id = None
        self._unsubscribe_book_on_ack = False
        self._book_unsubscribe_task = None
        self._funding_channel_id = None
        self._pending_funding_unsubscribe_channel_id = None
        self._unsubscribe_funding_on_ack = False
        self._funding_unsubscribe_task = None
        self._clear_base_subscriptions()
        self._book.clear()
        self._set_connected(False)
        await self._transport.close()

    def _consume_frame(
        self,
        frame: dict[str, object] | list[object],
    ) -> tuple[QuoteTick | OrderBookDeltas | FundingRateUpdate, ...]:
        if isinstance(frame, dict):
            self._consume_control(frame)
            return ()
        if len(frame) not in {2, 3}:
            raise BitfinexV1DataError("Bitfinex channel frame has invalid length")
        channel_id = _integer(frame[0], "channel id")
        if channel_id in {
            self._pending_unsubscribe_channel_id,
            self._pending_funding_unsubscribe_channel_id,
        }:
            return ()
        payload = frame[1]
        if payload == "hb" and len(frame) == 2:
            if channel_id == self._channel_id:
                # Bound-channel FIFO makes this a local re-observation, not a price change.
                if self._publish_quotes and self._book.is_actionable:
                    return (self._quote_tick(),)
                return ()
            if channel_id == self._funding_channel_id:
                return ()
            pending_ack_count = self._pending_subscription_ack_count()
            if pending_ack_count:
                if channel_id in self._pre_ack_heartbeat_channel_ids:
                    return ()
                if len(self._pre_ack_heartbeat_channel_ids) < pending_ack_count:
                    self._pre_ack_heartbeat_channel_ids.add(channel_id)
                    return ()
            raise BitfinexV1DataError(
                "Bitfinex unknown hb "
                f"requested=book:{int(self._subscription_requested)},"
                f"funding:{int(self._funding_subscription_requested)} "
                f"deferred=book:{int(self._unsubscribe_book_on_ack)},"
                f"funding:{int(self._unsubscribe_funding_on_ack)} "
                f"channels=book:{self._channel_id},funding:{self._funding_channel_id},"
                f"pend_book:{self._pending_unsubscribe_channel_id},"
                f"pend_funding:{self._pending_funding_unsubscribe_channel_id},in:{channel_id}"
            )
        if channel_id == self._channel_id:
            return self._consume_book_frame(frame)
        if channel_id == self._funding_channel_id:
            return self._consume_funding_frame(frame)
        raise BitfinexV1DataError(
            "Bitfinex frame belongs to an unexpected channel "
            f"{channel_id} (book={self._channel_id}, funding={self._funding_channel_id}, "
            f"pending_book={self._pending_unsubscribe_channel_id}, "
            f"pending_funding={self._pending_funding_unsubscribe_channel_id})"
        )

    def _pending_subscription_ack_count(self) -> int:
        return int(self._subscription_requested and self._channel_id is None) + int(
            self._funding_subscription_requested and self._funding_channel_id is None
        )

    def _consume_book_frame(
        self,
        frame: list[object],
    ) -> tuple[QuoteTick | OrderBookDeltas, ...]:
        payload = frame[1]
        if payload == "cs" and len(frame) == 3:
            checksum = _integer(frame[2], "checksum")
            if not self._book.verify(checksum):
                raise BitfinexV1DataError("Bitfinex book checksum mismatch")
        else:
            if len(frame) != 2 or not isinstance(payload, list) or not payload:
                raise BitfinexV1DataError("Bitfinex book frame is malformed")
            if isinstance(payload[0], list):
                self._book.apply_snapshot(payload)
            else:
                self._book.apply_delta(payload)
                return ()
        timestamp = self._clock.timestamp_ns()
        result: list[QuoteTick | OrderBookDeltas] = []
        if self._publish_quotes:
            result.append(self._quote_tick(timestamp))
        if self._publish_deltas:
            result.append(self._order_book_snapshot(timestamp))
        return tuple(result)

    def _consume_funding_frame(
        self,
        frame: list[object],
    ) -> tuple[FundingRateUpdate, ...]:
        if len(frame) != 2:
            raise BitfinexV1DataError("Bitfinex derivatives status frame is malformed")
        payload = frame[1]
        if not isinstance(payload, list) or len(payload) < _DERIV_STATUS_MIN_SIZE:
            raise BitfinexV1DataError(
                "Bitfinex derivatives status does not contain its funding field"
            )
        timestamp_ms = _integer(
            payload[_DERIV_STATUS_TIMESTAMP_INDEX],
            "derivatives status timestamp",
        )
        if not 0 < timestamp_ms <= (2**64 - 1) // 1_000_000:
            raise BitfinexV1DataError("Bitfinex derivatives status timestamp is invalid")
        rate = _decimal(
            payload[_DERIV_STATUS_FUNDING_INDEX],
            "next funding accrued",
        )
        if abs(rate) > 1:
            raise BitfinexV1DataError("Bitfinex next funding accrued is outside [-1, 1]")
        if not self._publish_funding:
            return ()
        return (
            FundingRateUpdate(
                instrument_id=self._instrument.id,
                rate=rate,
                ts_event=timestamp_ms * 1_000_000,
                ts_init=self._clock.timestamp_ns(),
            ),
        )

    def _consume_control(self, frame: dict[str, object]) -> None:
        event = frame.get("event")
        if event == "info" and frame.get("version") == 2 and "code" not in frame:
            platform = frame.get("platform")
            if not isinstance(platform, dict):
                raise BitfinexV1DataError("Bitfinex platform status is missing")
            if _integer(platform.get("status"), "platform status") != 1:
                raise BitfinexV1DataError("Bitfinex platform is not operational")
            return
        if event == "conf" and frame.get("status") == "OK":
            return
        if event == "subscribed":
            self._consume_subscribed(frame)
            return
        if event == "unsubscribed":
            self._consume_unsubscribed(frame)
            return
        raise BitfinexV1DataError(f"unsupported Bitfinex control event {event!r}")

    def _consume_subscribed(self, frame: dict[str, object]) -> None:
        channel = frame.get("channel")
        if channel == "book":
            requested = self._subscription
            if not self._subscription_requested:
                raise BitfinexV1DataError("unsolicited Bitfinex book subscription")
            if self._channel_id is not None:
                raise BitfinexV1DataError("Bitfinex repeated the book subscription")
        elif channel == "status":
            requested = self._funding_subscription
            if not self._funding_subscription_requested:
                raise BitfinexV1DataError("unsolicited Bitfinex funding subscription")
            if self._funding_channel_id is not None:
                raise BitfinexV1DataError("Bitfinex repeated the funding subscription")
        else:
            raise BitfinexV1DataError("Bitfinex subscribed an unsupported channel")

        for key, expected in requested.items():
            if key == "event":
                continue
            if frame.get(key) != expected:
                label = "book" if key in {"channel", "symbol"} else key
                raise BitfinexV1DataError(f"Bitfinex subscribed with wrong {label}")
        channel_id = _integer(frame.get("chanId"), "subscription channel id")
        if channel_id <= 0:
            raise BitfinexV1DataError("Bitfinex subscription channel must be positive")
        occupied = {
            self._channel_id,
            self._pending_unsubscribe_channel_id,
            self._funding_channel_id,
            self._pending_funding_unsubscribe_channel_id,
        }
        if channel_id in occupied:
            raise BitfinexV1DataError("Bitfinex reused an active or pending channel")

        pending_ack_count_before = self._pending_subscription_ack_count()
        remaining_pending = pending_ack_count_before - 1
        if channel_id in self._pre_ack_heartbeat_channel_ids:
            self._pre_ack_heartbeat_channel_ids.remove(channel_id)
        elif len(self._pre_ack_heartbeat_channel_ids) > remaining_pending:
            raise BitfinexV1DataError(
                "Bitfinex subscription ACK does not match pre-ACK heartbeat channel"
            )

        if channel == "book":
            self._channel_id = channel_id
            self._book.clear()
            if self._unsubscribe_book_on_ack:
                self._unsubscribe_book_on_ack = False
                self._subscription_requested = False
                self._channel_id = None
                self._pending_unsubscribe_channel_id = channel_id
                self._book_unsubscribe_task = self.create_task(
                    self._send_unsubscribe(channel_id),
                    log_msg="bitfinex-v1-book-unsubscribe",
                )
        else:
            self._funding_channel_id = channel_id
            if self._unsubscribe_funding_on_ack:
                self._unsubscribe_funding_on_ack = False
                self._funding_subscription_requested = False
                self._funding_channel_id = None
                self._pending_funding_unsubscribe_channel_id = channel_id
                self._funding_unsubscribe_task = self.create_task(
                    self._send_unsubscribe(channel_id),
                    log_msg="bitfinex-v1-funding-unsubscribe",
                )

    def _consume_unsubscribed(self, frame: dict[str, object]) -> None:
        if frame.get("status") != "OK":
            raise BitfinexV1DataError("Bitfinex unsubscribe was not acknowledged")
        channel_id = _integer(frame.get("chanId"), "unsubscribe channel id")
        if channel_id == self._pending_unsubscribe_channel_id:
            self._pending_unsubscribe_channel_id = None
            return
        if channel_id == self._pending_funding_unsubscribe_channel_id:
            self._pending_funding_unsubscribe_channel_id = None
            return
        if channel_id == self._channel_id:
            if self._subscription_requested or self._publish_quotes or self._publish_deltas:
                raise BitfinexV1DataError("Bitfinex unexpectedly ended the book subscription")
            self._channel_id = None
            self._book.clear()
            return
        if channel_id == self._funding_channel_id:
            if self._funding_subscription_requested or self._publish_funding:
                raise BitfinexV1DataError("Bitfinex unexpectedly ended the funding subscription")
            self._funding_channel_id = None
            return
        raise BitfinexV1DataError("Bitfinex unsubscribed the wrong channel")

    def _quote_tick(self, timestamp: int | None = None) -> QuoteTick:
        bid, bid_size, ask, ask_size = self._book.quote()
        timestamp = self._clock.timestamp_ns() if timestamp is None else timestamp
        return QuoteTick(
            instrument_id=self._instrument.id,
            bid_price=self._price(bid),
            ask_price=self._price(ask),
            bid_size=self._quantity(bid_size),
            ask_size=self._quantity(ask_size),
            ts_event=timestamp,
            ts_init=timestamp,
        )

    def _order_book_snapshot(self, timestamp: int) -> OrderBookDeltas:
        bids, asks = self._book.levels()
        rows = [
            (OrderSide.BUY, price, size) for price, size in bids
        ] + [
            (OrderSide.SELL, price, size) for price, size in asks
        ]
        deltas = [
            OrderBookDelta(
                instrument_id=self._instrument.id,
                action=BookAction.CLEAR,
                order=None,
                flags=RecordFlag.F_SNAPSHOT,
                sequence=0,
                ts_event=timestamp,
                ts_init=timestamp,
            )
        ]
        for index, (side, price, size) in enumerate(rows):
            flags = RecordFlag.F_SNAPSHOT
            if index == len(rows) - 1:
                flags |= RecordFlag.F_LAST
            deltas.append(
                OrderBookDelta(
                    instrument_id=self._instrument.id,
                    action=BookAction.ADD,
                    order=BookOrder(side, self._price(price), self._quantity(size), 0),
                    flags=flags,
                    sequence=0,
                    ts_event=timestamp,
                    ts_init=timestamp,
                )
            )
        return OrderBookDeltas(instrument_id=self._instrument.id, deltas=deltas)

    def _price(self, value: Decimal) -> Price:
        result = Price.from_str(f"{value:.{self._bfx_config.price_precision}f}")
        if (
            result.as_decimal() != value
            or value % self._bfx_config.price_increment != 0
        ):
            raise BitfinexV1DataError("Bitfinex book exceeds configured instrument precision")
        return result

    def _quantity(self, value: Decimal) -> Quantity:
        result = Quantity.from_str(f"{value:.{self._bfx_config.size_precision}f}")
        if (
            result.as_decimal() != value
            or value % self._bfx_config.size_increment != 0
        ):
            raise BitfinexV1DataError("Bitfinex book exceeds configured instrument precision")
        return result

    def _clear_base_subscriptions(self) -> None:
        self._remove_subscription_quote_ticks(self._instrument.id)
        self._remove_subscription_order_book_deltas(self._instrument.id)
        self._remove_subscription_funding_rates(self._instrument.id)

    def _require_instrument(self, instrument_id: InstrumentId) -> None:
        if instrument_id != self._instrument.id:
            raise BitfinexV1DataError(f"unsupported Bitfinex instrument {instrument_id}")


class BitfinexV1LiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LiveDataClient:
        if not isinstance(config, BitfinexV1DataClientConfig):
            raise TypeError("Bitfinex factory requires BitfinexV1DataClientConfig")
        if name != "BITFINEX":
            raise ValueError("Bitfinex v1 client name must be BITFINEX")
        instrument = instrument_from_config(config, ts_init=clock.timestamp_ns())
        provider = InstrumentProvider(config=config.instrument_provider)
        provider.add(instrument)
        return BitfinexV1DataClient(loop=loop, name=name, config=config, msgbus=msgbus,
                                   cache=cache, clock=clock, instrument_provider=provider)


def _validate_config(config: BitfinexV1DataClientConfig) -> None:
    if config.instrument_id != INSTRUMENT_ID or config.raw_symbol not in SUPPORTED_RAW_SYMBOLS:
        raise ValueError(
            "Bitfinex v1 supports only the production or paper XAUT perpetual profile"
        )
    if not config.url.startswith("wss://"):
        raise ValueError("Bitfinex public endpoint must use wss://")
    if config.routing.default or config.routing.venues != frozenset({"BITFINEX"}):
        raise ValueError("Bitfinex routing must be non-default and restricted to BITFINEX")
    decimals = (
        config.price_increment,
        config.size_increment,
        config.min_quantity,
        config.max_quantity,
        config.margin_init,
        config.margin_maint,
        config.maker_fee,
        config.taker_fee,
    )
    if any(not value.is_finite() for value in decimals):
        raise ValueError("Bitfinex instrument configuration must be finite")
    positive = (
        config.price_increment,
        config.size_increment,
        config.min_quantity,
        config.max_quantity,
    )
    if min(positive) <= 0:
        raise ValueError("Bitfinex increments and quantities must be positive")
    if config.price_precision != _decimal_places(config.price_increment):
        raise ValueError("price_precision differs from price_increment")
    if config.size_precision != _decimal_places(config.size_increment):
        raise ValueError("size_precision differs from size_increment")
    if config.min_quantity > config.max_quantity:
        raise ValueError("min_quantity exceeds max_quantity")
    quantities = (config.min_quantity, config.max_quantity)
    if any(value % config.size_increment != 0 for value in quantities):
        raise ValueError("Bitfinex configured quantities must align to size_increment")
    if not 0 <= config.margin_maint <= config.margin_init <= 1:
        raise ValueError("Bitfinex margin rates are invalid")
    if any(abs(fee) > 1 for fee in (config.maker_fee, config.taker_fee)):
        raise ValueError("Bitfinex fee rates are invalid")
    if not 1_000 <= config.open_timeout_ms <= 60_000:
        raise ValueError("Bitfinex open timeout is outside the supported range")


def _book_row(value: object) -> tuple[Decimal, int, Decimal]:
    if not isinstance(value, list) or len(value) != 3:
        raise BitfinexV1DataError("Bitfinex book level must have three fields")
    price = _decimal(value[0], "price")
    count = _integer(value[1], "count")
    amount = _decimal(value[2], "amount")
    if price <= 0 or count < 0 or amount == 0:
        raise BitfinexV1DataError("Bitfinex book level has invalid values")
    if count == 0 and amount not in {Decimal(-1), Decimal(1)}:
        raise BitfinexV1DataError("Bitfinex deletion amount must be exactly -1 or 1")
    return price, count, amount


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise BitfinexV1DataError(f"Bitfinex {label} must be a JSON number")
    result = Decimal(value)
    if not result.is_finite():
        raise BitfinexV1DataError(f"Bitfinex {label} must be finite")
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BitfinexV1DataError(f"Bitfinex {label} must be an integer")
    return value


def _crc_token(value: Decimal) -> str:
    if value == value.to_integral_value():
        return format(value, "f").split(".", maxsplit=1)[0]
    if value.copy_abs().adjusted() >= -6:
        return format(value, "f").rstrip("0").rstrip(".")
    return str(value.normalize()).lower().replace("e-0", "e-").replace("e+", "e")


def _decimal_places(value: Decimal) -> int:
    return max(0, -cast(int, value.as_tuple().exponent))
