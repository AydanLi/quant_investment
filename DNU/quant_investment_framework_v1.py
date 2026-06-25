"""
Quant Investment Framework v1
Author: OpenAI ChatGPT

Purpose:
- Pull market data
- Detect market regime
- Rank ETFs by momentum / volatility-adjusted score
- Apply risk controls
- Run historical backtest
- Produce trade suggestions for paper/live execution

Notes:
- This is a framework / MVP, not financial advice.
- Default universe is ETF-based for lower complexity and lower turnover.
- Uses end-of-day data by default.

Install:
    pip install pandas numpy yfinance matplotlib

Run:
    python quant_investment_framework_v1.py

Optional next steps:
- Split into modules
- Add broker API integration (Interactive Brokers / Alpaca)
- Add intraday bars
- Add sentiment / macro features
- Add database / dashboard
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


# =========================================================
# Configuration
# =========================================================


@dataclass
class Config:
    start_date: str = "2018-01-01"
    end_date: Optional[str] = None

    # Tradable universe
    universe: List[str] = field(
        default_factory=lambda: [
            "SPY",  # S&P 500
            "QQQ",  # Nasdaq 100
            "IWM",  # Russell 2000
            "TLT",  # Long bonds
            "GLD",  # Gold
            "XLE",  # Energy
            "XLV",  # Healthcare
            "BIL",  # Cash proxy
        ]
    )

    # Risk / regime references
    benchmark: str = "SPY"
    fear_gauge: str = "^VIX"

    # Strategy knobs
    rebalance_frequency: str = "M"  # D / W / M
    top_n: int = 3
    min_momentum_threshold: float = 0.0

    # Score weights
    weight_mom_20: float = 0.35
    weight_mom_60: float = 0.35
    weight_mom_120: float = 0.20
    weight_low_vol: float = 0.10

    # Regime thresholds
    vix_risk_off_threshold: float = 28.0
    vix_high_threshold: float = 22.0
    max_allowed_drawdown_from_200d: float = -0.08

    # Risk controls
    target_annual_vol: float = 0.12
    max_asset_weight: float = 0.40
    min_asset_weight: float = 0.00
    risk_off_cash_weight: float = 0.50
    trading_cost_bps: float = 5.0  # one-way estimate

    # Backtest
    initial_capital: float = 100000.0


# =========================================================
# Utilities
# =========================================================


def annualized_volatility(returns: pd.Series, periods_per_year: int = 252) -> float:
    if returns.dropna().empty:
        return np.nan
    return returns.std() * np.sqrt(periods_per_year)



def max_drawdown(equity_curve: pd.Series) -> float:
    running_max = equity_curve.cummax()
    dd = equity_curve / running_max - 1.0
    return float(dd.min())



def sharpe_ratio(returns: pd.Series, rf: float = 0.0, periods_per_year: int = 252) -> float:
    ret = returns.dropna()
    if ret.empty or ret.std() == 0:
        return np.nan
    excess = ret - rf / periods_per_year
    return float(excess.mean() / ret.std() * np.sqrt(periods_per_year))



def sortino_ratio(returns: pd.Series, rf: float = 0.0, periods_per_year: int = 252) -> float:
    ret = returns.dropna()
    if ret.empty:
        return np.nan
    downside = ret[ret < 0]
    if downside.std() == 0 or np.isnan(downside.std()):
        return np.nan
    excess = ret - rf / periods_per_year
    return float(excess.mean() / downside.std() * np.sqrt(periods_per_year))



def cagr(equity_curve: pd.Series, periods_per_year: int = 252) -> float:
    curve = equity_curve.dropna()
    if len(curve) < 2:
        return np.nan
    total_return = curve.iloc[-1] / curve.iloc[0]
    years = len(curve) / periods_per_year
    if years <= 0:
        return np.nan
    return float(total_return ** (1 / years) - 1)



def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(max(v, 0.0) for v in weights.values())
    if total <= 0:
        return {k: 0.0 for k in weights}
    return {k: max(v, 0.0) / total for k, v in weights.items()}


# =========================================================
# Data Layer
# =========================================================


class MarketDataLoader:
    def __init__(self, config: Config):
        self.config = config

    def load(self) -> Dict[str, pd.DataFrame]:
        tickers = list(set(self.config.universe + [self.config.benchmark, self.config.fear_gauge]))
        data = yf.download(
            tickers=tickers,
            start=self.config.start_date,
            end=self.config.end_date,
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=True,
        )

        result: Dict[str, pd.DataFrame] = {}
        for t in tickers:
            if t in data.columns.get_level_values(0):
                df = data[t].copy()
            else:
                # fallback for single structure edge cases
                cols = [c for c in data.columns if isinstance(c, tuple) and c[0] == t]
                if cols:
                    df = data[cols].copy()
                    df.columns = [c[1] for c in cols]
                else:
                    continue

            df = df.rename(columns=str.title)
            df = df[[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]]
            df = df.dropna(how="all")
            result[t] = df

        return result


# =========================================================
# Feature Engineering
# =========================================================


class FeatureEngineer:
    def __init__(self, data: Dict[str, pd.DataFrame], config: Config):
        self.data = data
        self.config = config

    def make_price_frame(self) -> pd.DataFrame:
        prices = {}
        for ticker, df in self.data.items():
            if "Close" in df.columns:
                prices[ticker] = df["Close"]
        price_df = pd.DataFrame(prices).sort_index().dropna(how="all")
        return price_df

    def make_returns_frame(self, prices: pd.DataFrame) -> pd.DataFrame:
        return prices.pct_change().replace([np.inf, -np.inf], np.nan)

    def compute_features(self, prices: pd.DataFrame, returns: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        features = {}

        features["mom_20"] = prices / prices.shift(20) - 1.0
        features["mom_60"] = prices / prices.shift(60) - 1.0
        features["mom_120"] = prices / prices.shift(120) - 1.0
        features["vol_20"] = returns.rolling(20).std() * np.sqrt(252)
        features["ma_50"] = prices.rolling(50).mean()
        features["ma_200"] = prices.rolling(200).mean()
        features["drawdown_200"] = prices / features["ma_200"] - 1.0

        return features


# =========================================================
# Regime Detection
# =========================================================


class RegimeDetector:
    def __init__(self, config: Config):
        self.config = config

    def classify(self, date: pd.Timestamp, prices: pd.DataFrame, features: Dict[str, pd.DataFrame]) -> str:
        benchmark = self.config.benchmark
        fear = self.config.fear_gauge

        # Guard clauses
        if benchmark not in prices.columns or fear not in prices.columns:
            return "neutral"

        try:
            spy_price = prices.at[date, benchmark]
            vix = prices.at[date, fear]
            ma_200 = features["ma_200"].at[date, benchmark]
            dd_200 = features["drawdown_200"].at[date, benchmark]
        except Exception:
            return "neutral"

        if pd.isna(spy_price) or pd.isna(vix) or pd.isna(ma_200) or pd.isna(dd_200):
            return "neutral"

        if vix >= self.config.vix_risk_off_threshold or dd_200 <= self.config.max_allowed_drawdown_from_200d:
            return "risk_off"
        if spy_price > ma_200 and vix < self.config.vix_high_threshold:
            return "bull_trend"
        if spy_price <= ma_200 and vix >= self.config.vix_high_threshold:
            return "bear_high_vol"
        return "neutral"


# =========================================================
# Strategy Engine
# =========================================================


class MomentumRotationStrategy:
    def __init__(self, config: Config):
        self.config = config

    def score_assets(
        self,
        date: pd.Timestamp,
        tradable_prices: pd.DataFrame,
        features: Dict[str, pd.DataFrame],
    ) -> pd.Series:
        tickers = [t for t in self.config.universe if t in tradable_prices.columns]

        mom_20 = features["mom_20"].loc[date, tickers]
        mom_60 = features["mom_60"].loc[date, tickers]
        mom_120 = features["mom_120"].loc[date, tickers]
        vol_20 = features["vol_20"].loc[date, tickers]

        inv_vol = 1.0 / vol_20.replace(0, np.nan)
        inv_vol = inv_vol.replace([np.inf, -np.inf], np.nan)

        # rank-based normalization for robustness
        def rank_norm(s: pd.Series) -> pd.Series:
            ranked = s.rank(pct=True)
            return ranked.fillna(0.0)

        score = (
            self.config.weight_mom_20 * rank_norm(mom_20)
            + self.config.weight_mom_60 * rank_norm(mom_60)
            + self.config.weight_mom_120 * rank_norm(mom_120)
            + self.config.weight_low_vol * rank_norm(inv_vol)
        )

        # hard filter on minimum momentum (using 60-day)
        score = score.where(mom_60 >= self.config.min_momentum_threshold, other=0.0)
        return score.sort_values(ascending=False)

    def target_weights(
        self,
        date: pd.Timestamp,
        regime: str,
        prices: pd.DataFrame,
        features: Dict[str, pd.DataFrame],
    ) -> Dict[str, float]:
        scores = self.score_assets(date, prices, features)
        selected = scores.head(self.config.top_n)

        if selected.empty or selected.max() <= 0:
            return {"BIL": 1.0} if "BIL" in self.config.universe else {self.config.universe[0]: 1.0}

        weights = normalize_weights(selected.to_dict())

        # regime overlay
        if regime == "risk_off":
            cash_weight = self.config.risk_off_cash_weight if "BIL" in self.config.universe else 0.0
            scaled = {k: v * (1.0 - cash_weight) for k, v in weights.items()}
            if cash_weight > 0:
                scaled["BIL"] = scaled.get("BIL", 0.0) + cash_weight
            weights = scaled
        elif regime == "bear_high_vol":
            # defensive tilt: only keep top 1-2 or allocate some to BIL
            top_items = dict(list(weights.items())[:2])
            top_items = normalize_weights(top_items)
            if "BIL" in self.config.universe:
                top_items = {k: v * 0.7 for k, v in top_items.items()}
                top_items["BIL"] = top_items.get("BIL", 0.0) + 0.3
            weights = top_items

        return weights


# =========================================================
# Risk Engine
# =========================================================


class RiskEngine:
    def __init__(self, config: Config):
        self.config = config

    def scale_to_target_vol(
        self,
        date: pd.Timestamp,
        raw_weights: Dict[str, float],
        returns: pd.DataFrame,
    ) -> Dict[str, float]:
        if not raw_weights:
            return raw_weights

        tickers = [t for t in raw_weights if t in returns.columns]
        if not tickers:
            return raw_weights

        hist = returns[tickers].loc[:date].tail(60).dropna(how="all")
        if len(hist) < 20:
            return raw_weights

        w = np.array([raw_weights[t] for t in tickers])
        cov = hist.cov().values * 252
        port_vol = float(np.sqrt(np.dot(w.T, np.dot(cov, w)))) if np.isfinite(cov).all() else np.nan

        if np.isnan(port_vol) or port_vol <= 0:
            return raw_weights

        scale = min(1.0, self.config.target_annual_vol / port_vol)
        scaled = {k: v * scale for k, v in raw_weights.items()}

        # residual goes to cash proxy if available
        residual = 1.0 - sum(scaled.values())
        if residual > 0 and "BIL" in self.config.universe:
            scaled["BIL"] = scaled.get("BIL", 0.0) + residual

        return scaled

    def enforce_weight_limits(self, weights: Dict[str, float]) -> Dict[str, float]:
        clipped = {
            k: min(max(v, self.config.min_asset_weight), self.config.max_asset_weight)
            for k, v in weights.items()
        }
        clipped = normalize_weights(clipped)
        return clipped

    def pre_trade_check(self, weights: Dict[str, float]) -> Tuple[bool, str]:
        total = sum(weights.values())
        if total <= 0.99 or total >= 1.01:
            return False, f"Weights do not sum close to 1.0: {total:.4f}"
        if any(v < -1e-9 for v in weights.values()):
            return False, "Negative weights not allowed in v1 long-only system."
        if any(v > max(self.config.max_asset_weight, self.config.risk_off_cash_weight, 1.0) + 1e-9 for v in weights.values()):
            return False, "Asset weight exceeds hard limit."
        return True, "OK"


# =========================================================
# Execution / OMS Mock
# =========================================================


class MockBroker:
    def __init__(self):
        self.order_log: List[Dict] = []

    def submit_orders(self, date: pd.Timestamp, current_weights: Dict[str, float], target_weights: Dict[str, float]) -> List[Dict]:
        orders = []
        all_tickers = sorted(set(current_weights.keys()).union(target_weights.keys()))

        for ticker in all_tickers:
            current_w = current_weights.get(ticker, 0.0)
            target_w = target_weights.get(ticker, 0.0)
            delta = target_w - current_w
            if abs(delta) > 1e-4:
                side = "BUY" if delta > 0 else "SELL"
                orders.append(
                    {
                        "date": date,
                        "ticker": ticker,
                        "side": side,
                        "weight_change": delta,
                    }
                )

        self.order_log.extend(orders)
        return orders


# =========================================================
# Backtesting Engine
# =========================================================


class Backtester:
    def __init__(
        self,
        config: Config,
        prices: pd.DataFrame,
        returns: pd.DataFrame,
        features: Dict[str, pd.DataFrame],
        regime_detector: RegimeDetector,
        strategy: MomentumRotationStrategy,
        risk_engine: RiskEngine,
    ):
        self.config = config
        self.prices = prices.copy()
        self.returns = returns.copy()
        self.features = features
        self.regime_detector = regime_detector
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.broker = MockBroker()

    def _get_rebalance_dates(self) -> pd.DatetimeIndex:
        idx = self.prices.index
        if self.config.rebalance_frequency == "D":
            return idx
        if self.config.rebalance_frequency == "W":
            return idx.to_series().groupby(idx.to_period("W")).tail(1).index
        return idx.to_series().groupby(idx.to_period("M")).tail(1).index

    def run(self) -> Dict[str, pd.DataFrame]:
        rebalance_dates = set(self._get_rebalance_dates())
        min_warmup = 220
        dates = self.prices.index[min_warmup:]

        current_weights: Dict[str, float] = {"BIL": 1.0} if "BIL" in self.config.universe else {}
        history = []
        equity = self.config.initial_capital
        prev_date = None

        for date in dates:
            # Portfolio PnL from previous holdings
            if prev_date is not None:
                daily_ret = 0.0
                for ticker, w in current_weights.items():
                    if ticker in self.returns.columns and pd.notna(self.returns.at[date, ticker]):
                        daily_ret += w * self.returns.at[date, ticker]
                equity *= (1.0 + daily_ret)
            else:
                daily_ret = 0.0

            regime = self.regime_detector.classify(date, self.prices, self.features)

            turnover = 0.0
            orders = []

            if date in rebalance_dates:
                target = self.strategy.target_weights(date, regime, self.prices, self.features)
                target = self.risk_engine.scale_to_target_vol(date, target, self.returns)
                target = self.risk_engine.enforce_weight_limits(target)

                ok, reason = self.risk_engine.pre_trade_check(target)
                if not ok:
                    raise ValueError(f"Pre-trade risk check failed on {date.date()}: {reason}")

                turnover = sum(abs(target.get(k, 0.0) - current_weights.get(k, 0.0)) for k in set(target).union(current_weights))
                est_cost = turnover * (self.config.trading_cost_bps / 10000.0)
                equity *= (1.0 - est_cost)

                orders = self.broker.submit_orders(date, current_weights, target)
                current_weights = target

            snapshot = {
                "date": date,
                "equity": equity,
                "daily_return": daily_ret,
                "regime": regime,
                "turnover": turnover,
            }
            for ticker in self.config.universe:
                snapshot[f"w_{ticker}"] = current_weights.get(ticker, 0.0)
            history.append(snapshot)
            prev_date = date

        result = pd.DataFrame(history).set_index("date")
        orders_df = pd.DataFrame(self.broker.order_log)
        return {
            "portfolio": result,
            "orders": orders_df,
        }


# =========================================================
# Reporting
# =========================================================


class ReportGenerator:
    def __init__(self, config: Config):
        self.config = config

    def summarize(self, portfolio: pd.DataFrame) -> pd.Series:
        equity_curve = portfolio["equity"]
        returns = portfolio["daily_return"]

        summary = pd.Series(
            {
                "Start Equity": float(equity_curve.iloc[0]),
                "End Equity": float(equity_curve.iloc[-1]),
                "Total Return": float(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0),
                "CAGR": cagr(equity_curve),
                "Annual Vol": annualized_volatility(returns),
                "Sharpe": sharpe_ratio(returns),
                "Sortino": sortino_ratio(returns),
                "Max Drawdown": max_drawdown(equity_curve),
                "Avg Turnover": float(portfolio["turnover"].mean()),
            }
        )
        return summary

    def print_latest_signal(self, portfolio: pd.DataFrame) -> None:
        latest = portfolio.iloc[-1]
        print("\n================ Latest Portfolio Suggestion ================")
        print(f"Date: {portfolio.index[-1].date()}")
        print(f"Regime: {latest['regime']}")
        print(f"Equity: ${latest['equity']:,.2f}")
        print("Suggested weights:")
        for ticker in self.config.universe:
            w = latest.get(f"w_{ticker}", 0.0)
            if w > 0.0001:
                print(f"  {ticker:<5} {w:>6.2%}")
        print("============================================================\n")

    def plot(self, portfolio: pd.DataFrame, benchmark_prices: pd.Series) -> None:
        equity_curve = portfolio["equity"].copy()
        bench = benchmark_prices.loc[equity_curve.index].dropna()
        bench_curve = self.config.initial_capital * (1.0 + bench.pct_change().fillna(0.0)).cumprod()

        plt.figure(figsize=(12, 6))
        plt.plot(equity_curve.index, equity_curve.values, label="Strategy Equity")
        plt.plot(bench_curve.index, bench_curve.values, label=f"{self.config.benchmark} Buy & Hold")
        plt.title("Strategy vs Benchmark")
        plt.xlabel("Date")
        plt.ylabel("Equity")
        plt.legend()
        plt.tight_layout()
        plt.show()


# =========================================================
# Live / Paper Signal Service
# =========================================================


class SignalService:
    def __init__(self, config: Config):
        self.config = config

    def generate_latest_allocation(self) -> Dict[str, float]:
        loader = MarketDataLoader(self.config)
        data = loader.load()
        fe = FeatureEngineer(data, self.config)
        prices = fe.make_price_frame()
        returns = fe.make_returns_frame(prices)
        features = fe.compute_features(prices, returns)

        date = prices.index[-1]
        regime_detector = RegimeDetector(self.config)
        strategy = MomentumRotationStrategy(self.config)
        risk_engine = RiskEngine(self.config)

        regime = regime_detector.classify(date, prices, features)
        target = strategy.target_weights(date, regime, prices, features)
        target = risk_engine.scale_to_target_vol(date, target, returns)
        target = risk_engine.enforce_weight_limits(target)

        ok, reason = risk_engine.pre_trade_check(target)
        if not ok:
            raise ValueError(f"Latest signal failed pre-trade check: {reason}")

        return target


# =========================================================
# Main Runner
# =========================================================


def run_backtest(config: Config) -> None:
    print("Loading data...")
    loader = MarketDataLoader(config)
    data = loader.load()

    print("Building features...")
    fe = FeatureEngineer(data, config)
    prices = fe.make_price_frame()
    returns = fe.make_returns_frame(prices)
    features = fe.compute_features(prices, returns)

    print("Running backtest...")
    regime_detector = RegimeDetector(config)
    strategy = MomentumRotationStrategy(config)
    risk_engine = RiskEngine(config)

    bt = Backtester(
        config=config,
        prices=prices,
        returns=returns,
        features=features,
        regime_detector=regime_detector,
        strategy=strategy,
        risk_engine=risk_engine,
    )
    results = bt.run()
    portfolio = results["portfolio"]
    orders = results["orders"]

    reporter = ReportGenerator(config)
    summary = reporter.summarize(portfolio)

    print("\n================ Backtest Summary ================")
    for k, v in summary.items():
        if isinstance(v, float):
            if "Equity" in k:
                print(f"{k:<16}: ${v:,.2f}")
            elif k in {"Sharpe", "Sortino", "Avg Turnover"}:
                print(f"{k:<16}: {v:.4f}")
            else:
                print(f"{k:<16}: {v:.2%}")
        else:
            print(f"{k:<16}: {v}")
    print("==================================================")

    reporter.print_latest_signal(portfolio)

    if not orders.empty:
        print("Recent orders:")
        print(orders.tail(10).to_string(index=False))

    reporter.plot(portfolio, prices[config.benchmark])


if __name__ == "__main__":
    config = Config(
        start_date="2018-01-01",
        end_date=None,
        rebalance_frequency="M",
        top_n=3,
        min_momentum_threshold=0.0,
        target_annual_vol=0.12,
        max_asset_weight=0.40,
        risk_off_cash_weight=0.50,
        trading_cost_bps=5.0,
    )

    run_backtest(config)

    # Example for signal-only usage:
    # service = SignalService(config)
    # latest = service.generate_latest_allocation()
    # print("Latest target allocation:", latest)
