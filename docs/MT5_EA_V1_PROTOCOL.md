# PY000 MT5 EA v1 protocol

Status: the deployed read-only build has passed a bounded endpoint-connected SHADOW
observation. The current source also passed one separately authorized DEMO-only
`MARKET` + `FOK` canary in an isolated portable terminal. That terminal has been removed;
the source is not deployed on 6001/6002, is not strategy-wired, and grants no production
execution authority.

## Boundary

The bounded topology is:

```text
Nautilus DataEngine <- Mt5V1DataClient      <- REQ/SUB -> PY000 EA -> MT5
Nautilus ExecEngine <- Mt5V1ExecutionClient <- REQ     -> PY000 EA -> MT5
```

Python owns Nautilus identifiers, mapping, order-policy checks, and event generation.
The EA owns only the physical MT5 slice: stable venue identity, one-turn snapshots, one
idempotent market-delta operation, and its durable execution journal. It is not an OMS,
strategy, generic router, or replacement risk platform.

The worktree contains both data and execution clients. Only the data client is registered
in the offline-buildable SHADOW node. No Maker/Taker strategy or execution client is
registered in a live composition. Reference state and response constructors remain
test-only.

v1 has exactly four REP operations:

1. `hello`
2. `get_snapshot`
3. `submit_market_delta`
4. `get_execution_events`

`submit_market_delta` returns `EXECUTION_DISABLED` by default. It becomes reachable only
when the operator explicitly selects `DEMO` and MT5 reports both a demo account and
retail-hedging margin mode. The only permitted native request is `MARKET` + `FOK`, capped
by `InpMaxOrderLots`; all other operation names return `UNKNOWN_OP`.

Tick PUB is advisory market data only. It cannot establish fills, account truth,
session authority, recovery completion, or permission to trade.

## Encoding and closed grammar

- UTF-8 JSON object, maximum 64 KiB and maximum nesting depth 32.
- External integer tokens are limited to 20 decimal digits before schema-specific
  uint64/range checks. Integer bombs, excessive nesting, lone surrogates, and encoder
  recursion/non-finite failures normalize to `MALFORMED` rather than escaping the
  protocol boundary.
- `protocol` is exactly `"py000.mt5"`.
- `version` is exact JSON integer `1`; boolean and float `1.0` are rejected.
- Duplicate keys, unknown keys, missing keys, non-finite constants, malformed UTF-8,
  NUL/control/surrogate text, untrimmed text, and over-limit strings are rejected.
- Request and response envelopes are closed. Nested binding, error, identity, snapshot,
  event, and PUB objects are closed too.
- `uint64`, cursor, broker login, native identity, magic, and UTC epoch-ms values are
  canonical decimal strings: `0` or a non-zero digit followed by digits, no sign and no
  leading zero. Positive-only fields additionally reject `0`.
- Price, lots, and money are finite non-exponent decimal strings with no leading zero.
  Quantities are strictly positive; side is represented separately as `buy` or `sell`.
- A request `request_id` and account/symbol identifiers are visible ASCII. EA build,
  stream, and boot IDs additionally use only `[A-Za-z0-9_-]` so journal fields cannot
  escape their namespace. Raw broker text is bounded, trimmed UTF-8 without controls.

## Request envelope

Common fields:

```json
{"protocol":"py000.mt5","version":1,"request_id":"r-1","op":"hello"}
```

`hello` has no binding and rejects a supplied binding.

Every other request has this exact binding:

```json
{
  "account_id":"mt5-sha256-a2ccbab7c131421a9275eac0599fe32b5e77742622c5e8084d2aae2f643b44ae",
  "symbol":"XAUUSD",
  "ea_build_id":"py000-mt5-ea-v1-readonly",
  "stream_id":"stream-sanitized-001",
  "boot_id":"boot-sanitized-001"
}
```

All five values must equal the currently running EA. Any mismatch returns the same
fail-closed `BINDING_MISMATCH`; the response does not reveal which field differed.

`get_snapshot` adds only `binding`.

`submit_market_delta` adds:

```json
{"client_request_id":"delta-1","side":"sell","quantity_lots":"0.01"}
```

The request is grammar-checked and identity-bound. A new client request ID is durably
reserved and flushed before preflight and before the single `OrderSend` call. Reusing the
same ID and payload returns its stored terminal event; changing side or quantity returns
`IDEMPOTENCY_CONFLICT`. Disabled mode and blocked recovery never reach `OrderSend`.

`get_execution_events` adds:

