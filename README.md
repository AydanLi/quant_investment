# Quant System v2.1

A modular ETF rotation quant framework with:
- Market data loading
- Feature engineering
- Regime detection
- Momentum rotation strategy
- Admitted EWMA + PCA dynamic-factor risk model
- Risk engine
- Backtesting engine
- Mock broker execution
- Read-only external brokerage position snapshots
- Reporting
- Latest allocation signal service
- Read-only factor diagnostics and exposure monitoring
- Read-only Monte Carlo tail-risk monitoring
- 88 automated unit and integration tests

Current architecture and system boundaries are documented in
[`quant_system_architecture_overview.md`](quant_system_architecture_overview.md).

## 1. Install

```bash
pip install -r requirements.txt
```

## 2. Database setup

The research database (`quant_research.db`) is **not** tracked in git — its
schema is managed by Alembic and it is a runtime artifact. Build it on a fresh
clone with:

```bash
alembic upgrade head            # create the schema (all tables) at the latest revision
```

This produces an empty database. To also import research runs from a legacy
`SQLiteStore` database, run the one-off migration:

```bash
python scripts/migrate_legacy_to_v2.py   # writes quant_research_v2.db, then copy it to quant_research.db
```

The database backend is configured by `Config.db_url` (default
`sqlite:///quant_research.db`). To move to Postgres/MySQL, change that URL and
install the matching driver — no code or schema changes are required.

## 3. Run

### Windows one-click Dashboard

After the virtual environment and dependencies are installed, double-click:

```text
Open Quant Dashboard.cmd
```

The launcher starts Streamlit in the background and opens
`http://localhost:8501` in the default browser. Repeated clicks reuse the
running Dashboard instead of starting duplicate processes. It fingerprints the
Dashboard and application Python sources; after a code change, the next click
automatically replaces the stale managed process before opening the page.
Startup logs, source state, and the process ID are written to the ignored
`.runtime/` directory.

If the launcher reports that the project Python is missing, create `.venv` and
install `requirements.txt` before trying again.

The Dashboard charges trading costs and slippage separately. The production
risk model uses a 20-day EWMA half-life and stresses the dominant PCA factor by
1.5x; the former 60-day sample covariance remains available as the reproducible
research baseline.

The **Factor Monitor** tab calculates lagged rolling exposures, return
attribution, risk contribution, and historical-percentile alerts on demand for
any stored run. It reads the existing portfolio and market-data cache, requires
no database migration, and is deliberately isolated from signals, the risk
engine, and target weights.

The **Monte Carlo Monitor** tab generates 3,000 reproducible one-year net-return
paths for the selected stored run. It shows loss probability, tail and median
drawdown, return and Sharpe distributions, turnover, recorded total cost, path
quantiles, and 10/20/40-day block sensitivity. It is also read-only and does not
change strategy, risk, execution, or target-weight state.

### Manual commands

```bash
python main_with_db.py                                   # backtest + save a run to the database
streamlit run streamlit_dashboard_db_v1_1_save_experiment.py   # browse history / save experiments
python -m scripts.validate_dynamic_factor_model          # rerun all model-admission gates
python -m scripts.analyze_factor_attribution             # proxy-factor regression and attribution
python -m scripts.analyze_monte_carlo                    # paired block-bootstrap robustness analysis
```

Robinhood Individual-account positions can be mirrored as immutable snapshots
in `brokerage_mirror_snapshots` and `brokerage_mirror_positions`. The mirror is
isolated from execution and is designed for pre-masked account references; do
not include login credentials, tokens, or full account identifiers in the input
file. The import command rejects sensitive fields at any nesting level, while
the repository stores only the normalized last four account-reference
characters and requires finite, non-negative position values. Use
`python scripts/import_brokerage_snapshot.py snapshot.json` for normalized JSON
exports; applying `alembic upgrade head` creates the required tables.

The admission command reads the local market-data cache without downloading or
changing it. It compares identical dates and costs, reports annual walk-forward
windows, parameter and start-date sensitivity, market regimes, crisis periods,
turnover, costs, slippage, and signal independence.

The factor-attribution command uses lagged 252-session regressions with no
network dependency. It reports static exposures, Newey-West alpha statistics,
one-day-ahead rolling attribution, and exact return reconciliation for both the
sample-covariance baseline and the admitted dynamic-factor system.

The Monte Carlo command resamples identical net-return blocks for the baseline
and current system. It reports paired Sharpe, drawdown, return, turnover, cost,
block-length, start-date, regime, and crisis distributions. It is a robustness
diagnostic only and does not generate signals or change target weights.

## 4. Validate

`pytest` is currently a development dependency and is not yet included in the
unpinned runtime `requirements.txt` file. In a fresh environment, install it
explicitly before running the suite:

```bash
python -m pip install pytest
python -m pytest -q
python -m compileall -q backtest config data execution report research risk services storage strategy tests utils
python -m pip check
alembic current
```

The expected result for the 2026-07-15 project snapshot is 88 passing tests and
Alembic revision `b91e2f08c4a1 (head)`. Project code emits no compatibility
deprecation warnings in the current suite. A local pytest cache ACL warning may
still appear on this Windows checkout and does not come from application code.
