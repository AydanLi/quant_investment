# Quant System architecture overview

Updated: 2026-07-15

This document describes the active architecture of the local ETF-rotation
research platform. Files under `DNU/` are legacy references and are not part of
the supported runtime path.

## 1. System map

```text
User / researcher
      |
      +--> main.py --------------------------> backtest + local report
      +--> main_with_db.py ------------------> backtest + persistence
      +--> Open Quant Dashboard.cmd ---------> primary Streamlit Dashboard
      +--> scripts/*.py ---------------------> research and import commands
                                                   |
                                                   v
  +------------------------ Application / service layer ---------------------+
  | signal_service | experiment_validation | factor_monitor | MC monitor     |
  +-------------------------------------------------------------------------+
               |                         |                       |
               v                         v                       v
  +------ Strategy / risk ------+  +---- Research ----+  +-- Read-only mirror --+
  | regime | momentum | risk    |  | admission        |  | normalized JSON      |
  | dynamic covariance         |  | factor attribution|  | brokerage snapshots  |
  | backtest | mock execution  |  | Monte Carlo      |  | no order methods      |
  +-----------------------------+  +------------------+  +----------------------+
               |                         |                       |
               +-------------------------+-----------------------+
                                         v
  +-------------------------- Persistence layer -----------------------------+
  | ResearchStore + SQLAlchemy repositories + Alembic schema                 |
  | experiments | portfolio | weights | orders | signals | market data        |
  | brokerage mirror snapshots and positions                                 |
  +-------------------------------------------------------------------------+
                                         |
                                         v
                               SQLite (current local DB)
```

## 2. Supported entry points

| Entry point | Purpose | State changes |
|---|---|---|
| `main.py` | Run a backtest and produce a local report | No experiment write; market cache may update |
| `main_with_db.py` | Run and persist a complete experiment | Writes research DB |
| `streamlit_dashboard_db_v1_1_save_experiment.py` | Primary interactive Dashboard | Reads runs; saves only after validation |
| `Open Quant Dashboard.cmd` | Windows launcher; restarts managed processes after source changes | Writes ignored `.runtime/` PID/log/source-state files |
| `robinhood_mirror_dashboard.py` | Read-only mirror and strict-result viewer | Reads mirror tables and ignored result JSON |
| `Open Robinhood Mirror.cmd` | Windows launcher for the mirror viewer | Writes ignored `.runtime/` PID/log files |
| `scripts/optimize_mirrored_portfolio.py` | Strict mirror walk-forward protocol | Reads cache by default; writes ignored CSV/JSON |
| `scripts/validate_dynamic_factor_model.py` | Reproduce model-admission gates | Read-only market cache |
| `scripts/analyze_factor_attribution.py` | Factor regression and attribution | Read-only market cache |
| `scripts/analyze_monte_carlo.py` | Paired Monte Carlo robustness analysis | Read-only market cache |
| `scripts/import_brokerage_snapshot.py` | Import a normalized brokerage snapshot | Writes mirror tables only |

`streamlit_dashboard_db.py` is a legacy lightweight database view. New product
work should target the primary Dashboard named above.

## 3. Core application flow

```text
Config
  -> MarketDataLoader / MarketDataRepository
  -> FeatureEngineer
  -> RegimeDetector
  -> MomentumRotationStrategy
  -> RiskEngine
       -> sample covariance (research baseline), or
       -> EWMA + stressed PCA covariance (production default)
  -> Backtester
       -> daily gross return
       -> turnover
       -> trading cost + slippage
       -> daily net return and equity
       -> MockBroker order log
  -> ReportGenerator / SignalService
  -> optional ResearchStore persistence
```

### Validation boundaries

- `Config.validate_risk_constraints()` rejects infeasible or invalid risk and
  cost settings.
- `RiskEngine.pre_trade_check()` validates final target weights before mock
  execution.
- Dashboard inputs are centrally validated before save.
- Backtest tests reconcile net daily returns with equity and implementation
  costs.
- Market-data loader rejects incomplete, stale, or internally gapped cache
  coverage rather than silently shortening a test.

## 4. Strategy and risk layers

### Feature engineering

`data/features.py` produces:

- 20/60/120-session momentum;
- 20-session annualized volatility;
- 50/200-session moving averages;
- distance/drawdown relative to the 200-session average.

Return calculation explicitly uses `pct_change(fill_method=None)`. Missing
prices therefore remain unknown instead of being silently forward-filled, and
calculation resumes once two adjacent prices are valid.

### Regime detection

`strategy/regime.py` assigns one of:

- `bull_trend`;
- `neutral`;
- `risk_off`;
- `bear_high_vol`.

### Portfolio construction

`strategy/momentum_rotation.py` ranks the configured universe and produces a
target portfolio. `risk/engine.py` then applies target-volatility scaling,
non-cash asset caps, BIL-aware feasibility, normalization, and pre-trade checks.

### Covariance models

- `risk_model="dynamic_factor"` is the admitted production default: a 20-day
  EWMA estimate with a 1.50x stress on the dominant PCA eigenvalue.
- `risk_model="sample"` preserves the former 60-day sample covariance as the
  reproducible baseline.

The admission result is documented in
`reports/dynamic_factor_model_admission_2026-07-15.md`.

