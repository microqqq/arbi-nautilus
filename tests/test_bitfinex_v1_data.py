from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import RoutingConfig, TradingNodeConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.data.messages import (
    SubscribeFundingRates,
    SubscribeOrderBook,
    SubscribeQuoteTicks,
    UnsubscribeFundingRates,
    UnsubscribeOrderBook,
    UnsubscribeQuoteTicks,
)
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import (
    FundingRateUpdate,
    OrderBookDelta,
    OrderBookDeltas,
    QuoteTick,
)
from nautilus_trader.model.enums import BookAction, BookType, RecordFlag
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from py000_nautilus.bitfinex_v1_data import (
    INSTRUMENT_ID,
    PAPER_RAW_SYMBOL,
    RAW_SYMBOL,
    BitfinexP0Book,
    BitfinexV1DataClient,
    BitfinexV1DataClientConfig,
    BitfinexV1DataError,
    BitfinexV1LiveDataClientFactory,
    instrument_from_config,
)
from py000_nautilus.bitfinex_v1_transport import (
    BitfinexV1Transport,
    BitfinexV1TransportError,
)

ROOT = Path(__file__).parents[1]
RAW_CAPTURE = cast(
    list[str],
    json.loads((ROOT / "tests/fixtures/bitfinex_xaut_book.json").read_text()),
)
CAPTURE = [json.loads(frame, parse_float=Decimal) for frame in RAW_CAPTURE]
CHANNEL_ID = 474_371
FUNDING_CHANNEL_ID = 474_372


def _config(**changes: object) -> BitfinexV1DataClientConfig:
    values: dict[str, object] = {
        "url": "wss://api-pub.bitfinex.com/ws/2",
        "instrument_id": INSTRUMENT_ID,
        "raw_symbol": RAW_SYMBOL,
        "price_precision": 1,
        "size_precision": 8,
        "price_increment": Decimal("0.1"),
        "size_increment": Decimal("0.00000001"),
        "min_quantity": Decimal("0.00000001"),
        "max_quantity": Decimal("100000"),
        "margin_init": Decimal("0.1"),
        "margin_maint": Decimal("0.05"),
        "maker_fee": Decimal(0),
        "taker_fee": Decimal("0.0002"),
        "routing": RoutingConfig(default=False, venues=frozenset({"BITFINEX"})),
    }
    values.update(changes)
    return BitfinexV1DataClientConfig(**values)  # type: ignore[arg-type]


class _FakeTransport:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, object] | list[object]] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.opened = False
        self.closed = False
        self.fail_send_at: int | None = None

    async def open(self) -> None:
        self.opened = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def send_json(self, payload: dict[str, object]) -> None:
        if self.fail_send_at == len(self.sent):
            raise ConnectionError("synthetic send failure")
        self.sent.append(payload)

    async def recv_json(self) -> dict[str, object] | list[object]:
        return await self.queue.get()


class _Socket:
    def __init__(self, messages: list[str | bytes]) -> None:
        self.messages = iter(messages)
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str | bytes:
        return next(self.messages)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


def _client(
    fake: _FakeTransport,
    *,
    raw_symbol: str = RAW_SYMBOL,
) -> BitfinexV1DataClient:
    clock = TestComponentStubs.clock()
    profile = (
        {
            "min_quantity": Decimal("2"),
            "max_quantity": Decimal("10000"),
            "margin_init": Decimal("0.01"),
            "margin_maint": Decimal("0.005"),
        }
        if raw_symbol == PAPER_RAW_SYMBOL
        else {}
    )
    return BitfinexV1DataClient(
        loop=asyncio.get_running_loop(),
        name="BITFINEX",
        config=_config(raw_symbol=raw_symbol, **profile),
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=clock,
        instrument_provider=InstrumentProvider(),
        transport=fake,
    )


def _subscription(
    channel_id: int = CHANNEL_ID,
    *,
    symbol: str = RAW_SYMBOL,
    prec: str = "P0",
    freq: str = "F0",
    length: str = "25",
    sub_id: str = "py000-xaut-book-v1",
) -> dict[str, object]:
    return {
        "event": "subscribed",
        "channel": "book",
        "chanId": channel_id,
        "symbol": symbol,
        "prec": prec,
        "freq": freq,
        "len": length,
        "subId": sub_id,
    }


