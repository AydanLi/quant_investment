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
        risk_free_source: str = "PROVIDED_SERIES",
        benchmark_returns: dict[str, pd.Series] | None = None,
        orders: pd.DataFrame | None = None,
        asset_returns: pd.DataFrame | None = None,
    ) -> pd.Series:
        if portfolio.empty:
            raise ValueError("Cannot summarize an empty portfolio.")
        equity_curve = portfolio["equity"]
        returns = portfolio["daily_return"]
        if not portfolio.index.is_unique or not portfolio.index.is_monotonic_increasing:
            raise ValueError("Portfolio sessions must be unique and increasing.")
        if not np.isfinite(equity_curve).all() or (equity_curve <= 0).any():
            raise ValueError("Portfolio equity must be finite and positive.")
        if not np.isfinite(returns).all() or (returns <= -1).any():
            raise ValueError("Portfolio returns must be finite and greater than -100%.")
        initial_equity = float(portfolio.attrs.get("initial_nav", self.config.initial_capital))
        if not np.isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("Opening NAV must be finite and positive.")
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
        rf_aligned = pd.Series(np.nan, index=returns.index, dtype=float)
        if risk_free_returns is not None:
            if not risk_free_returns.index.is_unique:
                raise ValueError("Risk-free observations must have unique sessions.")
            rf_aligned = risk_free_returns.astype(float).reindex(returns.index)
        rf_coverage = float(np.isfinite(rf_aligned).mean())
        rf_available = rf_coverage == 1.0
        rf_status = (
            "COMPLETE" if rf_available else
            "MISSING" if risk_free_returns is None or risk_free_returns.empty else "INCOMPLETE"
        )
        rf_input: float | pd.Series = rf_aligned if rf_available else 0.0
        unconfirmed_payments = bool(portfolio.get(
            "unconfirmed_dividend_payments", pd.Series(False, index=portfolio.index)
        ).fillna(True).astype(bool).any())

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
                "Sell Order Win Rate": trade_win_rate(trade_pnl),
                "Profit Factor": profit_factor(trade_pnl),
                "Average Win / Average Loss": (
                    float(trade_pnl[trade_pnl > 0].mean() / abs(trade_pnl[trade_pnl < 0].mean()))
                    if (trade_pnl > 0).any() and (trade_pnl < 0).any() else np.nan
                ),
                "Trade Metric Basis": "SELL_ORDERS_AVERAGE_COST_NET_OF_FEES",
                "Tax Basis": "PRE_TAX",
                "Metric Schema Version": 2,
                "Accounting Model": portfolio.attrs.get("accounting_model", "UNSPECIFIED"),
                "Execution Model": portfolio.attrs.get("execution_model", "UNSPECIFIED"),
                "Risk-free Source": risk_free_source if risk_free_returns is not None else "MISSING",
                "Risk-free Coverage": rf_coverage,
                "Risk-free Status": rf_status,
                "Cash-flow Evidence": "UNCONFIRMED_PAYMENT" if unconfirmed_payments else "CONFIRMED",
                "Observations": len(returns),
                "Period Start": str(returns.index[0]),
                "Period End": str(returns.index[-1]),
                "Universe Research Basis": (
                    "POINT_IN_TIME" if self.config.historical_universe_integrity else "CURRENT_UNIVERSE_BACKCAST"
                ),
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
                    "PROVISIONAL_CASH_FLOWS" if unconfirmed_payments else "PROVISIONAL"
                    if elapsed_days < 365
                    else ("FINAL" if rf_available else f"RF_{rf_status}")
                ),
            }
        for name, benchmark in (benchmark_returns or {}).items():
            if not benchmark.index.is_unique:
                raise ValueError(f"Benchmark {name} has duplicate sessions.")
            aligned = benchmark.astype(float).reindex(returns.index)
            coverage = float(np.isfinite(aligned).mean())
            complete = coverage == 1.0 and bool((aligned > -1.0).all())
            values[f"Benchmark {name} Coverage"] = coverage
            values[f"Benchmark {name} Status"] = "COMPLETE" if complete else "INCOMPLETE"
            if not complete and values["Metric Status"] == "FINAL":
                values["Metric Status"] = "BENCHMARK_INCOMPLETE"
            values[f"Benchmark {name} Total Return"] = (
                float((1.0 + aligned).prod() - 1.0) if complete else float("nan")
            )
            values[f"Benchmark {name} Sharpe"] = (
                sharpe_ratio(aligned, rf_input) if complete and rf_available else float("nan")
            )
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
        if portfolio.empty or not portfolio.index.is_unique or not benchmark_prices.index.is_unique:
            raise ValueError("Plot requires nonempty, uniquely dated portfolio and benchmark data.")
        initial = float(portfolio.attrs.get("initial_nav", self.config.initial_capital))
        previous_session = NyseCalendar().previous_session(portfolio.index[0])
        equity_curve = pd.concat([pd.Series([initial], index=[previous_session]), portfolio["equity"]])
        bench = benchmark_prices.reindex(equity_curve.index)
        if not np.isfinite(bench).all() or (bench <= 0).any():
            raise ValueError("Benchmark plot requires complete prices including the opening-NAV session.")
        bench_curve = initial * bench / float(bench.iloc[0])

        plt.figure(figsize=(12, 6))
        plt.plot(equity_curve.index, equity_curve.values, label="Strategy Equity")
        plt.plot(bench_curve.index, bench_curve.values, label=f"{self.config.benchmark} Buy & Hold")
        plt.title("Strategy vs Benchmark")
        plt.xlabel("Date")
        plt.ylabel("Equity")
        plt.legend()
        plt.tight_layout()
        plt.show()
