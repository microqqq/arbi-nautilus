# py000-nautilus

Minimal first-stage migration of the active PY000 Taker and Maker strategies to
NautilusTrader 1.231.0. Both are thin Nautilus `Strategy` implementations using
native limit orders, order/fill events, clock custody, cache, portfolio,
execution routing, and `BacktestEngine`.

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy
uv run pytest
uv run py000-sim
uv run py000-maker-sim
```

The simulations and tests use no credentials and contact no live endpoints. A
persisted unresolved source or hedge submission stops new source orders until an
operator reconciles it.

## MT5 EA v1 checkpoint

The PY000-specific protocol and EA are specified in
`docs/MT5_EA_V1_PROTOCOL.md`. The read path contains a strict Python codec, a
Nautilus `LiveMarketDataClient`, a small offline-buildable SHADOW node, atomic
snapshots, persistent event paging, and tick/heartbeat PUB.

The current source also contains one deliberately narrow execution candidate:

- the EA defaults to `DISABLED` and arms only under explicit `DEMO` mode on a
  hedging demo account;
- the only native trading path is one bounded `MARKET` + `FOK` call, with a durable
  reservation before `OrderSend` and terminal `order_rejected`, `order_filled`, or
  `order_unknown` evidence afterward;
- the Nautilus execution client accepts only `MARKET` + `FOK`, converts ounces to
  exact lots without rounding, submits once, consumes the journal contiguously, and
  leaves timeout or `order_unknown` pending;
- every connect binds an operator-supplied `expected_stream_id`, projects the complete
  retained journal without replaying historical terminal events, and verifies a stable
  journal/snapshot cut before admitting execution;
- `UNKNOWN`, a dangling reservation, blocked recovery, mismatched FOK quantity, or any
  same-symbol foreign-magic position keeps execution on HOLD and makes reconciliation
  reports fail explicitly. The existing poll loop refreshes the snapshot at a bounded
  configurable interval (default one second): identity, recovery, or critical execution-spec
  drift fails closed, while removal of foreign-magic positions can restore admission;
- the complete journal now projects deterministic bulk order/fill/position reports and an
  exact single-order report. Filled orders preserve native MT5 order, deal, and position IDs;
  rejected orders use a stable stream/account-scoped synthetic venue ID. HEDGING positions
  missing from the current snapshot report FLAT against their exact cached PositionId rather
  than an anonymous net position. Cancel/modify, production composition, and strategy wiring
  remain intentionally absent.

The strategy layer keeps venue roles explicit: Taker submits `LIMIT` + `IOC` on the
Bitfinex source leg, Maker maintains `LIMIT` + `GTC` + post-only source quotes, and both
MT5 hedge legs submit `MARKET` + `FOK` to match the currently verified adapter. They are
still not wired into a live composition.

## Bitfinex public read-side v1 candidate

The repository now contains one deliberately small, offline-testable Nautilus live-data
candidate for the production `tXAUTF0:USTF0` or paper
`tTESTXAUTF0:TESTUSDTF0` profile, each mapped explicitly to the canonical
`XAUTUSDT-PERP.BITFINEX` instrument. It subscribes only to the public `P0`/`F0`/`len=25`
book, enables Bitfinex checksum flag `131072`, maintains the two 25-level sides,
and publishes data only after the immediately preceding book state passes the venue CRC.
Quote subscribers receive the true BBO; depth subscribers receive a native Nautilus
`OrderBookDeltas` L2 snapshot (`CLEAR` plus 25 levels per side). A malformed frame, wrong
subscription contract or channel, incomplete book, checksum mismatch, or transport loss
closes the feed and stops publication.

All instrument precision, increments, quantity limits, margins, and fees are mandatory
reviewed configuration; there is no REST specification fallback. The current public paper
contract reports minimum/maximum quantities `2`/`10000` and initial/maintenance margins
`0.01`/`0.005`; these must not be copied from production blindly. Contract multiplier and
lot size are fixed at one because this client supports only the one-ounce XAUT contract.
The emitted `QuoteTick` always retains Nautilus's standard best-bid/best-ask meaning. The
Taker consumes the managed native L2 book, walks exact cumulative quantities to its
configured reference depth, and produces no opportunity when either side is insufficient.
It evaluates only on a new CRC-backed source-depth callback, never on a later hedge tick
against retained source data.

The checked-in 11-frame fixture is a bounded public wire capture. Factory and node
construction are tested without opening a socket. This public client has no credentials,
ordering, or account state. It has no retry supervisor or live probe, and is not
live-qualified. A future live composition must also require current client connectivity
because Nautilus retains the last cached market data after a feed disconnect.

## Bitfinex private execution v1 candidate

The repository also contains an offline-tested private WebSocket execution slice for the
same canonical instrument. Production binds `tXAUTF0:USTF0` to the `USTF0` margin wallet;
paper binds `tTESTXAUTF0:TESTUSDTF0` to `TESTUSDTF0`. Crossed profiles fail configuration,
and authenticated REST user info must match both the configured user ID and paper/live mode
before the private socket opens. It supports only Taker `LIMIT` + `IOC` and Maker
post-only `LIMIT` + `GTC`, with integer per-order leverage, price-only Maker modify, and
native-ID cancel. A CID is persisted before `OrderSubmitted` and before the single wire
send. Only `tu` creates fills; its venue fee, liquidity side, and delayed pre-modify price
are preserved. Every successful submit, modify, and cancel send has an independent ACK
deadline; silence or an ambiguous send becomes sticky `UNKNOWN` without a retry. A stream
gap with an unresolved order cannot hot-reconnect.

The client now supplies bounded native Nautilus order, fill, and NETTING position reports
from Bitfinex's authenticated read-only REST endpoints. Closed-order recovery is limited to
the venue's documented two-week window. Full pages are bisected or overlap-paged with ID
deduplication; an unprovable timestamp saturation fails reconciliation instead of silently
truncating it. A reconciled open order can be reconstructed lazily from the Nautilus cache
and its persisted CID. Factory and offline node construction are covered, as is the real
`ExecutionEngine` lifecycle from submit through partial fills and cancel.

This remains a candidate, not live qualification. In-flight strategy state stays on HOLD
after process restart until the strategy store and reconciled Nautilus cache are joined
explicitly. Same-process hot reconnect and a complete two-venue live composition remain
absent. Nautilus's default startup reconciliation must remain enabled. Paper and production
must use separate account IDs, CID/state paths, and caches; the shared canonical instrument
ID means the two profiles must never run concurrently in one node.

`py000-bitfinex-paper-canary` is the one bounded exception used to exercise this adapter.
It reads `BFX_TEST_API_KEY`, `BFX_TEST_API_SECRET`, and `BFX_TEST_USER_ID` from the process
environment or `.env`. Without `--execute` it performs authenticated REST preflight only:

```bash
uv run py000-bitfinex-paper-canary --output /new/path/bitfinex-preflight.jsonl
```

Mutation requires `--execute`, an unused CID-state path, a clean target symbol, sufficient
`TESTUSDTF0` balance for 1x, current CRC-backed data, connected data/execution clients, and
successful Nautilus startup reconciliation. The final account snapshot completes before the
paper-book subscription starts, so the next CRC can be checked and used immediately. It then
submits exactly one `BUY 2` post-only
`LIMIT/GTC` at 5% below the current bid and sends one native-ID cancel immediately after the
acceptance event. It never changes the quantity, side, or leverage; never retries a mutation;
and never uses cancel-all or touches MT5. `PASSED` additionally requires exact canceled-order
history for that CID and venue ID, no matching trade, a flat final position, no active order,
and final Nautilus reconciliation. `UNKNOWN` retains the CID transcript for manual inspection;
the existing CID state prevents an automatic rerun.
Paper history has been observed clearing a canceled post-only order's terminal flag to zero;
that form is accepted only when the same run's strictly validated acceptance names the exact
venue order ID, and the transcript records both the historical flag and witness use.

`InstrumentStatus` is only a REP-backed market-session observation. It is not
hedge readiness and must not open source-risk admission without a separately
connected, authenticated execution client and matching account/terminal/MQL authority.

`py000-mt5-shadow` is a bounded data-only observer. It requires the independently
derived expected account ID, reviewed source-manifest SHA, and a new output path;
it never overwrites its two-record summary transcript. It retains only first/last
quote and status samples plus counts, so high-rate ticks cannot block the Nautilus loop
with per-event disk writes. `OBSERVED` also requires a recent PUB for the currently
validated boot/stream; the summary records that actual identity and defaults to a
three-second PUB-silence ceiling. The supplied deployment endpoints default to
`tcp://10.211.55.13:6001` and `tcp://10.211.55.13:6002`. Running the command is an
explicit network action; importing or building the node remains offline.

