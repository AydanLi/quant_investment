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
from storage.sqlite_store import SQLiteStore
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


DB_PATH = "quant_research.db"


@st.cache_data(show_spinner=False)
def load_runs(limit: int) -> pd.DataFrame:
    store = SQLiteStore(DB_PATH)
    try:
        df = store.get_experiment_runs(limit)
    finally:
        store.close()
    return df


@st.cache_data(show_spinner=False)
def load_run_details(run_id: int):
    store = SQLiteStore(DB_PATH)
    try:
        portfolio = store.get_run_portfolio(run_id)
        orders = store.get_run_orders(run_id)
        signals = store.get_run_signals(run_id)
    finally:
        store.close()
    return portfolio, orders, signals


def format_pct(x):
    if pd.isna(x):
        return "N/A"
    return f"{x:.2%}"



def safe_float(x):
    if pd.isna(x):
        return None
    return float(x)



def execute_experiment_and_save(
    scenario_name: str,
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
) -> int:
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
    portfolio = results["portfolio"]
    orders = results["orders"]

    reporter = ReportGenerator(config)
    summary = reporter.summarize(portfolio)

    signal_service = SignalService(config)
    latest_signal = signal_service.generate_latest_allocation()

    store = SQLiteStore(DB_PATH)
    try:
        store.init_db()
        run_id = store.save_experiment_run(
            scenario_name=scenario_name,
            config=config,
            summary=summary,
            latest_signal=latest_signal,
        )
        store.save_portfolio_daily(run_id, portfolio)
        store.save_orders(run_id, orders)
        store.save_signals(run_id, latest_signal)
    finally:
        store.close()

    return run_id



