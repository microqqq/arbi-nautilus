# py000-nautilus

Migration of the active PY000 Taker and Maker strategies to
NautilusTrader 1.231.0. Both are Nautilus `Strategy` implementations using
native limit orders, order/fill events, clock custody, cache, portfolio,
execution routing, and `BacktestEngine`.

Current remediation design (updated 2026-09-08):
[doc-first implementation plan](docs/IMPLEMENTATION_PLAN.md).
It separates implemented local capabilities from remaining release and DEMO qualification.
The checkpoints below include historical validation results; old EA hashes are not
the identity of the current source or proof of the currently attached build.

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy
uv run pytest
uv run py000-sim
uv run py000-maker-sim
```

The simulations and tests use no credentials and contact no live trading endpoints.
The opt-in native-cache tests use a disposable local Redis. Persisted unresolved
source or hedge submissions block new source risk; only explicitly supported,
fully evidenced startup recovery can release that pause. Other HOLD/UNKNOWN
states remain for diagnosis, never deletion of state to unlock trading.

## Explicit Maker state migration (offline)

Fresh Maker states use schema 7. Existing schema 3–6 files remain readable and
upgrade on their next write, without inferring the cause of old freezes. New
files record whether a freeze came only from a normal fill cycle; this alone
does not authorize recovery without complete native and venue evidence.
Taker states now write schema 2 and still read schema 1. The new formats retain
the original zero-fill rejected hedge ID alongside its single permitted replacement.
Shared Maker/Taker uses a separate schema 9 file; it does not merge standalone states.
Older binaries cannot read schema 7/8/9 (Maker/shared) or 2 (Taker); do not downgrade them against upgraded
active state or restore stale state to bypass that check.
To convert an old single-file schema 2 state or
both schema 1 direction files, stop the old strategy and write to a **different**
state prefix:

```bash
uv run py000-maker-migrate \
  --input-prefix runtime/old-maker \
  --output-prefix runtime/maker-migrated \
  --source-instrument XAUTUSDT-PERP.BITFINEX \
  --hedge-instrument XAUUSD.MT5 \
  --stopped
