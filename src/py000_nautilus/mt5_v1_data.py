"""Nautilus read-only market-data adapter for one PY000 MT5 XAUUSD EA."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.data.messages import (
    SubscribeInstrumentStatus,
    SubscribeQuoteTicks,
    UnsubscribeInstrumentStatus,
    UnsubscribeQuoteTicks,
)
from nautilus_trader.live.data_client import LiveDataClient, LiveMarketDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.data import InstrumentStatus, QuoteTick
from nautilus_trader.model.enums import AssetClass, MarketStatusAction
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Cfd
from nautilus_trader.model.objects import Currency, Price, Quantity

from py000_nautilus.mt5_v1_protocol import Binding, Identity, JsonObject, RecoveryState
from py000_nautilus.mt5_v1_transport import (
    Mt5V1RemoteError,
    Mt5V1Transport,
)


class Mt5V1DataError(RuntimeError):
    """MT5 data cannot be projected without weakening the read-only contract."""


MAX_TICK_FUTURE_MS = 1_000


class Mt5V1DataClientConfig(LiveDataClientConfig, kw_only=True, frozen=True):
    """Explicit identity and endpoints for one read-only MT5 data client."""

    pub_url: str
    rep_url: str
    instrument_id: InstrumentId
    expected_account_id: str
    expected_symbol: str
    expected_magic: str
    expected_ea_build_id: str
    expected_source_sha256: str
    expected_execution_enabled: bool = False
    expected_server_timezone: str = "Europe/Athens"
    request_timeout_ms: int = 1_000
    snapshot_interval_ms: int = 1_000
    max_snapshot_age_ms: int = 5_000
    max_tick_age_ms: int = 15_000


class _ReadOnlyTransport(Protocol):
    @property
    def topic(self) -> bytes: ...

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def hello(self) -> tuple[Identity, RecoveryState]: ...

    async def snapshot(self, binding: Binding) -> JsonObject: ...

    async def recv_pub(self) -> tuple[bytes, JsonObject]: ...


def instrument_from_snapshot(
    snapshot: JsonObject,
    instrument_id: InstrumentId,
    *,
    ts_init: int,
) -> Cfd:
    """Create the single canonical-ounce CFD from a validated REP snapshot."""
    spec = cast(JsonObject, snapshot["symbol_spec"])
    identity = Identity.from_wire(snapshot["identity"])
    if spec["symbol"] != identity.symbol or instrument_id.symbol.value != identity.symbol:
        raise Mt5V1DataError("snapshot symbol does not match the configured instrument")
    if spec["currency_base"] != "XAU" or spec["currency_profit"] != "USD":
        raise Mt5V1DataError("v1 supports only an XAU/USD MT5 contract")

    contract_size = Decimal(cast(str, spec["contract_size"]))
    volume_step = Decimal(cast(str, spec["volume_step"]))
    volume_min = Decimal(cast(str, spec["volume_min"]))
    volume_max = Decimal(cast(str, spec["volume_max"]))
    tick_size = Decimal(cast(str, spec["tick_size"]))
    digits = cast(int, spec["digits"])
    size_step = contract_size * volume_step
    min_quantity = contract_size * volume_min
    max_quantity = contract_size * volume_max
    if min(contract_size, volume_step, volume_min, volume_max, tick_size) <= 0:
        raise Mt5V1DataError("MT5 contract and quantity increments must be positive")
    if size_step <= 0 or min_quantity < size_step:
        raise Mt5V1DataError("MT5 minimum quantity is smaller than one canonical step")

    price_increment = _fixed_decimal(tick_size, digits, "tick_size")
    size_precision = _decimal_places(size_step)
    size_increment = Quantity.from_str(_fixed_decimal(size_step, size_precision, "size_step"))
    lot_size = Quantity.from_str(
        _fixed_decimal(contract_size, size_precision, "contract_size")
    )
    observed_ns = int(cast(str, cast(JsonObject, snapshot["time"])["observed_utc_ms"])) * 1_000_000
    return Cfd(
        instrument_id=instrument_id,
        raw_symbol=Symbol(identity.symbol),
        asset_class=AssetClass.COMMODITY,
        base_currency=Currency.from_str("XAU"),
        quote_currency=Currency.from_str("USD"),
        price_precision=digits,
        size_precision=size_precision,
        price_increment=Price.from_str(price_increment),
        size_increment=size_increment,
        lot_size=lot_size,
        max_quantity=Quantity.from_str(
            _fixed_decimal(max_quantity, size_precision, "max_quantity")
        ),
        min_quantity=Quantity.from_str(
            _fixed_decimal(min_quantity, size_precision, "min_quantity")
        ),
        ts_event=observed_ns,
        ts_init=ts_init,
        info={
            "read_only": True,
            "canonical_quantity": "ounce",
            "mt5_contract_size_ounces": str(contract_size),
            "mt5_volume_step_lots": str(volume_step),
            "margin_and_fees_authoritative": False,
        },
    )


def quote_from_pub(
    message: JsonObject,
    instrument: Cfd,
    *,
    reference_utc_ms: int,
    now_utc_ms: int,
    timezone_name: str,
    ts_init: int,
    max_tick_age_ms: int,
) -> QuoteTick | None:
    """Map only tick PUB messages; heartbeat and market-open hints carry no quote fact."""
    if message["message_type"] == "heartbeat":
        return None
    server_wall_ms = int(cast(str, message["event_time_ms"]))
    ts_event = server_wall_ms_to_utc_ns(
        server_wall_ms,
        timezone_name=timezone_name,
        reference_utc_ms=reference_utc_ms,
    )
    age_ms = now_utc_ms - ts_event // 1_000_000
    if age_ms > max_tick_age_ms:
        raise Mt5V1DataError("MT5 tick is stale relative to the local live clock")
    if age_ms < -MAX_TICK_FUTURE_MS:
        raise Mt5V1DataError("MT5 tick is future-dated relative to the local live clock")
    return QuoteTick(
        instrument_id=instrument.id,
        bid_price=instrument.make_price(Decimal(cast(str, message["bid"]))),
        ask_price=instrument.make_price(Decimal(cast(str, message["ask"]))),
        bid_size=instrument.make_qty(0),
        ask_size=instrument.make_qty(0),
        ts_event=ts_event,
        ts_init=ts_init,
    )


def status_from_snapshot(
    snapshot: JsonObject,
    instrument_id: InstrumentId,
    *,
    ts_init: int,
) -> InstrumentStatus:
    """Project only REP session evidence; PUB market_open_hint is deliberately ignored."""
    session = cast(JsonObject, snapshot["session"])
    flags = cast(JsonObject, snapshot["authority_flags"])
    spec = cast(JsonObject, snapshot["symbol_spec"])
    symbol_trade_mode = cast(int, spec["trade_mode"])
    if flags["symbol_trade_mode"] != symbol_trade_mode:
        raise Mt5V1DataError("MT5 snapshot contains inconsistent symbol trade modes")
    is_open = cast(bool, session["session_open"])
    if is_open and symbol_trade_mode != 0:
        action = MarketStatusAction.TRADING
        reason = "mt5_rep_session_open"
    elif is_open:
        action = MarketStatusAction.NOT_AVAILABLE_FOR_TRADING
        reason = "mt5_rep_symbol_trade_disabled"
    elif (
        flags["terminal_connected"] is True
        and session["session_schedule_available"] is True
        and session["freshness"] == "fresh"
        and session["scheduled_open"] is False
    ):
        action = MarketStatusAction.CLOSE
        reason = "mt5_rep_session_closed"
    else:
        action = MarketStatusAction.NOT_AVAILABLE_FOR_TRADING
        reason = "mt5_rep_session_unavailable"
    observed_ns = int(cast(str, cast(JsonObject, snapshot["time"])["observed_utc_ms"])) * 1_000_000
    return InstrumentStatus(
        instrument_id=instrument_id,
        action=action,
        ts_event=observed_ns,
        ts_init=ts_init,
        reason=reason,
        trading_event=None,
        is_trading=action == MarketStatusAction.TRADING,
        is_quoting=None,
        is_short_sell_restricted=None,
    )


def server_wall_ms_to_utc_ns(
    server_wall_ms: int,
    *,
    timezone_name: str,
    reference_utc_ms: int,
) -> int:
    """Interpret an MT5 server-wall epoch label and resolve DST against a UTC fact."""
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise Mt5V1DataError(f"unknown MT5 server timezone {timezone_name!r}") from exc
    naive = datetime.fromtimestamp(server_wall_ms / 1_000, tz=UTC).replace(tzinfo=None)
    candidates: set[int] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=timezone, fold=fold)
        utc = aware.astimezone(UTC)
        if utc.astimezone(timezone).replace(tzinfo=None) == naive:
            candidates.add(int(utc.timestamp() * 1_000))
    if not candidates:
        raise Mt5V1DataError("MT5 server-wall timestamp falls in a DST gap")
    distances = sorted((abs(candidate - reference_utc_ms), candidate) for candidate in candidates)
    if len(distances) > 1 and distances[0][0] == distances[1][0]:
        raise Mt5V1DataError("MT5 server-wall timestamp is DST-ambiguous")
    return distances[0][1] * 1_000_000


class Mt5V1DataClient(LiveMarketDataClient):
    """Publish one MT5 hedge quote/status feed into the Nautilus data engine."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        name: str | None,
        config: Mt5V1DataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: InstrumentProvider,
        transport: _ReadOnlyTransport | None = None,
    ) -> None:
        _validate_config(config)
        super().__init__(
            loop=loop,
            client_id=ClientId(name or "MT5"),
            venue=Venue("MT5"),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._mt5_config = config
        self._transport: _ReadOnlyTransport = transport or Mt5V1Transport(
            pub_url=config.pub_url,
            rep_url=config.rep_url,
            topic=config.expected_symbol,
            request_timeout_ms=config.request_timeout_ms,
        )
        self._identity: Identity | None = None
        self._snapshot: JsonObject | None = None
        self._instrument: Cfd | None = None
        self._instrument_signature: tuple[object, ...] | None = None
        self._running = False
        self._quote_subscribed = False
        self._status_subscribed = False
        self._pub_task: asyncio.Task[None] | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        self._committed_snapshot_count = 0
        self._identity_matched_pub_count = 0
        self._last_identity_matched_pub_mono_ns: int | None = None
        self._last_validated_identity: Identity | None = None
        self._last_failure: str | None = None

    @property
    def committed_snapshot_count(self) -> int:
        return self._committed_snapshot_count

    @property
    def identity_matched_pub_count(self) -> int:
        return self._identity_matched_pub_count

    @property
    def identity_matched_pub_age_ms(self) -> int | None:
        if self._last_identity_matched_pub_mono_ns is None:
            return None
        return (time.monotonic_ns() - self._last_identity_matched_pub_mono_ns) // 1_000_000

    @property
    def observed_identity(self) -> Identity | None:
        return self._last_validated_identity

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    def connect(self) -> None:
        """Connect, then start background readers only after Nautilus marks success."""
        self._log.info("Connecting...")
        self.create_task(
            self._connect(),
            actions=self._finish_connect,
            success_msg="Connected",
            success_color=LogColor.GREEN,
        )

    async def _connect(self) -> None:
        self._last_failure = None
        self._last_identity_matched_pub_mono_ns = None
        self._last_validated_identity = None
        try:
            await self._transport.open()
            identity, recovery = await self._transport.hello()
            self._validate_identity(identity, recovery)
            snapshot = await self._transport.snapshot(identity.binding())
            self._commit_snapshot(snapshot, identity)
        except BaseException as exc:
            self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
            await self._transport.close()
            raise

    def _finish_connect(self) -> None:
        self._set_connected(True)
        self._running = True
        self._pub_task = self.create_task(self._run_pub(), log_msg="mt5-v1-pub")
        self._snapshot_task = self.create_task(
            self._run_snapshots(),
            log_msg="mt5-v1-snapshots",
        )

    async def _disconnect(self) -> None:
        self._running = False
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._pub_task, self._snapshot_task)
            if task is not None and task is not current
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pub_task = None
        self._snapshot_task = None
        await self._transport.close()
        self._identity = None
        self._snapshot = None

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        self._require_instrument(command.instrument_id)
        self._quote_subscribed = True

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        self._require_instrument(command.instrument_id)
        self._quote_subscribed = False

    async def _subscribe_instrument_status(self, command: SubscribeInstrumentStatus) -> None:
        self._require_instrument(command.instrument_id)
        self._status_subscribed = True
        if self._snapshot is not None:
            self._handle_data(
                status_from_snapshot(
                    self._snapshot,
                    self._mt5_config.instrument_id,
                    ts_init=self._clock.timestamp_ns(),
                )
            )

    async def _unsubscribe_instrument_status(self, command: UnsubscribeInstrumentStatus) -> None:
        self._require_instrument(command.instrument_id)
        self._status_subscribed = False

    async def _run_pub(self) -> None:
        try:
            while self._running:
                topic, message = await self._transport.recv_pub()
                self._publish_pub(topic, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_closed("MT5 PUB loop stopped", exc)

    async def _run_snapshots(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._mt5_config.snapshot_interval_ms / 1_000)
                await self._refresh_snapshot(allow_rehandshake=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_closed("MT5 snapshot loop stopped", exc)

    async def _refresh_snapshot(self, *, allow_rehandshake: bool) -> None:
        identity = self._require_identity()
        candidate_identity = identity
        try:
            snapshot = await self._transport.snapshot(identity.binding())
        except Mt5V1RemoteError as exc:
            if not allow_rehandshake or exc.code != "BINDING_MISMATCH":
                raise
            replacement, recovery = await self._transport.hello()
            self._validate_identity(replacement, recovery)
            snapshot = await self._transport.snapshot(replacement.binding())
            candidate_identity = replacement
        self._commit_snapshot(snapshot, candidate_identity)

    def _commit_snapshot(self, snapshot: JsonObject, identity: Identity) -> None:
        """Validate and install identity plus snapshot without an await boundary."""
        if snapshot["recovery_state"] != "ready":
            raise Mt5V1DataError("MT5 snapshot recovery state is blocked")
        if Identity.from_wire(snapshot["identity"]) != identity:
            raise Mt5V1DataError("MT5 snapshot identity does not match its binding")
        self._check_snapshot_age(snapshot)
        signature = _snapshot_instrument_signature(snapshot)
        if self._instrument_signature is not None and signature != self._instrument_signature:
            raise Mt5V1DataError("MT5 instrument specification changed while connected")
        if self._instrument is None:
            self._instrument = instrument_from_snapshot(
                snapshot,
                self._mt5_config.instrument_id,
                ts_init=self._clock.timestamp_ns(),
            )
            self._instrument_provider.add(self._instrument)
            self._handle_data(self._instrument)
            self._instrument_signature = signature
        if self._identity is not None and identity.binding() != self._identity.binding():
            self._committed_snapshot_count = 0
            self._identity_matched_pub_count = 0
            self._last_identity_matched_pub_mono_ns = None
        self._identity = identity
        self._last_validated_identity = identity
        self._snapshot = snapshot
        if self._status_subscribed:
            self._handle_data(
                status_from_snapshot(
                    snapshot,
                    self._mt5_config.instrument_id,
                    ts_init=self._clock.timestamp_ns(),
                )
            )
        self._committed_snapshot_count += 1

    def _publish_pub(self, topic: bytes, message: JsonObject) -> None:
        if not self._quote_subscribed or self._snapshot is None or self._instrument is None:
            return
        if topic != self._transport.topic:
            return
        pub_identity = Identity.from_wire(message["identity"])
        identity = self._require_identity()
        if pub_identity != identity:
            return
        self._identity_matched_pub_count += 1
        self._last_identity_matched_pub_mono_ns = time.monotonic_ns()
        snapshot_time = cast(JsonObject, self._snapshot["time"])
        reference_utc_ms = int(cast(str, snapshot_time["observed_utc_ms"]))
        ts_init = self._clock.timestamp_ns()
        quote = quote_from_pub(
            message,
            self._instrument,
            reference_utc_ms=reference_utc_ms,
            now_utc_ms=ts_init // 1_000_000,
            timezone_name=identity.server_timezone,
            ts_init=ts_init,
            max_tick_age_ms=self._mt5_config.max_tick_age_ms,
        )
        if quote is not None:
            self._handle_data(quote)

    async def _fail_closed(self, message: str, exc: Exception) -> None:
        if not self._running:
            return
        self._running = False
        self._last_failure = f"{type(exc).__name__}: {exc}"[:300]
        self._log.exception(message, exc)
        if self._status_subscribed:
            now = self._clock.timestamp_ns()
            self._handle_data(
                InstrumentStatus(
                    instrument_id=self._mt5_config.instrument_id,
                    action=MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
                    ts_event=now,
                    ts_init=now,
                    reason="mt5_data_client_disconnected",
                    trading_event=None,
                    is_trading=False,
                    is_quoting=None,
                    is_short_sell_restricted=None,
                )
            )
        sibling = (
            self._snapshot_task
            if asyncio.current_task() is self._pub_task
            else self._pub_task
        )
        if sibling is not None:
            sibling.cancel()
        await self._transport.close()
        self._set_connected(False)

    def _validate_identity(self, identity: Identity, recovery: RecoveryState) -> None:
        config = self._mt5_config
        expected = (
            ("account_id", identity.account_id, config.expected_account_id),
            ("symbol", identity.symbol, config.expected_symbol),
            ("magic", identity.magic, config.expected_magic),
            ("ea_build_id", identity.ea_build_id, config.expected_ea_build_id),
            (
                "declared_source_sha256",
                identity.declared_source_sha256,
                config.expected_source_sha256,
            ),
            (
                "execution_enabled",
                identity.execution_enabled,
                config.expected_execution_enabled,
            ),
            ("server_timezone", identity.server_timezone, config.expected_server_timezone),
        )
        for label, actual, wanted in expected:
            if actual != wanted:
                raise Mt5V1DataError(f"MT5 identity {label} does not match configuration")
        if recovery != "ready":
            raise Mt5V1DataError("MT5 recovery state is blocked")
        if self._identity is not None and identity.stream_id != self._identity.stream_id:
            raise Mt5V1DataError("MT5 journal stream changed while connected")

    def _check_snapshot_age(self, snapshot: JsonObject) -> None:
        observed_ms = int(cast(str, cast(JsonObject, snapshot["time"])["observed_utc_ms"]))
        now_ms = self._clock.timestamp_ns() // 1_000_000
        age_ms = now_ms - observed_ms
        if age_ms < -1_000 or age_ms > self._mt5_config.max_snapshot_age_ms:
            raise Mt5V1DataError("MT5 snapshot observation is stale or future-dated")

    def _require_identity(self) -> Identity:
        if self._identity is None:
            raise Mt5V1DataError("MT5 identity has not been established")
        return self._identity

    def _require_instrument(self, instrument_id: InstrumentId) -> None:
        if instrument_id != self._mt5_config.instrument_id:
            raise Mt5V1DataError(f"unsupported MT5 instrument {instrument_id}")


class Mt5V1LiveDataClientFactory(LiveDataClientFactory):
    """Build the one supported MT5 v1 read-only data client."""

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LiveDataClient:
        if not isinstance(config, Mt5V1DataClientConfig):
            raise TypeError("MT5 factory requires Mt5V1DataClientConfig")
        provider = InstrumentProvider(config=config.instrument_provider)
        return Mt5V1DataClient(
            loop=loop,
            name=name,
            config=config,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
        )


def _validate_config(config: Mt5V1DataClientConfig) -> None:
    if not config.pub_url.startswith("tcp://") or not config.rep_url.startswith("tcp://"):
        raise ValueError("MT5 v1 endpoints must use tcp://")
    if config.instrument_id.venue != Venue("MT5"):
        raise ValueError("MT5 v1 instrument venue must be MT5")
    if config.instrument_id.symbol.value != config.expected_symbol:
        raise ValueError("configured MT5 instrument and raw symbol differ")
    if not 50 <= config.request_timeout_ms <= 60_000:
        raise ValueError("request_timeout_ms is outside the supported range")
    if not 250 <= config.snapshot_interval_ms <= 60_000:
        raise ValueError("snapshot_interval_ms is outside the supported range")
    if config.max_snapshot_age_ms < config.snapshot_interval_ms:
        raise ValueError("max_snapshot_age_ms must cover one snapshot interval")
    if config.max_tick_age_ms < config.snapshot_interval_ms:
        raise ValueError("max_tick_age_ms must cover one snapshot interval")


def _snapshot_instrument_signature(snapshot: JsonObject) -> tuple[object, ...]:
    spec = cast(JsonObject, snapshot["symbol_spec"])
    return tuple(
        spec[key]
        for key in (
            "symbol",
            "contract_size",
            "currency_base",
            "currency_profit",
            "digits",
            "tick_size",
            "volume_min",
            "volume_max",
            "volume_step",
        )
    )


def _decimal_places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -cast(int, exponent))


def _fixed_decimal(value: Decimal, precision: int, label: str) -> str:
    quantum = Decimal(1).scaleb(-precision)
    try:
        fixed = value.quantize(quantum)
    except ArithmeticError as exc:
        raise Mt5V1DataError(f"{label} cannot be represented") from exc
    if fixed != value:
        raise Mt5V1DataError(f"{label} exceeds declared precision")
    return f"{fixed:.{precision}f}"