```json
{"after_cursor":"0","limit":100}
```

`limit` is an exact JSON integer from 1 through 500.

Constructed Python requests cross one production `validate_request(request)` boundary.
Both `request_to_wire` and `decode_response_for` call it first, so an empty or invalid
request ID, overwritten operation, invalid binding/request dataclass type, bad submit
field, cursor, or limit becomes `WireError` rather than a Python implementation error.

## Identity

Every successful REP response and every PUB message carries the same identity object:

- `account_id`: `mt5-sha256-` plus lowercase SHA-256 of the domain-separated
  canonical bytes `b"py000.mt5/account-id/v1\\0" || uint32_be(len(server_utf8)) ||
  server_utf8 || b"\\0" || ascii(login)`. Raw server UTF-8 is hashed without lossy
  normalization, so names such as `Broker-Demo`, `Broker Demo`, `Broker α`, and
  `Broker β` cannot collapse through a slug. Fixed vectors live in the shared
  conformance fixture.
- `broker_server`: raw bounded broker server string.
- `broker_login`: canonical uint64 string.
- `symbol`.
- `terminal_build`: exact JSON integer.
- `ea_build_id` and lowercase 64-character `declared_source_sha256`.
- configured `server_timezone` and `server_timezone_source="configured_input"`.
- persistent `stream_id`.
- new `boot_id` on every `OnInit`.
- configured stable `magic` as a uint64 string.
- boot-stable `execution_enabled`: true only when explicit DEMO mode, demo account, and
  retail-hedging account mode all held during `OnInit`; otherwise false.

`hello` also returns `recovery_state` (`ready` or `blocked`) and the exact capability
list. It exposes protocol identity even when journal recovery is blocked.

The worktree `InpDeclaredSourceSha256` value is an all-zero placeholder. `OnInit`
rejects that placeholder. A build operator may supply a lowercase SHA-256 declaration,
but an editable EA input cannot authenticate the running source bytes; consumers must
treat it as metadata and bind compilation evidence independently.

## Atomic read-only snapshot

`get_snapshot` reads these values in one MQL event-loop turn, without yielding:

- the complete XAUUSD symbol specification;
- account values and raw broker identity;
- every current raw XAUUSD position, not only the configured magic;
- separately sourced account, terminal, MQL, symbol, and session/trade observations.

The snapshot has separate closed `time` and `session` objects. Observation UTC comes
from second-precision `TimeGMT()` and carries `observed_utc_precision_ms="1000"` plus
source provenance. `TimeCurrent()` is retained only as the last-known quote/server
fact. The configured server timezone is an explicit interpretation input, not an
inferred broker fact. Session membership is sampled against calculated second-precision
`TimeTradeServer()` and `SymbolInfoSessionTrade`; freshness compares that advancing
sample with the latest `SymbolInfoTick` time. `scheduled_open` reports schedule
membership, while `session_open` is true only when scheduled, terminal-connected, and
fresh. Unknown time, a stale tick, or a disconnected terminal forces it false. These
remain read-only observations and never grant submission authority.

The closed `symbol_spec` object carries symbol/currencies, digits/point, tick size and
all three tick values, contract size, volume min/max/step/limit, initial and maintenance
margin, long/short swap plus swap mode, stops/freeze levels, and trade calculation,
trade, order, filling, and expiration modes. The closed account, authority-flag, and
position schemas in the Python codec reject omitted or invented fields.

Numbers use the string rules above. Native position ticket/identifier and timestamps
are canonical decimal strings. Position volume is positive and side is separate.
`recovery_state=blocked` does not suppress `hello`, `get_snapshot`, or structurally safe
event reads. It blocks every new submission. `execution_enabled` remains the boot-stable
armed-capability fact; it does not flip when recovery later becomes blocked.

## Event stream and journal

The business cursor is `event_seq`, never timestamp or broker deal ID.

- `stream_id` is created once and persisted.
- `boot_id` changes on every successful `OnInit` attempt.
- Each successful boot appends `stream_started` as that boot's first event.
- Execution appends `submission_reserved` before native submission, followed by exactly
  one `order_rejected`, `order_filled`, or `order_unknown` terminal event when it can
  establish and persist that fact.
- `order_filled` carries history-read order, deal, and position IDs, full fill quantity,
  price, commission, and broker retcode. Missing or mismatched fill facts become
  `order_unknown`, never an invented fill.
- A dangling reservation or any `order_unknown` keeps recovery blocked across restart.
  Structurally valid events remain pageable for operator reconciliation.
