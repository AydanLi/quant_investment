from __future__ import annotations

from typing import Union

import pandas as pd
import streamlit as st

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from data.loader import MarketDataLoader
from report.reporter import ReportGenerator
from risk.engine import RiskEngine
from services.experiment_validation import validate_experiment_parameters
from services.factor_monitor import FACTOR_LABELS, build_factor_monitor
from services.signal_service import SignalService
from storage.store import ResearchStore
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


DB_PATH = "quant_research.db"
Numeric = Union[int, float]


def synced_numeric_parameter(
    label: str,
    key: str,
    *,
    min_value: Numeric,
    max_value: Numeric,
    value: Numeric,
    step: Numeric,
    number_format: str,
) -> Numeric:
    """Render a slider and direct-entry box backed by synchronized state."""
    slider_key = f"{key}_slider"
    input_key = f"{key}_input"
    if slider_key not in st.session_state:
        st.session_state[slider_key] = value
    if input_key not in st.session_state:
        st.session_state[input_key] = value

    def sync_from_slider() -> None:
        st.session_state[input_key] = st.session_state[slider_key]

    def sync_from_input() -> None:
        candidate = st.session_state[input_key]
        if pd.notna(candidate) and min_value <= candidate <= max_value:
            st.session_state[slider_key] = candidate

    st.markdown(f"**{label}**")
    slider_col, input_col = st.columns([2, 1], gap="small")
    with slider_col:
        st.slider(
            f"{label} slider",
            min_value=min_value,
            max_value=max_value,
            step=step,
            format=number_format,
            key=slider_key,
            on_change=sync_from_slider,
            label_visibility="collapsed",
        )
    with input_col:
        st.number_input(
            f"{label} direct input",
            step=step,
            format=number_format,
            key=input_key,
            on_change=sync_from_input,
            label_visibility="collapsed",
        )
    return st.session_state[input_key]


@st.cache_data(show_spinner=False)
def load_runs(limit: int) -> pd.DataFrame:
    store = ResearchStore()
    try:
        df = store.get_experiment_runs(limit)
    finally:
        store.close()
    return df


@st.cache_data(show_spinner=False)
def load_run_details(run_id: int):
    store = ResearchStore()
    try:
        portfolio = store.get_run_portfolio(run_id)
        orders = store.get_run_orders(run_id)
        signals = store.get_run_signals(run_id)
    finally:
        store.close()
    return portfolio, orders, signals


@st.cache_data(show_spinner=False)
def load_factor_monitor(run_id: int):
    store = ResearchStore()
    try:
        portfolio = store.get_run_portfolio(run_id)
        prices = store.market_data.get_close_frame(
            ["SPY", "QQQ", "IWM", "TLT", "GLD", "XLE", "XLV", "BIL"]
        )
    finally:
        store.close()
    return build_factor_monitor(portfolio, prices)


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
    slippage_bps: float,
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
        slippage_bps=slippage_bps,
        risk_model="dynamic_factor",
        ewma_half_life_days=20,
        pca_stress_multiplier=1.50,
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

    store = ResearchStore()
    try:
        store.init_db()
        run_id = store.save_full_run(
            scenario_name=scenario_name,
            config=config,
            summary=summary,
            portfolio=portfolio,
            order_df=orders,
            latest_signal=latest_signal,
        )
    finally:
        store.close()

    return run_id



