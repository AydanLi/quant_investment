from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config.settings import Config
from data.calendar import NyseCalendar
from risk.exposure import analyze_exposure
from utils.metrics import (
    annualized_volatility,
    cagr,
    calmar_ratio,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    trade_win_rate,
)


class ReportGenerator:
    def __init__(self, config: Config):
        self.config = config

    def summarize(
        self,
        portfolio: pd.DataFrame,
        *,
        risk_free_returns: pd.Series | None = None,
        benchmark_returns: dict[str, pd.Series] | None = None,
        orders: pd.DataFrame | None = None,
        asset_returns: pd.DataFrame | None = None,
    ) -> pd.Series:
        if portfolio.empty:
            raise ValueError("Cannot summarize an empty portfolio.")
        equity_curve = portfolio["equity"]
        returns = portfolio["daily_return"]
        initial_equity = float(self.config.initial_capital)
        first_date = NyseCalendar().previous_session(equity_curve.index[0])
        equity_with_initial = pd.concat(
            [pd.Series([initial_equity], index=[first_date]), equity_curve]
        ).sort_index()
        gross_returns = portfolio.get("gross_return", returns).fillna(0.0)
        gross_end = initial_equity * float((1.0 + gross_returns).prod())
        cost_dollars = float(
            portfolio.get("cost_dollars", pd.Series(0.0, index=portfolio.index))
            .fillna(0.0)
            .sum()
        )
        if cost_dollars == 0.0 and "est_cost" in portfolio:
            cost_dollars = float(
                (portfolio["est_cost"].fillna(0.0) * portfolio["equity"].shift(1).fillna(initial_equity)).sum()
            )
        gross_profit = gross_end - initial_equity
        elapsed_days = (
            pd.Timestamp(equity_with_initial.index[-1])
            - pd.Timestamp(equity_with_initial.index[0])
        ).days
        rf_available = risk_free_returns is not None
        rf_input: float | pd.Series = risk_free_returns if rf_available else 0.0

        trade_pnl = pd.Series(dtype=float)
        if orders is not None and "realized_pnl" in orders:
            trade_pnl = orders["realized_pnl"]
        latest_weights = {
            str(column)[2:]: float(portfolio[column].iloc[-1])
            for column in portfolio.columns
            if str(column).startswith("w_") and pd.notna(portfolio[column].iloc[-1])
        }
        exposure = analyze_exposure(latest_weights, asset_returns)
        stop_rows = portfolio[
            portfolio.get("stop_triggered", pd.Series(False, index=portfolio.index)).astype(bool)
        ]
        stop_trigger_drawdown = (
            float(stop_rows["drawdown"].iloc[0]) if not stop_rows.empty else np.nan
        )
        realized_after_stop = (
            float(portfolio.loc[stop_rows.index[0]:, "drawdown"].min())
            if not stop_rows.empty
            else np.nan
        )

        values: dict[str, object] = {
                "Start Equity": initial_equity,
                "End Equity": float(equity_curve.iloc[-1]),
                "Total Return": float(equity_curve.iloc[-1] / initial_equity - 1.0),
                "Gross Total Return": float(gross_end / initial_equity - 1.0),
                "CAGR": cagr(equity_with_initial),
                "Annual Vol": annualized_volatility(returns),
                "Sharpe": sharpe_ratio(returns, rf_input) if rf_available else np.nan,
                "Sortino": sortino_ratio(returns, rf_input) if rf_available else np.nan,
                "Sharpe (rf=0)": sharpe_ratio(returns),
                "Sortino (rf=0)": sortino_ratio(returns),
                "Max Drawdown": max_drawdown(equity_with_initial),
                "Calmar": calmar_ratio(equity_with_initial),
                "Avg Turnover": float(portfolio["turnover"].mean()),
                "Total Cost Dollars": cost_dollars,
                "Cost / Gross Profit": (
                    cost_dollars / gross_profit if gross_profit > 0.0 else float("nan")
                ),
                "Trade Win Rate": trade_win_rate(trade_pnl),
                "Profit Factor": profit_factor(trade_pnl),
                "Risk-free Source": "FRED DGS3MO" if rf_available else "MISSING",
                "Historical Universe Integrity": self.config.historical_universe_integrity,
                "Risky Exposure": exposure.risky_exposure,
                "Cash Exposure": exposure.cash_exposure,
                "Largest Risk Position": exposure.largest_risky_position,
                "Largest Asset Class Exposure": exposure.largest_asset_class_exposure,
                "Correlation Concentration": exposure.correlation_concentration,
                "Effective Independent Bets": exposure.effective_independent_bets,
                "Asset Class Exposures": dict(exposure.asset_class_exposures),
                "Drawdown Stop Trigger Value": stop_trigger_drawdown,
                "Worst Realized Drawdown After Trigger": realized_after_stop,
                "Metric Status": (
                    "PROVISIONAL"
                    if elapsed_days < 365
                    else ("FINAL" if rf_available else "RF_MISSING")
                ),
            }
        for name, benchmark in (benchmark_returns or {}).items():
            aligned = benchmark.reindex(returns.index).dropna()
            values[f"Benchmark {name} Total Return"] = (
                float((1.0 + aligned).prod() - 1.0) if not aligned.empty else float("nan")
            )
            values[f"Benchmark {name} Sharpe"] = sharpe_ratio(aligned, rf_input)
        return pd.Series(values)

    def print_latest_signal(self, portfolio: pd.DataFrame) -> None:
        latest = portfolio.iloc[-1]
        print("\n================ Backtest Diagnostic Snapshot ================")
        print(f"Date: {portfolio.index[-1].date()}")
        print(f"Regime: {latest['regime']}")
        print(f"Equity: ${latest['equity']:,.2f}")
        print("Backtested weights (not an order or actionable signal):")
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
