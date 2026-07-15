# Factor regression and return attribution test

Run date: 2026-07-15
Cached data: 2018-01-02 through 2026-07-10
Analysis period: 2022-01-03 through 2026-07-10
Compared systems: sample-covariance baseline and admitted dynamic-factor system
Costs in both systems: 5 bps trading cost + 2 bps slippage

## Decision

**PASSED AS A DIAGNOSTIC MODEL.** The factor regression is useful for exposure
monitoring and return attribution, but it does not yet change portfolio weights.
It therefore has not been evaluated as a new trading strategy.

All diagnostic gates passed:

- identical baseline and candidate dates;
- 100% rolling analysis coverage during the test period;
- exact daily return reconciliation;
- no current-day or future data used to estimate current exposures;
- static adjusted R-squared above 59%;
- rolling one-day-ahead R-squared above 58%;
- standardized factor-matrix condition number of 3.13, indicating no serious
  multicollinearity in this proxy set.

## Factor definitions

The regression target is portfolio daily return minus BIL daily return.

| Factor | Definition |
|---|---|
| Equity market | SPY minus BIL |
| Growth tilt | QQQ minus SPY |
| Size tilt | IWM minus SPY |
| Duration | TLT minus BIL |
| Gold | GLD minus BIL |
| Energy tilt | XLE minus SPY |
| Defensive tilt | XLV minus SPY |

These are transparent ETF proxy factors built entirely from the existing cache.
They are not the canonical Fama-French academic factors.

## Static exposure comparison

| Exposure | Baseline | Dynamic factor | Change |
|---|---:|---:|---:|
| Equity market | 0.3336 | 0.2987 | -0.0349 |
| Growth tilt | 0.2434 | 0.1990 | -0.0444 |
| Size tilt | 0.0643 | 0.0480 | -0.0163 |
| Duration | -0.0243 | -0.0189 | +0.0054 |
| Gold | 0.2562 | 0.2316 | -0.0246 |
| Energy tilt | 0.1348 | 0.1218 | -0.0130 |
| Defensive tilt | 0.0394 | 0.0269 | -0.0125 |

The admitted risk model lowered all material risky-factor exposures. The
largest relative reductions were in defensive, size, and growth tilts. This is
consistent with its lower turnover and maximum drawdown; it is not evidence of
a new return-prediction signal.

## Static arithmetic return attribution

Annualized contributions are arithmetic means multiplied by 252. They add to
the annualized arithmetic portfolio return but are not geometric CAGR.

| Component | Baseline | Dynamic factor |
|---|---:|---:|
| BIL/cash baseline | 3.76% | 3.76% |
| Regression alpha | -2.61% | -1.58% |
| Equity market | 3.10% | 2.78% |
| Growth tilt | 0.79% | 0.65% |
| Size tilt | -0.21% | -0.16% |
| Duration | 0.28% | 0.22% |
| Gold | 4.01% | 3.62% |
| Energy tilt | 1.30% | 1.17% |
| Defensive tilt | -0.28% | -0.19% |
| Total annualized arithmetic return | 10.14% | 10.27% |

Static alpha was not statistically significant for either system: Newey-West
t-statistics were -0.74 for the baseline and -0.50 for the dynamic-factor
system. The result does not support claiming persistent factor-adjusted alpha.

## Risk attribution

The proxy factors explain approximately 60% of daily return variance. Around
40% remains residual, showing that changing weights, market-state decisions,
transaction costs, and omitted factors still matter.

The largest variance contributions were:

| Component | Baseline | Dynamic factor |
|---|---:|---:|
| Equity market | 29.82% | 29.57% |
| Gold | 22.37% | 22.68% |
| Growth tilt | 5.85% | 5.20% |
| Residual | 40.07% | 40.31% |

The portfolio is therefore not simply an equity-momentum strategy: gold is a
second major risk and return driver. The residual share is large enough that
this model should be treated as a diagnostic layer, not a complete explanation.

## Rolling out-of-sample attribution

Each day's exposures were estimated from the preceding 252 sessions, with a
minimum of 126 observations, and then applied to the next day.

| Metric | Baseline | Dynamic factor |
|---|---:|---:|
| Rolling OOS R-squared | 59.33% | 58.97% |
| Annualized actual arithmetic return | 10.14% | 10.27% |
| Annualized predicted component | 4.29% | 4.92% |
| Annualized rolling residual | 5.86% | 5.35% |
| Maximum reconciliation error | 0 | 0 |

The positive rolling residual is a forecast error, not automatically alpha.
It may contain allocation timing, omitted factors, costs, and model drift.

## Conclusion and next gate

Factor regression is now suitable for:

- monitoring rolling market, growth, size, duration, gold, energy, and
  defensive exposures;
- explaining whether future changes reduce genuine concentration risk;
- testing whether a proposed signal is independent after controlling for these
  factors;
- separating broad factor returns from unexplained strategy returns.

The next possible strategy experiment is a factor-exposure cap, especially on
combined equity-market and growth exposure. That overlay must be implemented as
a separate candidate and pass the full walk-forward production-admission gates
before it is allowed to change live weights.

## Dashboard monitoring integration

The model is integrated as an on-demand, read-only Dashboard layer. It reads a
stored run's daily portfolio returns and cached proxy-ETF prices, then displays:

- rolling out-of-sample explanatory power;
- latest exposure versus that run's historical 10th, 50th, and 90th
  percentiles;
- annualized arithmetic return contribution;
- variance contribution and residual risk;
- alpha significance and data-quality warnings.

The integration exposes `affects_weights=False` and does not call the strategy,
risk engine, broker, or target-weight code. Existing runs work without a schema
migration.

An integration smoke check on stored run 11 produced 1,795 rolling observations
and 46.82% out-of-sample R-squared. Duration exposure was below that run's own
historical 10th percentile, so the Dashboard correctly reported `watch` while
leaving all positions unchanged. This alert is an observation, not a trading
instruction.

## Reproduce

```powershell
python -m scripts.analyze_factor_attribution
```