def main() -> None:
    st.set_page_config(page_title="Quant Research DB Dashboard v1.1", layout="wide")
    st.title("Quant Research DB Dashboard v1.1")
    st.caption("读取 SQLite 历史实验，并支持一键保存当前参数为新实验")

    with st.sidebar:
        st.header("数据库设置")
        limit = int(
            synced_numeric_parameter(
                "读取最近实验数量",
                "history_limit",
                min_value=5,
                max_value=100,
                value=20,
                step=5,
                number_format="%d",
            )
        )
        st.write(f"当前数据库文件：`{DB_PATH}`")

        st.header("新实验参数")
        scenario_name = st.text_input("Scenario Name", value="dashboard_manual_run")
        start_date = st.text_input("Start Date", value="2018-01-01")
        rebalance_frequency = st.selectbox("Rebalance Frequency", ["D", "W", "M"], index=2)
        top_n = int(
            synced_numeric_parameter(
                "Top N Assets",
                "top_n",
                min_value=1,
                max_value=6,
                value=3,
                step=1,
                number_format="%d",
            )
        )
        min_momentum_threshold = float(
            synced_numeric_parameter(
                "Min Momentum Threshold",
                "min_momentum_threshold",
                min_value=-0.10,
                max_value=0.20,
                value=0.00,
                step=0.01,
                number_format="%.2f",
            )
        )
        target_annual_vol = float(
            synced_numeric_parameter(
                "Target Annual Vol",
                "target_annual_vol",
                min_value=0.05,
                max_value=0.30,
                value=0.12,
                step=0.01,
                number_format="%.2f",
            )
        )
        max_asset_weight = float(
            synced_numeric_parameter(
                "Max Asset Weight",
                "max_asset_weight",
                min_value=0.10,
                max_value=1.00,
                value=0.40,
                step=0.05,
                number_format="%.2f",
            )
        )
        risk_off_cash_weight = float(
            synced_numeric_parameter(
                "Risk-Off Cash Weight",
                "risk_off_cash_weight",
                min_value=0.00,
                max_value=1.00,
                value=0.50,
                step=0.05,
                number_format="%.2f",
            )
        )
        vix_risk_off_threshold = float(
            synced_numeric_parameter(
                "VIX Risk-Off Threshold",
                "vix_risk_off_threshold",
                min_value=15.0,
                max_value=50.0,
                value=28.0,
                step=1.0,
                number_format="%.1f",
            )
        )
        vix_high_threshold = float(
            synced_numeric_parameter(
                "VIX High Threshold",
                "vix_high_threshold",
                min_value=12.0,
                max_value=40.0,
                value=22.0,
                step=1.0,
                number_format="%.1f",
            )
        )
        trading_cost_bps = float(
            synced_numeric_parameter(
                "Trading Cost (bps)",
                "trading_cost_bps",
                min_value=0.0,
                max_value=30.0,
                value=5.0,
                step=0.5,
                number_format="%.1f",
            )
        )
        slippage_bps = float(
            synced_numeric_parameter(
                "Slippage (bps)",
                "slippage_bps",
                min_value=0.0,
                max_value=30.0,
                value=2.0,
                step=0.5,
                number_format="%.1f",
            )
        )
        st.caption(
            "已准入风险模型：EWMA(20日半衰期) + PCA第一因子1.5倍压力。"
        )

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

        validation_errors = validate_experiment_parameters(
            history_limit=limit,
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
            slippage_bps=slippage_bps,
        )
        if validation_errors:
            st.error(
                "请修正以下参数：\n\n"
                + "\n".join(f"- {message}" for message in validation_errors)
            )

        if st.button(
            "保存当前参数为新实验",
            type="primary",
            width="stretch",
            disabled=bool(validation_errors),
        ):
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
                        slippage_bps=slippage_bps,
                    )
                    st.cache_data.clear()
                    st.success(f"保存成功，run_id = {run_id}")
                except Exception as exc:
                    st.error(f"保存失败：{exc}")

    effective_limit = limit if 5 <= limit <= 100 else 20
    try:
        runs = load_runs(effective_limit)
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
        "slippage_bps",
        "risk_model",
        "ewma_half_life_days",
        "pca_stress_multiplier",
        "created_at",
    ]
    config_snapshot = selected_row.get("config_json")
    if not isinstance(config_snapshot, dict):
        config_snapshot = {}
    param_df = pd.DataFrame(
        [
            {
                "Parameter": col,
                "Value": selected_row.get(col)
                if pd.notna(selected_row.get(col))
                else config_snapshot.get(col),
            }
            for col in param_cols
        ]
    )
    st.dataframe(param_df, use_container_width=True)

    try:
        portfolio, orders, signals = load_run_details(int(selected_run_id))
    except Exception as exc:
        st.error(f"读取 run 详情失败：{exc}")
        return

    if not portfolio.empty:
        portfolio = portfolio.copy()
        portfolio["date"] = pd.to_datetime(portfolio["date"])
        portfolio = portfolio.sort_values("date")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["净值曲线", "订单日志", "信号快照", "因子监控", "原始数据"]
    )

    with tab1:
        st.subheader("净值曲线")
        if portfolio.empty:
            st.info("该 run 没有 portfolio_daily 数据。")
        else:
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
        st.subheader("因子诊断与监控")
        st.info("当前为只读诊断层：不会修改策略信号、风险引擎或目标仓位。")
        if portfolio.empty:
            st.info("该 run 没有可用于因子归因的日收益数据。")
        else:
            try:
                monitor = load_factor_monitor(int(selected_run_id))
            except Exception as exc:
                st.warning(f"暂时无法生成因子监控：{exc}")
            else:
                summary = monitor.rolling_summary
                regression = monitor.static_regression
                residual_share = regression.variance_contribution.get(
                    "residual", float("nan")
                )
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("滚动 OOS R²", format_pct(summary["oos_r_squared"]))
                m2.metric(
                    "年化回归 Alpha",
                    format_pct(regression.coefficients["alpha"] * 252.0),
                )
                m3.metric("Alpha t 值", f"{regression.t_statistics['alpha']:.2f}")
                m4.metric("残差风险占比", format_pct(residual_share))
                m5.metric("归因观测数", str(summary["observations"]))

                if monitor.status == "normal":
                    st.success("当前因子暴露处于本次实验的历史正常区间。")
                else:
                    st.warning("当前监控状态：需要观察。")
                    for message in monitor.warnings:
                        st.write(f"- {message}")

                st.subheader("最新暴露与历史区间")
                st.dataframe(
                    monitor.exposure_table.reset_index(drop=True),
                    use_container_width=True,
                )

                st.subheader("最近两年滚动因子暴露")
                exposure_chart = monitor.rolling_attribution.exposures[
                    list(FACTOR_LABELS)
                ].rename(columns=FACTOR_LABELS)
                st.line_chart(exposure_chart.tail(504))

                component_labels = {
                    "cash": "现金基线",
                    "alpha": "回归 Alpha",
                    "residual": "回归残差",
                    **FACTOR_LABELS,
                }
                st.subheader("年化算术收益贡献")
                return_contribution = monitor.return_contribution.rename(
                    index=component_labels
                ).rename("贡献")
                st.bar_chart(return_contribution)

                st.subheader("收益波动风险贡献")
                risk_contribution = monitor.risk_contribution.rename(
                    index=component_labels
                ).rename("占比")
                st.dataframe(
                    risk_contribution.to_frame(), use_container_width=True
                )

                if abs(regression.t_statistics["alpha"]) < 1.96:
                    st.caption(
                        "当前 Alpha 未达到 |t| ≥ 1.96，不能视为统计显著的独立超额收益。"
                    )

    with tab5:
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
        "created_at",
    ]
    existing_compare_cols = [c for c in compare_cols if c in runs.columns]
    st.dataframe(runs[existing_compare_cols], use_container_width=True)


if __name__ == "__main__":
    main()