def _funding_subscription(
    channel_id: int = FUNDING_CHANNEL_ID,
    *,
    symbol: str = RAW_SYMBOL,
) -> dict[str, object]:
    return {
        "event": "subscribed",
        "channel": "status",
        "chanId": channel_id,
        "key": f"deriv:{symbol}",
    }


def _funding_payload(
    *,
    timestamp_ms: object = 1_788_439_852_000,
    rate: object = Decimal("-0.00073345"),
) -> list[object]:
    return [
        timestamp_ms,
        None,
        Decimal("4477.35"),
        Decimal("4476.6"),
        None,
        Decimal("-60319661.40911106"),
        None,
        1_788_451_200_000,
        rate,
        5_578,
        None,
        Decimal("-0.00092158"),
        None,
        None,
        Decimal("4476.6"),
        None,
        None,
        Decimal("8.6061607"),
        None,
        None,
        None,
        Decimal("0.0005"),
        Decimal("0.0025"),
    ]


def _quote_command() -> SubscribeQuoteTicks:
    return SubscribeQuoteTicks(
        instrument_id=INSTRUMENT_ID,
        client_id=ClientId("BITFINEX"),
        venue=Venue("BITFINEX"),
        command_id=UUID4(),
        ts_init=0,
    )


def _quote_unsubscribe_command() -> UnsubscribeQuoteTicks:
    return UnsubscribeQuoteTicks(
        instrument_id=INSTRUMENT_ID,
        client_id=ClientId("BITFINEX"),
        venue=Venue("BITFINEX"),
        command_id=UUID4(),
        ts_init=0,
    )


def _funding_command() -> SubscribeFundingRates:
    return SubscribeFundingRates(
        instrument_id=INSTRUMENT_ID,
        client_id=ClientId("BITFINEX"),
        venue=Venue("BITFINEX"),
        command_id=UUID4(),
        ts_init=0,
    )


def _funding_unsubscribe_command() -> UnsubscribeFundingRates:
    return UnsubscribeFundingRates(
        instrument_id=INSTRUMENT_ID,
        client_id=ClientId("BITFINEX"),
        venue=Venue("BITFINEX"),
        command_id=UUID4(),
        ts_init=0,
    )


def _book_command(
    *,
    book_type: BookType = BookType.L2_MBP,
    depth: int = 25,
) -> SubscribeOrderBook:
    return SubscribeOrderBook(
        instrument_id=INSTRUMENT_ID,
        book_data_type=OrderBookDelta,
        book_type=book_type,
        client_id=ClientId("BITFINEX"),
        venue=Venue("BITFINEX"),
        command_id=UUID4(),
        ts_init=0,
        depth=depth,
        managed=True,
    )


def _book_unsubscribe_command() -> UnsubscribeOrderBook:
    return UnsubscribeOrderBook(
        instrument_id=INSTRUMENT_ID,
        book_data_type=OrderBookDelta,
        client_id=ClientId("BITFINEX"),
        venue=Venue("BITFINEX"),
        command_id=UUID4(),
        ts_init=0,
    )


def _replay(book: BitfinexP0Book) -> None:
    for frame in CAPTURE:
        payload = frame[1]
        if payload == "cs":
            assert book.verify(frame[2])
        elif isinstance(payload[0], list):
            book.apply_snapshot(payload)
        else:
            book.apply_delta(payload)


def test_real_capture_checksum_and_bbo_semantics() -> None:
    book = BitfinexP0Book()
    _replay(book)

    assert book.quote() == (
        Decimal("4455.2"),
        Decimal("0.11766725"),
        Decimal("4456"),
        Decimal("0.17296923"),
    )


def test_verified_book_projects_a_native_depth_25_snapshot() -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        _replay(client._book)
        snapshot = client._order_book_snapshot(123)

        assert isinstance(snapshot, OrderBookDeltas)
        assert snapshot.is_snapshot
        assert len(snapshot.deltas) == 51
        assert snapshot.deltas[0].action == BookAction.CLEAR
        assert all(delta.flags & RecordFlag.F_SNAPSHOT for delta in snapshot.deltas)
        assert not any(delta.flags & RecordFlag.F_LAST for delta in snapshot.deltas[:-1])
        assert snapshot.deltas[-1].flags == (
            RecordFlag.F_SNAPSHOT | RecordFlag.F_LAST
        )
        bids = [delta for delta in snapshot.deltas if delta.order.side.name == "BUY"]
        asks = [delta for delta in snapshot.deltas if delta.order.side.name == "SELL"]
        assert len(bids) == len(asks) == 25
        assert str(bids[0].order.price) == "4455.2"
        assert str(bids[0].order.size) == "0.11766725"
        assert str(asks[0].order.price) == "4456.0"
        assert str(asks[0].order.size) == "0.17296923"

    asyncio.run(scenario())