- `event_seq` is a contiguous uint64 sequence across boots.
- `after_cursor` is exclusive.
- A page returns `events`, `next_cursor`, `has_more`, `first_retained_cursor`, and
  `last_cursor`.
- Page construction measures the actual UTF-8 success envelope and stops before the
  64 KiB wire ceiling. A large requested limit may therefore return a smaller
  contiguous page with `has_more=true`; the first event is always `after_cursor + 1`.
- A successful page must prove `first_retained_cursor <= after_cursor <= last_cursor`;
  repeated boot IDs within one page are rejected. Cross-page boot-ID uniqueness remains
  future stateful-adapter custody.
- `after_cursor > last_cursor` returns `CURSOR_AHEAD`.
- `after_cursor < first_retained_cursor` returns `CURSOR_EXPIRED`.
- Any event gap, malformed header, identity mismatch, checksum/schema failure, or
  namespace mismatch, including a duplicate boot ID, sets `recovery_state=blocked`;
  it is never silently repaired.
- v1 performs no automatic retention, so a healthy new stream begins with
  `first_retained_cursor="0"`.

The journal namespace is `PY000_MT5_V1_` plus lowercase SHA-256 of canonical bytes
`b"py000.mt5/journal-namespace/v1\\0" || ascii(account_id) || b"\\0" ||
symbol_utf8 || b"\\0" || ascii(magic)`. Full raw symbol bytes are retained, so
`XAUUSD.a`, `XAUUSD_a`, `XAUUSD a`, and `XAUUSD#a` cannot collide through a slug.
The owner lock, identity file, and events file use only this digest namespace. Fixed
vectors are executed by Python tests. v1 never reads, deletes, migrates, or interprets
any legacy journal or trade cache.

## PUB messages

PUB uses UTF-8 multipart: frame 1 is the symbol topic and frame 2 is the JSON payload.

`tick` contains positive ordered `bid`/`ask`, `event_time_ms`,
`event_time_source="mt5_symbol_info_tick"`, `market_open_hint`, and full identity.
Only a real `SymbolInfoTick` result may update the remembered tick time.

`heartbeat` contains second-precision UTC `event_time_ms`,
`event_time_source="mt5_time_gmt_second_precision"`, nullable `last_tick_time_ms`,
`market_open_hint`, and the same identity. It never contains bid/ask and never refreshes
`last_tick_time_ms`. `market_open_hint` remains advisory in both message types.

## Nautilus clients

`Mt5V1Transport` owns exactly one REQ and one SUB socket. Open is transactional, and a
timed-out, cancelled, or failed REQ round trip discards that REQ socket before the next
request. The SUB boundary requires exactly two frames and the data client requires the
exact XAUUSD topic rather than merely accepting ZeroMQ prefix subscription matches.

`Mt5V1DataClient` binds the configured account, symbol, magic, EA build, declared source
hash, and server timezone to `hello`, then requires full identity equality on each REP
snapshot. An EA boot change on the same journal stream may re-handshake; the replacement
identity and its snapshot are validated and installed together, so a new-boot PUB can
never be interpreted against an old snapshot. A journal stream change fails closed.

The MT5 contract is represented in canonical ounces: a 100-ounce contract is the
Nautilus `lot_size`, and a 0.01-lot MT5 volume step becomes a one-ounce
`size_increment`. Tick PUB becomes `QuoteTick`; unavailable L1 sizes remain zero and
heartbeat PUB creates no quote. DST fold selection uses the latest authoritative REP UTC
observation, while stale/future tick checks use the current Nautilus `LiveClock`.
The bounded observer retains only first/last public samples and counters, requires a
fresh identity-matched PUB for the currently validated boot/stream, and writes the
expected plus observed identities into a non-overwriting two-record summary. A boot
re-handshake resets the snapshot/PUB evidence counters so facts from the old boot cannot
qualify the replacement boot.

`Mt5V1ExecutionClient` separately requires an enabled identity, an operator-supplied
`expected_stream_id`, and an identity-equal snapshot. On connect it reads the complete
retained journal, captures the snapshot, and proves that the journal tail did not move.
Historical request IDs are hydrated without replaying old terminal events. A same-process
timeout may be resolved only by its durable terminal event; `order_unknown`, a dangling
reservation, blocked recovery, a mismatched FOK fill, or any same-symbol foreign-magic
position keeps new execution on HOLD.

