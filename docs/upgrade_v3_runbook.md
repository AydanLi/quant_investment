# Quant System v3 runbook

## 1. Security and setup

- Never place Tiingo or broker credentials in source, `.env.example`, command
  arguments, screenshots, logs, or research JSON.
- Run `alembic upgrade head` before building a trusted snapshot.
- Use `python -m scripts.validate_data_sources` for a non-persistent entitlement
  smoke test. It reads the token with a hidden prompt when the environment
  variable is absent.
- Use `python -m scripts.build_trusted_snapshot` only after the smoke test. A
  blocked snapshot is retained for audit but cannot generate an order.
- A provider HTTP 429 exits with `provider_rate_limit`; wait for the documented
  provider quota reset and rerun. Do not rotate credentials, silently switch to
  Yahoo, or treat an incomplete fetch as a trusted snapshot.

## 2. Data incident workflow

1. Stop signal/order generation when the snapshot is blocked.
2. Record provider, ticker, date, raw values, retrieval time and issue code.
3. Check the primary authoritative source and an independent source. Do not
   overwrite the original provider row.
4. Wait for a provider correction or create a separately reviewed resolution
   policy/version. Never silently suppress a discrepancy.
5. Fetch again and create a new immutable snapshot. Do not change the old
   snapshot or reuse its ID.

The 2026-07-17 smoke test found a real unresolved example: official CBOE VIX
close was 17.76 for 2026-02-06 while Yahoo reported 20.37. The >20 bp rule
correctly blocks the snapshot. No exception has been assumed; the user must
approve any future adjudication policy.

## 3. Research sequence

1. Build an actionable dataset snapshot and approve its universe version.
2. Create the immutable 135-candidate protocol with
   `python -m scripts.create_research_protocol`.
3. Run nested core admission and persist every trial, including failures.
4. Freeze one strategy version only if all historical gates pass.
5. Evaluate the six risk-model candidates with:

   `python -m scripts.validate_dynamic_factor_model --snapshot-id <id> --strategy-version <version>`

6. Keep sample covariance if no candidate passes.
7. Start the paper clock only after the final strategy version is frozen.

Any change to the seed pool, filter rules, parameter grid, signal, cost formula,
or execution method requires a new strategy version and a new 12-month paper
period. A coefficient-only recalibration from the preregistered cost formula
does not restart the clock.

## 4. Daily/monthly operating flow

- Non-month-end: diagnostic display only.
- Month-end after 20:30 ET: require zero staleness, dual-source checks,
  approved universe, frozen strategy and normal/reviewable risk state.
- T+1 around 09:30 ET: refresh account, positions, open orders, reconciliation,
  quality and quotes.
- T+1 at or after 09:35 ET: create limit-order drafts. Review sells first, then
  buys. Approve each order individually.
- Cancel unfilled orders after five minutes. A move from 20 bp to at most 40 bp
  needs a second approval. Cancel/review partial fills after ten minutes.
- Reconcile positions, cash, fees, fills and open orders after execution.

## 5. Halt and recovery

| Trigger | Immediate action | Earliest recovery |
|---|---|---|
| Drawdown at least 15% | cancel buys, freeze risk orders, draft liquidation to `CASH_USD` | next monthly rebalance after reconciliation, incident report and human authorization |
| Daily loss at least 5% | temporary halt, no automatic liquidation | next NYSE session after dual-source confirmation and full account reconciliation |
| Stale/conflicting data | block signal and orders | new consistent immutable snapshot |
| Account mismatch over `max($5, 5 bp NAV)`, negative cash, leverage, short or unknown holding | cancel new orders and lock account | zero difference, no open orders, incident confirmation |
| Spread over 20 bp, order over 1% ADV, reject/duplicate, stale partial fill | stop affected orders | human quote/order review and new approved draft |
| Risk target outside 10%-35% | block | valid target after full pre-trade checks |
| Live drift above 40% | block new buys; permit reviewed risk-reducing sells | reviewed target and account reconciliation |

Every incident must retain trigger value, realized outcome, snapshots, operator,
timestamps and recovery authorization.

## 6. Paper/live gates

Do not enable live IBKR methods until all missing account and permission inputs
are supplied and tested in paper. Paper admission requires 12 months, 12
completed rebalances and at least 30 fills. The first live review is for a
$10,000 cash account, fractional orders only when explicitly supported, no
margin, leverage, shorting, or automatic order approval.
