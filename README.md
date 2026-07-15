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
- Reporting
- Latest allocation signal service
- Basic unit tests

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
running Dashboard instead of starting duplicate processes. Startup logs and
the process ID are written to the ignored `.runtime/` directory.

If the launcher reports that the project Python is missing, create `.venv` and
install `requirements.txt` before trying again.

The Dashboard charges trading costs and slippage separately. The production
risk model uses a 20-day EWMA half-life and stresses the dominant PCA factor by
1.5x; the former 60-day sample covariance remains available as the reproducible
research baseline.

### Manual commands

```bash
python main_with_db.py                                   # backtest + save a run to the database
streamlit run streamlit_dashboard_db_v1_1_save_experiment.py   # browse history / save experiments
python -m scripts.validate_dynamic_factor_model          # rerun all model-admission gates
```

The admission command reads the local market-data cache without downloading or
changing it. It compares identical dates and costs, reports annual walk-forward
windows, parameter and start-date sensitivity, market regimes, crisis periods,
turnover, costs, slippage, and signal independence.