The same execution poll loop refreshes the REP snapshot no more often than
`snapshot_refresh_interval_ms` (default 1000 ms), under the existing state lock and through
the same REQ socket. Each refresh follows journal poll -> snapshot -> unchanged-tail proof.
Identity, recovery-state, or critical execution-spec drift closes the client; a foreign-magic
position holds admission but remains observable, so a later clean snapshot can restore
admission when no journal or local UNKNOWN blocker remains.

The client accepts only Nautilus `MARKET` orders with `FOK` and no reduce-only,
quote-quantity, or execution-algorithm semantics. It binds one configured MT5 instrument,
verifies contract size, converts canonical ounces to an exact permitted lot quantity
without rounding, and submits once.

The four Nautilus 1.231 report APIs are deterministic projections of the retained journal
and latest successfully refreshed snapshot. Before mass status, the client holds the existing
state lock, consumes the journal tail, forces one snapshot refresh, proves the tail unchanged,
and then delegates aggregation to Nautilus. The active-reconciliation flag is cleared even if
that operation fails. Individual report methods perform no transport I/O and take no second
lock.

- `order_filled` produces a `FILLED` `MARKET`/`FOK` order report plus one fill report using
  native MT5 order, deal, and position IDs, canonical `lots * contract_size` quantity, fill
  price, USD commission, and journal event time.
- `order_rejected` produces a `REJECTED` order report and no fill. Its required venue order
  identity is `PY000_REJ_` plus SHA-256 over a fixed domain, stream, account, symbol, magic,
  and client order ID; it is stable across processes and cannot collide with numeric MT5 IDs.
- order and fill bulk queries honor instrument, UTC start/end, `open_only`, and venue-order
  filters. Position reports are current-state facts and intentionally ignore historical
  start/end windows, as long-lived open HEDGING tickets must not disappear from a lookback.
- matching-magic snapshot positions use their native identifier. Every currently open cached
  position for the same account/instrument which disappeared from the snapshot produces a
  zero-quantity `FLAT` report with that exact PositionId and the cached stable `ts_last`.
  Snapshot and cache both empty produces no anonymous flat report.

Any `UNKNOWN`, dangling reservation, mismatched FOK fill, blocked recovery, foreign-magic
position, local pending order, or disconnected client makes report queries fail explicitly;
an empty list is reserved for a valid query with no matches. Single-order queries match every
provided client and/or venue ID exactly and return `None` only when valid state contains no such
order.

The first future live composition should start fail closed with
`generate_missing_orders=False` and continuous inflight/open/position checks disabled while
engine integration is qualified. With that setting, any position discrepancy makes startup
reconciliation fail and applies no synthetic correction: the application must require both a
successful engine reconciliation result and `execution_admitted` before starting either strategy.
Setting `generate_missing_orders=True` is the Nautilus-native path which can reconstruct or close
an exact HEDGING PositionId; the offline A/B-position test covers that mechanism, but enabling it
remains an explicit deployment decision. Cancel/modify/order-list operations remain unsupported.

`InstrumentStatus` is only a market-session observation. It is derived from REP session
evidence, rejects inconsistent symbol trade modes, and does not publish `TRADING` for a
disabled symbol. It is **not** hedge readiness: the separately authenticated execution
client also requires account/terminal/MQL, identity, recovery, and order-semantic facts.
The current Taker and Maker strategies do not subscribe to this status and are not wired
to the execution canary. A later composition must combine those facts rather than feeding
`InstrumentStatus.is_trading` directly into `update_hedge_session`.

The deployed read-only build was observed on these supplied endpoints. The current DEMO
candidate was not attached to them:

```json
{
  "server_timezone": "Europe/Athens",
  "zmq_pub_url": "tcp://10.211.55.13:6001",
  "zmq_rep_url": "tcp://10.211.55.13:6002"
}
```

## Response and error envelope

Success:

```json
{"protocol":"py000.mt5","version":1,"request_id":"r-1","op":"hello","ok":true,"data":{}}
```

Failure:

```json
{"protocol":"py000.mt5","version":1,"request_id":"r-1","op":"get_execution_events","ok":false,"error":{"code":"CURSOR_AHEAD","message":"after_cursor exceeds last_cursor"}}
```

Stable v1 codes are `MALFORMED`, `SCHEMA_MISMATCH`, `UNKNOWN_OP`,
`BINDING_MISMATCH`, `EXECUTION_DISABLED`, `IDEMPOTENCY_CONFLICT`,
`RECOVERY_BLOCKED`, `CURSOR_AHEAD`, and `CURSOR_EXPIRED`.

