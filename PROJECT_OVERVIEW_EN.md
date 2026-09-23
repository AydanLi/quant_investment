# Quant System v3 Project Overview

Language: [中文](PROJECT_OVERVIEW.md) | English

Last updated: 2026-09-22

## Project Positioning

Quant System v3 is a single-user ETF asset-rotation research platform. It puts
data quality, backtesting, parameter admission, risk control, and simulated
execution into one auditable chain designed to answer four questions:

1. Which code, data, and ETF-universe versions support a strategy conclusion?
2. Within which training and validation boundaries were parameters selected,
   and was future data ever visible?
3. Do returns include realistic costs, cash, settlement, and risk constraints?
4. Does the system refuse to proceed when data, account, or execution evidence
   is incomplete?

The project is currently a personal research tool. It is not an automated
trading bot and has not been admitted for live trading.

## Strategy Outline

- Research universe: 24 risky ETFs plus the cash ETF `BIL`; internal cash is
  represented by `CASH_USD`.
- Asset coverage: US large-, mid-, and small-cap equities; international
  equities; government and credit bonds; gold, commodities, REITs, and sector
  ETFs.
- Core signal: 20/60/120-day momentum and low-volatility ranking, with weighted
  raw momentum required to be positive.
- Rebalancing: formal candidates are monthly. Signals are calculated after the
  close on T and executed at the raw open on T+1.
- Portfolio: select the top three, four, or five ETFs; target 8%, 10%, or 12%
  volatility; constrain each risky asset's target weight to 10%-35%. Unallocated
  capital goes to `BIL` or `CASH_USD`.
- Regime: VIX and long-term trend determine defensive, baseline, or slow-risk
  allocation.
- Baseline risk model: sample covariance. The dynamic-factor model requires a
  separate admission after the core strategy is frozen.
- Research costs: 2/7/20 bp scenarios, square-root impact above 0.1% ADV, and a
  hard block above 1% ADV.

These dimensions form a fixed grid of 135 core candidates. The system does not
permit unlimited searches for the “optimal parameters” after results are known.

## Problems the Platform Addresses

### 1. Trusted Data

The system stores raw provider OHLCV, dividends, splits, security metadata, and
revision history, then reconstructs total-return prices locally with a fixed
algorithm. Tiingo/Yahoo ETF differences and CBOE/FRED VIX differences pass
through a unified quality gate.

A data-quality decision may address only one exact issue fingerprint and must
bind the raw snapshot and hash. Source snapshots are never modified; a new
derived snapshot is created after adjudication. Any unresolved blocking issue
continues to prevent admission and orders.

### 2. Trustworthy Backtesting

The backtest uses a share-and-cash ledger and does not assume free daily
rebalancing. Signals and fills are separated by at least one market session.
The engine fails closed when a T+1 Open, an active holding's price, or the BIL
benchmark is missing. Trading costs, slippage, impact, operating cash, and T+1
settlement all enter the ledger.

### 3. Research Governance

The research protocol locks the code commit, data snapshot, universe version,
candidate parameters, cost scenarios, fold dates, benchmarks, and selection
rules. Admission uses at least five years of training history and 12-month
outer test windows. Results for all 135 candidates must be retained. A
`StrategyVersion` can be frozen only after its `AdmissionRun` passes every gate
and a human explicitly approves it. Passing research does not start observation.

### 4. Simulated Execution and Risk Control

Local `REPLAY_OPEN` stores immutable signals, manual approvals, orders, fills,
accounts, settlements, reconciliations, and incident state in SQLite. Stable
cycle, order, and execution IDs prevent duplicate fills after restart.

A 15% drawdown from the high-water mark creates a liquidation draft for the
next trading session. Re-entry must wait at least until the next monthly
rebalance and requires reconciliation plus manual authorization. A 5% daily
loss halts trading without automatic liquidation; manual recovery can occur no
earlier than the next trading session.

## Current Maturity

