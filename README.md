# Quant System v2.1

A modular ETF rotation quant framework with:
- Market data loading
- Feature engineering
- Regime detection
- Momentum rotation strategy
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

```bash
python main_with_db.py                                   # backtest + save a run to the database
streamlit run streamlit_dashboard_db_v1_1_save_experiment.py   # browse history / save experiments
```