The production Python response entry point is `decode_response_for(request, raw)`.
It binds `request_id`, operation, non-hello identity, requested page limit, contiguous
retained/last cursor context, first cursor, empty-page cursor stability, and `has_more`
progress to the originating request. A syntactically valid but misbound response is
rejected.

## EA initialization custody

`OnInit` must fail with `INIT_FAILED` unless all of these complete:

1. bounded execution-mode configuration and boot-stable capability derivation;
2. exact account/symbol/magic identity derivation;
3. owner-single-instance lock acquisition;
4. strict persistent stream-identity and journal namespace bind;
5. PUB and REP socket initialization and bind;
6. when journal recovery is structurally ready, a new boot ID plus durable `stream_started` append
   as the last fallible initialization step.

A corrupt or missing journal for an existing persistent identity enters the deliberately
restricted `recovery_state=blocked` service: `hello` and `get_snapshot` remain available,
structurally unsafe event reads are rejected, and every new submission is blocked. The
boot-stable capability flag may remain true; it is not permission to bypass recovery.
This mode does not create, repair, truncate, or replace journal evidence. Any failure to
establish the persistent identity, namespace, owner lock, or sockets still returns
`INIT_FAILED`.
Socket linger is zero, HWM is bounded, REP receive/send is non-blocking, the REP pump
has a time/count budget and re-entry guard, and all sockets/context plus the owner lock
are released during deinitialization. Zero-byte requests receive a bounded `MALFORMED`
reply. Multipart requests are fully drained and rejected only within 16 frames and
64 KiB cumulative bytes; exceeding either budget is fatal. `RCVMORE`/receive failures
other than `EAGAIN`, short sends, budget exhaustion, and REP state failures stop the EA with
`ExpertRemove()` rather than reusing an uncertain request/reply socket.

The shared fixture contains valid and hostile request cases (duplicate keys, wrong
JSON scalar types, unsafe tokens, surrogates, integer bombs, and excessive depth) plus
fixed identity vectors. Python tests replay that corpus through the test-only request
decoder, and the isolated MetaQuotes-Demo check replayed it through the compiled EA and
real libzmq transport.

## Selective read-only references

The source of record is immutable tree
`4d2c62edf16329c7cabb9afb2a683f777b215752` from repo_pr017 commit
`11c2ab35c12f526c9d6ee3f5953d0912c470f7c6`.

Only these mechanisms were consulted:

- `EA/ZmqBridge.mqh`, blob `16332d42ab94b8c96761e43a5ce2bae874811409`:
  libzmq FFI, UTF-8 byte conversion, multipart PUB, linger/HWM, and cleanup shape.
- `EA/common/EA_Common_Rep.mqh`, blob `2270dc97b43be219117d4b8484a0d2e048336bb0`:
  bounded non-blocking pump and re-entry guard.
- `EA/common/EA_Common_Snapshots.mqh`, blob
  `d4bdab85fd5980f07584f80ad7df710a5a67186e`: native MT5 account and position field
  inventory only; its JSON number format was not copied.
- `EA/EA_ZMQ_Bridge_MT5.mq5`, blob `e46f3f2ec921ce5d949cc8ee73c4e888279cda0d`:
  native AccountInfo/SymbolInfo/PositionGet/terminal flag collection call sites only.

No generic command router, MT4 code, pending/modify/cancel/reduce path, broadcast or
monitor platform, old cache/cursor, synchronous trading block, or large asynchronous
execution block was copied.

The reference FFI declared C `size_t` lengths as MQL `int` and ignored option-setting
failures. That is not safe on 64-bit MT5. This EA declares the `zmq_setsockopt`,
`zmq_getsockopt`, `zmq_send`, and `zmq_recv` length parameters as MQL `long`, checks every
initialization stage, and has a regression guard for those signatures.

## Verified checkpoints and remaining HOLD

The exact current six-source set compiled in MetaEditor with 0 errors and 0 warnings.
The temporary EX5 had SHA-256
`64085652db210b58ceb943df7deb94f7fffa482a8b69b16b997def6346094909` and was deleted
with its isolated compile directory; it was never attached. The source hashes are:

- `2e95396bcfc0797b057bb547eb5095213f5d7fc6b5975ebee5f9686c19697519`
  (`PY000_Nautilus_MT5.mq5`);
- `98e53aff6c3c57d001464d54057e1f79f9b1b69844be56d8dae331c990b2ebd8`
  (`Py000Execution.mqh`);
- `bfa560576987a9f000fc23891a641da2ff9486a88014a692087f695153c419a9`
  (`Py000Journal.mqh`);