| Stage | Objective | Current state |
|---|---|---|
| Trusted data | A reproducible actionable snapshot | **Blocked**: all five snapshots are `BLOCKED` |
| Trustworthy backtest | T+1, cash, cost, and look-ahead tests pass | Engineering implementation and tests pass |
| Historical admission | Complete the fixed 135-candidate run | Not started |
| Prospective local replay | Post-freeze out-of-sample records | Clock not started |
| Broker paper | Real quotes, orders, fills, and implementation shortfall | Not integrated |
| Small live account | USD 10,000 cash account with per-order manual approval | Not admitted; date cannot be determined |

The remediation separates total-return signals from raw-price accounting and
adds dividend receivables, official closing baselines, and frozen runtime identity.
Continuous outer accounts validate the selection procedure, not an equivalent
independent holdout return for the final fixed strategy. See the
[remediation record](docs/remediation_status_en.md). External data, historical
universe, new samples, and broker evidence remain separate gates;
`CURRENT_UNIVERSE_BACKCAST` remains in effect.

Database evidence rechecked read-only on 2026-09-22:

- Five data snapshots, all `BLOCKED`.
- Two narrow XLF 2016 event decisions cover 2,640 derivative blocking issues.
- The latest snapshot still contains 706 unresolved issues: 588 close-price
  mismatches, 85 corporate-action value mismatches, nine missing
  corporate-action records, and 24 unconfirmed extreme returns.
- `UV-001` remains a `draft`, with
  `historical_universe_integrity=false`.
- Strategy versions, admission runs, parameter trials, signal decisions, paper
  accounts, orders, fills, reconciliations, and incidents are all zero.
- Eleven legacy experiments are marked `invalid_data_v1` and cannot enter
  comparisons or admission.

The most important current conclusion is therefore not “how well did the
strategy perform?” but “there is not yet a trustworthy historical conclusion
under the system's own standards.” This is not a reason to bypass the system;
it shows that the quality gate is working as designed.

## Intended and Unsupported Uses

Suitable uses:

- Researching the financial rationale and robustness of ETF rotation.
- Validating data, backtest, admission, and risk-control engineering.
- Accumulating single-user local replay records after strategy freeze.
- Preserving auditable boundaries for future broker paper and manually
  approved live trading.

Unsupported or not yet implemented:

- Automatically connecting to a broker and submitting real orders.
- Intraday real-time market-data or news-event trading.
- Demonstrating real limit-order execution quality or a real 7 bp cost.
- Full after-tax return, wash-sale, or product-tax-feature simulation.
- Claiming a completely survivorship-bias-free ETF universe since 2006.

## Admission Roadmap

```text
Resolve data blocks
  -> produce an actionable DatasetSnapshot
  -> manually approve the UniverseVersion
  -> freeze the research protocol and run all 135 candidates
  -> AdmissionRun: ADMITTED or REJECTED
  -> explicitly approve and freeze an ADMITTED StrategyVersion
  -> start the prospective local replay clock
  -> at least 12 months / 12 rebalances / 30 broker-paper fills
  -> then evaluate a USD 10,000 manually approved live account
```

Local raw-open replay does not count toward the broker-paper fill threshold. A
live-money date must be determined by future evidence and cannot be inferred
from the engineering completion date.

## Project Principles

- Evidence before equity curves.
- Data and governance fail closed by default.
- Previously inspected history is never presented as genuinely unseen.
- Every parameter trial, including failed results, is retained.
- Simulation, broker paper, and live trading are three different evidence
  levels.
- Manual approval is an operating constraint and is not bypassed for
  automation convenience.
- Current historical results are pre-tax and labeled
  `CURRENT_UNIVERSE_BACKCAST`.

## Further Reading

- Usage and validation: [README](README_EN.md)
- Technical design: [Architecture](quant_system_architecture_overview_en.md)
- Operations: [upgrade and operations runbook](docs/upgrade_v3_runbook.md)
- Confirmed constraints:
  [personal research operating profile](docs/personal_research_operating_profile.md)

This project is not investment, tax, or legal advice.