def test_delta_delete_is_side_specific_and_active_level_can_switch_side() -> None:
    book = BitfinexP0Book()
    snapshot = CAPTURE[0][1]
    book.apply_snapshot(snapshot)
    book.apply_delta([Decimal("4455.2"), 0, 1])
    book.apply_delta([Decimal("4455.2"), 0, 1])
    with pytest.raises(BitfinexV1DataError, match="exactly"):
        book.apply_delta([Decimal("4456"), 0, Decimal("-0.5")])
    book.apply_delta([Decimal("4456"), 1, Decimal("0.25")])
    book.apply_delta([Decimal("4456"), 0, -1])
    assert book._bids[Decimal("4456")] == Decimal("0.25")
    assert Decimal("4456") not in book._asks


def test_book_rejects_malformed_snapshot_delta_and_unavailable_quote() -> None:
    book = BitfinexP0Book()
    with pytest.raises(BitfinexV1DataError, match="actionable"):
        book.quote()
    with pytest.raises(BitfinexV1DataError, match="50 levels"):
        book.apply_snapshot([])
    with pytest.raises(BitfinexV1DataError, match="before"):
        book.apply_delta([1, 1, 1])
    with pytest.raises(BitfinexV1DataError, match="three fields"):
        book.apply_snapshot([[1, 1]] * 50)


def test_atomic_snapshot_is_actionable_until_a_delta_requires_crc() -> None:
    book = BitfinexP0Book()
    book.apply_snapshot(CAPTURE[0][1])

    assert book.quote()[0] == Decimal("4455.2")

    book.apply_delta(CAPTURE[1][1])

    assert not book.is_actionable
    with pytest.raises(BitfinexV1DataError, match="actionable"):
        book.quote()


def test_instrument_comes_only_from_complete_strict_config() -> None:
    instrument = instrument_from_config(_config(), ts_init=7)

    assert isinstance(instrument, CryptoPerpetual)
    assert instrument.id == INSTRUMENT_ID
    assert instrument.raw_symbol == Symbol(RAW_SYMBOL)
    assert str(instrument.price_increment) == "0.1"
    assert str(instrument.size_increment) == "0.00000001"
    assert str(instrument.multiplier) == "1"
    assert instrument.info["checksum_required"] is True


