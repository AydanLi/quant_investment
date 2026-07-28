# Trusted snapshot validation

Date: 2026-07-19
Database revision: `a14f0c9d7e62`
Current immutable snapshot: `dataset_snapshot_id=3`

## Outcome

The governed data build ran successfully but the resulting snapshot is
`BLOCKED`, not actionable. The block is a data-quality result, not a provider
quota or freshness failure. No signal or order may use this snapshot.

Operational facts:

- requested history: 2006-01-01 onward;
- latest completed NYSE session: 2026-07-17;
- staleness: 0 NYSE sessions;
- Tiingo authenticated requests: 25 of the configured 50-per-hour budget;
- tickers returned: 25 ETFs plus VIX;
- primary rows: 128,457 Tiingo ETF rows and 5,198 CBOE VIX rows;
- validation rows: 128,457 Yahoo ETF rows and 5,197 FRED VIXCLS rows;
- mutable raw rows: 267,309;
- provider revisions detected across the repeated builds: 0;
- `historical_universe_integrity=false` remains in force.

The immutable content hash is
`c489720ea69296d899193f9c7317145b6ca1efb6205d36261f938881bfbadeb1`.

## Quality result

Snapshot 3 contains 510 warnings and 3,346 blocks:

| Code | Warning | Block |
|---|---:|---:|
| `CROSS_SOURCE_CLOSE_MISMATCH` | 392 | 3,226 |
| `CONFIRMED_EXTREME_RETURN` | 118 | 0 |
| `UNCONFIRMED_EXTREME_RETURN` | 0 | 24 |
| `CORPORATE_ACTION_VALUE_MISMATCH` | 0 | 85 |
| `CORPORATE_ACTION_MISMATCH` | 0 | 11 |

The largest cluster is XLF before the 2016 XLRE spin-off: Yahoo represents the
event as a 1.231 split-like adjustment while Tiingo records a cash distribution.
That convention difference accounts for 2,696 blocked close comparisons and a
large portion of the XLF action-value conflicts. It has not been auto-resolved.

Other blocks include isolated historical price discrepancies, distributions
whose per-share values differ by more than documented display rounding, 11
actions present in only one provider, and 24 extreme raw returns that lack an
adequate second-source or action confirmation under the registered rule.

Issuer/filing spot checks show why the remaining blocks must not be mass
waived:

- Invesco reports QQQ's 2023-09-18 distribution as $0.53555. Yahoo's $0.536 is
  consistent with display rounding, while Tiingo's $0.53885 is not.
- Vanguard reports VNQ's 2025-12-22 distribution as $0.604057 dividend plus
  $0.196443 return of capital, totaling $0.800500. This confirms the exact
  half-mill rounding boundary used by the corrected validator.
- An SEC-filed XLF notice describes the 2016 event as an in-kind distribution
  of 0.139146 XLRE shares per XLF share. A cash dividend or a synthetic split
  alone is therefore an incomplete representation.

References:

- https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=investors&productId=QQQ&ticker=QQQ
- https://investor.vanguard.com/investment-products/etfs/profile/vnq
- https://www.sec.gov/Archives/edgar/data/886982/000156459021029848/gs-424b2.htm

## False-positive correction retained in code

The first persisted quality pass compared vendor closes without accounting for
different split bases and produced 31,911 blocks. Tiingo preserves as-traded
historical prices, while Yahoo rewrites historical OHLC to a current-share
basis. The corrected validator now converts only the comparison series and
per-share distributions to a common split basis; immutable source rows are not
changed. It also treats exact half-mill Yahoo display rounding as representation
noise, without expanding the economic mismatch threshold.

The corrected final pass reduced blocks to 3,346. Snapshots 1 and 2 remain
immutable and blocked for audit; they cannot enter research admission.

## Required resolution before research admission

1. Obtain official issuer/corporate-action evidence for the 11 missing events
   and 85 value conflicts, starting with XLF/XLRE and the most recent events.
2. Adjudicate the 3,226 price blocks against a third licensed or official source;
   do not create ticker/date exceptions from the backtest result.
3. Confirm the 24 extreme returns using official prices or accepted company
   actions.
4. Rebuild a new immutable snapshot. Only a `TRUSTED` or warning-only result may
   enter feature generation and admission.

CRSP remains the preferred licensed point-in-time master for dead/merged ETF
coverage. It does not by itself replace daily official corporate-action
adjudication.
