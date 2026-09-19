# Quant System v3

Language: [中文](README.md) | English

An ETF momentum-rotation platform for personal research. The project's priority
is not to produce an attractive historical equity curve, but to make every
research conclusion traceable to a code commit, immutable data snapshot, ETF
universe version, preregistered parameters, cost assumptions, and execution
records. If any critical evidence is missing, the system fails closed.

> Current status (2026-08-26): the engineering workflow is implemented and all
> 270 tests pass, but all five data snapshots remain `BLOCKED`, with 706
> unresolved blocking issues in the latest snapshot. `UV-001` remains a
> `draft`, and there are no strategy versions, admission runs, or local replay
> fills. The project can currently support diagnostics and engineering
> verification, but it cannot claim that a strategy has been admitted, broker
> paper trading has been validated, or live trading is ready.

## Documentation

| Document | Purpose |
|---|---|
| [Project overview](PROJECT_OVERVIEW_EN.md) | Positioning, strategy outline, maturity, and current conclusion |
| [Architecture](quant_system_architecture_overview_en.md) | Data flow, governance state machines, storage model, and execution boundaries |
| [Operations runbook](docs/upgrade_v3_runbook.md) | Data incidents, admission, local replay, halts, and recovery |
| [Personal research operating profile](docs/personal_research_operating_profile.md) | Account, tax, data-source, alerting, and historical-universe constraints |
| [Historical audit dated 2026-07-28](reports/quant_system_audit_2026-07-28.md) | Historical findings and corrections; not the current admission status |

## Core Capabilities

- Tiingo raw ETF daily bars and corporate actions as the primary source, with
  Yahoo as an ETF cross-check; VIX uses the CBOE/FRED path.
- Traceable, immutable snapshots of raw data, corporate actions, provider
  revisions, and the exact data used by each result.
- Data-quality decisions bound to a specific snapshot, issue fingerprint, raw
  data hash, ticker, date, evidence, and operator. Whole-ticker or whole-history
  wildcard approval is prohibited.
- Monthly ETF momentum rotation, market regimes, volatility scaling, and a
  sample-covariance risk model.
- A share-and-cash ledger with T+1 raw-open execution, natural weight drift,
  T+1 settlement, and cost/impact modeling.
- Nested expanding-window admission over a fixed set of 135 candidates, with
  every candidate and failed result persisted.
- SQLite-backed local `REPLAY_OPEN` for signals, manual approval, idempotent
  fills, settlement, reconciliation, risk incidents, and Pushover retry.
- Read-only factor attribution, Monte Carlo analysis, a Robinhood mirror, and
  the formal Streamlit Dashboard.

## Capability Boundaries

| Capability | Current state | What it does not prove |
|---|---|---|
| Dual-source data and adjudication | Implemented; current snapshots remain `BLOCKED` | Data is admissible for research |
| Backtest and 135-candidate admission engine | Implemented and resumable | A trustworthy strategy conclusion exists |
| Local `REPLAY_OPEN` | Implemented and restart-idempotency tested | Real bid/ask execution, limit fills, or a real 7 bp cost |
| Pushover | Incidents are committed before sending; failed sends can retry | An incident has been reconciled or recovered |
| IBKR adapter boundary | Disconnected by default and protected by two switches | Broker paper or live trading is available |
| Tax and historical ETF universe | Constraints are documented only | After-tax performance or freedom from survivorship bias |
| Real-time news and market data | Not implemented | Intraday event-driven trading capability |

All historical ETF results must be labeled `CURRENT_UNIVERSE_BACKCAST`, with
`historical_universe_integrity=false`. Current performance reporting is pre-tax.

## Installation

The validated environment is Windows with CPython 3.14.3; see
`.python-version`.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.lock
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints.lock
.\.venv\Scripts\python.exe -m scripts.check_environment
```

`requirements.txt` pins nine direct runtime dependencies, while
`constraints.lock` pins the 68 runtime and test dependencies in the validated
environment. Any dependency upgrade must update both the direct requirements
and the full constraint set, followed by the complete validation suite.

## Database and Credentials

The default runtime database is `sqlite:///quant_research.db`. It is managed by
Alembic and excluded from Git. SQLite is currently the only backend for which
migrations, integrity checks, foreign keys, and restart recovery have all been
validated. Other database URLs are unvalidated extensions and must not be used
in production merely by changing configuration.

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

