# Quant System v3 architecture overview

Updated: 2026-07-17

The active platform is a research and paper-execution system for a long-only,
cash-account ETF rotation strategy. It is **not live-admitted**. Legacy
`market_data` rows and all experiments without an immutable v3 dataset snapshot
remain available for audit, but cannot enter rankings or admission decisions.

## 1. System map

```text
Tiingo raw ETF bars/actions ----+
Yahoo raw bars/actions ---------+--> dual-source quality gate
CBOE VIX / Yahoo VIX -----------+          |
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
                 StrategyVersion (frozen)
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
                                human-approved paper OMS
                                       |
                                IBKR adapter boundary
```

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
- raw close differences above 5 bp warn and above 20 bp block;
- dividend/split conflicts block (provider display rounding is tolerated only
  within the documented decimal precision);
- ETF raw returns over 10% require a second source or corporate action;
- VIX is exempt from the ETF 10% rule but remains subject to CBOE/Yahoo close
  comparison.

The seed universe is the fixed 25-symbol list in `config/universe.py`. Risk ETFs
need 756 sessions, 60-session median dollar volume of at least $25 million,
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

Operational controls include:

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

`research/nested_walk_forward.py` uses 12-month outer tests after at least five
years of training. Parameter choice is repeated inside every outer training
sample using annual expanding folds. Evaluators receive sliced data only; no
full-sample portfolio or feature object is cut after calculation. Every success
and failure is persisted. Final freeze-date selection evaluates all 135
candidates and records neighbor and start-date robustness.

Historical gates cover median excess Sharpe, BIL outperformance, positive outer
windows, 20 bp costs, neighboring parameters, start dates, stop overshoot and
stop frequency. Replacing a repaired baseline additionally requires at least
0.05 excess-Sharpe improvement and a 10% drawdown improvement.

The risk model is a separate stage. Sample covariance is the default. Only the
six preregistered combinations `half-life {20,40,60} x stress {1.0,1.5}` may be
evaluated, and only after the core strategy is frozen. No old 20-day/1.5 model
is treated as admitted by default.

## 5. Signals and execution

`services/signal_service.py` emits one `SignalDecision` containing strategy,
universe and dataset versions; signal/data timestamps; next execution session;
target/current weights and dollar differences; estimated cost; quality issues;
and risk state.

Only a month-end decision generated after 20:30 ET with current trusted data and
approved/frozen versions can be `ACTIONABLE`. The following morning,
`execution/pretrade.py` rechecks account state, reconciliation, quotes, data and
risk. `execution/oms.py` does not draft the initial limit orders before 09:35 ET.

The OMS enforces:

- `DRAFT -> APPROVED -> SUBMITTED -> PARTIAL/FILLED/CANCELED/REJECTED`;
- deterministic client IDs and idempotent persistence;
- sell-first ordering and broker-reported cash limits;
- explicit human approval before every submission;
- a 20 bp initial limit, cancellation after five minutes, and a second human
  approval for repricing up to 40 bp;
- cancellation/review after a partial fill remains open ten minutes;
- fractional orders only when the adapter confirms support.

Research, paper, and live execution records are environment-scoped. The IBKR
adapter is intentionally connection-blocked until the user supplies account
entity/region, paper permissions, market-data/fractional entitlements,
commission plan, and TWS/Gateway settings. Constructing it cannot connect or
submit an order.

## 6. Persistence and reproducibility

Alembic revision `f7a2c9e4b301` adds trusted raw bars, actions, revisions,
immutable snapshot payloads, universe/strategy versions, admission runs and all
candidate trials, order intents/fills, reconciliation, and risk incidents. Old
tables are retained. Legacy experiment rows without a dataset snapshot are
marked `invalid_data_v1` and `admissible=0`.
Revision `c8e3f1047a92` adds average entry cost and gross/net realized P&L to
backtest orders so trade win rate and profit factor are calculated from actual
closed quantities instead of placeholders.

Any admissible result must identify:

- code commit;
- immutable dataset snapshot;
- universe version;
- strategy/protocol version;
- all attempted candidate outcomes.

## 7. Admission status

Engineering completion does not grant trading admission. A frozen version must
then complete at least 12 months, 12 rebalances, and 30 fills in paper trading,
with no unresolved authorization/reconciliation incidents, median
implementation shortfall no greater than 7 bp, 95th percentile no greater than
20 bp, and no 15% portfolio halt. Only then may a $10,000 IBKR cash account be
considered for individually approved live orders.

See `docs/upgrade_v3_runbook.md` for operation and recovery procedures.
