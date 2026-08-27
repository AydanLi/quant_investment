# Quant System v3 runbook

## 1. Security and setup

- Never place Tiingo or broker credentials in source, `.env.example`, command
  arguments, screenshots, logs, or research JSON.
- Run `.\.venv\Scripts\python.exe -m alembic upgrade head` before building a
  trusted snapshot.
- Use `.\.venv\Scripts\python.exe -m scripts.validate_data_sources` for a
  non-persistent entitlement smoke test. It reads the token with a hidden prompt
  when the environment variable is absent.
- Use `.\.venv\Scripts\python.exe -m scripts.build_trusted_snapshot` only after
  the smoke test. A
  blocked snapshot is retained for audit but cannot generate an order.
- A provider HTTP 429 exits with `provider_rate_limit`; wait for the documented
  provider quota reset and rerun. Do not rotate credentials, silently switch to
  Yahoo, or treat an incomplete fetch as a trusted snapshot.
- The free Tiingo plan permits 50 requests per hour. The 25-symbol snapshot uses
  25 authenticated price-history requests; metadata comes from Tiingo's daily
  `supported_tickers.zip` bulk catalog and does not consume per-symbol API
  requests. The provider enforces a local 50-request preflight budget before it
  starts a batch.

## 2. Data incident workflow

1. Stop signal/order generation when the snapshot is blocked.
2. Record provider, ticker, date, raw values, retrieval time and issue code.
3. Check the primary authoritative source and an independent source. Normalize
   each vendor's documented split basis before comparing historical prices or
   per-share distributions; retain the original rows unchanged. Do not
   overwrite the original provider row.
4. Wait for a provider correction or record a narrow, immutable decision with
   `scripts.record_data_quality_decision`. A decision must bind the exact issue
   fingerprint, source snapshot, raw-data hash, ticker/date range, official
   evidence, reason, operator, and timestamp. Whole-ticker and whole-history
   exemptions are prohibited.
5. Run `scripts.materialize_adjudicated_snapshot` to create a derived immutable
   snapshot. The source snapshot is never changed. If raw values or hashes
   change, the old decision does not apply. Any unresolved blocking issue keeps
   the derived snapshot `BLOCKED`; only a fully reproducible result may become
   `TRUSTED_WITH_EXCEPTIONS`.

The 2026-07-17 smoke test found a real discrepancy: official CBOE VIX close was
17.76 for 2026-02-06 while Yahoo reported 20.37. From 2026-07-19 onward, VIX is
validated against FRED `VIXCLS`, whose documented underlying source is CBOE.
This gives a separate official publication path but not an independently
calculated index. Yahoo remains the ETF validation source and is no longer used
as the blocking VIX source. CBOE/FRED differences still use the same 5 bp
warning and 20 bp blocking thresholds; no discrepancy is silently suppressed.

## 3. Research sequence

1. Build an actionable dataset snapshot and approve its universe version.
2. Create the immutable 135-candidate protocol with
   `.\.venv\Scripts\python.exe -m scripts.create_research_protocol`.
3. Run the single core-admission entry point:

   `.\.venv\Scripts\python.exe -m scripts.run_core_admission`
   `--protocol <protocol.json> --strategy-version <version>`

   Treat the run as incomplete unless it reaches a terminal `AdmissionRun` and
   persists all 135 unique final candidate outcomes, including failures.
4. Freeze one strategy version only if all historical gates pass.
5. Evaluate the six risk-model candidates with:

   `.\.venv\Scripts\python.exe -m scripts.validate_dynamic_factor_model`
   `--snapshot-id <id> --strategy-version <version>`

   This command prints evaluation output; it does not by itself create the
   admitted database record required to expose `dynamic_factor` in the formal
   Dashboard.
6. Keep sample covariance unless a selected candidate is persisted as admitted
   for the frozen strategy version.
7. Start the local simulation clock only after the final strategy version is
   frozen. It does not count as broker-paper validation unless a future
   strategy version explicitly enters `BROKER_PAPER` mode.

Any change to the seed pool, filter rules, parameter grid, signal, cost formula,
or execution method requires a new strategy version and a new 12-month paper
period. A coefficient-only recalibration from the preregistered cost formula
does not restart the clock.

## 4. Local simulation operating flow

The local `REPLAY_OPEN` simulator is a persistent research replay, not broker
paper. It intentionally uses the trusted T+1 raw open plus preregistered costs;
it does not claim that a real 09:35 limit order would fill there.

- Non-month-end: diagnostic display only.
- Month-end from 20:30 ET through T+1 09:25 ET: require zero staleness, an
  actionable snapshot, approved compatible universe, admitted/frozen strategy,
  started local clock, and normal/reviewable account state. The catch-up window
  exists for a workstation that was off overnight.
- Before T+1 09:25 ET: persist the immutable decision and draft; a human must
  approve every order. At or after 09:25, an unapproved cycle is `MISSED` and
  must never be backdated.
- After the T+1 raw open is published: replay each approved order once, derive
  `PARTIAL/FILLED` only from persisted fills, update quantities and three cash
  components, and create T+1 settlement entries.
- Reconcile positions, settled/unsettled/available cash, NAV, fees, fills and
  unfinished orders. A restart reuses the same cycle and execution IDs and
  cannot duplicate a fill.
- Run the idempotent CLI with `python -m scripts.paper_cycle --help`; use Windows
  Task Scheduler for invocation. No always-on scheduler service is required.

A future broker-paper implementation will separately enforce arrival
bid/ask/mid capture, 09:35 limits, five-minute cancellation, second approval for
20-to-40 bp repricing, and ten-minute partial-fill review. Those controls are
not validated by local raw-open replay.

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

## 6. Simulation and future broker gates

The active configuration is `PERSONAL_RESEARCH`: external broker connectivity
and live submission are both disabled. The local coordinator persists linked
decision, cycle, order, fill, account, settlement, reconciliation and incident
records and has restart-idempotency tests. Its clock still cannot start until
the current database has an actionable snapshot, complete admitted run and
frozen strategy. An external paper adapter requires the separate
`BROKER_PAPER` mode and an explicit connectivity switch.
Future live use requires `MANUAL_LIVE` plus both connectivity and
live-submission switches; every order still needs individual human approval.

Do not enable any IBKR method until the missing account, permission, market
data, commission, fractional-order, and TWS/Gateway inputs are supplied and
tested in broker paper. Broker-paper admission requires 12 months, 12 completed
rebalances and at least 30 fills. The first live review is for a $10,000 cash
account, fractional orders only when explicitly supported, no margin, leverage,
shorting, or automatic order approval.

The local Pushover sender reads `PUSHOVER_APP_TOKEN` and
`PUSHOVER_USER_KEY` from the environment; secrets must remain outside version
control. The coordinator commits `RiskIncident` first, then attempts delivery.
A failed request leaves a persisted pending notification that
`retry-notifications` can resend without duplicating the incident. Dashboard
state, structured logs, and database records remain mandatory even when an
alert is delivered.

Research reports remain pre-tax and must retain
`historical_universe_integrity=false`. IBKR, real-time market data, and news
analysis remain deferred in this operating phase.