def main() -> None:
    st.set_page_config(page_title="Quant Research DB Dashboard v1.1", layout="wide")
    st.title("Quant Research DB Dashboard v1.1")
    st.caption("读取 SQLite 历史实验，并支持一键保存当前参数为新实验")

    with st.sidebar:
        st.header("数据库设置")
        limit = st.slider("读取最近实验数量", min_value=5, max_value=100, value=20, step=5)
        st.write(f"当前数据库文件：`{DB_PATH}`")

        st.header("新实验参数")
        scenario_name = st.text_input("Scenario Name", value="dashboard_manual_run")
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
        target_annual_vol = st.slider("Target Annual Vol", 0.05, 0.30, 0.12, 0.01)
        max_asset_weight = st.slider("Max Asset Weight", 0.10, 1.00, 0.40, 0.05)
        risk_off_cash_weight = st.slider("Risk-Off Cash Weight", 0.00, 1.00, 0.50, 0.05)
        vix_risk_off_threshold = st.slider("VIX Risk-Off Threshold", 15.0, 50.0, 28.0, 1.0)
        vix_high_threshold = st.slider("VIX High Threshold", 12.0, 40.0, 22.0, 1.0)
        trading_cost_bps = st.slider("Trading Cost (bps)", 0.0, 30.0, 5.0, 0.5)

        auto_name = (
            f"dashboard_{rebalance_frequency}"
            f"_top{top_n}"
            f"_mom{min_momentum_threshold:.2f}"
            f"_vol{target_annual_vol:.2f}"
            f"_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"
        )

        use_auto_name = st.checkbox("自动生成实验名称", value=True)
        final_scenario_name = auto_name if use_auto_name else scenario_name
        st.caption(f"当前将保存为：{final_scenario_name}")

        if st.button("保存当前参数为新实验", type="primary", use_container_width=True):
            with st.spinner("正在回测并写入数据库..."):
                try:
                    run_id = execute_experiment_and_save(
                        scenario_name=final_scenario_name,
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
                    st.cache_data.clear()
                    st.success(f"保存成功，run_id = {run_id}")
                except Exception as exc:
                    st.error(f"保存失败：{exc}")

    try:
        runs = load_runs(limit)
    except Exception as exc:
        st.error(f"读取数据库失败：{exc}")
        return

    if runs.empty:
        st.warning("数据库里还没有实验记录。先点击左侧按钮保存一条新实验，或先运行 `python main_with_db.py`。")
        return

    st.subheader("最近实验记录")
    st.dataframe(runs, use_container_width=True)

    run_id_list = runs["id"].tolist()
    selected_run_id = st.selectbox("选择 run_id", options=run_id_list, index=0)
    selected_row = runs[runs["id"] == selected_run_id].iloc[0]

    st.subheader(f"实验摘要 · run_id={selected_run_id}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Scenario", str(selected_row.get("scenario_name", "N/A")))
    c2.metric("CAGR", format_pct(selected_row.get("cagr")))
    c3.metric("Sharpe", f"{selected_row['sharpe']:.2f}" if pd.notna(selected_row.get("sharpe")) else "N/A")
    c4.metric("Sortino", f"{selected_row['sortino']:.2f}" if pd.notna(selected_row.get("sortino")) else "N/A")
    c5.metric("Max Drawdown", format_pct(selected_row.get("max_drawdown")))

    c6, c7, c8, c9, c10 = st.columns(5)
    c6.metric("Annual Vol", format_pct(selected_row.get("annual_vol")))
    c7.metric("Avg Turnover", f"{selected_row['avg_turnover']:.4f}" if pd.notna(selected_row.get("avg_turnover")) else "N/A")
    c8.metric("Rebalance", str(selected_row.get("rebalance_frequency", "N/A")))
    c9.metric("Top N", str(selected_row.get("top_n", "N/A")))
    c10.metric("Latest Regime", str(selected_row.get("latest_regime", "N/A")))

    st.subheader("参数快照")
    param_cols = [
        "start_date",
        "rebalance_frequency",
        "top_n",
        "min_momentum_threshold",
        "target_annual_vol",
        "max_asset_weight",
        "risk_off_cash_weight",
        "vix_risk_off_threshold",
        "vix_high_threshold",
        "trading_cost_bps",
        "run_time",
    ]
    param_df = pd.DataFrame(
        [{"Parameter": col, "Value": selected_row.get(col)} for col in param_cols]
    )
    st.dataframe(param_df, use_container_width=True)

    try:
        portfolio, orders, signals = load_run_details(int(selected_run_id))
    except Exception as exc:
        st.error(f"读取 run 详情失败：{exc}")
        return

    tab1, tab2, tab3, tab4 = st.tabs(["净值曲线", "订单日志", "信号快照", "原始数据"])

    with tab1:
        st.subheader("净值曲线")
        if portfolio.empty:
            st.info("该 run 没有 portfolio_daily 数据。")
        else:
            portfolio = portfolio.copy()
            portfolio["date"] = pd.to_datetime(portfolio["date"])
            portfolio = portfolio.sort_values("date")
            chart_df = portfolio[["date", "equity"]].set_index("date")
            st.line_chart(chart_df)

            d1, d2, d3, d4 = st.columns(4)
            d1.metric("Start Equity", f"${safe_float(portfolio['equity'].iloc[0]):,.2f}" if not portfolio.empty else "N/A")
            d2.metric("End Equity", f"${safe_float(portfolio['equity'].iloc[-1]):,.2f}" if not portfolio.empty else "N/A")
            d3.metric("Rows", str(len(portfolio)))
            d4.metric("Last Regime", str(portfolio['regime'].iloc[-1]) if 'regime' in portfolio.columns and not portfolio.empty else "N/A")

            st.subheader("Regime 分布")
            if "regime" in portfolio.columns:
                regime_counts = portfolio["regime"].value_counts().rename_axis("regime").reset_index(name="count")
                st.bar_chart(regime_counts.set_index("regime"))

    with tab2:
        st.subheader("订单日志")
        if orders.empty:
            st.info("该 run 没有订单记录。")
        else:
            st.dataframe(orders, use_container_width=True)

    with tab3:
        st.subheader("信号快照")
        if signals.empty:
            st.info("该 run 没有 signals 数据。")
        else:
            signals = signals.copy()
            st.dataframe(signals, use_container_width=True)
            if {"ticker", "weight"}.issubset(signals.columns):
                signal_chart = signals[["ticker", "weight"]].copy()
                signal_chart = signal_chart.set_index("ticker")
                st.bar_chart(signal_chart)

    with tab4:
        st.subheader("portfolio_daily")
        st.dataframe(portfolio, use_container_width=True)
        st.subheader("orders")
        st.dataframe(orders, use_container_width=True)
        st.subheader("signals")
        st.dataframe(signals, use_container_width=True)

    st.subheader("实验横向比较")
    compare_cols = [
        "id",
        "scenario_name",
        "rebalance_frequency",
        "top_n",
        "min_momentum_threshold",
        "target_annual_vol",
        "vix_risk_off_threshold",
        "vix_high_threshold",
        "cagr",
        "sharpe",
        "sortino",
        "max_drawdown",
        "annual_vol",
        "avg_turnover",
        "latest_regime",
        "run_time",
    ]
    existing_compare_cols = [c for c in compare_cols if c in runs.columns]
    st.dataframe(runs[existing_compare_cols], use_container_width=True)


if __name__ == "__main__":
    main()
