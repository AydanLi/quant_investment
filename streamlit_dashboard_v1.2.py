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
def run_system(
    start_date: str,
    rebalance_frequency: str,
    top_n: int,
    min_momentum_threshold: float,
    target_annual_vol: float,
    max_asset_weight: float,
    risk_off_cash_weight: float,
    vix_risk_off_threshold: float,
    vix_high_threshold: float,
    trading_cost_bps: float,
):
    config = Config(
        start_date=start_date,
        end_date=None,
        rebalance_frequency=rebalance_frequency,
        top_n=top_n,
        min_momentum_threshold=min_momentum_threshold,
        target_annual_vol=target_annual_vol,
        max_asset_weight=max_asset_weight,
        risk_off_cash_weight=risk_off_cash_weight,
        vix_risk_off_threshold=vix_risk_off_threshold,
        vix_high_threshold=vix_high_threshold,
        trading_cost_bps=trading_cost_bps,
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

    return {
        "config": config,
        "prices": prices,
        "returns": returns,
        "features": features,
        "portfolio": results["portfolio"],
        "orders": results["orders"],
        "summary": summary,
        "latest_signal": latest_signal,
        "strategy": strategy,
        "risk_engine": risk_engine,
        "regime_detector": regime_detector,
    }



def format_percent(value: float) -> str:
    return f"{value:.2%}"



def explain_latest_decision(system: dict) -> dict:
    config = system["config"]
    prices = system["prices"]
    returns = system["returns"]
    features = system["features"]
    strategy = system["strategy"]
    regime_detector = system["regime_detector"]

    date = prices.index[-1]
    regime = regime_detector.classify(date, prices, features)
    raw_scores = strategy.score_assets(date, prices, features)
    selected = raw_scores.head(config.top_n)

    benchmark = config.benchmark
    fear = config.fear_gauge

    benchmark_price = prices.at[date, benchmark] if benchmark in prices.columns else None
    vix = prices.at[date, fear] if fear in prices.columns else None
    ma_200 = features["ma_200"].at[date, benchmark] if benchmark in features["ma_200"].columns else None
    dd_200 = features["drawdown_200"].at[date, benchmark] if benchmark in features["drawdown_200"].columns else None

    latest_signal = system["latest_signal"]
    latest_weights = latest_signal["weights"]

    selected_detail = []
    for ticker in selected.index:
        selected_detail.append(
            {
                "Ticker": ticker,
                "Score": float(selected[ticker]),
                "Mom20": float(features["mom_20"].at[date, ticker]) if ticker in features["mom_20"].columns else None,
                "Mom60": float(features["mom_60"].at[date, ticker]) if ticker in features["mom_60"].columns else None,
                "Mom120": float(features["mom_120"].at[date, ticker]) if ticker in features["mom_120"].columns else None,
                "Vol20": float(features["vol_20"].at[date, ticker]) if ticker in features["vol_20"].columns else None,
                "FinalWeight": float(latest_weights.get(ticker, 0.0)),
            }
        )

    recent_portfolio_returns = returns[[k for k in latest_weights if k in returns.columns]].tail(60).dropna(how="all")
    est_portfolio_vol = None
    if not recent_portfolio_returns.empty:
        tickers = [k for k in latest_weights if k in recent_portfolio_returns.columns]
        if tickers:
            w = pd.Series({k: latest_weights[k] for k in tickers})
            cov = recent_portfolio_returns[tickers].cov() * 252
            try:
                est_portfolio_vol = float((w.values.T @ cov.values @ w.values) ** 0.5)
            except Exception:
                est_portfolio_vol = None

    return {
        "date": str(date.date()),
        "regime": regime,
        "benchmark_price": benchmark_price,
        "vix": vix,
        "ma_200": ma_200,
        "drawdown_200": dd_200,
        "selected_detail": pd.DataFrame(selected_detail),
        "latest_weights": pd.DataFrame(
            [{"Ticker": k, "Weight": v} for k, v in latest_weights.items()]
        ).sort_values("Weight", ascending=False),
        "est_portfolio_vol": est_portfolio_vol,
    }



def summary_to_row(name: str, system: dict) -> dict:
    summary = system["summary"]
    latest_signal = system["latest_signal"]
    return {
        "Scenario": name,
        "Rebalance": system["config"].rebalance_frequency,
        "TopN": system["config"].top_n,
        "MinMom": system["config"].min_momentum_threshold,
        "TargetVol": system["config"].target_annual_vol,
        "VIX RiskOff": system["config"].vix_risk_off_threshold,
        "VIX High": system["config"].vix_high_threshold,
        "CAGR": summary["CAGR"],
        "Sharpe": summary["Sharpe"],
        "Sortino": summary["Sortino"],
        "Max Drawdown": summary["Max Drawdown"],
        "Annual Vol": summary["Annual Vol"],
        "Avg Turnover": summary["Avg Turnover"],
        "Latest Regime": latest_signal["regime"],
    }



def main() -> None:
    st.set_page_config(page_title="Quant Dashboard v1.2", layout="wide")
    st.title("Quant Investment Dashboard v1.2")
    st.caption("ETF 轮动 + 市场状态识别 + 风控 + 回测 + 策略解释 + 参数对比实验")

    with st.sidebar:
        st.header("主场景参数")
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

        st.header("风控参数")
        target_annual_vol = st.slider("Target Annual Vol", 0.05, 0.30, 0.12, 0.01)
        max_asset_weight = st.slider("Max Asset Weight", 0.10, 1.00, 0.40, 0.05)
        risk_off_cash_weight = st.slider("Risk-Off Cash Weight", 0.00, 1.00, 0.50, 0.05)
        trading_cost_bps = st.slider("Trading Cost (bps)", 0.0, 30.0, 5.0, 0.5)

        st.header("Regime 参数")
        vix_risk_off_threshold = st.slider("VIX Risk-Off Threshold", 15.0, 50.0, 28.0, 1.0)
        vix_high_threshold = st.slider("VIX High Threshold", 12.0, 40.0, 22.0, 1.0)

        st.header("对比实验")
        enable_compare = st.checkbox("开启双场景对比", value=True)
        compare_rebalance_frequency = st.selectbox("对比 Rebalance", ["D", "W", "M"], index=1)
        compare_top_n = st.slider("对比 Top N", min_value=1, max_value=6, value=4)
        compare_min_momentum_threshold = st.slider(
            "对比 Min Momentum",
            min_value=-0.10,
            max_value=0.20,
            value=0.02,
            step=0.01,
        )
        compare_target_annual_vol = st.slider("对比 Target Vol", 0.05, 0.30, 0.15, 0.01)
        compare_vix_risk_off_threshold = st.slider("对比 VIX Risk-Off", 15.0, 50.0, 30.0, 1.0)
        compare_vix_high_threshold = st.slider("对比 VIX High", 12.0, 40.0, 24.0, 1.0)

    try:
        base_system = run_system(
            start_date=start_date,
            rebalance_frequency=rebalance_frequency,
            top_n=top_n,
            min_momentum_threshold=min_momentum_threshold,
            target_annual_vol=target_annual_vol,
            max_asset_weight=max_asset_weight,
            risk_off_cash_weight=risk_off_cash_weight,
            vix_risk_off_threshold=vix_risk_off_threshold,
            vix_high_threshold=vix_high_threshold,
            trading_cost_bps=trading_cost_bps,
        )
    except Exception as exc:
        st.error(f"主场景加载失败：{exc}")
        return

    compare_system = None
    if enable_compare:
        try:
            compare_system = run_system(
                start_date=start_date,
                rebalance_frequency=compare_rebalance_frequency,
                top_n=compare_top_n,
                min_momentum_threshold=compare_min_momentum_threshold,
                target_annual_vol=compare_target_annual_vol,
                max_asset_weight=max_asset_weight,
                risk_off_cash_weight=risk_off_cash_weight,
                vix_risk_off_threshold=compare_vix_risk_off_threshold,
                vix_high_threshold=compare_vix_high_threshold,
                trading_cost_bps=trading_cost_bps,
            )
        except Exception as exc:
            st.warning(f"对比场景加载失败：{exc}")

    config = base_system["config"]
    prices = base_system["prices"]
    portfolio = base_system["portfolio"]
    orders = base_system["orders"]
    summary = base_system["summary"]
    latest_signal = base_system["latest_signal"]
    explanation = explain_latest_decision(base_system)

    st.subheader("主场景关键指标")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("CAGR", format_percent(summary["CAGR"]))
    c2.metric("Sharpe", f"{summary['Sharpe']:.2f}" if pd.notna(summary["Sharpe"]) else "N/A")
    c3.metric("Sortino", f"{summary['Sortino']:.2f}" if pd.notna(summary["Sortino"]) else "N/A")
    c4.metric("Max Drawdown", format_percent(summary["Max Drawdown"]))
    c5.metric("Annual Vol", format_percent(summary["Annual Vol"]))

    st.subheader("净值曲线")
    equity_df = portfolio[["equity"]].copy()
    bench = prices[config.benchmark].loc[equity_df.index].dropna()
    bench_curve = config.initial_capital * (1.0 + bench.pct_change().fillna(0.0)).cumprod()

    chart_df = pd.DataFrame(
        {
            "Base Strategy": equity_df["equity"],
            f"{config.benchmark} Buy&Hold": bench_curve,
        }
    ).dropna()

    if compare_system is not None:
        compare_curve = compare_system["portfolio"]["equity"].copy()
        compare_curve.name = "Compare Strategy"
        chart_df = chart_df.join(compare_curve, how="inner")

    st.line_chart(chart_df)

    left, right = st.columns([1, 1])
    with left:
        st.subheader("市场状态分布")
        regime_counts = portfolio["regime"].value_counts().rename_axis("regime").reset_index(name="count")
        st.bar_chart(regime_counts.set_index("regime"))
    with right:
        st.subheader("最新信号")
        st.write(f"**Date:** {latest_signal['date']}")
        st.write(f"**Regime:** {latest_signal['regime']}")
        st.dataframe(
            pd.DataFrame(
                [{"Ticker": k, "Weight": v} for k, v in latest_signal["weights"].items()]
            ).sort_values("Weight", ascending=False),
            use_container_width=True,
        )

    st.subheader("为什么今天会给这个仓位")
    e1, e2, e3, e4 = st.columns(4)
    e1.metric("Current Regime", explanation["regime"])
    e2.metric("VIX", f"{explanation['vix']:.2f}" if explanation["vix"] is not None and pd.notna(explanation["vix"]) else "N/A")
    e3.metric("SPY vs 200DMA", f"{((explanation['benchmark_price'] / explanation['ma_200']) - 1):.2%}" if explanation["benchmark_price"] is not None and explanation["ma_200"] is not None and pd.notna(explanation["ma_200"]) and explanation["ma_200"] != 0 else "N/A")
    e4.metric("Est Portfolio Vol", f"{explanation['est_portfolio_vol']:.2%}" if explanation["est_portfolio_vol"] is not None else "N/A")

    regime_text = {
        "bull_trend": "当前更偏趋势市场，系统更愿意持有高分动量资产。",
        "neutral": "当前不是强趋势也不是极端风险，系统会按综合打分进行中性配置。",
        "risk_off": "当前触发风险关闭条件，系统会显著提高现金/BIL 权重。",
        "bear_high_vol": "当前偏高波动防御环境，系统会压缩持仓数量并提高防守仓位。",
    }.get(explanation["regime"], "当前为默认中性解释。")
    st.info(regime_text)

    st.subheader("入选资产打分细节")
    details_df = explanation["selected_detail"].copy()
    if not details_df.empty:
        st.dataframe(details_df, use_container_width=True)
    else:
        st.warning("当前没有可展示的入选资产明细。")

    st.subheader("参数对比实验")
    if compare_system is None:
        st.info("未开启或未成功加载对比场景。")
    else:
        compare_table = pd.DataFrame(
            [
                summary_to_row("Base", base_system),
                summary_to_row("Compare", compare_system),
            ]
        )
        st.dataframe(compare_table, use_container_width=True)

        delta_row = {
            "Metric": ["CAGR", "Sharpe", "Sortino", "Max Drawdown", "Annual Vol", "Avg Turnover"],
            "Compare - Base": [
                compare_system["summary"]["CAGR"] - base_system["summary"]["CAGR"],
                compare_system["summary"]["Sharpe"] - base_system["summary"]["Sharpe"],
                compare_system["summary"]["Sortino"] - base_system["summary"]["Sortino"],
                compare_system["summary"]["Max Drawdown"] - base_system["summary"]["Max Drawdown"],
                compare_system["summary"]["Annual Vol"] - base_system["summary"]["Annual Vol"],
                compare_system["summary"]["Avg Turnover"] - base_system["summary"]["Avg Turnover"],
            ],
        }
        st.dataframe(pd.DataFrame(delta_row), use_container_width=True)

    st.subheader("最新组合权重快照")
    st.dataframe(explanation["latest_weights"], use_container_width=True)

    st.subheader("最近订单")
    if orders.empty:
        st.info("暂无订单记录")
    else:
        st.dataframe(orders.tail(20), use_container_width=True)

    st.subheader("最近回测记录")
    st.dataframe(portfolio.tail(20), use_container_width=True)


if __name__ == "__main__":
    main()
