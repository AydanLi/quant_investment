from __future__ import annotations

import pandas as pd
import streamlit as st

from storage.sqlite_store import SQLiteStore


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


def main() -> None:
    st.set_page_config(page_title="Quant Research DB Dashboard", layout="wide")
    st.title("Quant Research DB Dashboard")
    st.caption("从 SQLite 读取历史实验、净值、订单、信号")

    with st.sidebar:
        st.header("数据库设置")
        limit = st.slider("读取最近实验数量", min_value=5, max_value=100, value=20, step=5)
        st.write(f"当前数据库文件：`{DB_PATH}`")

    try:
        runs = load_runs(limit)
    except Exception as exc:
        st.error(f"读取数据库失败：{exc}")
        return

    if runs.empty:
        st.warning("数据库里还没有实验记录。先运行 `python main_with_db.py` 写入数据。")
        return

    st.subheader("最近实验记录")
    st.dataframe(runs, use_container_width=True)

    run_id_list = runs["id"].tolist()
    default_run_id = int(run_id_list[0])

    selected_run_id = st.selectbox(
        "选择 run_id",
        options=run_id_list,
        index=0,
    )

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