def test_paper_profile_preserves_canonical_instrument_and_uses_test_wire_symbol() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        client = _client(fake, raw_symbol=PAPER_RAW_SYMBOL)
        instrument = instrument_from_config(
            _config(
                raw_symbol=PAPER_RAW_SYMBOL,
                min_quantity=Decimal("2"),
                max_quantity=Decimal("10000"),
                margin_init=Decimal("0.01"),
                margin_maint=Decimal("0.005"),
            ),
            ts_init=7,
        )

        assert instrument.id == INSTRUMENT_ID
        assert instrument.raw_symbol == Symbol(PAPER_RAW_SYMBOL)
        assert instrument.quote_currency.code == "USDT"
        assert instrument.info["bitfinex_environment"] == "paper"

        await fake.open()
        client._set_connected(True)
        client._running = True
        await client._subscribe_quote_ticks(_quote_command())
        assert fake.sent[-1]["symbol"] == PAPER_RAW_SYMBOL
        client._consume_frame(_subscription(symbol=PAPER_RAW_SYMBOL))
        assert client._channel_id == CHANNEL_ID
        await client._disconnect()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"raw_symbol": "tBTCF0:USTF0"}, "supports only"),
        ({"instrument_id": InstrumentId.from_str("OTHER.BITFINEX")}, "supports only"),
        ({"price_precision": 2}, "price_precision"),
        ({"url": "ws://127.0.0.1"}, "wss"),
        ({"routing": RoutingConfig()}, "routing"),
    ],
)
def test_config_rejects_scope_or_spec_drift(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        instrument_from_config(_config(**change), ts_init=0)


def test_subscription_is_exact_and_send_failure_rolls_back() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        client = _client(fake)
        await fake.open()
        client._set_connected(True)
        client._running = True
        await client._subscribe_quote_ticks(_quote_command())
        assert fake.sent == [
            {"event": "conf", "flags": 131_072},
            {
                "event": "subscribe",
                "channel": "book",
                "symbol": RAW_SYMBOL,
                "prec": "P0",
                "freq": "F0",
                "len": "25",
                "subId": "py000-xaut-book-v1",
            },
        ]
        await client._subscribe_order_book_deltas(_book_command())
        assert len(fake.sent) == 2
        await client._unsubscribe_quote_ticks(_quote_unsubscribe_command())
        assert len(fake.sent) == 2
        assert client._subscription_requested
        client._consume_frame(_subscription())
        await client._unsubscribe_order_book_deltas(_book_unsubscribe_command())
        assert fake.sent[-1] == {"event": "unsubscribe", "chanId": CHANNEL_ID}
        assert client._channel_id is None
        await client._subscribe_quote_ticks(_quote_command())
        assert fake.sent[-1] == {
            "event": "subscribe",
            "channel": "book",
            "symbol": RAW_SYMBOL,
            "prec": "P0",
            "freq": "F0",
            "len": "25",
            "subId": "py000-xaut-book-v1",
        }
        assert client._consume_frame([CHANNEL_ID, "hb"]) == ()
        assert client._consume_frame(
            [CHANNEL_ID, [Decimal("4455"), 1, Decimal("0.1")]]
        ) == ()
        client._consume_frame(
            {"event": "unsubscribed", "status": "OK", "chanId": CHANNEL_ID}
        )
        client._consume_frame(_subscription(channel_id=CHANNEL_ID + 1))
        assert client._channel_id == CHANNEL_ID + 1
        assert client._pending_unsubscribe_channel_id is None

        failed = _FakeTransport()
        failed.fail_send_at = 1
        blocked = _client(failed)
        await failed.open()
        blocked._set_connected(True)
        blocked._running = True
        with pytest.raises(ConnectionError, match="synthetic"):
            await blocked._subscribe_quote_ticks(_quote_command())
        assert not blocked.is_connected
        assert blocked._subscription_requested is False
        assert failed.closed

        unsubscribe_failed = _FakeTransport()
        stale = _client(unsubscribe_failed)
        await unsubscribe_failed.open()
        stale._set_connected(True)
        stale._running = True
        stale._subscription_requested = True
        stale._publish_quotes = True
        stale._consume_frame(_subscription())
        unsubscribe_failed.fail_send_at = 0
        with pytest.raises(ConnectionError, match="synthetic"):
            await stale._unsubscribe_quote_ticks(_quote_unsubscribe_command())
        assert not stale.is_connected
        assert stale._channel_id is None
        assert unsubscribe_failed.closed

    asyncio.run(scenario())


def test_book_and_funding_share_transport_with_independent_lifecycles() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        client = _client(fake)
        await fake.open()
        client._set_connected(True)
        client._running = True

        await client._subscribe_quote_ticks(_quote_command())
        client._consume_frame(_subscription())
        await client._subscribe_funding_rates(_funding_command())
        assert fake.sent == [
            {"event": "conf", "flags": 131_072},
            {
                "event": "subscribe",
                "channel": "book",
                "symbol": RAW_SYMBOL,
                "prec": "P0",
                "freq": "F0",
                "len": "25",
                "subId": "py000-xaut-book-v1",
            },
            {
                "event": "subscribe",
                "channel": "status",
                "key": f"deriv:{RAW_SYMBOL}",
            },
        ]
        client._consume_frame(_funding_subscription())

        updates = client._consume_frame([FUNDING_CHANNEL_ID, _funding_payload()])
        assert len(updates) == 1
        funding = updates[0]
        assert isinstance(funding, FundingRateUpdate)
        assert funding.instrument_id == INSTRUMENT_ID
        assert str(funding.rate) == "-0.00073345"
        assert funding.ts_event == 1_788_439_852_000_000_000

        await client._unsubscribe_quote_ticks(_quote_unsubscribe_command())
        assert fake.sent[-1] == {"event": "unsubscribe", "chanId": CHANNEL_ID}
        assert client._funding_channel_id == FUNDING_CHANNEL_ID
        assert isinstance(
            client._consume_frame([FUNDING_CHANNEL_ID, _funding_payload()])[0],
            FundingRateUpdate,
        )
        client._consume_frame(
            {"event": "unsubscribed", "status": "OK", "chanId": CHANNEL_ID}
        )

        await client._unsubscribe_funding_rates(_funding_unsubscribe_command())
        assert fake.sent[-1] == {
            "event": "unsubscribe",
            "chanId": FUNDING_CHANNEL_ID,
        }
        assert cast(Any, client)._channel_id is None
        assert cast(Any, client)._funding_channel_id is None
        client._consume_frame(
            {
                "event": "unsubscribed",
                "status": "OK",
                "chanId": FUNDING_CHANNEL_ID,
            }
        )
        await client._disconnect()

    asyncio.run(scenario())


def test_funding_first_book_arm_and_deferred_cancel_keep_channel_routes_isolated() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        client = _client(fake)
        await fake.open()
        client._set_connected(True)
        client._running = True

        # Match the one-shot Taker lifecycle: funding is live before the book is armed.
        await client._subscribe_funding_rates(_funding_command())
        client._consume_frame(_funding_subscription())
        assert isinstance(
            client._consume_frame([FUNDING_CHANNEL_ID, _funding_payload()])[0],
            FundingRateUpdate,
        )

        await client._subscribe_order_book_deltas(_book_command())
        assert client._channel_id is None
        assert client._funding_channel_id == FUNDING_CHANNEL_ID
        assert client._consume_frame([FUNDING_CHANNEL_ID, "hb"]) == ()
        assert isinstance(
            client._consume_frame([FUNDING_CHANNEL_ID, _funding_payload()])[0],
            FundingRateUpdate,
        )

        # If shutdown races the book ACK, only that book channel is retired. Funding
        # remains routable throughout the deferred unsubscribe handshake.
        await client._unsubscribe_order_book_deltas(_book_unsubscribe_command())
        client._consume_frame(_subscription())
        await _wait_until(lambda: fake.sent[-1].get("event") == "unsubscribe")
        assert fake.sent[-1] == {"event": "unsubscribe", "chanId": CHANNEL_ID}
        assert client._channel_id is None
        assert cast(Any, client)._pending_unsubscribe_channel_id == CHANNEL_ID
        assert client._consume_frame(CAPTURE[0]) == ()
        assert isinstance(
            client._consume_frame([FUNDING_CHANNEL_ID, _funding_payload()])[0],
            FundingRateUpdate,
        )
        client._consume_frame(
            {"event": "unsubscribed", "status": "OK", "chanId": CHANNEL_ID}
        )
        assert cast(Any, client)._pending_unsubscribe_channel_id is None
        assert client._funding_channel_id == FUNDING_CHANNEL_ID
        await client._disconnect()

    asyncio.run(scenario())


def test_funding_subscription_ack_is_bound_to_exact_key_and_unique_channel() -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        client._funding_subscription_requested = True

        with pytest.raises(BitfinexV1DataError, match="wrong key"):
            client._consume_frame(_funding_subscription(symbol="tBTCF0:USTF0"))

        client._channel_id = CHANNEL_ID
        with pytest.raises(BitfinexV1DataError, match="reused"):
            client._consume_frame(_funding_subscription(channel_id=CHANNEL_ID))

        client._consume_frame(_funding_subscription())
        with pytest.raises(BitfinexV1DataError, match="repeated"):
            client._consume_frame(_funding_subscription(channel_id=FUNDING_CHANNEL_ID + 1))

        unsolicited = _client(_FakeTransport())
        with pytest.raises(BitfinexV1DataError, match="unsolicited.*funding"):
            unsolicited._consume_frame(_funding_subscription())

    asyncio.run(scenario())


def test_unsubscribe_before_ack_is_deferred_without_closing_shared_transport() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        client = _client(fake)
        await fake.open()
        client._set_connected(True)
        client._running = True

        await client._subscribe_quote_ticks(_quote_command())
        client._consume_frame(_subscription())
        await client._subscribe_funding_rates(_funding_command())
        await client._unsubscribe_funding_rates(_funding_unsubscribe_command())
        assert fake.sent[-1]["event"] == "subscribe"

        client._consume_frame(_funding_subscription())
        await _wait_until(lambda: fake.sent[-1].get("event") == "unsubscribe")
        assert fake.sent[-1] == {
            "event": "unsubscribe",
            "chanId": FUNDING_CHANNEL_ID,
        }
        assert client._channel_id == CHANNEL_ID
        assert client._consume_frame([CHANNEL_ID, "hb"]) == ()
        assert client.is_connected

        client._consume_frame(
            {
                "event": "unsubscribed",
                "status": "OK",
                "chanId": FUNDING_CHANNEL_ID,
            }
        )
        await client._disconnect()

    asyncio.run(scenario())


def test_unsubscribe_ack_must_explicitly_confirm_success() -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        client._pending_funding_unsubscribe_channel_id = FUNDING_CHANNEL_ID

        with pytest.raises(BitfinexV1DataError, match="not acknowledged"):
            client._consume_frame(
                {
                    "event": "unsubscribed",
                    "status": "ERROR",
                    "chanId": FUNDING_CHANNEL_ID,
                }
            )

        assert client._pending_funding_unsubscribe_channel_id == FUNDING_CHANNEL_ID

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (
            [FUNDING_CHANNEL_ID, _funding_payload()[:8]],
            "does not contain its funding field",
        ),
        (
            [FUNDING_CHANNEL_ID, _funding_payload(timestamp_ms=True)],
            "timestamp must be an integer",
        ),
        (
            [FUNDING_CHANNEL_ID, _funding_payload(timestamp_ms=0)],
            "timestamp is invalid",
        ),
        (
            [FUNDING_CHANNEL_ID, _funding_payload(rate="-0.0001")],
            "must be a JSON number",
        ),
        (
            [FUNDING_CHANNEL_ID, _funding_payload(rate=Decimal("NaN"))],
            "must be finite",
        ),
        (
            [FUNDING_CHANNEL_ID, _funding_payload(rate=Decimal("1.1"))],
            "outside",
        ),
        ([FUNDING_CHANNEL_ID, _funding_payload(), 1], "status frame is malformed"),
    ],
)
def test_funding_frame_is_strict(frame: list[object], message: str) -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        client._funding_channel_id = FUNDING_CHANNEL_ID
        client._publish_funding = True
        with pytest.raises(BitfinexV1DataError, match=message):
            client._consume_frame(frame)

    asyncio.run(scenario())


