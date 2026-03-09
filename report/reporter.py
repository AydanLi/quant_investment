from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config.settings import Config


class ReportGenerator:
    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def annualized_volatility(returns: pd.Series, periods_per_year: int = 252) -> float:
        if returns.dropna().empty:
            return np.nan
        return float(returns.std() * np.sqrt(periods_per_year))

    @staticmethod
    def max_drawdown(equity_curve: pd.Series) -> float:
        running_max = equity_curve.cummax()
        dd = equity_curve / running_max - 1.0
        return float(dd.min())

    @staticmethod
    def sharpe_ratio(returns: pd.Series, periods_per_year: int = 252) -> float:
        ret = returns.dropna()
        if ret.empty or ret.std() == 0:
            return np.nan
        return float(ret.mean() / ret.std() * np.sqrt(periods_per_year))

    @staticmethod
    def cagr(equity_curve: pd.Series, periods_per_year: int = 252) -> float:
        curve = equity_curve.dropna()
        if len(curve) < 2:
            return np.nan
        total_return = curve.iloc[-1] / curve.iloc[0]
        years = len(curve) / periods_per_year
        if years <= 0:
            return np.nan
        return float(total_return ** (1 / years) - 1)

    def summarize(self, portfolio: pd.DataFrame) -> pd.Series:
        equity_curve = portfolio["equity"]
        returns = portfolio["daily_return"]

        return pd.Series(
            {
                "Start Equity": float(equity_curve.iloc[0]),
                "End Equity": float(equity_curve.iloc[-1]),
                "Total Return": float(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0),
                "CAGR": self.cagr(equity_curve),
                "Annual Vol": self.annualized_volatility(returns),
                "Sharpe": self.sharpe_ratio(returns),
                "Max Drawdown": self.max_drawdown(equity_curve),
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