The deployed read-only build on the supplied 6001/6002 endpoints completed a bounded
30-second SHADOW observation: 201 quotes, 28 status samples, 28 snapshots, and 228
identity-matched PUB messages. Its execution flag was false. The private local transcript
`py000-mt5-shadow-20260902T073530Z.jsonl` has SHA-256
`8460b4c8c52c35463327dbe0174a5551e53b45d322fe35957319293b8a4a355f` and is deliberately
not committed because it contains host/account runtime metadata.

The first six-source DEMO candidate, manifest
`d5bb74d0a2960aa82cf5df9e548705a6a533f7da08773e16bee5bd17061c54ef`, was
attached in a separate portable terminal on isolated ports 6101/6102. One explicitly
authorized `BUY 0.01` request was durably reserved and rejected before `OrderSend` as
`VOLUME_INVALID`; the account retained its original 16 positions and gained no new
position. The isolated terminal was then stopped, while the deployed read-only process
on 6001/6002 remained untouched. The private mode-0600 intent and result records are
`py000-mt5-canary-20260902T093819Z-fe941a64.intent.json` (SHA-256
`d70c3d6d803a35f37c67a4898e22ed9106aef545a55a229def47159375f10a89`) and
`py000-mt5-canary-20260902T093819Z-fe941a64.result.json` (SHA-256
`d139ec830873510f656194661b8f382dce843b9a5d39cc058a032352fd1088f6`).

