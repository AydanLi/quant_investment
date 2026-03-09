# Quant Investment Framework v2.1 (Runnable Project Package)

这是一份更接近可落地开发的 v2.1 项目版。

相比 v2，新增了：

* `utils/metrics.py`：绩效指标统一管理
* `services/signal_service.py`：只输出最新调仓建议
* `tests/`：基础单元测试骨架
* 更清晰的 README
* 更清晰的扩展位点（后续接 Streamlit / Broker API / DB）

---

## Project Structure

```text
quant_system/
├── main.py
├── requirements.txt
├── README.md
├── config/
│   ├── __init__.py
│   └── settings.py
├── data/
│   ├── __init__.py
│   ├── loader.py
│   └── features.py
├── strategy/
│   ├── __init__.py
│   ├── regime.py
│   └── momentum_rotation.py
├── risk/
│   ├── __init__.py
│   └── engine.py
├── execution/
│   ├── __init__.py
│   └── broker.py
├── backtest/
│   ├── __init__.py
│   └── engine.py
├── report/
│   ├── __init__.py
│   └── reporter.py
├── utils/
│   ├── __init__.py
│   └── metrics.py
├── services/
│   ├── __init__.py
│   └── signal_service.py
└── tests/
    ├── __init__.py
    ├── test_metrics.py
    └── test_risk_engine.py
```

---

## File: `requirements.txt`

```txt
pandas
numpy
yfinance
matplotlib
pytest
```

---

## File: `README.md`

````md
# Quant System v2.1

A modular ETF rotation quant framework with:
- Market data loading
- Feature engineering
- Regime detection
- Momentum rotation strategy
- Risk engine
- Backtesting engine
- Mock broker execution
- Reporting
- Latest allocation signal service
- Basic unit tests

## 1. Install

```bash
pip install -r requirements.txt
````

## 2. Run backtest

```bash
python main.py
```

## 3. Run tests

```bash
pytest -q
```

## 4. Current scope

This version is:

* Long-only
* ETF-focused
* End-of-day data
* Suitable for research / prototype / paper trading

This version is NOT yet:

* Institution-grade OMS/EMS
* Tick-level or HFT-ready
* Broker-live by default

## 5. Suggested next upgrades

* Add `dashboard/streamlit_app.py`
* Add real broker integration (IBKR / Alpaca)
* Add database persistence
* Add walk-forward testing
* Add news sentiment features
* Add parameter optimization

````

---

## File: `config/__init__.py`

```python
# empty
````

---

## File: `config/settings.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Config:
    start_date: str = "2018-01-01"
    end_date: Optional[str] = None

    universe: List[str] = field(
        default_factory=lambda: [
            "SPY",
            "QQQ",
            "IWM",
            "TLT",
            "GLD",
            "XLE",
            "XLV",
            "BIL",
        ]
    )

    benchmark: str = "SPY"
    fear_gauge: str = "^VIX"

    rebalance_frequency: str = "M"  # D / W / M
    top_n: int = 3
    min_momentum_threshold: float = 0.0

    weight_mom_20: float = 0.35
    weight_mom_60: float = 0.35
    weight_mom_120: float = 0.20
    weight_low_vol: float = 0.10

    vix_risk_off_threshold: float = 28.0
    vix_high_threshold: float = 22.0
    max_allowed_drawdown_from_200d: float = -0.08

    target_annual_vol: float = 0.12
    max_asset_weight: float = 0.40
    min_asset_weight: float = 0.00
    risk_off_cash_weight: float = 0.50
    trading_cost_bps: float = 5.0

    initial_capital: float = 100000.0
```

---

## File: `data/__init__.py`

```python
# empty
```

---

## File: `data/loader.py`

```python
from __future__ import annotations

from typing import Dict

import pandas as pd
import yfinance as yf

