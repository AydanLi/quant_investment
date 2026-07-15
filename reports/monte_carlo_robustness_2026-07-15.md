# Paired Monte Carlo robustness test

Run date: 2026-07-15
Cached data: 2018-01-02 through 2026-07-10
Out-of-sample source period: 2022-01-03 through 2026-07-10
Compared systems: sample-covariance baseline and admitted dynamic-factor system
Costs in both systems: 5 bps trading cost + 2 bps slippage

## Decision

**PASSED AS A ROBUSTNESS DIAGNOSTIC, NOT AS A TRADING MODEL.** The paired Monte
Carlo results strengthen the evidence that the current dynamic-factor risk
model's lower drawdown is not dependent on the single observed return order.
Monte Carlo produces no independent forecast or target weight, so it remains a
research validation tool with `affects_weights=False`.

The evidence is positive but not universal. The advantage weakened materially
when the source history started in 2025, and the 2020 COVID window did not show
an improvement. Those failures are retained as explicit monitoring limits.

## Method

The main test generated 5,000 paired circular block-bootstrap paths:

- each path contains 1,134 trading days, equal to the OOS source horizon;
- the default block length is 20 trading days to retain short-term return,
  volatility, and cost clustering;
- the baseline and candidate use the exact same sampled row indices;
- `daily_return` is already net of trading costs and slippage;
- turnover, trading cost, slippage, and total cost are sampled with each return
  for audit and are not deducted a second time;
- the seed is fixed at `20260715` for exact reproducibility.

This method tests return-order and finite-sample uncertainty. It does not prove
that the historical return distribution will remain valid after a structural
market change.

## Main paired result

| Metric | 5th percentile | Median | 95th percentile |
|---|---:|---:|---:|
| Sharpe improvement | +0.0401 | +0.1158 | +0.1952 |
| Maximum-drawdown reduction | +4.51% | +16.69% | +25.15% |
| Net total-return improvement | -7.63 pp | +1.64 pp | +8.15 pp |

Across the 5,000 paths:

- Sharpe improved in 99.52%;
- maximum drawdown improved in 99.22%;
- Sharpe and maximum drawdown improved together in 98.80%;
- net total return improved in 63.58%.

The return advantage is much less certain than the risk-adjusted and drawdown
advantage. The result supports the dynamic-factor model primarily as a risk
improvement, not as a reliable source of higher absolute return.

## Path distributions

| Metric | Baseline 5% / median / 95% | Dynamic factor 5% / median / 95% |
|---|---:|---:|
| Sharpe | 0.259 / 0.902 / 1.552 | 0.389 / 1.018 / 1.674 |
| Maximum drawdown | -22.91% / -13.51% / -8.76% | -18.86% / -11.36% / -7.87% |
| Net total return | 10.98% / 53.43% / 110.89% | 17.04% / 55.17% / 106.01% |
| Total turnover | 40.86 / 46.24 / 51.55 | 36.85 / 42.05 / 47.12 |
| Total modeled cost | 2.86% / 3.24% / 3.61% | 2.58% / 2.94% / 3.30% |

The probability of a negative 1,134-session total return was 1.70% for the
baseline and 0.72% for the dynamic-factor system. These are conditional
historical-bootstrap estimates, not forward guarantees.

## Cost and slippage audit

The source-period totals were:

| Component | Baseline | Dynamic factor |
|---|---:|---:|
| Turnover | 46.256 | 42.073 |
| Trading cost | 2.313% | 2.104% |
| Slippage | 0.925% | 0.841% |
| Total modeled cost | 3.238% | 2.945% |

All simulated total returns compound the stored net `daily_return`. Cost
columns are reported separately to demonstrate that positive results were not
obtained by omitting implementation friction.

## Block-length perturbation