## 5. Research and admission layer

`research/model_admission.py` performs the production gates:

- identical comparison dates and costs;
- out-of-sample Sharpe and maximum drawdown;
- annual walk-forward windows;
- costs, slippage, and turnover;
- parameter perturbation;
- different starting dates;
- bull, bear, sideways, risk-off, and crisis periods;
- correlation with the original momentum signal.

`research/factor_attribution.py` provides static and lagged rolling proxy-factor
regression with Newey-West alpha statistics and exact return reconciliation.

`research/monte_carlo.py` provides reproducible paired circular-block bootstrap
analysis. The same sampled rows are used for the baseline and candidate, and
stored net returns are not charged a second time.

Research reports live under `reports/`. Passing as a diagnostic does not permit
a model to change weights.

## 6. Dashboard and monitoring

The primary Dashboard has six tabs:

1. equity curve;
2. order log;
3. signal snapshot;
4. factor monitoring;
5. Monte Carlo monitoring;
6. raw stored data.

### Factor Monitor

`services/factor_monitor.py` reads a stored run plus cached proxy ETF prices and
shows rolling exposures, return/risk contribution, alpha statistics, historical
percentile warnings, and rolling out-of-sample explanatory power.

### Monte Carlo Monitor

`services/monte_carlo_monitor.py` generates 3,000 deterministic 252-session
paths for the selected stored run. It shows loss probability, tail drawdown,
return, Sharpe, turnover, recorded cost, equity bands, and 10/20/40-session
block sensitivity.

Both result objects set `affects_weights=False`. They do not call strategy,
risk, execution, or target-weight mutation code.

## 7. Persistence layer

### ResearchStore and repositories

`storage/store.py` is the application facade over:

| Repository | Tables / responsibility |
|---|---|
| `experiments.py` | Experiment metadata, config snapshot/hash, summary |
| `portfolio.py` | Daily portfolio state and long-form daily weights |
| `orders.py` | Mock order history |
| `signals.py` | Latest allocation snapshot per run |
| `market_data.py` | Shared cache-first OHLCV bars and coverage checks |

### Database tables

- `experiment_runs`;
- `portfolio_daily`;
- `portfolio_weights`;
- `orders`;
- `signals`;
- `market_data`;
- `brokerage_mirror_snapshots`;
- `brokerage_mirror_positions`.

Alembic is authoritative for schema changes. The current revision is
`b91e2f08c4a1`, which adds the two brokerage mirror tables on top of the initial
research schema.

### Brokerage mirror boundary

`storage/repositories/brokerage_mirror.py` stores immutable external position
snapshots separately from backtest state and mock orders. It intentionally has
no order-submission method and is not a live broker integration.

The import layer recursively rejects credential, token, and full-account fields.
The repository accepts only the last four account-reference characters or an
explicitly masked equivalent, stores only the normalized last four characters,
and rejects non-finite, negative, or internally inconsistent position values.

Mirror optimization ranks 96 predeclared variants only on expanding validation
folds before a final untouched holdout. It uses the production system as the
same-date, same-cost baseline and reports turnover, cost, parameter, start-date,
regime, and crisis diagnostics. Because it reuses momentum and conditions the
historical universe on today's snapshot, its independent-information and
historical-universe gates remain false and it cannot authorize position changes.
Legacy single-split and stale-snapshot results are blocked by the mirror viewer.

## 8. Test and runtime health

As of 2026-07-15:

- 98 pytest tests pass;
- all active Python modules compile;
- `pip check` reports no broken installed dependencies;
- Alembic has one head and the local database is current;
- Streamlit's application test executes the primary Dashboard with zero app
  exceptions and all six tabs present;
- database Dashboard parameter tables normalize mixed values to strings before
  Arrow serialization, and all active dataframes use `width="stretch"`;
- the Windows launcher fingerprints active Python sources, replaces stale
  managed processes, and reuses a healthy process when sources are unchanged.

Coverage includes risk caps, cost accounting, cache completeness, missing-price
return semantics, UTC market-data timestamps, dynamic covariance, admission
gates, mirror holdout isolation and privacy, factor attribution/monitoring,
Monte Carlo analysis/monitoring, experiment validation, metrics, and brokerage
snapshots.

Project code emits no compatibility deprecation warnings in the current test
suite. The remaining local pytest cache warning is an environment ACL issue.

## 9. Current system boundaries

The platform currently supports local personal research. It does not provide:

- live broker authentication or order routing;
- automatic position reconciliation;
- scheduled research or signal delivery;
- centralized operational telemetry and alert delivery;
- multi-user authorization;
- institutional market data or low-latency execution;
- permission for factor or Monte Carlo diagnostics to alter holdings.

## 10. Recommended evolution

### Immediate hardening

1. pin runtime and development dependencies;
2. persist gross return and split cost components for new experiment runs.

### Research evolution

1. continue accumulating unseen monitoring observations;
2. add a candidate only when it has a causal or independently testable signal;
3. repeat the complete admission process before any candidate changes weights;
4. keep diagnostic output separate from production target generation.

### Product evolution

1. move to PostgreSQL before multi-user/service deployment;
2. add authentication, audit trails, scheduler, and notifications;
3. add broker reconciliation before considering live execution;
4. treat order submission as a separately permissioned subsystem.
