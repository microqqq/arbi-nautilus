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
  same-symbol foreign-magic position keeps execution on HOLD. Matching-magic positions
  have a startup snapshot report; order/fill reports, runtime position refresh,
  cancel/modify, production composition, and strategy wiring remain intentionally absent.

The strategy layer keeps venue roles explicit: Taker submits `LIMIT` + `IOC` on the
Bitfinex source leg, Maker maintains `LIMIT` + `GTC` + post-only source quotes, and both
MT5 hedge legs submit `MARKET` + `FOK` to match the currently verified adapter. They are
still not wired into a live composition.

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

It does **not** establish complete oracle or live parity. Bitfinex data/execution,
MT5 `MARKET` + `IOC` partial-fill handling, and live Maker/Taker composition remain
unimplemented; the isolated DEMO `MARKET` + `FOK` candidate is not strategy parity.
Depth/VWAP beyond
an L1 level, dynamic venue margin capacity/account reports, authoritative cancel
reconciliation, Maker modify/cancel query closure, and application of per-order leverage by Bitfinex
remain unimplemented. The strategies pass computed leverage in execution
`params`, but the simulated venue only uses configured account leverage. The
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
