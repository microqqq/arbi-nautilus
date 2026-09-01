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

No credentials or live endpoints are used. A persisted unresolved source or
hedge submission stops new source orders until an operator reconciles it.

## Scope boundary

The simulations validate Taker direction and return terms plus Maker bid/ask
pricing, mirrored account selection, two-sided GTC maintenance, strict relative
requote thresholds, post-only two-tick source-touch clamps, actual-fill-driven
hedging, duplicate/late fill identity, stale/session cleanup, and restart stops.
Canonical strategy and Nautilus quantity is always gold ounces; a future MT5
adapter must convert 100 oz/lot at its wire boundary.

The authenticated active caller establishes Maker side selection, reference
price, spread inputs, fee/funding/swap mapping, signed amount, leverage, and
`fixed_amount=false`. The final multiplicative price law (`bid = base *
(1-spread)`, `ask = base * (1+spread)`) and the legacy XAUT absolute `0.2`
passive clamp come from the Owner-frozen, review-attested callee boundary; the
exact carrier is excluded because it contains credential literals. This repo
therefore tests that distinction explicitly and generalizes `0.2` as two
instrument ticks, rather than claiming executable callee provenance.

It does **not** establish complete oracle or live parity. Bitfinex and MT5-ZMQ
data/execution clients, depth/VWAP beyond an L1 level, dynamic venue margin
capacity/account reports, authoritative cancel reconciliation, Maker
modify/cancel query closure, and application of per-order leverage by Bitfinex
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
