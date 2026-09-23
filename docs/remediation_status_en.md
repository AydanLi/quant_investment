# Economic workflow remediation and acceptance

Language: [中文](remediation_status.md) | English

Baseline: `90fd792`, 2026-09-22. The baseline suite actually run for this change passed **283 tests**.
This document records software changes and acceptance boundaries. Passing tests does not admit data, a strategy, or a broker integration.

## Findings and verification

| Finding | Implementation | Acceptance evidence |
|---|---|---|
| S01 Raw prices and corporate actions | `data/features.py`, `execution/accounting.py`, `backtest/ledger.py`, execution repository | `test_remediation_accounting.py`: receivables, splits, entitlements surviving sales, future closes cannot change completed orders, continuous accounts |
| S02, S03 Data and calendar | `data/quality.py`, `data/features.py`, `backtest/engine.py` | `test_remediation_data.py`: invalid OHLCV, shared gaps, unknown price basis rejected |
| S04, S05, M03–M05 Budget, halts and reconciliation | `execution/budget.py`, `risk/controls.py`, `services/paper_cycle.py`, execution repository | `test_remediation_execution.py`: costs and buffer, fixed prior close, halt escalation, drift, unknown fractional positions, nonfinite marks |
| S06 Frozen identity | `research/runtime.py`, governance/signal/experiment repositories, paper CLI | `test_remediation_research.py`, `test_remediation_reporting.py`: economic configuration, code and dependencies; daily data can advance while research identity remains fixed |
| S07, S08, M06 Research validation | `research/core_evaluator.py`, `research/nested_walk_forward.py`, dynamic risk validation | Protocol, continuous account, first-day -20% drawdown and outer-data perturbation tests |
| M07–M09 Metrics, atomic persistence and UI | `report/reporter.py`, `storage/store.py`, official Dashboard | `test_remediation_reporting.py`, `test_dashboard_language.py`: coverage, full rollback, invalid result exclusion, display-only language switching |
| M10 Mirror | `scripts/optimize_mirrored_portfolio.py` | Offline full-OHLCV mirror calculation and read-only boundary tests |
| S09, M01, M02 Readiness evidence | `scripts/check_readiness.py`, `research/paper_admission.py` | Read-only prerequisites, persisted stage clocks, replay/broker evidence separation |
| O01–O04 Diagnostics and maintainability | Full workflow stress, research diagnostics and metric definitions | `test_economic_chain_stress.py` exercises actual features, strategy, orders, accounting and risk; ablation and timing remain diagnostics |

## Shared economic rules

- Total-return series are for signals. Execution requires explicit raw Open; valuation requires explicit raw Close. Neither silently substitutes for the other.
- Accounts start in cash; existing positions require explicit account state. NAV = raw-share market value + settled cash + net trade settlements + dividend receivables.
- Ex-date entitlement is recognized before opening trades. Unknown payment date or source leaves a non-spendable receivable; payment dates are not invented. Unknown cost basis remains unknown, and safe sales do not fabricate zero realized P&L.
- Slippage and impact enter fill price; commission is booked separately. Basket budgeting precedes any fill. Restart resumes only the remainder and never silently rescales approved quantities.
- A backtest sizing at known opening prices and `REPLAY_OPEN` using quantities approved before the open are different execution models. Identical fill inputs share accounting; gaps need not produce identical quantities across the two models.
- Daily loss uses the immediately previous session's official close. Missing prior close blocks added risk, while computable drawdown and safe risk reductions remain available. Halt reasons are stored independently.
- Passing research only records a result awaiting approval. Explicit human approval enables the frozen runtime; economic, source or dependency changes invalidate old approval. Account initialization starts validation.
- Unconfirmed dividend payments produce `PROVISIONAL_CASH_FLOWS` and cannot enter valid rankings. Reports and UI include known opening NAV; missing legacy values remain unknown.
- Continuous walk-forward results evaluate the selection procedure; they are not identical independent out-of-sample evidence for the final fixed winner. Formal dynamic-risk acceptance requires data untouched by core research; insufficient evidence retains sample covariance.

## Migration and recovery

Incremental revision: `6b2e1d9a4f30`. Take a consistent backup, rehearse on a separate copy, verify every legacy column, row count, integrity and foreign keys, then upgrade the working database.

```powershell
.venv\Scripts\python.exe -m scripts.backup_database
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m scripts.check_readiness
```

Old snapshots, hashes, adjudications and experiments are preserved. Missing payment dates, cost basis, prior closes and runtime identities are never back-signed. New event receipt timestamps are database-generated; legacy timestamps remain NULL. Downgrade is possible before new events or frozen identities exist. Once new evidence exists, downgrade refuses to delete it; roll forward or restore a consistent backup and replay.

## Verification commands

```powershell
.venv\Scripts\python.exe -m pytest -q --basetemp .runtime\pytest-remediation
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m scripts.check_environment
.venv\Scripts\python.exe -m scripts.check_readiness
```

Final measurements on 2026-09-22:

| Check | Result |
|---|---|
| Full regression and integration suite | **368 passed / 148.95 seconds**; baseline 283, increase of 85 tests |
| Environment | Python 3.14.3 and all 68 locked packages match; `pip check` finds no broken requirements |
| Compilation, diff and documentation | Compilation passes, no whitespace errors, local links valid in nine documents |
| Rehearsal and working database upgrade | Consistent-copy rehearsal completed; working database upgraded to `6b2e1d9a4f30` |
| Legacy preservation | All old-column contents across 28 tables and 1,729,411 rows retain matching per-table hashes; integrity passes, zero foreign-key errors and schema drift |
| Readiness | Correctly returns `ready_for_live=false`; software tests do not waive external prerequisites |

Consistent pre-upgrade backup: `.runtime/remediation-before-production-upgrade-20260922.db`. Test, migration and readiness evidence are in `.runtime/integration-final-stable.log`, `.runtime/remediation-production-migration.json` and `.runtime/remediation-readiness-final.json`.

Offline diagnostics used 3,915 synthetic input rows: three feature runs had a 0.095-second median; the baseline and six ablation scenarios took 3.83–4.91 seconds each, with 2.94–5.81 MB peak Python-traced memory. Results are in `.runtime/remediation-performance.json`. These local synthetic measurements are not production throughput commitments or strategy/out-of-sample evidence. Full workflow stress regressions exercise actual features, signals, orders, accounting, risk and SQLite concurrency.

Changes remain uncommitted. Review and commit shared contracts/migration, data quality, corporate-action accounting, execution/risk, frozen approval, research validation, metrics/UI and diagnostic readiness as focused groups.

## External gates

Read-only inspection still finds five `BLOCKED` snapshots; latest snapshot 5 has 706 unadjudicated blocking issues. `UV-001` is a draft, historical universe integrity is false, and strategy, admission and execution histories are empty. Requalify and rebuild snapshots under the new OHLCV and corporate-action rules; an old hash is not automatically certification under the new model.

Required evidence still includes original provider discrepancies, corporate-action/payment announcements, historical candidates/liquidations/mergers, new post-freeze samples, and target-broker paper orders/reconciliations. Prospective observation starts when its stage actually starts and checks both event and recorded timestamps. The 365-day, 12-rebalance and 30-fill gates remain. `REPLAY_OPEN` never establishes broker execution quality or live readiness.
