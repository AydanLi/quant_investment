# Quant System v3 architecture overview

Updated: 2026-08-13

The active platform is a personal research system with a persistent local
raw-open replay subsystem for a long-only, cash-account ETF rotation strategy.
It is **not broker-connected, broker-paper validated, or live-admitted**, and
the local simulation clock has not started because no strategy is admitted.
Legacy
`market_data` rows and all experiments without an immutable v3 dataset snapshot
remain available for audit, but cannot enter rankings or admission decisions.

## 1. System map

```text
Tiingo raw ETF bars/actions ----+
Yahoo raw bars/actions ---------+--> dual-source quality gate
CBOE VIX / FRED VIXCLS ---------+          |
NYSE calendar ------------------+          v
                                     immutable DatasetSnapshot
                                               |
                       +-----------------------+----------------------+
                       |                                              |
                       v                                              v
             point-in-time features                         UniverseVersion
                       |                                    quarterly approval
                       v                                              |
             135-candidate protocol <--------------------------------+
                       |
              nested expanding windows
                        |
              StrategyVersion (target: frozen)
                       |
        +--------------+----------------+
        |                               |
        v                               v
 T+1 quantity/cash backtest       SignalDecision
 costs + settlement + risk       DIAGNOSTIC/ACTIONABLE/
        |                         BLOCKED/HALTED
        v                               |
 AdmissionRun                           v
                                T+1 pre-trade verification
                                       |
                                 persistent local REPLAY_OPEN cycle
                                        |
                                 IBKR adapter boundary
```

The code contains research evaluators and a resumable core-admission command,
but their existence is not admission evidence: a successful run must persist
one complete `AdmissionRun`, all 135 final candidate outcomes, and frozen
strategy references. The local coordinator can create linked replay fills,
update an account, settle cash, reconcile, record incidents, and recover after
restart. Until an admitted strategy produces actual future records, that is
tested capability rather than operating evidence; it is never broker-paper
evidence.

The Robinhood mirror, factor monitor, and Monte Carlo monitor remain read-only
diagnostics. They cannot call the paper/live OMS.

## 2. Trusted data boundary

`data/providers.py` separates three provider capabilities: raw OHLCV, corporate
actions, and security metadata. The Tiingo credential is accepted only from
process memory/environment and is sent in the authorization header, never as a
URL parameter. Provider failures never silently switch sources.

`data/trusted_loader.py` performs a full-history refresh and stores:

- raw provider bars without vendor back-adjustment;
- explicit dividends and splits;
- provider metadata and every detected revision;
- a content-hashed, immutable copy of all bars/actions used by a result;
- the quality report and source provenance.

Local total-return OHLC is rebuilt by `data/adjustments.py`. This removes the
old incremental adjusted-price stitching problem. `data/calendar.py` makes the
NYSE calendar authoritative; VIX-only dates cannot enter ETF return or
execution calendars.

Executable quality rules are:

- 0 stale NYSE sessions for actionable data;
- 1 stale session is diagnostic only; 2 or more block;
- split-basis-normalized, distribution-unadjusted close differences above 5 bp
  warn and above 20 bp block; original vendor rows remain immutable;
- dividend/split conflicts block (provider display rounding is tolerated only
  within the documented decimal precision);
- ETF raw returns over 10% require a second source or corporate action;
- VIX is exempt from the ETF 10% rule but remains subject to a CBOE/FRED close
  comparison. FRED `VIXCLS` is an official redistribution sourced from CBOE,
  not an independently calculated index.

The seed universe is the fixed 25-symbol list in `config/universe.py`. Its
non-leveraged classification is explicitly recorded. Any newly proposed ticker
with no reviewed leveraged/inverse classification fails closed. Risk ETFs need
756 sessions, 60-session median dollar volume of at least $25 million,
price of at least $5, at least 98% completeness, and no leveraged/inverse flag.
Membership is calculated point in time and frozen by quarter. New proposals are
drafts until manually approved and never backfilled.

## 3. Strategy, backtest, and risk

The admitted execution sequence is always:

1. calculate a signal after the T close;
2. execute at the T+1 raw open plus modeled costs;
3. mark quantities at each close and let weights drift naturally.

`backtest/ledger.py` stores fractional quantities, settled cash, signed T+1
cash settlements, and an order-level audit. Missing prices for active holdings
block valuation. Cash, quantities, costs, and NAV reconcile exactly in tests.

Costs are charged per dollar of one-sided turnover. Research runs use 2/7/20 bp
scenarios. A square-root impact term starts at 0.1% ADV, orders above 1% ADV are
blocked, and risk-off execution has a minimum 20 bp pre-impact cost.

Risk assets have target weights from 10% through 35%. Positions below 10% after
volatility scaling leave the portfolio and residual capital goes to BIL. If BIL
is unavailable, residual goes to `CASH_USD`; other risk positions are never
re-expanded. BIL and `CASH_USD` are exempt from the 35% cap. The ledger retains
the greater of 0.5% NAV and $25 as operational cash.

The controls below are persisted by the local replay runtime and covered by
restart tests. They remain inactive until the governed local clock starts:

- 15% high-water drawdown: draft T+1 liquidation to `CASH_USD`, then require
  reconciliation, incident recording, the next monthly rebalance, and human
  authorization before re-entry;
- 5% daily loss: temporary halt without automatic liquidation;
- risk-weight drift above 35%: warning; above 40%: review state that blocks new
  buys while preserving risk-reducing sells;
- negative cash, leverage, short/unknown holdings, material account mismatch,
  stale data, wide spreads, and oversized orders: pre-trade block.

The report distinguishes the 15% trigger from realized post-trigger drawdown,
which can be worse after gaps and slippage.

