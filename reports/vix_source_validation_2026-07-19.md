# VIX source validation

Date: 2026-07-19

## Decision

Use CBOE historical VIX as the primary series and FRED `VIXCLS` as the blocking
publication-path check. Retain Yahoo VIX only as a non-blocking diagnostic.

## Evidence

A read-only comparison from 2006-01-01 through the latest common observation
found:

- 5,197 overlapping dated closes;
- maximum absolute close difference: 0.0 bp;
- observations above the 5 bp warning threshold: 0;
- observations above the 20 bp blocking threshold: 0.

FRED documents CBOE as the source of `VIXCLS`. Agreement therefore verifies
transport/publication consistency, not independent recomputation of the VIX
methodology. This limitation is preserved in snapshot provenance and reports.

## Prior incident

The previous CBOE/Yahoo comparison contained a material mismatch on 2026-02-06
(CBOE 17.76 versus Yahoo 20.37). The quality gate correctly blocked that pair;
the discrepancy was not suppressed or tolerance-expanded.

## References

- CBOE VIX historical data:
  https://www.cboe.com/tradable_products/vix/vix_historical_data
- FRED VIXCLS: https://fred.stlouisfed.org/series/VIXCLS
