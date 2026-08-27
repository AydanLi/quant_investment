# Personal research operating profile

Updated: 2026-08-13

## Confirmed scope

- Owner: United States tax resident.
- Tax jurisdiction: Boston, Massachusetts; full-year Massachusetts resident;
  federal filing status: single.
- 2026 planning estimate: ordinary taxable income and MAGI are each
  approximately USD 110,000 before strategy gains and distributions.
- Account: regular taxable individual brokerage account.
- Capital assumption: USD 10,000, cash only, no margin, leverage, shorting, or
  automatic broker orders.
- Current mode: `PERSONAL_RESEARCH`. Local simulation interfaces exist, but the
  end-to-end paper cycle is not operational and its clock has not started.
  External broker connectivity and live order submission are disabled
  independently.
- Future path: broker paper first, then separately authorized manual live mode;
  the adapter boundary and per-order human approval remain in place.
- Future tax results will report annual estimated liability separately and will
  not deduct simulated tax payments from portfolio cash. Current performance
  reports are pre-tax; the tax-lot and product-character model is not
  implemented or validated.
- IBKR connectivity, real-time market data, and news analysis are deferred.

## Data-source decisions

- ETF primary: Tiingo raw daily bars and corporate-action fields.
- ETF validation: Yahoo Finance. A Tiingo failure never silently promotes Yahoo
  to primary.
- VIX primary: official CBOE history.
- VIX validation: FRED `VIXCLS`. FRED identifies CBOE as the underlying source,
  so this is a separate official publication path, not independent index
  calculation. Yahoo VIX is diagnostic only after the observed historical
  discrepancy.
- Tiingo free-tier control: the provider preflights a 50-request hourly budget.
  The fixed 25-symbol batch consumes 25 authenticated history requests; daily
  security metadata comes from Tiingo's public `supported_tickers.zip` catalog.
- Quota exhaustion: wait for Tiingo's hourly reset. Rotating/resetting an API
  token is not treated as a quota-reset mechanism and is not automated.

Official references:

- Tiingo limits and reset behavior: https://www.tiingo.com/about/pricing and
  https://www.tiingo.com/documentation/general
- CBOE VIX history: https://www.cboe.com/tradable_products/vix/vix_historical_data
- FRED VIXCLS: https://fred.stlouisfed.org/series/VIXCLS

## External alert options

Selected channel for one-person operation: Pushover (`transport_verified`),
because it has a
small HTTPS API, mobile push delivery, priority controls, and an explicit
application quota. `ntfy` is the privacy/control alternative when self-hosting
is preferred. Telegram Bot API is a workable third choice but creates another
bot/chat security boundary.

The `Quant Research` sending application, account, user key, and one active iOS
device are verified. Its real API token and user key are stored only in the
ignored local `.env`; neither value may be committed to the repository. The
Pushover API accepted an end-to-end test message on 2026-07-28 and the
application usage counter increased to one; receipt on the registered iOS
device was confirmed by the owner.
The sender is not yet connected to persisted `RiskIncident` creation, retry, or
resolution. Operational alert status must therefore be determined from current
code call sites and database records, not from the successful transport test.
Alerts supplement rather than replace Dashboard state, structured logs, and
persisted `RiskIncident` records.

References: https://pushover.net/api, https://docs.ntfy.sh/publish/ and
https://core.telegram.org/bots/api

## Tax-model boundary

For a US taxable account, a correct after-tax simulation must track individual
tax lots, holding periods, realized gains/losses, wash-sale adjustments,
qualified-dividend holding periods, annual distributions, and product-specific
tax character. Short-term gains generally follow ordinary-income treatment;
long-term gains use the applicable capital-gain bracket. Net investment income
tax may add 3.8% above the statutory modified-AGI thresholds.

The current universe also prevents a single flat tax-rate shortcut:

- GLD's trust structure can make long-term gains subject to the collectibles
  maximum rate.
- DBC is a partnership product that may issue Schedule K-1/K-3 and allocate
  taxable items independently of cash distributions; relevant futures can have
  mark-to-market treatment.
- VNQ distributions can contain ordinary dividend income, capital gains, and
  return of capital, with final character known from annual fund tax reporting.

Confirmed modeling inputs are Massachusetts, single filing status,
approximately USD 110,000 of 2026 ordinary taxable income and MAGI before
strategy gains and distributions, and annual tax-liability reporting without
portfolio cash-tax drag. FIFO is the primary audit-neutral report; Specific ID
is an auxiliary scenario only when it follows a deterministic pre-trade lot
rule and retains broker confirmation. There is currently no capital-loss
carryforward or other account trading the same or similar ETFs. Exact after-tax
reporting remains blocked until the model's product-specific tax-character
inputs are validated.

Primary references: IRS Publication 550,
https://www.irs.gov/publications/p550; IRS Topic 559,
https://www.irs.gov/taxtopics/tc559; and the current GLD, DBC, and VNQ issuer tax
materials. This research model is not a substitute for tax-return preparation
or personalized tax advice.

## Historical ETF master

Tiingo's daily supported-ticker catalog supplies current operational metadata
and start/end dates, but it is not accepted as proof of a complete historical,
point-in-time ETF universe. The best verified fit for delisted, merged, renamed,
and inactive US ETFs is the CRSP Survivor-Bias-Free US Mutual Fund Database,
whose schema includes ETF/ETN identification, dead-fund flags, delisting reason,
acquiring fund, and historical header records.

CRSP is a separately licensed data product. The owner's consumer Morningstar
subscription does not establish entitlement to the `CRSP Survivor-Bias-Free US
Mutual Fund Database` or its historical inactive-ETF fields. Norgate's
three-week trial exposes only two years of price history, so it cannot validate
the governed 2006-present research period and will not be integrated solely for
that trial. `historical_universe_integrity=false` must remain in every report.
Tiingo may be used to build a forward-only security history from the first
governed universe version, but it cannot retroactively prove absence of
survivorship bias.

Reference: https://www.crsp.org/crsp_pdf/crsp-survivor-bias-free-us-mutual-fund-database-guide-crspsift/