## 4. Research governance

`research/protocol.py` defines exactly 135 core candidates:

- five momentum/low-volatility weighting sets;
- `top_n` of 3, 4, or 5;
- target volatility of 8%, 10%, or 12%;
- defensive, baseline, or slow regime parameters.

Monthly frequency, T+1 execution, the ETF seed pool, 10%-35% bounds, and cost
scenarios are not searchable. Daily/weekly Dashboard configurations are stored
as `exploratory_only`.

The research protocol requires 12-month outer tests after at least five years
of training, annual expanding inner folds, sliced inputs, and persistence of all
successes and failures. `scripts/run_core_admission.py` is the intended single
runner over those components. Its output is accepted only when the database
contains a terminal `AdmissionRun`, all 135 unique final candidate outcomes,
and the strategy freeze references. Dashboard runs are experiments, not
substitutes for that evidence.

Historical gates cover median excess Sharpe, BIL outperformance, positive outer
windows, 20 bp costs, neighboring parameters, start dates, stop overshoot and
stop frequency. Replacing a repaired baseline additionally requires at least
0.05 excess-Sharpe improvement and a 10% drawdown improvement.

The risk model is a separate stage. Sample covariance is the default. Only the
six preregistered combinations `half-life {20,40,60} x stress {1.0,1.5}` may be
evaluated, and only after the core strategy is frozen. No old 20-day/1.5 model
is treated as admitted by default. The formal Dashboard exposes a dynamic model
only when an admitted result is present for a corresponding frozen strategy
version; otherwise sample covariance is the only choice.

## 5. Signals and execution

`services/signal_service.py` emits a `SignalDecision` containing strategy,
universe and dataset versions; signal/data timestamps; next execution session;
target/current weights and dollar differences; estimated cost; quality issues;
and risk state. Executable decisions are persisted immutably with one paper
cycle identity for T+1 restart recovery.

Only a month-end decision generated from T 20:30 ET through T+1 09:25 ET with
current trusted data and approved/admitted/frozen references can be
`ACTIONABLE`. The following morning, `execution/pretrade.py` rechecks account,
data and risk. Missing the 09:25 human-approval deadline persists `MISSED`; the
local simulator never backdates an order.

The models and unit-level checks encode these target OMS rules:

- `DRAFT -> APPROVED -> SUBMITTED -> PARTIAL/FILLED/CANCELED/REJECTED`;
- deterministic client IDs and intended idempotent persistence;
- sell-first ordering and broker-reported cash limits;
- explicit human approval before every submission;
- a 20 bp initial limit, cancellation after five minutes, and a second human
  approval for repricing up to 40 bp;
- cancellation/review after a partial fill remains open ten minutes;
- fractional orders only when the adapter confirms support.

Research, paper, and live execution records are environment-scoped. The active
`PERSONAL_RESEARCH` configuration permits only broker-isolated local research.
`services/paper_cycle.py` coordinates SQLite-backed decision, order, replay
fill, account, T+1 settlement, reconciliation and risk state; stable cycle,
order and broker-execution identities make restarts idempotent. The fill is the
trusted T+1 raw open plus preregistered costs, not a simulated exchange match.
External broker connectivity and live submission remain separate disabled
switches. The IBKR adapter and real-time quote/news integrations are deferred;
constructing the adapter cannot connect or submit an order.

The local coordinator commits a `RiskIncident` before calling Pushover and
persists notification attempts. Delivery failures are retryable without
duplicating the incident. A received alert still does not prove that the
incident has been reconciled or resolved.

## 6. Persistence and reproducibility

Alembic revision `f7a2c9e4b301` adds trusted raw bars, actions, revisions,
immutable snapshot payloads, universe/strategy versions, admission runs and all
candidate trials, order intents/fills, reconciliation, and risk incidents. Old
tables are retained. Legacy experiment rows without a dataset snapshot are
marked `invalid_data_v1` and `admissible=0`.
Revision `c8e3f1047a92` adds average entry cost and gross/net realized P&L to
backtest orders so trade win rate and profit factor are calculated from actual
closed quantities instead of placeholders.
Revision `a14f0c9d7e62` removes an accidental `role` column from mutable raw
market data. Source roles remain attached only to immutable snapshot rows.
Revision `0984b8c06f2e` adds immutable quality decisions, governed lifecycle
constraints, executable signal decisions, local paper accounts/cycles/cash
movements, and the references required for restart-safe execution and recovery.
Revision `5f74c1a9d2b0` resets the known legacy loader-created universe approval
to `draft`, so it must pass the independent operator approval command before it
can enter a strategy version.

Any admissible result must identify:

- code commit;
- immutable dataset snapshot;
- universe version;
- strategy/protocol version;
- all attempted candidate outcomes.

Schema availability is not operational evidence. A paper run is demonstrated
only by consistent, linked records across order intents, execution fills,
account state, reconciliation, and incidents, plus an end-to-end restart test.

## 7. Admission status

Engineering completion does not grant trading admission. A frozen version must
then complete at least 12 months, 12 rebalances, and 30 fills in broker paper,
with no unresolved authorization/reconciliation incidents, median
implementation shortfall no greater than 7 bp, 95th percentile no greater than
20 bp, and no 15% portfolio halt. Only then may a $10,000 IBKR cash account be
considered for individually approved live orders. Local T+1 open-price replay
is research simulation and cannot satisfy the broker-paper fill requirement.

All current performance reporting is pre-tax. Historical ETF results remain
conditional current-universe backcasts and must retain
`historical_universe_integrity=false`.

See `docs/upgrade_v3_runbook.md` for operation and recovery procedures.