def test_funding_frame_ignores_documented_tail_extensions() -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        client._funding_channel_id = FUNDING_CHANNEL_ID
        client._publish_funding = True
        payload = [*_funding_payload(), "future-field"]

        updates = client._consume_frame([FUNDING_CHANNEL_ID, payload])

        assert len(updates) == 1
        assert isinstance(updates[0], FundingRateUpdate)
        assert str(updates[0].rate) == "-0.00073345"

    asyncio.run(scenario())


def test_failed_public_funding_subscription_rolls_back_and_can_retry() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        fake.fail_send_at = 1
        client = _client(fake)
        await fake.open()
        client._set_connected(True)
        client._running = True

        client.subscribe_funding_rates(_funding_command())
        await _wait_until(lambda: fake.closed and not client.is_connected)
        assert INSTRUMENT_ID not in client.subscribed_funding_rates()
        assert not client._funding_subscription_requested

        fake.fail_send_at = None
        fake.sent.clear()
        await fake.open()
        client._set_connected(True)
        client._running = True
        client.subscribe_funding_rates(_funding_command())
        await _wait_until(lambda: len(fake.sent) == 2)
        assert fake.sent[-1] == {
            "event": "subscribe",
            "channel": "status",
            "key": f"deriv:{RAW_SYMBOL}",
        }
        assert client.subscribed_funding_rates() == [INSTRUMENT_ID]
        await client._disconnect()

    asyncio.run(scenario())


