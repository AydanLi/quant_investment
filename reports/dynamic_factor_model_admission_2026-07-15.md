# Dynamic-factor risk model admission

Run date: 2026-07-15  
Cached data: 2018-01-02 through 2026-07-10  
Rolling out-of-sample comparison: 2022-01-03 through 2026-07-10  
Costs applied to both systems: 5 bps trading cost + 2 bps slippage per unit of turnover

## Decision

**ADMITTED**: the production risk model is now `dynamic_factor`, combining:

- EWMA covariance with a 20-trading-day half-life;
- PCA concentration detection;
- a 1.50x stress multiplier on the largest covariance eigenvalue.

The former 60-day sample covariance remains available as `risk_model="sample"`
only for reproducible baseline comparisons.

## Same-period, same-cost result

| Metric | Existing system | Dynamic factor | Increment |
|---|---:|---:|---:|
| Net Sharpe | 0.8946 | 1.0106 | +0.1161 |
| Maximum drawdown | -10.39% | -8.47% | 18.47% reduction |
| Net total return | 53.22% | 54.98% | +1.75 percentage points |
| Total turnover | 46.23 | 42.06 | -9.03% |
| Total modeled cost | 3.236% | 2.944% | -0.292 percentage points |
| Trading-cost component | 2.311% | 2.103% | lower |
| Slippage component | 0.925% | 0.841% | lower |

Cost figures are summed return fractions charged on rebalance dates, not dollar
fees. Returns and Sharpe are net of both cost components.

## Admission gates

| Gate | Required | Result | Status |
|---|---:|---:|---|
| OOS Sharpe improvement | at least +0.05 | +0.1161 | PASS |
| Maximum-drawdown reduction | at least 10% | 18.47% | PASS |
| Effective rolling windows | at least 60% | 5/5, 100% | PASS |
| Effective after costs and slippage | net return above baseline | +1.75 pp | PASS |
| Parameter perturbation | at least 67% pass | 9/9, 100% | PASS |
| Start-date robustness | at least 67% pass | 4/4, 100% | PASS |
| Independent information | absolute correlation at most 0.80 | 0.0425 | PASS |

Each yearly test window was preceded by a three-year training/observation
window. The fixed model uses only returns available on or before each rebalance
date; it does not use future returns to estimate covariance.

## Rolling windows

| Test window | Sharpe increment | Drawdown reduction | Result |
|---|---:|---:|---|
| 2022 | +0.3313 | 22.27% | PASS |
| 2023 | +0.0781 | 11.95% | PASS |
| 2024 | +0.0998 | 13.16% | PASS |
| 2025 | +0.0222 | 4.48% | PASS |
| 2026 through July 10 | +0.0272 | 7.27% | PASS |

The window gate requires both a positive Sharpe increment and a non-worse
maximum drawdown. It does not allow a few unusually strong years to hide losing
windows.

## Robustness checks

All nine combinations of EWMA half-life `{16, 20, 24}` and PCA stress
`{1.35, 1.50, 1.65}` improved both aggregate OOS Sharpe and maximum drawdown.
All four tested starting dates (2022, 2023, 2024, and 2025) also improved both
risk-adjusted measures.

The monthly correlation between the original momentum score and the new PCA
first-factor concentration signal was 0.0425 across 55 observations. The risk
model therefore contributes information that is not a disguised copy of the
existing momentum ranking.

## Market regimes and crises

The candidate improved Sharpe and maximum drawdown in bull, bear, sideways, and
rule-based risk-off observations. The 2022 inflation bear window improved from
-0.1763 to +0.1863 Sharpe and reduced drawdown from -10.39% to -8.08%.

The 2020 COVID window was essentially unchanged on drawdown (-5.54% for both)
and was 0.03 percentage points worse in total return. This is disclosed rather
than treated as a win; the aggregate and every later rolling window still clear
the stated admission gates.

## Rejected alternatives

The following candidates were evaluated but are not retained in production:

- standalone EWMA covariance: improvement was below the Sharpe and drawdown gates;
- standalone PCA stress: reduced drawdown but missed the minimum Sharpe increment;
- inverse-volatility reweighting: reduced OOS Sharpe and worsened drawdown;
- probabilistic regime overlay: did not add stable OOS value;
- PCA residual-momentum signal: results depended too heavily on specific years.

## Reproduce

```powershell
python -m scripts.validate_dynamic_factor_model
```

This is a historical walk-forward result, not proof of future returns. The model
should continue to be monitored on genuinely unseen live data; any future model
must pass the same gates before becoming a production default.
