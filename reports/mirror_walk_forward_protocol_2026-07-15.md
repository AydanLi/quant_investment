# Mirror Portfolio Walk-Forward Protocol

Date: 2026-07-15  
Status: implemented; no position-change authorization

## Decision

The former mirror optimizer used one fixed train/test split and included test
Sharpe, drawdown, and turnover in parameter selection. That output is retired
and blocked by the Dashboard. It must not be used to change holdings.

The replacement protocol separates selection from final evaluation:

1. Build the eligible universe using only data available before the first
   validation window.
2. Run the current production settings as the baseline with the same dates,
   5 bps trading cost, and 2 bps slippage as every candidate.
3. Evaluate the 96 predeclared parameter variants in expanding-training,
   12-month validation folds that all end before the final holdout.
4. Select one candidate from aggregate pre-holdout validation results.
5. Freeze that candidate and evaluate it once in the untouched final holdout.
6. Report rolling-window win rate, costs, turnover, regime and crisis behavior,
   parameter-neighbor robustness, and alternative validation start dates.

The final holdout is never used to rank the 96 variants. Offline tests include
a candidate that dominates only in the holdout and verify that it cannot win
selection.

## Admission gates

The output records the existing model-admission thresholds for holdout Sharpe,
drawdown reduction, rolling-window effectiveness, after-cost return,
parameter robustness, and start-date robustness.

Two gates intentionally remain false:

- `independent_information`: the variants reuse the existing momentum signal;
- `historical_universe_integrity`: the historical universe originates from
  today's mirror snapshot, so survivorship conditioning remains.

Consequently, `admitted` and `position_changes_authorized` remain false even if
performance gates pass. The result is diagnostic research only.

## Actual evaluation result

An explicitly authorized Yahoo Finance run generated schema version 2 at
`2026-07-16T03:11:14Z` and evaluated all 96 variants. The selected diagnostic
configuration used monthly rebalancing, top 3 assets, a 3% momentum threshold,
10% target volatility, 25% maximum asset weight, and 30% risk-off cash.

The candidate did not pass admission:

- validation window win rate: 0 of 2;
- parameter-neighbor pass rate: 0%;
- alternative-start-date pass rate: 0%;
- final holdout Sharpe: 1.338 versus 1.264 baseline, an improvement of 0.074;
- final holdout maximum drawdown: -6.77% versus -8.29%, an 18.3% reduction;
- final holdout return: 19.10% versus 21.71%, a 2.61 percentage-point deficit;
- final holdout turnover: 7.60 versus 9.13 total turnover units;
- recorded candidate cost: 0.380% trading cost plus 0.152% slippage.

The candidate passed identical-cost, holdout-Sharpe, and drawdown gates. It
failed rolling windows, after-cost incremental return, parameter robustness,
start-date robustness, independent information, and historical-universe
integrity. The correct decision is to reject it as a position-changing model.

## Result integrity

Version 2 output records the methodology, generation timestamp, source-code
fingerprint, mirror snapshot id, baseline and selected parameters, identical
cost assumptions, fold audit, final holdout comparison, admission gates, and
latest diagnostic weights.

The Robinhood mirror Dashboard rejects:

- legacy single-split output;
- output that does not use the strict methodology;
- output generated from a different mirror snapshot.

## Data privacy and reproduction

The safe default uses only the local market-data cache:

```powershell
python -m scripts.optimize_mirrored_portfolio
```

The current cache is missing 28 of 34 required symbols, so default regeneration
stops without sending the private mirrored symbol list outside the workspace.

External Yahoo Finance download is available only through the explicit
`--allow-external-symbol-disclosure` flag. That flag sends the mirrored ticker
list to an external provider and should be used only after informed approval.
The actual evaluation above was generated after that approval was provided.

## Verification

- 10 focused offline tests cover holdout isolation, expanding folds, cost
  equality, independent-information and universe-integrity gates, parameter
  perturbations, pre-validation universe filtering, local-cache privacy, and
  legacy/stale result blocking.
- The current schema version 2 result matches the source fingerprint and mirror
  snapshot and renders with zero Streamlit application errors or exceptions.
- No brokerage snapshot or order state is modified by the protocol.