That canary exposed a one-ULP parsing defect: decimal accumulation parsed `"0.01"` as
slightly greater than the strict 0.01-lot cap. The current source applies the single
required `NormalizeDouble` at the existing protocol limit of eight fractional digits;
it keeps the strict cap unchanged. Current manifest
`3c7d978935b9973a25eb0c526d9b845db0ceb1584bec24d5664c464d0f3a7e27`
compiled in MetaEditor with 0 errors and 0 warnings. A second, separately authorized
isolated canary used a new request ID and produced contiguous durable
`submission_reserved` and `order_filled` events. MetaQuotes-Demo filled `BUY 0.01` at
`4306.18` with retcode `10009`; the snapshot added exactly one `BUY 0.01` position with
magic `900000002`, while the original 16 positions remained unchanged. The position was
not flattened. The candidate terminal was stopped and removed, and 6001/6002 remained
untouched. Its private mode-0600 records are
`py000-mt5-canary-fix1-20260902T103452Z-6b0af557.intent.json` (SHA-256
`00b525843889c4bf412dd08d44a96ab29bcdd45ae4a37b5266cc54a9c491010c`) and
`py000-mt5-canary-fix1-20260902T103452Z-6b0af557.result.json` (SHA-256
`24c1d6aabb91bb49f7bfee16ca00432ea88b41ec496926e6937fadebfc59290e`).
This validates only the bounded DEMO `MARKET` + `FOK` path, not strategy parity or
production execution.

## Scope boundary

The simulations validate Taker direction and return terms plus Maker bid/ask
pricing, mirrored account selection, two-sided GTC maintenance, strict relative
requote thresholds, post-only two-tick source-touch clamps, actual-fill-driven
hedging, duplicate/late fill identity, stale/session cleanup, and restart stops.
Canonical strategy and Nautilus quantity is always gold ounces. The MT5 data mapping
and DEMO execution candidate both map one standard 100-ounce lot and its 0.01-lot step
to 100 ounces and one ounce respectively.

The authenticated active caller establishes Maker side selection, reference
price, spread inputs, fee/funding/swap mapping, signed amount, leverage, and
`fixed_amount=false`. The final multiplicative price law (`bid = base *
(1-spread)`, `ask = base * (1+spread)`) and the legacy XAUT absolute `0.2`
passive clamp come from the Owner-frozen, review-attested callee boundary; the
exact carrier is excluded because it contains credential literals. This repo
therefore tests that distinction explicitly and generalizes `0.2` as two
instrument ticks, rather than claiming executable callee provenance.

It does **not** establish complete oracle or live parity. The bounded Bitfinex private
command/event and startup-report slices exist, but strategy-state restart release, hot
reconnect, and live composition remain unimplemented. MT5 `MARKET` + `IOC` partial-fill handling also
remains unimplemented; the isolated DEMO `MARKET` + `FOK` candidate is not strategy parity.
Dynamic venue margin capacity, authoritative cancel reconciliation, and Maker query closure
remain live gates. The private Bitfinex candidate applies the strategy's validated integer
per-order leverage; the simulated venue still uses configured account leverage. The
shared leverage helper preserves legacy floor division when its result is at
least one and intentionally clamps smaller results to one as a migration safety
guard; that edge is not claimed as bitwise legacy parity.

Initial funding/swap/FX and MT5-session snapshots exist for the backtests. Live
composition must refresh them through `update_cost_snapshot` and
`update_hedge_session`; changed costs, future-dated facts, closed sessions, and
stale/skewed inputs withdraw active Maker quotes and fail closed. Any Maker
source fill also reserves its fill/hedge obligation first, then persists a
two-sided fill freeze and cancels both working quotes. A cycle releases without
timeouts only after every known source order has authoritative terminal
evidence, every hedge obligation is completed by real fills, and outstanding
and rounding exposure are zero on both stores. Cancel/expiry still needs an
external authoritative reconciliation call; a terminal event alone does not
release its gate.

The selected source→hedge account/client route is stored with each source order,
so same-process, canceled, and replayed late fills retain their original hedge
responsibility. `keep_last_accounts=true` is explicitly rejected until its
preference can be made restart-safe. On restart, incomplete evidence stays
stopped; a fully persisted, cross-side-consistent terminal/hedge record can
release the cycle without guessing. The store exposes true outstanding source
exposure, while its separate rounding residual is only the sub-step amount not
yet converted into a hedge obligation. A green simulation is not live
qualification.

The zero-residual release rule is an intentional fail-closed divergence from
the active script: legacy can clear `executed_vol` after canceling a fractional
partial and continue, losing a 0.5-ounce rounding residual. This slice keeps the
cycle frozen for any nonzero residual until a future, explicitly authorized
dust-risk or fractional-hedge rule supplies evidence to resolve it.

The example notionals are derived from Nautilus `filled_qty * avg_px` and the
resulting portfolio positions. They demonstrate ounce-scale execution rather
than the former $24 unit error; they are not account margin or PnL proof.