```

This command needs no credentials and does not connect, trade, stop processes, or
change profiles. `--stopped` is the operator's declaration, not a process lock;
the tool also rejects inputs that change during conversion. Both old direction
files must be present for schema 1, including a valid empty file if one direction
never traded. Its instrument IDs are operator-supplied bindings because schema 1
has no instrument header; schema 2 must match its existing header.

The new schema 8 file preserves old orders, fill identities, hedge plans and
UNKNOWN/HOLD states. A labeled legacy checkpoint retains known historical totals
without inventing missing individual fill quantities or replaying old hedges.
Only subsequent real fills enter the new per-fill allocation ledger. The old
files remain unchanged, and an existing destination is never overwritten. If
publication succeeds but directory sync fails, the complete new file remains:
inspect it rather than rerunning over it or deleting state to unlock trading.
Failure to remove a temporary name after successful publication only emits a
warning: the complete destination remains valid and the command still succeeds.

Review the result before selecting the new `store_path_prefix`. Conversion is not
reconciliation or recovery of Nautilus's runtime cache. Nonzero route residuals
still block new orders in strict mode; bounded carry is not enabled by migration.

## Maker residual budgets

`MakerStrategyConfig` defaults to `residual_mode="strict"`,
`residual_limit_ounces=Decimal("0")`, and `max_unhedged_ounces=None`.
Strict requires zero residual before the next cycle. For offline bounded-carry
tests, normal strategy configuration can explicitly select:

```python
residual_mode="bounded-carry"
residual_limit_ounces=Decimal("0.5")
max_unhedged_ounces=Decimal("2.5")
```

Bounded carry is limited to one explicit source/hedge account route. It preserves
the existing allocation ledger and only releases a completed cycle after source
terminal reconciliation and full hedge completion. UNKNOWN, unfinished hedges,
foreign-route residuals and existing holds do not gain an exemption. The next
order must still be an ordinary economic opportunity, not a dust-cleanup trade.

Both working sides consume the exposure budget separately. With 2oz on each
side, admission uses a conservative 2.5oz unhedged bound; opposite quotes are not
assumed to cancel each other. MT5 cumulative exposure and individual order-lot
limits are checked separately, including ticket changes after partial fills.
The current broker minimum and step must also permit the original 1oz rounding
unit whenever a hedge may be required.
The residual limit may be smaller than 0.5oz, but never larger. A stop retains
and reports signed residuals; nonzero residual is not FLAT.

These fields do not change funding/swap `CarryConfig`. Existing online profiles
and canaries remain strict with their original lot limits. Shared mode uses one
route carry budget across all views, not a separate allowance for each strategy.
Local recovery and shared tests do not certify the current deployed EA or a DEMO session.

## MT5 EA v1 checkpoint

The PY000-specific protocol and EA are specified in
`docs/MT5_EA_V1_PROTOCOL.md`. The read path contains a strict Python codec, a
Nautilus `LiveMarketDataClient`, a small offline-buildable SHADOW node, atomic
snapshots, persistent event paging, and tick/heartbeat PUB.

The current source also contains one deliberately narrow execution candidate:

- the EA defaults to `DISABLED` and arms only under explicit `DEMO` mode on a
  hedging demo account;
- the only native trading path is one bounded `MARKET` + `FOK` call, used either to
  open exposure or to reduce one exact hedging ticket; both paths durably reserve before
  `OrderSend` and record terminal `order_rejected`, `order_filled`, or `order_unknown`
  evidence afterward;
- the Nautilus execution client accepts ordinary opens or `reduce_only` orders carrying
  an exact `PositionId`, converts ounces to exact lots without rounding, submits once,
  consumes the journal contiguously, and leaves timeout or `order_unknown` pending;
- every connect binds an operator-supplied `expected_stream_id`, projects the complete
  retained journal without replaying historical terminal events, and verifies a stable
  journal/snapshot cut before admitting execution;
- the snapshot exposes the EA's configured single-order ceiling as the closed
  `execution_limits.max_order_lots` value. Execution binds it to
  `expected_max_order_lots`, intersects it with the broker volume maximum, and rejects
  an oversized order locally before transport. Before creating a Bitfinex source order,
  live Taker also checks its maximum possible hedge against that same current MT5
  quantity rule; MT5 execution repeats the check after any source fill;
- `UNKNOWN`, a dangling reservation, blocked recovery, mismatched FOK quantity, or any
  same-symbol foreign-magic position keeps execution on HOLD and makes reconciliation
  reports fail explicitly. The existing poll loop refreshes the snapshot at a bounded
  configurable interval (default one second): identity, recovery, or critical execution-spec
  drift fails closed, while removal of foreign-magic positions can restore admission;
- the complete journal now projects deterministic bulk order/fill/position reports and an
  exact single-order report. Filled orders preserve native MT5 order, deal, and position IDs;
  rejected orders use a stable stream/account-scoped synthetic venue ID. HEDGING positions
  missing from the current snapshot report FLAT against their exact cached PositionId rather
  than an anonymous net position. This reconciliation report is not a broker close command.
  Cancel/modify and production deployment remain intentionally absent. General Taker and Maker
  hedges apply signed deltas across MT5 HEDGING positions in deterministic PositionId order: each
  opposing ticket is closed with an exact reduce-only order, then at most one residual position is
  opened after all planned closes finish. MT5 legs are serialized per standalone strategy,
  or across both strategies for the full bound plan in the shared node;
  position-shape drift, rejection, or UNKNOWN persists HOLD and stops the remaining legs. Multiple
  Bitfinex partial fills are durably queued behind the same single-flight boundary. The bounded
  one-shot `--close-existing` canary remains restricted to one exact ticket; a multi-ticket close
  canary remains outside this checkpoint.
- the MT5 client publishes snapshot-backed equity, used margin, and free margin as a Nautilus
  `AccountState` before reconciliation. Live readiness requires both venue accounts to be
  registered; an inconsistent account equation fails closed.
- new MT5 tickets have a fixed 32-ticket same-symbol capacity check in Python preflight and
  the EA, including a second native check after `OrderCheck`. Exact closes keep their existing
  checks and are not blocked by this count. Full snapshots are never truncated; pre-existing
  over-size accounts can still require manual recovery. The measured byte envelope and narrow
  2oz test-profile scope are documented in [the protocol](docs/MT5_EA_V1_PROTOCOL.md).

The strategy layer keeps venue roles explicit: Taker submits `LIMIT` + `IOC` on the
Bitfinex source leg, Maker maintains `LIMIT` + `GTC` + post-only source quotes, and both
MT5 hedge legs submit `MARKET` + `FOK` to match the currently verified adapter. They are
not yet deployed as the current remediation candidate. Thin ordinary compositions bind the four existing
Bitfinex/MT5 data and execution clients with explicit account and venue routes. The
`py000-taker-live`, `py000-maker-live`, and `py000-both-live` share credential loading,
and lifecycle code. Default validation is offline, including when the profile specifies a
cache database. Explicit `--rehearse` reads the three `BFX_TEST_*` values and connects after
removing the strategy and business recovery Actor: it neither trades nor recovers business
state. Adapter observations, including CID fees and explicit native-cache writes, are permitted;
rehearsal is not a promise that every local file remains unchanged. Mutually exclusive
`--run-paper` requires the bound Bitfinex paper symbol, wallet, and account and runs the ordinary
strategy until a signal or explicit node stop. Neither command is the canary subclass.
The live MT5 account and configured magic must be dedicated to this node so an external position
change cannot invalidate the preflight between a Bitfinex submission and its fill.

## Ordinary startup and costs

Live costs stay inside the venue boundaries already owned by Nautilus. The Bitfinex data client
subscribes to `status` with `deriv:<symbol>` and publishes `NEXT_FUNDING_ACCRUED` as a native
`FundingRateUpdate`; a missing, future-dated, stale, or conflicting update keeps source admission
closed. The MT5 data client exposes its validated POINTS swap specification through the instrument,
and Taker normalizes the signed long/short daily rates from that specification, the current MT5 ask,
the broker server timezone, and the broker-native seven-item Sunday-through-Saturday `swap_rates`
vector.

Cost parity has a precise boundary: the original ZIP reads Bitfinex
`position.margin_funding`, not `NEXT_FUNDING_ACCRUED`. Both map an explicitly
supplied value to `(long=f, short=-f)`, but the producers are not interchangeable.
The original Python MT5 enum also numbers POINTS as 0, while the current native
protocol uses DISABLED=0 and POINTS=1. The ZIP contains no EA with which to verify
the old wire numbering. POINTS formula comparisons therefore map the enum and
complete weekday dictionary explicitly; they do not certify raw-wire equivalence.
Current missing-data rejection is intentional: the legacy zero-cost and inferred
weekday fallbacks are not restored. Expected carry is not realized cash flow.
The checked-in [caller vectors](tests/fixtures/legacy_caller_vectors.json) and
[carry vectors](tests/fixtures/legacy_carry_vectors.json) record the authenticated
inputs/outputs and these limits; their tests require no local legacy ZIP.

The active legacy strategy's combined fee (`0.00065`) and implicit USD/USDT parity (`1:1`) remain
ordinary strategy configuration. They are not fabricated as periodically refreshed venue facts.
There is no cost-file producer, daemon, database, or extra lifecycle.

Offline validation builds and disposes the node without reading `.env` or opening either venue:

```bash
uv run py000-taker-live --profile /path/to/taker-profile.json
uv run py000-maker-live --profile /path/to/maker-profile.json
uv run py000-both-live --profile /path/to/both-profile.json
```

[The redacted profile example](examples/live_profiles.py) prints a complete typed JSON
profile for any of those modes (`uv run python examples/live_profiles.py both`). It
does not read `.env`, write state, or connect. Replace the dummy account IDs, endpoints,
build/hash/stream and instrument specifications with verified values; the example is
not ready to trade. Keep API key/secret out of JSON: network modes load only
`BFX_TEST_API_KEY`, `BFX_TEST_API_SECRET`, and `BFX_TEST_USER_ID` from the existing environment
or `.env`. Native Redis is explicit; do not reuse a previous run's TraderId namespace
with unrelated business/CID files, and do not erase history to make a profile validate.

### One node, two strategies

`both` uses one `TradingNode`, four venue clients, one CID writer, one recovery Actor,
and one atomic `<shared_store_prefix>.shared.json` owner with Maker bid/ask and Taker
views. Each strategy retains its actual native StrategyId and order ownership.
The standalone paths in the nested configurations are not migration inputs: existing
standalone state is rejected, not silently imported. Retain all original shared/CID/native
history together for a restart; never run independent Maker/Taker runners against these accounts.

The account cap is shared. Working, inflight and pending-cancel source orders count at their
worst possible fill; opposite unfilled orders do not offset. Already created hedge obligations
execute in allocation order, holding the lane across every close/open leg. A Taker close can
reduce a Maker ticket without transferring the native Position's opening owner.
Unexpected current venue/native position differences pause both strategies and start the
existing bounded reconciliation, without an automatic account cleanup trade.

This is non-preemptive source admission, not an opportunity scheduler. At a 2oz cap,
Maker working quotes of 2oz on both sides can leave Taker no capacity until a quote is
confirmed closed. It is valid for Taker to wait indefinitely while that capacity is occupied;
the entry does not secretly raise the cap or cancel another strategy's valid quote.
Both strategies' participation is tested where the shared capacity permits it.
Current-source joint, installed-process and finite-DEMO qualification are separate gates
in the implementation plan; a present CLI is not a claim that all have passed.

`--rehearse` is an explicit authenticated network action, but still starts no strategy. It proves
only adapter connection, reconciliation, portfolio initialization, and clean shutdown—not live
strategy readiness.

`--run-paper` is an authenticated paper-only action, not an unattended production deployment.
The typed profile accepts optional `cache_database` (native Redis `DatabaseConfig`) and
`stop_timeout_seconds` (finite, in `(0, 60]`, default `10`). With no database, it does not promise
cross-process native history. Validation deliberately does not test the configured Redis backend.
Ordinary startup uses the existing evidenced recovery rules, not a blanket clear-HOLD operation.

Inspect an existing business file offline, without credentials, Redis, or venue access:

```bash
uv run py000-taker-live --profile /path/to/taker-profile.json --inspect-recovery
uv run py000-maker-live --profile /path/to/maker-profile.json --inspect-recovery
uv run py000-both-live --profile /path/to/both-profile.json --inspect-recovery
```

Inspection reports local pauses and unfinished obligations; it cannot certify remote execution.
After reviewing the cause, `--run-paper --resume-held` explicitly requests full reconciliation of
an old pause **for this startup only**. Missing history, uncertain requests, conflicting positions
or fees still block recovery. A new pause/failure during this startup revokes that review.
For an explicitly planned hedge whose original order and complete EA journal both prove a
zero-fill rejection, add `--retry-rejected-hedge OLD_CLIENT_ORDER_ID`. This retains the old ID,
consumes at most one replacement per obligation, and lets the original dispatcher generate a
new ID for that same leg. Current session, ticket, lot, quote and account capacity must qualify
within 30 seconds of startup capture. This is a qualification deadline, not a fill guarantee;
waiting for the first MT5 PUB also respects the configured reconciliation-round timeout.
MT5's protocol has no depth: zero native quote sizes mean unknown, not fabricated liquidity.
Fresh valid prices and current account/permission/plan facts are still required.
The resulting confirmed obligation uses the normal pending/drain lifecycle. A second rejection
remains held. Unknown or partially filled requests are never classified as zero-fill rejection.
These temporary choices are not profile settings, do not raise order/account budgets, and do
not apply to canaries. Never edit/delete custody files to manufacture a clean start.

Normal node/signal stop first closes new source/modify admission while keeping execution and
fill/terminal callbacks online. It requests each known source cancel at most once and progresses
only existing confirmed hedge obligations, then calls native stop. It does not flatten an already
hedged position. `drain_complete` reports that execution result separately from accounting.
`PAPER_STOPPED` requires a completed drain and a `FINAL` accounting report;
`PAPER_INCOMPLETE` exits nonzero with pending IDs/reasons and residuals.
A timeout, old external HOLD or failed pause publication
cannot be reported as success. Keep retained state for diagnosis; don't rerun blindly or delete it.
The in-tree ordinary process matrix covers 22 Maker/Taker cases using real SIGKILL/SIGTERM and
disposable Redis, with synthetic venue I/O. It retains UNKNOWN/old HOLD and reports incomplete
accounting when retained native fills cannot prove missing NETTING cycles. This is not installed-artifact,
real-EA/network, power-loss or finite-DEMO qualification. Those remaining release boundaries
are tracked in the implementation plan; installing a command does not enable an automatic canary.

### Realized trading PnL and commission at exit

The ordinary paper entry reports retained native facts **before** disposing the cache.
`accounting` separates native realized PnL, native booked and already-embedded commission,
venue raw and quantized costs, rounding, and provisional correction by currency. The bridge is
`native realized + embedded commission - venue raw cost`; this avoids charging MT5 fees twice
or assuming a Bitfinex USD fee was already deducted from its USDT-settled Position.
Only then does it value each currency's net amount in USDT using the profile's explicit
USD/USDT bid for positive USD and ask for negative USD. This is valuation, not a real FX trade.

`FINAL` applies only to owned cached realized trading PnL and trade commission. Funding,
swap, other broker fees and unrealized PnL are excluded, not estimated as zero or taken from
`CarryConfig`. It is **not** the account's complete net profit. Native Position snapshots are
included; missing old NETTING cycles, incomplete fees, unresolved orders or conflicting facts
produce `PENDING` with reasons and null final values. Observed subtotals remain visible.
When native Redis has retained all owned Bitfinex fills but not old in-memory NETTING
snapshots, the report can reconstruct missing closed cycles using pinned Nautilus position
math in a disposable, disconnected context. It first checks retained positions, calculation
fields and unambiguous fill order; it never repairs the live cache or replays trading events.
`reconstructed_closed_cycles` identifies this report-only provenance. Unprovable history
remains `PENDING`; MT5 ticket and fee checks are unchanged.
A successful execution drain does not override an incomplete report. Validate and rehearse
do not generate this report; neither command executes business recovery.

For `both`, the report is explicitly the two owners' **native virtual realized trading
PnL and commissions**, not venue realized cash flow. Opposite native strategy positions
can remain open while the venue is flat. Their virtual realized PnL cannot be relabeled
as account cash profit, and an exact MT5 cross-owner close does not invent a new fee allocation.

The attached cap-bound Taker-paper EA candidate defaults to build ID
`py000-mt5-ea-v1-taker-paper` and `InpMaxOrderLots=0.02`. Its six-source manifest is
`9a361346c2b18c388287df62f3a3fe49b6adb02eb6348392aa120493af14820a`; the isolated
MetaEditor build completed with 0 errors and 0 warnings and produced
`PY000_Nautilus_MT5_taker_paper_9a361346.ex5` (SHA-256
`a1f78a5dce41affb72780d1654049a38afe7d286c38eea0978a28d4e31b45b55`). It was attached
to the paper account on ports 6001/6002 and passed a strategy-free rehearsal on 2026-09-03:
all four clients connected, both accounts registered, reconciliation and portfolio
initialization succeeded, and the node shut down cleanly with zero open positions. No strategy
or order ran in that rehearsal. The subsequent historical e812 source exposes the broker-native
`swap_rates` vector as exactly seven values ordered Sunday through Saturday. Its source manifest is
`e8126bf3ef0b42d01facdd2ef30f048972062b5ca38c81b84717156fb74cad00`. MetaEditor
compiled it with 0 errors and 0 warnings as
`PY000_Nautilus_MT5_taker_paper_e8126bf3.ex5` (SHA-256
`686c5386e15fc74b6b622b133225c33b70f72768296912e1a3132ea738c2b551`). After the e812
build was attached, a fresh read-only `hello` and snapshot authenticated the full manifest,
journal stream, 0.02-lot ceiling, server timezone, and native `[0,1,1,3,1,1,0]` swap vector;
recovery was ready and the account had zero positions and zero margin. The first strategy-free
rehearsal ended at the original 10-second connection-stage limit about half a second before the
Bitfinex paper execution session finished authenticating. No strategy or order ran. With the
paper profile's bounded stage timeout corrected to 20 seconds, a fresh rehearsal connected all
four clients, reconciled both venues, initialized a zero-open-order/zero-open-position portfolio,
and shut down cleanly with `REHEARSED / adapter_startup_rehearsed`. A final read-only check found
only `stream_started` in the current EA boot and no mutation event. Any canary still requires
separate, explicit authorization.

The thin `py000-taker-canary` entry point is now available for that next checkpoint. Its default
mode is offline and disarmed: it validates the separate canary profile and builds the existing
Taker composition without reading credentials or opening a connection. The profile fixes every
source, hedge, and risk quantity at exactly 2oz while leaving the rehearsal profile at zero
capacity. The dedicated paper profile currently uses a deliberately permissive `-0.005` long
threshold so the canary tests execution flow rather than profitability. An explicitly authorized
`--execute` run, always with a fresh strategy-state path, permits one source `LIMIT` + `IOC` attempt in
the selected `long` or `short` direction, persistently closes the source gate before submission,
and lets the existing fill-driven Taker submit the opposite MT5 `MARKET` + `FOK` hedge. In open
mode, only exact 2oz fills on both legs, no open orders, and exact opposite final positions produce
`PASSED_PAIRED`; that successful mode deliberately leaves the pair open. With `--close-existing`,
the start must instead be one exact opposite 2oz pair with one exact MT5 ticket. Both source and
hedge orders must be reduce-only, and only a fully flat reconciled result produces `PASSED_FLAT`;
multi-ticket close canary execution is outside this checkpoint. Partial or uncertain outcomes freeze
that run's state for operator review and never auto-unwind. Before execution, the dedicated MT5 test account must also be
manually checked for orders from other programs or manual trading because the v1 snapshot exposes
positions and this EA's journal, but not an account-wide pending-order list.

Offline preparation only:

```bash
uv run py000-taker-canary --profile runtime/taker-paper-canary.json --direction long
```

`py000-taker-smoke` composes those same one-shot canaries into a repeatable paper/demo
open-close check. It accepts a positive-threshold base profile and leaves that profile unchanged.
Only the derived test legs use the forced economic threshold `-0.005` and the bounded 5s quote /
2s cross-leg windows needed by the two asynchronous feeds. Default mode builds every leg offline
without credentials, network access, or filesystem writes:

```bash
uv run py000-taker-smoke \
  --profile runtime/taker-paper-rehearsal.json \
  --direction both