Real credentials must exist only in environment variables, an ignored local
`.env`, or operating-system secret storage. The repository's `.env.example`
must contain empty values only. Before enabling unattended jobs, rotate any
Tiingo or Pushover credential previously exposed in chat, screenshots, or
command arguments.

Validate data-source access and build a complete snapshot with:

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_data_sources
.\.venv\Scripts\python.exe -m scripts.build_trusted_snapshot
```

An HTTP 429 or provider failure does not silently switch the primary source.
A build may legitimately return `BLOCKED`: this means the evidence was saved,
not that the result may be ignored. See the
[operations runbook](docs/upgrade_v3_runbook.md) for adjudication and derived
snapshot procedures.

## Entry Points

### Formal Dashboard

Double-click:

```text
Open Quant Dashboard.cmd
```

The formal entry point is
`streamlit_dashboard_db_v1_1_save_experiment.py`. Its defaults are sample
covariance and a 35% cap on risky assets. `dynamic_factor` appears only when
the database contains a formal risk-model admission record bound to a frozen
strategy. Daily and weekly experiments are always marked `exploratory_only`.

Use **Language / 语言** at the top of the sidebar to switch between **中文**
(the default) and **English**. Labels, messages, diagnostic tables, and charts
follow the selection while entered parameters and the selected experiment stay
unchanged. The URL retains `?lang=zh` or `?lang=en` for refreshes and shared links.
The Raw data tab keeps database field names; user input, stored values, and
underlying exception details remain unchanged.

`streamlit_dashboard_db.py`, `main.py`, `main_with_db.py`, and `DNU/` exist only
for historical compatibility or audit and are not formal admission entry
points.

### Read-only Robinhood Mirror

Double-click:

```text
Open Robinhood Mirror.cmd
```

The mirror displays imported position snapshots and diagnostic walk-forward
results only. It does not connect to the order path or authorize position
changes.

## Research Admission

Run this workflow only after an actionable snapshot, a manually approved
universe version, and a pre-freeze research protocol exist:

```powershell
.\.venv\Scripts\python.exe -m scripts.approve_universe `
  --version <universe-version> --approved-by <operator>

$commit = git rev-parse HEAD
.\.venv\Scripts\python.exe -m scripts.create_research_protocol `
  --version <protocol-version> `
  --code-commit $commit `
  --dataset-snapshot-id <snapshot-id> `
  --universe-version <universe-version> `
  --output .runtime\core_protocol.json

.\.venv\Scripts\python.exe -m scripts.run_core_admission `
  --protocol .runtime\core_protocol.json `
  --strategy-version <strategy-version>
```

The admission command must persist exactly 135 final candidate results. It is
a valid research conclusion for every candidate to be rejected; the parameter
grid must not be expanded after reviewing results. The current database has no
actionable snapshot, so formal admission should not be run yet.

## Local Replay

Local replay is available only after a strategy has been admitted, frozen, and
its prospective clock has started:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_cycle `
  --strategy-version <strategy-version> --help
```

It deterministically replays T+1 raw-open prices plus preregistered costs and
persists decisions, orders, fills, three cash balances, settlements,
reconciliations, and risk incidents. It is not broker paper trading and cannot
validate 09:35 limit orders, order-book liquidity, real market impact, or
fractional-share routing.

## Validation

```powershell
.\.venv\Scripts\python.exe -m scripts.check_environment
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q backtest config data execution report research risk scripts services storage strategy tests utils
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check
```

Validation recorded on 2026-08-26: 270 tests passed, dependencies were
consistent, SQLite returned `integrity_check=ok` with no foreign-key
violations, and Alembic was at `5f74c1a9d2b0 (head)`.

## Repository Layout

| Path | Responsibility |
|---|---|
| `config/` | Strategy, risk, execution-mode, and ETF-universe defaults |
| `data/` | Providers, trading calendar, adjustments, quality checks, and trusted loading |
| `strategy/` | Momentum rotation and market regimes |
| `risk/` | Covariance, position constraints, risk controls, and exposures |
| `backtest/` | T+1 backtest engine and share/cash ledger |
| `research/` | Protocol, nested walk-forward, admission, and diagnostics |
| `services/` | Signals, Dashboard views, local replay, and Pushover |
| `execution/` | Pre-trade checks, OMS, and broker isolation boundary |
| `storage/` | SQLAlchemy schema and repositories |
| `scripts/` | The auditable operations and research CLI surface |
| `tests/` | Unit, integration, migration, look-ahead, governance, and restart tests |

This project is for research and software-engineering validation only. It is
not investment, tax, or legal advice.