def test_failed_public_book_subscription_rolls_back_and_can_retry() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        fake.fail_send_at = 1
        client = _client(fake)
        await fake.open()
        client._set_connected(True)
        client._running = True

        client.subscribe_order_book_deltas(_book_command())
        await _wait_until(lambda: fake.closed and not client.is_connected)
        assert not client.is_subscribed_order_book_deltas(INSTRUMENT_ID)

        fake.fail_send_at = None
        fake.sent.clear()
        await fake.open()
        client._set_connected(True)
        client._running = True
        client.subscribe_order_book_deltas(_book_command())
        await _wait_until(lambda: len(fake.sent) == 2)
        assert client.is_subscribed_order_book_deltas(INSTRUMENT_ID)
        await client._disconnect()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command",
    [
        _book_command(book_type=BookType.L3_MBO),
        _book_command(depth=10),
    ],
)
def test_native_book_subscription_requires_l2_depth_25(
    command: SubscribeOrderBook,
) -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        client = _client(fake)
        client._add_subscription_order_book_deltas(command.instrument_id)
        with pytest.raises(BitfinexV1DataError, match="L2_MBP depth-25"):
            await client._subscribe_order_book_deltas(command)
        assert not client.is_subscribed_order_book_deltas(command.instrument_id)
        assert fake.sent == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (_subscription(symbol="tBTCF0:USTF0"), "wrong book"),
        (_subscription(prec="R0"), "wrong prec"),
        (_subscription(freq="F1"), "wrong freq"),
        (_subscription(length="100"), "wrong len"),
        (_subscription(sub_id="other"), "wrong subId"),
    ],
)
def test_subscription_ack_must_echo_the_exact_contract(
    frame: dict[str, object], message: str
) -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        client._subscription_requested = True
        with pytest.raises(BitfinexV1DataError, match=message):
            client._consume_frame(frame)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        ({"event": "info", "version": 2}, "status is missing"),
        ({"event": "info", "version": 2, "platform": {"status": 0}}, "not operational"),
        (
            {"event": "info", "version": 2, "platform": {"status": True}},
            "must be an integer",
        ),
    ],
)
def test_platform_must_explicitly_report_operational(
    frame: dict[str, object], message: str
) -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        with pytest.raises(BitfinexV1DataError, match=message):
            client._consume_frame(frame)

    asyncio.run(scenario())