| Block length | Median Sharpe increment | Median drawdown reduction | Joint-win probability |
|---:|---:|---:|---:|
| 5 days | +0.1169 | 14.50% | 98.28% |
| 10 days | +0.1164 | 15.75% | 97.96% |
| 20 days | +0.1158 | 16.69% | 98.80% |
| 40 days | +0.1156 | 18.03% | 99.52% |
| 60 days | +0.1174 | 18.74% | 99.64% |

All five perturbations retained positive median Sharpe and non-worse median
drawdown. The conclusion is not sensitive to a narrow block-length choice.

## Different starting dates

| Source start | Median Sharpe increment | Median drawdown reduction | Joint-win probability |
|---|---:|---:|---:|
| 2022 | +0.1158 | 16.69% | 98.80% |
| 2023 | +0.0587 | 11.38% | 89.24% |
| 2024 | +0.0535 | 10.85% | 80.68% |
| 2025 | +0.0253 | 3.58% | 58.36% |

All starts retained positive median improvements, but the effect decayed as the
history shortened. The 2025 result is below the 60% majority-of-paths reference
and must be treated as a live-monitoring warning rather than a clean win.

## Market states and crises

Market-state tests use paired conditional resampling with block length 1 so
non-contiguous regime observations are not falsely treated as adjacent blocks.

| State | Observations | Median Sharpe increment | Median drawdown reduction | Joint-win probability |
|---|---:|---:|---:|---:|
| Bull | 766 | +0.0694 | 11.76% | 86.20% |
| Bear | 102 | +0.1530 | 12.25% | 81.96% |
| Sideways | 143 | +0.2092 | 16.49% | 94.76% |
| Risk-off | 123 | +0.3213 | 13.78% | 97.92% |

| Crisis window | Observations | Median Sharpe increment | Median drawdown reduction | Joint-win probability |
|---|---:|---:|---:|---:|
| COVID 2020 | 51 | -0.0138 | 0.00% | 11.16% |
| Inflation bear 2022 | 196 | +0.3581 | 21.27% | 99.80% |

The COVID result is a genuine non-win: the candidate was approximately the same
on drawdown and slightly worse on Sharpe. Monte Carlo does not erase the known
historical weakness disclosed by the original model-admission test.

## Gate interpretation

The robustness evidence cleared the defined diagnostic gates:

- median OOS Sharpe improvement at least +0.05;
- median maximum-drawdown reduction at least 10%;
- at least 60% of paths improve both Sharpe and drawdown;
- more than 50% of paths improve net return after recorded costs and slippage;
- at least 67% of block-length perturbations remain positive;
- at least 67% of tested starting dates remain positive.

Signal independence is not applicable because Monte Carlo generates no signal.
It must not be promoted directly into a position-sizing rule. A future
simulation-derived overlay would be a different candidate and would need the
full walk-forward admission process before affecting target weights.

## Dashboard monitoring integration

Monte Carlo is integrated as a second read-only Dashboard diagnostics tab. For
the selected stored run it calculates, on demand and without a schema change:

- 3,000 reproducible 252-session net-return paths;
- one-year loss probability and 5% tail maximum drawdown;
- median return, drawdown, Sharpe, turnover, and recorded cost;
- 5%, median, and 95% equity-path bands;
- 10, 20, and 40-session block-length sensitivity.

The monitor uses each run's stored net `daily_return`, turnover, and combined
`est_cost`. Legacy runs do not contain enough metadata to identify a safe
same-cost risk-model baseline or split trading cost from slippage, so the
Dashboard does not invent either value. The formal paired comparison remains
the responsibility of `scripts.analyze_monte_carlo`.

An integration smoke check on stored run 11 produced a `watch` state: the
simulated one-year loss probability was 33.37%, the 5% tail maximum drawdown was
-21.21%, median total return was 4.90%, median Sharpe was 0.434, and median
recorded cost was 1.77%. The monitor reported both tail-risk warnings while
retaining `affects_weights=False`.

## Reproduce

```powershell
python -m scripts.analyze_monte_carlo
```
