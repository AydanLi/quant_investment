from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Mapping, Sequence

import pandas as pd


US_EQUITY_ETFS = ("SPY", "QQQ", "IWM", "MDY")
INTERNATIONAL_ETFS = ("EFA", "EEM")
BOND_ETFS = ("SHY", "IEF", "TLT", "TIP", "LQD", "HYG")
REAL_ASSET_ETFS = ("GLD", "DBC", "VNQ")
SECTOR_ETFS = ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY")
RISK_ETFS = (
    US_EQUITY_ETFS
    + INTERNATIONAL_ETFS
    + BOND_ETFS
    + REAL_ASSET_ETFS
    + SECTOR_ETFS
)
CASH_ETF = "BIL"
SYNTHETIC_CASH = "CASH_USD"
INITIAL_ETF_UNIVERSE = RISK_ETFS + (CASH_ETF,)


@dataclass(frozen=True)
class EligibilityRules:
    minimum_history_sessions: int = 756
    liquidity_window_sessions: int = 60
    minimum_median_dollar_volume: float = 25_000_000.0
    minimum_price: float = 5.0
    minimum_data_completeness: float = 0.98
    leveraged_and_inverse_allowed: bool = False


@dataclass(frozen=True)
class UniverseEligibility:
    ticker: str
    as_of: str
    eligible: bool
    reasons: tuple[str, ...]
    history_sessions: int
    median_dollar_volume: float | None
    latest_price: float | None
    data_completeness: float


@dataclass(frozen=True)
class UniverseVersion:
    version: str
    effective_date: str
    seed_tickers: tuple[str, ...]
    rules: EligibilityRules
    approved: bool = False
    approved_by: str | None = None
    historical_universe_integrity: bool = False

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["seed_tickers"] = list(self.seed_tickers)
        return payload


class UniversePolicy:
    """Point-in-time ETF eligibility with no retroactive membership changes."""

    def __init__(
        self,
        seed_tickers: Sequence[str] = INITIAL_ETF_UNIVERSE,
        rules: EligibilityRules = EligibilityRules(),
    ) -> None:
        self.seed_tickers = tuple(dict.fromkeys(seed_tickers))
        self.rules = rules

    def assess(
        self,
        ticker: str,
        frame: pd.DataFrame | None,
        *,
        as_of: pd.Timestamp,
        sessions: pd.DatetimeIndex,
        metadata: Mapping[str, object] | None = None,
    ) -> UniverseEligibility:
        as_of = pd.Timestamp(as_of).normalize()
        reasons: list[str] = []
        if ticker == CASH_ETF:
            return UniverseEligibility(
                ticker=ticker,
                as_of=str(as_of.date()),
                eligible=frame is not None and not frame.loc[:as_of].empty,
                reasons=() if frame is not None and not frame.loc[:as_of].empty else ("no_data",),
                history_sessions=0 if frame is None else len(frame.loc[:as_of]),
                median_dollar_volume=None,
                latest_price=None,
                data_completeness=1.0,
            )

        if frame is None or frame.empty:
            reasons.append("no_data")
            history = pd.DataFrame()
        else:
            history = frame.loc[:as_of].copy()

        required_columns = {"Close", "Volume"}
        if not required_columns.issubset(history.columns):
            reasons.append("missing_close_or_volume")

        valid = history.dropna(subset=[c for c in required_columns if c in history])
        history_sessions = len(valid)
        if history_sessions < self.rules.minimum_history_sessions:
            reasons.append("insufficient_history")

        latest_price: float | None = None
        median_dollar_volume: float | None = None
        if not valid.empty and required_columns.issubset(valid.columns):
            latest_price = float(valid["Close"].iloc[-1])
            if latest_price < self.rules.minimum_price:
                reasons.append("price_below_minimum")
            liquidity = (valid["Close"] * valid["Volume"]).tail(
                self.rules.liquidity_window_sessions
            )
            if len(liquidity) < self.rules.liquidity_window_sessions:
                reasons.append("insufficient_liquidity_history")
            else:
                median_dollar_volume = float(liquidity.median())
                if median_dollar_volume < self.rules.minimum_median_dollar_volume:
                    reasons.append("insufficient_dollar_volume")

        # Completeness is measured from the instrument's actual first observed
        # session.  Counting pre-listing NYSE sessions would create an
        # impossible hurdle for ETFs that legitimately joined later.
        first_observed = (
            pd.Timestamp(history.index.min()).normalize()
            if not history.empty
            else as_of
        )
        expected = sessions[(sessions >= first_observed) & (sessions <= as_of)]
        if not history.empty and len(expected):
            observed = pd.DatetimeIndex(history.index).normalize().intersection(expected)
            data_completeness = len(observed) / len(expected)
        else:
            data_completeness = 0.0
        if data_completeness < self.rules.minimum_data_completeness:
            reasons.append("insufficient_data_completeness")

        metadata = metadata or {}
        if not self.rules.leveraged_and_inverse_allowed and bool(
            metadata.get("leveraged_or_inverse", False)
        ):
            reasons.append("leveraged_or_inverse")

        return UniverseEligibility(
            ticker=ticker,
            as_of=str(as_of.date()),
            eligible=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            history_sessions=history_sessions,
            median_dollar_volume=median_dollar_volume,
            latest_price=latest_price,
            data_completeness=float(data_completeness),
        )

    @staticmethod
    def next_quarter_effective_date(as_of: pd.Timestamp) -> date:
        as_of = pd.Timestamp(as_of)
        quarter_end = as_of.to_period("Q").end_time.normalize()
        return (quarter_end + pd.Timedelta(days=1)).date()

    def propose_next_version(
        self,
        *,
        version: str,
        as_of: pd.Timestamp,
        historical_universe_integrity: bool = False,
    ) -> UniverseVersion:
        """Create a draft only; a human must approve before it can be used."""
        return UniverseVersion(
            version=version,
            effective_date=str(self.next_quarter_effective_date(as_of)),
            seed_tickers=self.seed_tickers,
            rules=self.rules,
            approved=False,
            historical_universe_integrity=historical_universe_integrity,
        )