def test_server_ended_subscription_disconnects_the_reader() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        client = _client(fake)
        client.connect()
        await _wait_until(lambda: client.is_connected)
        await client._subscribe_quote_ticks(_quote_command())
        await fake.queue.put(_subscription())
        await fake.queue.put(
            {"event": "unsubscribed", "status": "OK", "chanId": CHANNEL_ID}
        )
        await _wait_until(lambda: fake.closed and not client.is_connected)
        assert client.last_failure is not None
        assert "unexpectedly ended" in client.last_failure

    asyncio.run(scenario())


def test_unknown_channel_duplicate_ack_and_bad_crc_fail_immediately() -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        with pytest.raises(
            BitfinexV1DataError,
            match=(
                r"unexpected channel 474371 \(book=None, funding=None, "
                r"pending_book=None, pending_funding=None\)"
            ),
        ):
            client._consume_frame(CAPTURE[0])
        client._subscription_requested = True
        client._consume_frame(_subscription())
        with pytest.raises(BitfinexV1DataError, match="repeated"):
            client._consume_frame(_subscription())
        for frame in CAPTURE[:-1]:
            client._consume_frame(frame)
        bad = [CHANNEL_ID, "cs", CAPTURE[-1][2] + 1]
        with pytest.raises(BitfinexV1DataError, match="checksum mismatch"):
            client._consume_frame(bad)

    asyncio.run(scenario())