from config.settings import Config


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

        if data.empty:
            raise ValueError("No market data returned. Check ticker list, dates, or network connection.")

        result: Dict[str, pd.DataFrame] = {}
        for t in tickers:
            try:
                if t in data.columns.get_level_values(0):
                    df = data[t].copy()
                else:
                    cols = [c for c in data.columns if isinstance(c, tuple) and c[0] == t]
                    if cols:
                        df = data[cols].copy()
                        df.columns = [c[1] for c in cols]
                    else:
                        continue

                df = df.rename(columns=str.title)
                keep_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
                df = df[keep_cols].dropna(how="all")
                if not df.empty:
                    result[t] = df
            except Exception:
                continue

        if not result:
            raise ValueError("Failed to parse downloaded market data.")

        return result
```

---

## File: `data/features.py`

```python
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from config.settings import Config


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
        if price_df.empty:
            raise ValueError("Price frame is empty. Cannot continue.")
        return price_df

    def make_returns_frame(self, prices: pd.DataFrame) -> pd.DataFrame:
        return prices.pct_change().replace([np.inf, -np.inf], np.nan)

    def compute_features(self, prices: pd.DataFrame, returns: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        features: Dict[str, pd.DataFrame] = {}
        features["mom_20"] = prices / prices.shift(20) - 1.0
        features["mom_60"] = prices / prices.shift(60) - 1.0
        features["mom_120"] = prices / prices.shift(120) - 1.0
        features["vol_20"] = returns.rolling(20).std() * np.sqrt(252)
        features["ma_50"] = prices.rolling(50).mean()
        features["ma_200"] = prices.rolling(200).mean()
        features["drawdown_200"] = prices / features["ma_200"] - 1.0
        return features
```

---

## File: `strategy/__init__.py`

```python
# empty
```

---

## File: `strategy/regime.py`

```python
from __future__ import annotations

import pandas as pd

from config.settings import Config


class RegimeDetector:
    def __init__(self, config: Config):
        self.config = config

    def classify(self, date: pd.Timestamp, prices: pd.DataFrame, features: dict) -> str:
        benchmark = self.config.benchmark
        fear = self.config.fear_gauge

        if benchmark not in prices.columns or fear not in prices.columns:
            return "neutral"

        try:
            benchmark_price = prices.at[date, benchmark]
            vix = prices.at[date, fear]
            ma_200 = features["ma_200"].at[date, benchmark]
            dd_200 = features["drawdown_200"].at[date, benchmark]
        except Exception:
            return "neutral"

        if pd.isna(benchmark_price) or pd.isna(vix) or pd.isna(ma_200) or pd.isna(dd_200):
            return "neutral"

        if vix >= self.config.vix_risk_off_threshold or dd_200 <= self.config.max_allowed_drawdown_from_200d:
            return "risk_off"
        if benchmark_price > ma_200 and vix < self.config.vix_high_threshold:
            return "bull_trend"
        if benchmark_price <= ma_200 and vix >= self.config.vix_high_threshold:
            return "bear_high_vol"
        return "neutral"
```

---

## File: `strategy/momentum_rotation.py`

```python
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from config.settings import Config


class MomentumRotationStrategy:
    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        total = sum(max(v, 0.0) for v in weights.values())
        if total <= 0:
            return {k: 0.0 for k in weights}
        return {k: max(v, 0.0) / total for k, v in weights.items()}

    def score_assets(self, date: pd.Timestamp, prices: pd.DataFrame, features: dict) -> pd.Series:
        tickers = [t for t in self.config.universe if t in prices.columns]
        if not tickers:
            return pd.Series(dtype=float)

        mom_20 = features["mom_20"].loc[date, tickers]
        mom_60 = features["mom_60"].loc[date, tickers]
        mom_120 = features["mom_120"].loc[date, tickers]
        vol_20 = features["vol_20"].loc[date, tickers]

        inv_vol = 1.0 / vol_20.replace(0, np.nan)
        inv_vol = inv_vol.replace([np.inf, -np.inf], np.nan)

        def rank_norm(series: pd.Series) -> pd.Series:
            return series.rank(pct=True).fillna(0.0)

        score = (
            self.config.weight_mom_20 * rank_norm(mom_20)
            + self.config.weight_mom_60 * rank_norm(mom_60)
            + self.config.weight_mom_120 * rank_norm(mom_120)
            + self.config.weight_low_vol * rank_norm(inv_vol)
        )

        score = score.where(mom_60 >= self.config.min_momentum_threshold, other=0.0)
        return score.sort_values(ascending=False)

    def target_weights(self, date: pd.Timestamp, regime: str, prices: pd.DataFrame, features: dict) -> Dict[str, float]:
        scores = self.score_assets(date, prices, features)
        selected = scores.head(self.config.top_n)

        if selected.empty or selected.max() <= 0:
            return {"BIL": 1.0} if "BIL" in self.config.universe else {self.config.universe[0]: 1.0}

        weights = self.normalize_weights(selected.to_dict())

        if regime == "risk_off":
            cash_weight = self.config.risk_off_cash_weight if "BIL" in self.config.universe else 0.0
            scaled = {k: v * (1.0 - cash_weight) for k, v in weights.items()}
            if cash_weight > 0:
                scaled["BIL"] = scaled.get("BIL", 0.0) + cash_weight
            weights = scaled
        elif regime == "bear_high_vol":
            top_items = dict(list(weights.items())[:2])
            top_items = self.normalize_weights(top_items)
            if "BIL" in self.config.universe:
                top_items = {k: v * 0.7 for k, v in top_items.items()}
                top_items["BIL"] = top_items.get("BIL", 0.0) + 0.3
            weights = top_items

        return weights
```

---

## File: `risk/__init__.py`

```python
# empty
```

---

## File: `risk/engine.py`

```python
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from config.settings import Config


class RiskEngine:
    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        total = sum(max(v, 0.0) for v in weights.values())
        if total <= 0:
            return {k: 0.0 for k in weights}
        return {k: max(v, 0.0) / total for k, v in weights.items()}

    def scale_to_target_vol(self, date: pd.Timestamp, raw_weights: Dict[str, float], returns: pd.DataFrame) -> Dict[str, float]:
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
        if not np.isfinite(cov).all():
            return raw_weights

        port_vol = float(np.sqrt(np.dot(w.T, np.dot(cov, w))))
        if port_vol <= 0:
            return raw_weights

        scale = min(1.0, self.config.target_annual_vol / port_vol)
        scaled = {k: v * scale for k, v in raw_weights.items()}

        residual = 1.0 - sum(scaled.values())
        if residual > 0 and "BIL" in self.config.universe:
            scaled["BIL"] = scaled.get("BIL", 0.0) + residual

        return scaled

    def enforce_weight_limits(self, weights: Dict[str, float]) -> Dict[str, float]:
        clipped = {
            k: min(max(v, self.config.min_asset_weight), self.config.max_asset_weight)
            for k, v in weights.items()
        }
        return self.normalize_weights(clipped)

    def pre_trade_check(self, weights: Dict[str, float]) -> Tuple[bool, str]:
        total = sum(weights.values())
        if total <= 0.99 or total >= 1.01:
            return False, f"Weights do not sum close to 1.0: {total:.4f}"
        if any(v < -1e-9 for v in weights.values()):
            return False, "Negative weights not allowed in v2.1 long-only system."
        if any(v > 1.000001 for v in weights.values()):
            return False, "Single asset weight exceeds 100%."
        return True, "OK"
```

---

## File: `execution/__init__.py`

```python
# empty
```

---

## File: `execution/broker.py`

```python
from __future__ import annotations

from typing import Dict, List

import pandas as pd


class MockBroker:
    def __init__(self):
        self.order_log: List[Dict] = []

    def submit_orders(self, date: pd.Timestamp, current_weights: Dict[str, float], target_weights: Dict[str, float]) -> List[Dict]:
        orders: List[Dict] = []
        all_tickers = sorted(set(current_weights.keys()).union(target_weights.keys()))

        for ticker in all_tickers:
            current_w = current_weights.get(ticker, 0.0)
            target_w = target_weights.get(ticker, 0.0)
            delta = target_w - current_w
            if abs(delta) > 1e-4:
                orders.append(
                    {
                        "date": date,
                        "ticker": ticker,
                        "side": "BUY" if delta > 0 else "SELL",
                        "weight_change": delta,
                    }
                )

        self.order_log.extend(orders)
        return orders
```

---

## File: `backtest/__init__.py`

```python
# empty
```

---

## File: `backtest/engine.py`

```python
from __future__ import annotations

from typing import Dict

import pandas as pd

from config.settings import Config
from execution.broker import MockBroker


class Backtester:
    def __init__(self, config: Config, prices: pd.DataFrame, returns: pd.DataFrame, features: dict, regime_detector, strategy, risk_engine):
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

        if len(dates) == 0:
            raise ValueError("Not enough data after warmup window. Extend start_date earlier.")

        current_weights = {"BIL": 1.0} if "BIL" in self.config.universe else {}
        history = []
        equity = self.config.initial_capital
        prev_date = None

        for date in dates:
            if prev_date is not None:
                daily_ret = 0.0
                for ticker, weight in current_weights.items():
                    if ticker in self.returns.columns and pd.notna(self.returns.at[date, ticker]):
                        daily_ret += weight * self.returns.at[date, ticker]
                equity *= (1.0 + daily_ret)
            else:
                daily_ret = 0.0

            regime = self.regime_detector.classify(date, self.prices, self.features)
            turnover = 0.0

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

                self.broker.submit_orders(date, current_weights, target)
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

        portfolio = pd.DataFrame(history).set_index("date")
        orders = pd.DataFrame(self.broker.order_log)
        return {"portfolio": portfolio, "orders": orders}
```

---

## File: `utils/__init__.py`

```python
# empty
```

---

## File: `utils/metrics.py`

```python
from __future__ import annotations

import numpy as np
import pandas as pd


def annualized_volatility(returns: pd.Series, periods_per_year: int = 252) -> float:
    ret = returns.dropna()
    if ret.empty:
        return np.nan
    return float(ret.std() * np.sqrt(periods_per_year))



def max_drawdown(equity_curve: pd.Series) -> float:
    curve = equity_curve.dropna()
    if curve.empty:
        return np.nan
    running_max = curve.cummax()
    dd = curve / running_max - 1.0
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
    if downside.empty or downside.std() == 0:
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
```

---

## File: `report/__init__.py`

```python
# empty
```

---

## File: `report/reporter.py`

```python
from __future__ import annotations

import matplotlib.pyplot as plt
import pandas as pd

from config.settings import Config
from utils.metrics import annualized_volatility, cagr, max_drawdown, sharpe_ratio, sortino_ratio


class ReportGenerator:
    def __init__(self, config: Config):
        self.config = config

    def summarize(self, portfolio: pd.DataFrame) -> pd.Series:
        equity_curve = portfolio["equity"]
        returns = portfolio["daily_return"]

        return pd.Series(
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

    def print_latest_signal(self, portfolio: pd.DataFrame) -> None:
        latest = portfolio.iloc[-1]
        print("\n================ Latest Portfolio Suggestion ================")
        print(f"Date: {portfolio.index[-1].date()}")
        print(f"Regime: {latest['regime']}")
        print(f"Equity: ${latest['equity']:,.2f}")
        print("Suggested weights:")
        for ticker in self.config.universe:
            weight = latest.get(f"w_{ticker}", 0.0)
            if weight > 0.0001:
                print(f"  {ticker:<5} {weight:>6.2%}")
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
```

---

## File: `services/__init__.py`

```python
# empty
```

---

## File: `services/signal_service.py`

```python
from __future__ import annotations

from config.settings import Config
from data.features import FeatureEngineer
from data.loader import MarketDataLoader
from risk.engine import RiskEngine
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


class SignalService:
    def __init__(self, config: Config):
        self.config = config

    def generate_latest_allocation(self) -> dict:
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

        return {
            "date": str(date.date()),
            "regime": regime,
            "weights": target,
        }
```

---

## File: `tests/__init__.py`

```python
# empty
```

---

## File: `tests/test_metrics.py`

```python
import pandas as pd

from utils.metrics import annualized_volatility, cagr, max_drawdown


def test_max_drawdown_negative_or_zero():
    equity = pd.Series([100, 110, 105, 90, 95])
    result = max_drawdown(equity)
    assert result <= 0


def test_annualized_volatility_non_negative():
    returns = pd.Series([0.01, -0.02, 0.005, 0.003])
    result = annualized_volatility(returns)
    assert result >= 0


def test_cagr_float_output():
    equity = pd.Series([100, 105, 110, 120])
    result = cagr(equity, periods_per_year=4)
    assert isinstance(result, float)
```

---

## File: `tests/test_risk_engine.py`

```python
from config.settings import Config
from risk.engine import RiskEngine


def test_pre_trade_check_valid_weights():
    config = Config()
    engine = RiskEngine(config)
    ok, reason = engine.pre_trade_check({"SPY": 0.5, "QQQ": 0.5})
    assert ok is True
    assert reason == "OK"


def test_pre_trade_check_invalid_sum():
    config = Config()
    engine = RiskEngine(config)
    ok, reason = engine.pre_trade_check({"SPY": 0.4, "QQQ": 0.4})
    assert ok is False
    assert "Weights do not sum" in reason
```

---

## File: `main.py`

```python
from __future__ import annotations

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from data.loader import MarketDataLoader
from report.reporter import ReportGenerator
from risk.engine import RiskEngine
from services.signal_service import SignalService
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


def run_backtest() -> None:
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
    for key, value in summary.items():
        if isinstance(value, float):
            if "Equity" in key:
                print(f"{key:<16}: ${value:,.2f}")
            elif key in {"Sharpe", "Sortino", "Avg Turnover"}:
                print(f"{key:<16}: {value:.4f}")
            else:
                print(f"{key:<16}: {value:.2%}")
        else:
            print(f"{key:<16}: {value}")
    print("==================================================")

    reporter.print_latest_signal(portfolio)

    if not orders.empty:
        print("Recent orders:")
        print(orders.tail(10).to_string(index=False))

    reporter.plot(portfolio, prices[config.benchmark])


def run_signal_only() -> None:
    config = Config()
    service = SignalService(config)
    signal = service.generate_latest_allocation()
    print("Latest signal:")
    print(signal)


if __name__ == "__main__":
    run_backtest()
    # run_signal_only()
```

---

## 本地创建方式

在本地先建目录：

```bash
mkdir quant_system
cd quant_system
mkdir config data strategy risk execution backtest report utils services tests
```

然后把对应代码分别复制进去，最后运行：

```bash
pip install -r requirements.txt
python main.py
pytest -q
```

---

## v2.1 的意义

这版已经不只是“代码示例”，而是比较像你后面能持续迭代的个人量化研究项目骨架。

它适合继续往下长成：

* v2.2：加 Streamlit dashboard
* v2.3：加真实 Broker API
* v2.4：加数据库与日志
* v2.5：加新闻情绪与宏观因子
* v3.0：多策略组合 + walk-forward + 参数管理

---

## 我建议你下一步最该做的事

不是继续堆功能，而是优先做下面两件：

1. 把这个 v2.1 真正在你本地跑通
2. 我再帮你做一个 `dashboard/streamlit_app.py`

这样你就不只是有代码，还有一个你能每天看的量化驾驶舱。
