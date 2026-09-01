# py000-nautilus

Minimal first-stage migration of the active PY000 Taker strategy to
NautilusTrader 1.231.0. It uses Nautilus `Strategy`, native limit orders,
order/fill events, clock cancellation, cache, portfolio, execution routing,
and `BacktestEngine`.

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy
uv run pytest
uv run py000-sim
```

No credentials or live endpoints are used. A persisted unresolved source or
hedge submission stops new source orders until an operator reconciles it.

## Scope boundary

This simulation validates Taker direction, net-return terms, account-selection
ordering, position limits, executable L1 size gating, source-fill-driven hedge
obligations, duplicate/late fill handling, and restart stops. Canonical strategy
and Nautilus quantity is always gold ounces; a future MT5 adapter must convert
100 oz/lot at its wire boundary.

It does **not** establish complete oracle or live parity. Bitfinex and MT5-ZMQ
data/execution clients, depth/VWAP beyond an L1 level, dynamic venue margin
capacity/account reports, authoritative cancel reconciliation, and application
of per-order leverage by Bitfinex remain unimplemented. The strategy passes the
computed leverage in execution `params`, but the simulated venue only uses its
configured account leverage. Initial funding/swap/FX and MT5-session snapshots
exist for the backtest; live composition must refresh them through
`update_cost_snapshot` and `update_hedge_session`, and stale/skewed/closed inputs
fail closed. A green simulation is not live qualification.

The example notionals are derived from Nautilus `filled_qty * avg_px` and the
resulting portfolio positions. They demonstrate ounce-scale execution rather
than the former $24 unit error; they are not account margin or PnL proof.
