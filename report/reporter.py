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
        initial_equity = float(self.config.initial_capital)
        equity_with_initial = pd.concat(
            [
                pd.Series([initial_equity], dtype=float),
                equity_curve.reset_index(drop=True),
            ],
            ignore_index=True,
        )

        return pd.Series(
            {
                "Start Equity": initial_equity,
                "End Equity": float(equity_curve.iloc[-1]),
                "Total Return": float(equity_curve.iloc[-1] / initial_equity - 1.0),
                "CAGR": cagr(equity_with_initial),
                "Annual Vol": annualized_volatility(returns),
                "Sharpe": sharpe_ratio(returns),
                "Sortino": sortino_ratio(returns),
                "Max Drawdown": max_drawdown(equity_with_initial),
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