- `cc66ba7647c84e72e4845dbbf257448833be1be9c1eb51bf60d9a7263b688027`
  (`Py000Json.mqh`);
- `388ceb0da1a614c21cda6c7aedcea2f8624ee04b5ae45ee0dcf1b702007cf26c`
  (`Py000Protocol.mqh`);
- `e9936e5e063cd85146937182f6c9c9f6a6f375d9f27697d41ae1f4b09a70bf40`
  (`Py000Zmq.mqh`).

Hashing those six `shasum` output lines again, in the order above and including the final
LF, gives manifest
`3c7d978935b9973a25eb0c526d9b845db0ceb1584bec24d5664c464d0f3a7e27`.
It must be supplied through `InpDeclaredSourceSha256`; writing it into the hashed source
would invalidate the manifest by self-reference. The checked-in all-zero value remains a
deliberate non-runnable placeholder.

The preceding manifest
`d5bb74d0a2960aa82cf5df9e548705a6a533f7da08773e16bee5bd17061c54ef`
was attached in a separate portable terminal on 6101/6102 with magic `900000002`.
The authenticated identity was execution-enabled and recovery-ready. Its one explicitly
authorized `BUY 0.01` request produced contiguous durable events
`submission_reserved` then `order_rejected`, with reason `VOLUME_INVALID` and broker
retcode zero. The rejection occurred before `OrderCheck` and `OrderSend`; the snapshot
remained at the original 16 positions, all magic zero. The isolated terminal was stopped
immediately afterward and 6001/6002 remained owned by the original read-only process.

That result identified decimal accumulation as the cause: parsing `"0.01"` through
successive `0.1` multiplications produced a double one ULP above the strict 0.01-lot cap.
The current source normalizes the already validated 0-to-8-digit decimal before applying
the unchanged strict broker and configured limits. The original one-order authorization
was consumed by the rejection.

A second explicit authorization used corrected manifest
`3c7d978935b9973a25eb0c526d9b845db0ceb1584bec24d5664c464d0f3a7e27`, magic
`900000002`, a fresh request ID, and isolated ports 6101/6102. The first portable boot
initialized before the account synchronized and therefore appended a read-only
`stream_started`; it was stopped without any submission. After the DEMO hedging account
synchronized, a fresh boot passed full identity, recovery, journal, position, authority,
session, volume, MARKET, and FOK checks. The only submission appended journal sequences
6 and 7: `submission_reserved` followed by `order_filled`. MetaQuotes-Demo filled
`BUY 0.01` at `4306.18`, retcode `10009`, order/position `10318917007`, deal
`10033461086`. The post-submit snapshot contained the unchanged original 16 positions
plus exactly one `BUY 0.01` position with magic `900000002`. No retry or flatten occurred.
The isolated terminal, firewall rule, build staging, and portable clone were removed;
the durable journal was retained and 6001/6002 remained on the original read-only process.
The private mode-0600 intent and result records are
`py000-mt5-canary-fix1-20260902T103452Z-6b0af557.intent.json` (SHA-256
`00b525843889c4bf412dd08d44a96ab29bcdd45ae4a37b5266cc54a9c491010c`) and
`py000-mt5-canary-fix1-20260902T103452Z-6b0af557.result.json` (SHA-256
`24c1d6aabb91bb49f7bfee16ca00432ea88b41ec496926e6937fadebfc59290e`).

Separately, the deployed read-only build on 6001/6002 completed a 30-second SHADOW run
with 201 quotes, 28 status samples, 28 snapshots, and 228 identity-matched PUB messages.
It reported build `py000-mt5-ea-v1-readonly`, source manifest
`839e47a1cb51006df99b681df25c44ac269ada38ca71e3cd6c4a4a25a303fe04`, and
`execution_enabled=false`. The private mode-0600 transcript is
`py000-mt5-shadow-20260902T073530Z.jsonl`, SHA-256
`8460b4c8c52c35463327dbe0174a5551e53b45d322fe35957319293b8a4a355f`.
That proves the read path for that deployed build, not the current execution source.

The corrected decimal parser and bounded DEMO `MARKET` + `FOK` path have now been
compile-, regression-, and single-canary-tested. The remaining boundary is unchanged:
there is no crash/power-loss durability proof, retention policy, broker-specific
conformance beyond MetaQuotes-Demo, `MARKET` + `IOC` partial-fill
support, or Maker/Taker composition. Bitfinex live adapters and full strategy parity remain outside
this checkpoint.
