from __future__ import annotations

from dataclasses import asdict
from typing import Mapping

import pandas as pd

from config.settings import Config
from config.universe import EligibilityRules, UniversePolicy, UniverseVersion
from data.adjustments import locally_adjust_ohlcv
from data.calendar import NyseCalendar
from data.models import DataQualityReport, ProviderPayload
from data.providers import (
    CboeVixProvider,
    MarketDataProvider,
    ProviderError,
    TiingoMarketDataProvider,
    YahooMarketDataProvider,
)
from data.quality import assess_market_data_quality
from storage.db import create_all
from storage.repositories import GovernanceRepository, TrustedMarketDataRepository


class TrustedMarketDataLoader:
    """Full-history raw-data loader with explicit actions and dual-source QA.

    Open-ended refreshes deliberately fetch the complete requested history.
    This is more expensive than tail stitching, but prevents a newly declared
    dividend from changing old adjusted prices without updating the cache.
    """

    def __init__(
        self,
        config: Config,
        *,
        primary_provider: MarketDataProvider | None = None,
        secondary_provider: MarketDataProvider | None = None,
        vix_provider: CboeVixProvider | None = None,
        repository: TrustedMarketDataRepository | None = None,
        calendar: NyseCalendar | None = None,
        as_of: pd.Timestamp | None = None,
        persist: bool = True,
    ) -> None:
        self.config = config
        self.calendar = calendar or NyseCalendar()
        self.as_of = as_of
        self.vix_provider = vix_provider or CboeVixProvider()
        self.primary_provider = primary_provider or self._default_primary()
        if secondary_provider is not None:
            self.secondary_provider = secondary_provider
        elif self.primary_provider.name == "tiingo":
            self.secondary_provider = YahooMarketDataProvider()
        else:
            self.secondary_provider = None
        self.repository = repository
        if persist and self.repository is None:
            repo = TrustedMarketDataRepository(db_url=config.db_url)
            create_all(repo.engine)
            self.repository = repo
        self.quality_report: DataQualityReport | None = None
        self.dataset_snapshot_id: int | None = None
        self.primary_payload: ProviderPayload | None = None
        self.secondary_payload: ProviderPayload | None = None
        self.universe_version_recorded = False
        self._loaded_data: dict[str, pd.DataFrame] | None = None

    @staticmethod
    def _default_primary() -> MarketDataProvider:
        # Tiingo is the declared primary. Missing credentials must fail loudly;
        # silently promoting the validation source would destroy provenance.
        return TiingoMarketDataProvider()

    def _provider_payload(self, provider: MarketDataProvider) -> ProviderPayload:
        tickers = sorted(set(self.config.universe + [self.config.benchmark]))
        provider_tickers = [ticker for ticker in tickers if ticker != self.config.fear_gauge]
        if provider.name == "yahoo":
            provider_tickers.append(self.config.fear_gauge)
        payload = provider.fetch(provider_tickers, self.config.start_date, self.config.end_date)
        bars = dict(payload.bars)
        metadata = dict(payload.metadata)
        if self.config.fear_gauge not in bars:
            if provider.name != "tiingo":
                raise ProviderError(
                    f"{provider.name} did not return {self.config.fear_gauge}."
                )
            bars[self.config.fear_gauge] = self.vix_provider.fetch(
                self.config.start_date, self.config.end_date
            )
            metadata[self.config.fear_gauge] = {"source": self.vix_provider.name}
        source = provider.name if provider.name != "tiingo" else "tiingo+cboe"
        return ProviderPayload(
            bars=bars,
            actions=payload.actions,
            metadata=metadata,
            source=source,
        )

    @staticmethod
    def _actions_by_ticker(payload: ProviderPayload) -> dict[str, list]:
        result: dict[str, list] = {}
        for action in payload.actions:
            result.setdefault(action.ticker, []).append(action)
        return result

    def _adjusted_frames(self, payload: ProviderPayload) -> dict[str, pd.DataFrame]:
        actions = self._actions_by_ticker(payload)
        benchmark = payload.bars.get(self.config.benchmark)
        if benchmark is None or benchmark.empty:
            raise ValueError("Trusted data requires the benchmark session calendar.")
        benchmark_sessions = pd.DatetimeIndex(benchmark.index).tz_localize(None).normalize()
        result: dict[str, pd.DataFrame] = {}
        for ticker, raw in payload.bars.items():
            frame = raw.copy().sort_index()
            frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
            frame = frame.loc[frame.index.intersection(benchmark_sessions)]
            if ticker == self.config.fear_gauge:
                for column in ("Open", "High", "Low", "Close"):
                    if column in frame:
                        frame[f"Adjusted {column}"] = frame[column].astype(float)
                frame["Adjustment Factor"] = 1.0
                frame["Dividend"] = 0.0
                frame["Split Factor"] = 1.0
                result[ticker] = frame
            else:
                result[ticker] = locally_adjust_ohlcv(frame, actions.get(ticker, ()))
        return result

    def load(self, *, force_refresh: bool = False) -> dict[str, pd.DataFrame]:
        if self._loaded_data is not None and not force_refresh:
            return {
                ticker: frame.copy(deep=True)
                for ticker, frame in self._loaded_data.items()
            }

        primary = self._provider_payload(self.primary_provider)
        secondary = (
            None
            if self.secondary_provider is None
            else self._provider_payload(self.secondary_provider)
        )
        required = sorted(
            set(self.config.universe + [self.config.benchmark, self.config.fear_gauge])
        )
        report = assess_market_data_quality(
            primary,
            secondary,
            required_tickers=required,
            config=self.config,
            calendar=self.calendar,
            as_of=self.as_of,
        )
        self.primary_payload = primary
        self.secondary_payload = secondary
        self.quality_report = report

        if self.repository is not None:
            for ticker, frame in primary.bars.items():
                source = str(
                    primary.metadata.get(ticker, {}).get(
                        "source", self.primary_provider.name
                    )
                )
                self.repository.upsert_raw_bars({ticker: frame}, source=source)
            self.repository.upsert_actions(primary.actions)
            self.repository.upsert_metadata(
                primary.metadata, source=self.primary_provider.name
            )
            if secondary is not None:
                for ticker, frame in secondary.bars.items():
                    source = str(
                        secondary.metadata.get(ticker, {}).get(
                            "source", self.secondary_provider.name
                        )
                    )
                    self.repository.upsert_raw_bars({ticker: frame}, source=source)
                self.repository.upsert_actions(secondary.actions)
                self.repository.upsert_metadata(
                    secondary.metadata, source=self.secondary_provider.name
                )
            as_of = pd.Timestamp.now(tz="UTC") if self.as_of is None else pd.Timestamp(self.as_of)
            source_by_ticker = {
                ticker: str(
                    primary.metadata.get(ticker, {}).get(
                        "source", self.primary_provider.name
                    )
                )
                for ticker in primary.bars
            }
            self.dataset_snapshot_id = self.repository.create_snapshot(
                report,
                as_of=as_of.isoformat(),
                start_date=self.config.start_date,
                end_date=self.config.end_date,
                bars=primary.bars,
                actions=primary.actions,
                source_by_ticker=source_by_ticker,
                secondary_payload=secondary,
            )
            if report.latest_session is not None:
                session = pd.Timestamp(report.latest_session)
                benchmark = primary.bars[self.config.benchmark]
                sessions = self.calendar.sessions(benchmark.index.min(), session)
                policy = UniversePolicy(self.config.universe, EligibilityRules())
                eligibility = [
                    asdict(
                        policy.assess(
                            ticker,
                            primary.bars.get(ticker),
                            as_of=session,
                            sessions=sessions,
                            metadata=primary.metadata.get(ticker, {}),
                        )
                    )
                    for ticker in self.config.universe
                ]
                GovernanceRepository(engine=self.repository.engine).save_universe_version(
                    UniverseVersion(
                        version=self.config.universe_version,
                        effective_date=self.config.universe_effective_date,
                        seed_tickers=tuple(self.config.universe),
                        rules=policy.rules,
                        approved=True,
                        approved_by="implementation_plan_2026-07-17",
                        historical_universe_integrity=self.config.historical_universe_integrity,
                    ),
                    eligibility=eligibility,
                )
                self.universe_version_recorded = True
        self._loaded_data = self._adjusted_frames(primary)
        return {
            ticker: frame.copy(deep=True)
            for ticker, frame in self._loaded_data.items()
        }

    @property
    def actionable(self) -> bool:
        return bool(self.quality_report and self.quality_report.actionable)