```

With `--execute`, it writes fresh credential-free leg profiles under `runtime/`, verifies both
test accounts from new read-only connections, then runs SHORT open/close followed by LONG
open/close. Every leg remains exactly 2oz/0.02 lot. A non-pass, authority mismatch, or unreadable
result stops the sequence without a retry; overall `PASSED` requires an independently observed
flat final state. The base profile must use positive strategy thresholds and freshness no wider
than 5s for quotes, 2s cross-leg skew, and 15s for the MT5 tick, so the earlier 240s diagnostic
profiles cannot be reused here. Derived legs retain the 15s MT5 tick ceiling and use at least a
5s MT5 REP read deadline, matching the bounded mitigation already proven by the mirror canary.

## Bitfinex public read-side v1 candidate

The repository now contains one deliberately small, offline-testable Nautilus live-data
candidate for the production `tXAUTF0:USTF0` or paper
`tTESTXAUTF0:TESTUSDTF0` profile, each mapped explicitly to the canonical
`XAUTUSDT-PERP.BITFINEX` instrument. It subscribes to the public `P0`/`F0`/`len=25`
book plus the exact derivative-status key, enables Bitfinex checksum flag `131072`, and maintains
the two 25-level sides. It publishes the complete atomic initial snapshot as the baseline. After
any delta it withholds the changed book until that state passes the venue CRC.
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
It evaluates only on a new actionable source-depth callback—either the atomic initial snapshot or
a post-update CRC-verified state—never on a later hedge tick against retained source data.

The checked-in 11-frame fixture is a bounded public wire capture. Factory and node
construction are tested without opening a socket. This public client has no credentials,
ordering, or account state. It has no retry supervisor or live probe, and is not
live-qualified. Any runnable composition must require current client connectivity because
Nautilus retains the last cached market data after a feed disconnect; the offline Taker builder
already installs that independent readiness gate.

## Bitfinex private execution v1 candidate

The repository also contains an offline-tested private WebSocket execution slice for the
same canonical instrument. Production binds `tXAUTF0:USTF0` to the `USTF0` margin wallet;
paper binds `tTESTXAUTF0:TESTUSDTF0` to `TESTUSDTF0`. Crossed profiles fail configuration,
and authenticated REST user info must match both the configured user ID and paper/live mode
before the private socket opens. It supports only Taker `LIMIT` + `IOC` and Maker
post-only `LIMIT` + `GTC`, with integer per-order leverage, price-only Maker modify, and
native-ID cancel. A CID is persisted before `OrderSubmitted` and before the single wire
send. Production creates fills only from the final `tu`, preserving its venue fee and liquidity
side. On the dedicated Bitfinex paper symbol, the earlier `te` execution event creates a prompt
zero-commission fill with explicit fee-unavailable provenance so the MT5 hedge is not delayed;
a later `tu` must match and is deduplicated. If both streamed trade events are absent but a
same-process cached order has an exact fully-executed history delta, Nautilus reconciliation can
infer the missing fill before the canary continues. Every successful submit, modify, and cancel
send has an independent ACK
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
explicitly. Same-process hot reconnect and a continuously runnable two-venue composition remain
absent. The offline and ordinary open-mode Taker builders keep startup reconciliation enabled and
disable synthetic missing-order generation. Close-only canary mode narrowly enables cold position
reconstruction for exactly the configured source and hedge instruments and claims only the source
position for the Taker strategy. Paper and production
must use separate account IDs, CID/state paths, and caches; the shared canonical instrument
ID means the two profiles must never run concurrently in one node.

Both ordinary Python builders now accept an optional native `cache_database: DatabaseConfig`.
The default remains in-memory and builds without a network connection. Supplying Redis connects
the cache during construction, but never opens trading transports. The existing TraderId stays
fixed across runs; instance-specific keys and startup flushing are disabled. Use a dedicated
cache for each account/mode: loaded accounts, instruments, owners and execution-client indexes
must match the single-strategy composition, otherwise construction fails without clearing data.
An unfilled MT5 reduce-only order must retain its exact-close position index; Bitfinex NETTING
reduce-only orders do not require an MT5-style ticket index.
Both ordinary profiles expose this option. `--run-paper` and `--rehearse` pass it to the native
builder; default offline validation checks its configuration but does not connect the backend.

Native Redis writes and fresh-process loads have offline integration coverage, including
partial fills, the MT5 identifier and pending exact-close index, fee provenance and duplicate
TradeIds. This is **not** automatic strategy recovery: loading historical orders or positions
keeps new-source admission closed with `restart reconciliation is pending`. Joining the venue
facts and business obligations is implemented for the recorded startup categories, not every
HOLD or rejected order. Online drain is wired to ordinary node stop; abrupt-crash consistency,
the remaining recovery categories and current-account qualification remain W6/W9 work.
No Redis service is installed or managed by this package.

The opt-in integration test requires a **fresh disposable local Redis**, used only for synthetic
test data. Set `PY000_NATIVE_CACHE_PORT` to its localhost port and run
`.venv/bin/python -m pytest tests/test_native_cache.py`. Without the variable it is explicitly
skipped, not certified. The missing-position test restores only its known synthetic fixture index
before the independent missing-client test, which intentionally leaves an incomplete cache.
Discard the disposable instance afterward instead of pointing another run at it. The test never
starts a service, flushes a database or deletes an existing namespace.

`py000-bitfinex-paper-canary` is the one bounded exception used to exercise this adapter.
It reads `BFX_TEST_API_KEY`, `BFX_TEST_API_SECRET`, and `BFX_TEST_USER_ID` from the process
environment or `.env`. Without `--execute` it performs authenticated REST preflight only:

```bash
uv run py000-bitfinex-paper-canary --output /new/path/bitfinex-preflight.jsonl
```

Mutation requires `--execute`, an unused CID-state path, a clean target symbol, sufficient
`TESTUSDTF0` balance for 1x, current actionable book data, connected data/execution clients, and
successful Nautilus startup reconciliation. The final account snapshot completes before the
paper-book subscription starts. The complete initial snapshot can satisfy the actionable quote
wait immediately; any later delta again requires a matching checksum before publication. The quote
wait remains independently bounded at 60 seconds, while connection, REST, reconciliation, and
mutation operations retain their separate 10-second default. It then
submits exactly one `BUY 2` post-only
`LIMIT/GTC` at 5% below the current bid and sends one native-ID cancel immediately after the
acceptance event. It never changes the quantity, side, or leverage; never retries a mutation;
and never uses cancel-all or touches MT5. `PASSED` additionally requires exact canceled-order
history for that CID and venue ID, no matching trade, a flat final position, no active order,
and final Nautilus reconciliation. `PASSED` qualifies that submit/cancel lifecycle, not an
independent proof that the paper venue enforced post-only. `UNKNOWN` retains the CID transcript
for manual inspection; the existing CID state prevents an automatic rerun.
Bitfinex paper has been observed omitting the submitted post-only flag from both active and
canceled order rows. Only a same-process pending submit with otherwise exact identity and
semantics may use an unfilled active row to bind its native ID; production, restart, mass, partial,
and terminal reconciliation remain strict. The venue report retains `post_only=false`; only the
internal same-run matcher treats it as missing evidence. Final evidence records raw flags and
`post_only_assurance=SUBMITTED_INTENT_ONLY` rather than claiming venue enforcement. A raw `4096`
is recorded as `VENUE_FLAG_OBSERVED`.

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

The first explicitly authorized 6001/6002 open-to-close canary for the exact-ticket
extension stopped before `OrderSend`: MQL `ZeroMemory` left the open request's omitted
close-target strings as `NULL`, while the new journal presence check compared them with
`""`. No reservation event was appended, no order or position was created, and the
account balance/equity remained unchanged; the client did not retry. The corrected source
uses string length for optional-target presence. Its manifest is
`1fcc4a3ff232167292234f798409202441b7881b34b463dd9141d3bd23db5224`; MetaEditor compiled
the candidate with 0 errors and 0 warnings. After deployment, a fresh read-only admission
and two consecutive action-time session samples passed. The separately authorized canary
filled `BUY 0.01` at `4427.69`, resolved position ticket/identifier `10338497947`, then
filled one exact-target `SELL 0.01` close at `4427.35`. Both broker retcodes were `10009`;
there were no mutation retries. Journal sequences 14 through 17 are exactly open reserved,
open filled, close reserved, and close filled. The final snapshot is empty with margin zero,
balance/equity `79292.95`, and recovery ready. This validates one DEMO exact-close path,
while the separate strategy wiring remains offline-tested only; neither grants production
authority.

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

It does **not** establish complete oracle or live parity. The current remediation
checkpoints cover dynamic venue margin and ordinary Maker/Taker continuous execution
through the two execution adapters with offline venue I/O. Both ordinary entries expose
`--run-paper`, optional native persistence, and online drain before native stop.
Complete process-crash recovery, shared-account operation and current-code DEMO
acceptance remain unfinished. See the implementation plan for
the exact accepted boundaries; these are not live-readiness claims.

The following is the **historical 2026-09-03 initial checkpoint**, not the current
capability list. At that time, the only runnable strategy path was the separately
authorized fixed-2oz one-shot canary. Its first paper execution filled the 2oz Bitfinex source order but exposed
the missing-`tu` propagation gap before an automatic hedge; a bounded compensating MT5 hedge restored
an exact opposite test position. That incident remains `UNKNOWN` with paired recovery evidence—not
`PASSED_PAIRED`. After the `te` paper-fill fix and a fresh state path, the authorized 2026-09-03
rerun returned `PASSED_PAIRED / exact_2oz_pair_reconciled`: Bitfinex BUY 2oz
(`243269180012` / trade `1967905585`) drove the MT5 SELL 0.02-lot hedge
(`10349046774` / deal `10065080588`). Final reconciliation and an independent account snapshot
both showed zero open orders and exact opposite `+2oz / -2oz` positions. The general
Taker startup entry then validated exact four-client wiring and rehearsed the adapters
only after removing the strategy. MT5 `MARKET` + `IOC` partial-fill execution remains
outside the current supported contract; the DEMO `MARKET` + `FOK` path is not full
strategy parity.

The first authorized close canary did not qualify. Its Bitfinex `SELL 2` IOC was accepted and
fully executed by the venue with reduce-only flag `1024` (order `243271104376`, trade
`1967947277`, price `4466.9`), but the private stream supplied no authoritative terminal to the
strategy before the bounded deadline. Local state therefore correctly remained `SUBMITTED` with
zero observed fill and the run ended `UNKNOWN`; no automatic MT5 close occurred. One separately
bounded exact-ticket recovery then bought `0.02` lots against position `10349046774` (order
`10349852165`, deal `10065921857`, price `4482.89`, retcode `10009`). An independent read proved
both venues flat, zero Bitfinex active orders, zero MT5 margin, and MT5 recovery ready. This is
`UNKNOWN + RECOVERED_FLAT`, not `PASSED_FLAT`; the incident state is preserved and close
requalification requires fresh open and close state paths after the silent-terminal reconciliation
fix is verified.

That fresh requalification completed on 2026-09-04 against the current reader-fix tree. One
pre-arm readiness window expired without creating state or sending an order. The next fresh open
run returned `PASSED_PAIRED`: Bitfinex BUY 2oz (`243337564815` / trade `1967999385`, price
`4476.8`) drove MT5 SELL 0.02 lot (`10350460228` / deal `10066556793`, price `4479.48`). A
first close signal window at the realistic `0.0005` short threshold returned `NO_ATTEMPT` with no
state or mutation. Restoring the test-only short threshold to `-0.005` then produced
`PASSED_FLAT`: Bitfinex SELL 2oz reduce-only (`243326255693` / trade `1967999516`, price
`4470.4`, flags `1024`) drove an exact-ticket MT5 BUY 0.02-lot close (`10350506592` / deal
`10066605263`, price `4478.78`, target position `10350460228`). No ambiguous mutation was
retried. Final reconciliation passed, and a separate post-run read found Bitfinex position zero,
zero Bitfinex active orders, no MT5 positions, and MT5 recovery/session ready. This qualifies the
bounded paper/demo open-close path on the recorded working tree, not continuous or production
operation. The mode-0600 evidence record is
`runtime/taker-paper-readerfix-cycle-20260904.json`.

The canary readiness deadline now emits one bounded, value-free snapshot of the missing gate
names, input ages, and callback counters. It does not include hold text, account values, prices,
quantities, or credentials; a diagnostic failure falls back to a fixed redacted reason. This
changes diagnosis only: readiness limits are unchanged, the gate is never armed on timeout, and
there is no automatic retry.

The separately authorized mirror run on 2026-09-04 exercised SHORT open followed by LONG close.
The source SELL 2oz filled on Bitfinex (`243321477533` / trade `1968172784`, price `4470.1`),
and the MT5 journal independently proved the BUY 0.02-lot hedge filled (`10351975608` / deal
`10068147732`, price `4482.55`). The run nevertheless remains `UNKNOWN`, not `PASSED_PAIRED`,
because the local strategy did not consume that terminal: a periodic MT5 snapshot timed out after
the hedge submit, and the one-second client deadline reset the transport while the synchronous EA
mutation occupied the single REP service. The source order was not retried. An independently
verified exact pair was then closed with one fresh LONG close profile whose MT5 data and execution
request timeouts were set to five seconds. That close returned `PASSED_FLAT`: Bitfinex BUY 2oz
reduce-only (`243362148837` / trade `1968172833`, price `4475.9`, flags `1024`) drove the exact
MT5 SELL 0.02-lot close (`10352033578` / deal `10068208788`, price `4478.21`, target position
`10351975608`). A separate final read found Bitfinex position zero, zero Bitfinex active orders,
no MT5 positions, and MT5 recovery/session ready. The timeout override is a bounded test-profile
mitigation, not a complete transport fix. The mode-0600 record is
`runtime/taker-paper-readerfix-mirror-cycle-20260904.json`.

A fresh mirror requalification then used separate open and close state files with five-second
MT5 data and execution deadlines. Its SHORT open returned `PASSED_PAIRED`: Bitfinex SELL 2oz
(`243343249726` / trade `1968172875`, price `4467.1`) drove MT5 BUY 0.02 lot
(`10352076527` / deal `10068253576`, price `4477.71`). An independent read proved the exact
`-2oz / +2oz` pair and zero active source orders. The LONG close subsequently returned
`PASSED_FLAT`: Bitfinex BUY 2oz reduce-only (`243357267718` / trade `1968172895`, price
`4471.6`, flags `1024`) drove the exact-ticket MT5 SELL 0.02-lot close (`10352105294` / deal
`10068283910`, price `4472.43`, target position `10352076527`). No mutation was retried, and
a separate final read found both accounts flat, zero Bitfinex active orders, and MT5
recovery/session ready. During the MT5 close, however, the periodic DataClient snapshot still
exceeded the five-second deadline; the DataClient failed closed, cancelled its PUB task, closed
its transport, and remained disconnected for the rest of the run. The independent ExecClient
later consumed the durable terminal; no ExecClient transport error was observed, so submit
timeout recovery is not claimed. The timing is consistent with the synchronous mutation occupying
the single REP service beyond the DataClient deadline, but that queue occupancy was not directly
instrumented. The one-shot outcomes are therefore valid, but the shared single-REP
continuous-operation boundary remains unqualified; five seconds is mitigation, not a structural
fix. The mode-0600 record is
`runtime/taker-paper-rep5s-mirror-requalification-20260904.json`.

The subsequent adapter-side repair keeps the Data and Exec sockets independent but serializes
their requests through one event-loop/REP-address lane, matching the EA's single-threaded REP
service. Query deadlines now begin only when a request reaches that lane, while MT5 mutations use
a separate 15-second deadline. A periodic snapshot timeout makes Data readiness unavailable but
keeps PUB and the retry loop alive; only a later validated snapshot restores admission. Execution
query timeouts likewise hold admission until a valid poll, while mutation timeout remains sticky
`UNKNOWN` and is never replayed. Deterministic offline tests cover the delayed-mutation collision,
production composition wiring, recovery, and hard-error fail-closed boundaries. This code repair
does not rewrite the earlier evidence record or by itself qualify continuous live operation; that
still requires one fresh, explicitly authorized canary.

Dynamic venue margin capacity and live qualification of Maker cancel reconciliation remain
live gates. The private Bitfinex candidate applies the strategy's validated integer
per-order leverage; the simulated venue still uses configured account leverage. The
shared leverage helper preserves legacy floor division when its result is at
least one and intentionally clamps smaller results to one as a migration safety
guard; that edge is not claimed as bitwise legacy parity.

Initial funding/swap/FX and MT5-session snapshots exist for the backtests. The live Taker starts
with its session closed and its cost timestamp unset, consumes MT5 `InstrumentStatus`, subscribes
to Bitfinex `FundingRateUpdate`, derives POINTS swap from the current MT5 instrument snapshot and
quote, and independently requires both data clients plus both execution clients to be ready before
a source decision. Future-dated or stale funding, an invalid swap specification, a closed session,
or stale/skewed quotes keep Taker admission closed. Static fee and 1:1 USD/USDT parity remain
ordinary legacy strategy configuration; no passive cost file or producer exists. Any Maker
source fill also reserves its fill/hedge obligation first, then persists a
two-sided fill freeze and cancels both working quotes. A cycle releases without
timeouts only after every known source order has authoritative terminal
evidence, every hedge obligation is completed by real fills, and outstanding
and rounding exposure are zero on both stores. A Bitfinex cancel event now starts one
single-flight reconciliation task for the exact client and venue order IDs. Matching terminal
identity, state, quantity, fills, price, and average across the REST report, Nautilus cache, and
durable Maker record confirms the exposure; the paper venue's omitted post-only flag does not
claim venue enforcement. Missing, active, or conflicting reports leave the persisted gate closed.
This path is offline-tested, not yet live qualified; expiry and unknown outcomes remain stopped
for explicit recovery.

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
