from __future__ import annotations

import pandas as pd
import streamlit as st

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from data.loader import MarketDataLoader
from report.reporter import ReportGenerator
from risk.engine import RiskEngine
from services.signal_service import SignalService
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


@st.cache_data(show_spinner=False)
def load_backtest_data(start_date: str, rebalance_frequency: str, top_n: int, min_momentum_threshold: float):
    config = Config(
        start_date=start_date,
        end_date=None,
        rebalance_frequency=rebalance_frequency,
        top_n=top_n,
        min_momentum_threshold=min_momentum_threshold,
        target_annual_vol=0.12,
        max_asset_weight=0.40,
        risk_off_cash_weight=0.50,
        trading_cost_bps=5.0,
    )

    loader = MarketDataLoader(config)
    data = loader.load()

    fe = FeatureEngineer(data, config)
    prices = fe.make_price_frame()
    returns = fe.make_returns_frame(prices)
    features = fe.compute_features(prices, returns)

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

    reporter = ReportGenerator(config)
    summary = reporter.summarize(results["portfolio"])

    signal_service = SignalService(config)
    latest_signal = signal_service.generate_latest_allocation()

    return config, prices, results["portfolio"], results["orders"], summary, latest_signal


def format_percent(value: float) -> str:
    return f"{value:.2%}"


def main() -> None:
    st.set_page_config(page_title="Quant Dashboard", layout="wide")
    st.title("Quant Investment Dashboard")
    st.caption("ETF 轮动 + 市场状态识别 + 风控 + 回测")

    with st.sidebar:
        st.header("参数")
        start_date = st.text_input("Start Date", value="2018-01-01")
        rebalance_frequency = st.selectbox("Rebalance Frequency", ["D", "W", "M"], index=2)
        top_n = st.slider("Top N Assets", min_value=1, max_value=6, value=3)
        min_momentum_threshold = st.slider(
            "Min Momentum Threshold",
            min_value=-0.10,
            max_value=0.20,
            value=0.00,
            step=0.01,
        )

    try:
        config, prices, portfolio, orders, summary, latest_signal = load_backtest_data(
            start_date=start_date,
            rebalance_frequency=rebalance_frequency,
            top_n=top_n,
            min_momentum_threshold=min_momentum_threshold,
        )
    except Exception as exc:
        st.error(f"加载失败：{exc}")
        return

    st.subheader("关键指标")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("CAGR", format_percent(summary["CAGR"]))
    c2.metric("Sharpe", f"{summary['Sharpe']:.2f}" if pd.notna(summary["Sharpe"]) else "N/A")
    c3.metric("Max Drawdown", format_percent(summary["Max Drawdown"]))
    c4.metric("Annual Vol", format_percent(summary["Annual Vol"]))

    st.subheader("净值曲线")
    equity_df = portfolio[["equity"]].copy()
    bench = prices[config.benchmark].loc[equity_df.index].dropna()
    bench_curve = config.initial_capital * (1.0 + bench.pct_change().fillna(0.0)).cumprod()
    chart_df = pd.DataFrame(
        {
            "Strategy": equity_df["equity"],
            f"{config.benchmark} Buy&Hold": bench_curve,
        }
    ).dropna()
    st.line_chart(chart_df)

    st.subheader("市场状态分布")
    regime_counts = portfolio["regime"].value_counts().rename_axis("regime").reset_index(name="count")
    st.bar_chart(regime_counts.set_index("regime"))

    st.subheader("最新信号")
    s1, s2 = st.columns([1, 2])
    with s1:
        st.write(f"**Date:** {latest_signal['date']}")
        st.write(f"**Regime:** {latest_signal['regime']}")
    with s2:
        weights_df = pd.DataFrame(
            [{"Ticker": k, "Weight": v} for k, v in latest_signal["weights"].items()]
        ).sort_values("Weight", ascending=False)
        if not weights_df.empty:
            st.dataframe(weights_df, use_container_width=True)

    st.subheader("最新组合权重快照")
    latest_portfolio = portfolio.iloc[-1]
    current_weights = []
    for ticker in config.universe:
        weight = latest_portfolio.get(f"w_{ticker}", 0.0)
        if weight > 0.0001:
            current_weights.append({"Ticker": ticker, "Weight": weight})
    current_weights_df = pd.DataFrame(current_weights).sort_values("Weight", ascending=False)
    if not current_weights_df.empty:
        st.dataframe(current_weights_df, use_container_width=True)

    st.subheader("最近订单")
    if orders.empty:
        st.info("暂无订单记录")
    else:
        display_orders = orders.tail(20).copy()
        st.dataframe(display_orders, use_container_width=True)

    st.subheader("最近回测记录")
    st.dataframe(portfolio.tail(20), use_container_width=True)


if __name__ == "__main__":
    main()