def test_client_publishes_atomic_snapshot_then_revalidates_deltas_with_crc() -> None:
    async def scenario() -> None:
        clock = TestComponentStubs.clock()
        msgbus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock)
        fake = _FakeTransport()
        client = BitfinexV1DataClient(
            loop=asyncio.get_running_loop(),
            name="BITFINEX",
            config=_config(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(),
            transport=fake,
        )
        engine.register_client(client)
        try:
            client.connect()
            await _wait_until(lambda: client.is_connected)
            engine.execute(_quote_command())
            engine.execute(_book_command())
            await _wait_until(lambda: len(fake.sent) == 2)
            await fake.queue.put(_subscription())
            await fake.queue.put(CAPTURE[0])
            await _wait_until(lambda: cache.quote_tick(INSTRUMENT_ID) is not None)
            assert client.book_is_actionable
            quote = cache.quote_tick(INSTRUMENT_ID)
            assert isinstance(quote, QuoteTick)
            assert str(quote.bid_price) == "4455.2"
            assert str(quote.bid_size) == "0.11766725"
            assert str(quote.ask_price) == "4456.0"
            assert str(quote.ask_size) == "0.17296923"
            book = cache.order_book(INSTRUMENT_ID)
            assert book is not None
            assert book.book_type == BookType.L2_MBP
            assert len(book.bids()) == len(book.asks()) == 25

            for frame in CAPTURE[1:-1]:
                await fake.queue.put(frame)
            await _wait_until(lambda: not client.book_is_actionable)
            await fake.queue.put(CAPTURE[-1])
            await _wait_until(lambda: client.book_is_actionable)
            quote = cache.quote_tick(INSTRUMENT_ID)
            assert isinstance(quote, QuoteTick)
            assert str(quote.bid_price) == "4455.2"
            assert str(quote.bid_size) == "0.11766725"
            assert str(quote.ask_price) == "4456.0"
            assert str(quote.ask_size) == "0.17296923"
            assert book.book_type == BookType.L2_MBP
            assert len(book.bids()) == len(book.asks()) == 25
            assert str(book.best_bid_price()) == "4455.2"
            assert str(book.best_bid_size()) == "0.11766725"
            assert str(book.best_ask_price()) == "4456.0"
            assert str(book.best_ask_size()) == "0.17296923"
            engine.execute(_quote_unsubscribe_command())
            await asyncio.sleep(0.01)
            assert len(fake.sent) == 2
            engine.execute(_book_unsubscribe_command())
            await _wait_until(lambda: len(fake.sent) == 3)
            assert fake.sent[-1] == {"event": "unsubscribe", "chanId": CHANNEL_ID}
        finally:
            client.disconnect()
            await _wait_until(lambda: fake.closed and not client.is_connected)
            engine.dispose()

    asyncio.run(scenario())


def test_quote_tick_rejects_instrument_precision_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        client = _client(_FakeTransport())
        monkeypatch.setattr(
            client._book,
            "quote",
            lambda: (
                Decimal("4455.25"),
                Decimal("0.11766725"),
                Decimal("4456"),
                Decimal("0.17296923"),
            ),
        )
        with pytest.raises(BitfinexV1DataError, match="precision"):
            client._quote_tick()

    asyncio.run(scenario())


def test_reader_checksum_failure_disconnects_after_atomic_snapshot() -> None:
    async def scenario() -> None:
        fake = _FakeTransport()
        client = _client(fake)
        client.connect()
        await _wait_until(lambda: client.is_connected)
        await client._subscribe_quote_ticks(_quote_command())
        await fake.queue.put(_subscription())
        for frame in CAPTURE[:-1]:
            await fake.queue.put(frame)
        await fake.queue.put([CHANNEL_ID, "cs", CAPTURE[-1][2] + 1])
        await _wait_until(lambda: fake.closed and not client.is_connected)
        assert client.last_failure is not None
        assert "checksum mismatch" in client.last_failure

    asyncio.run(scenario())


def test_transport_rejects_non_strict_json_without_network() -> None:
    async def scenario() -> None:
        transport = BitfinexV1Transport("wss://example.invalid")
        socket = _Socket(['{"x":1,"x":2}', "NaN", "[1,2.5]"])
        transport._socket = cast(Any, socket)
        with pytest.raises(BitfinexV1TransportError, match="strict JSON"):
            await transport.recv_json()
        with pytest.raises(BitfinexV1TransportError, match="strict JSON"):
            await transport.recv_json()
        assert await transport.recv_json() == [1, Decimal("2.5")]
        await transport.send_json({"event": "conf", "flags": 131_072})
        assert socket.sent == ['{"event":"conf","flags":131072}']
        await transport.close()
        assert socket.closed

    asyncio.run(scenario())


def test_factory_and_trading_node_build_do_not_open_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_open(_transport: BitfinexV1Transport) -> None:
        raise AssertionError("factory build must stay offline")

    monkeypatch.setattr(BitfinexV1Transport, "open", unexpected_open)
    loop = asyncio.new_event_loop()
    node = TradingNode(
        config=TradingNodeConfig(
            trader_id=TraderId("PY000-BFX-READ-001"),
            data_clients={"BITFINEX": _config()},
            exec_clients={},
            exec_engine=LiveExecEngineConfig(reconciliation=False),
        ),
        loop=loop,
    )
    try:
        node.add_data_client_factory("BITFINEX", BitfinexV1LiveDataClientFactory)
        node.build()
        client = node.kernel.data_engine.routing_map[Venue("BITFINEX")]
        assert isinstance(client, BitfinexV1DataClient)
        assert not client.is_connected
        assert node.kernel.exec_engine.registered_clients == []
    finally:
        node.dispose()

    assert loop.is_closed()


async def _wait_until(predicate: Callable[[], bool], attempts: int = 40) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")
