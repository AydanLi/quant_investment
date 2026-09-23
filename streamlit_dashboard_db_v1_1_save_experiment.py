from __future__ import annotations

from collections.abc import Mapping
from functools import partial
import re
from typing import Union

import pandas as pd
import streamlit as st
from sqlalchemy import select

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from data.trusted_loader import TrustedMarketDataLoader
from data.providers import FredRiskFreeProvider, ProviderError
from report.benchmarks import build_benchmark_returns
from report.reporter import ReportGenerator
from risk.engine import RiskEngine
from services.dashboard_display import equity_chart, eligible_comparison_runs, format_parameter_display_value, runtime_is_verified
from services.dashboard_i18n import localize_frame, translate, translate_warning
from services.experiment_validation import validate_experiment_parameters
from services.factor_monitor import FACTOR_LABELS, build_factor_monitor
from services.monte_carlo_monitor import build_monte_carlo_monitor
from services.signal_service import SignalService
from storage.schema import admission_runs, strategy_versions
from storage.store import ResearchStore
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


DB_PATH = "quant_research.db"
Numeric = Union[int, float]
EXPLORATORY_FREQUENCIES = frozenset({"D", "W"})
_DYNAMIC_FACTOR_LABEL = re.compile(
    r"dynamic_half_life_(?P<half_life>\d+)_stress_(?P<stress>\d+(?:\.\d+)?)"
)
_ADMITTED_DYNAMIC_COMBINATIONS = {
    (half_life, stress)
    for half_life in (20, 40, 60)
    for stress in (1.0, 1.5)
}


def frequency_governance_status(rebalance_frequency: str) -> str:
    return (
        "exploratory_only"
        if rebalance_frequency in EXPLORATORY_FREQUENCIES
        else "admission_candidate"
    )


def parse_admitted_dynamic_factor(
    results: object,
) -> dict[str, object] | None:
    """Return the selected dynamic-factor settings from an admission result."""
    if not isinstance(results, Mapping):
        return None
    if (
        results.get("core_strategy_frozen") is not True
        or results.get("baseline_model") != "sample"
        or results.get("candidate_count") != 6
    ):
        return None
    label = results.get("selected_admitted_candidate")
    if not isinstance(label, str):
        return None
    match = _DYNAMIC_FACTOR_LABEL.fullmatch(label)
    if match is None:
        return None
    half_life = int(match.group("half_life"))
    stress = float(match.group("stress"))
    if (half_life, stress) not in _ADMITTED_DYNAMIC_COMBINATIONS:
        return None
    evaluations = results.get("evaluations")
    if (
        not isinstance(evaluations, Mapping)
        or not isinstance(evaluations.get(label), Mapping)
        or evaluations[label].get("admitted") is not True
    ):
        return None
    return {
        "risk_model": "dynamic_factor",
        "ewma_half_life_days": half_life,
        "pca_stress_multiplier": stress,
        "admission_label": label,
    }


def dashboard_risk_model_options(
    admitted_dynamic_factor: Mapping[str, object] | None,
) -> dict[str, dict[str, object]]:
    baseline = Config()
    options = {
        "Sample covariance（基准）": {
            "risk_model": baseline.risk_model,
            "ewma_half_life_days": baseline.ewma_half_life_days,
            "pca_stress_multiplier": baseline.pca_stress_multiplier,
        }
    }
    if admitted_dynamic_factor is not None:
        label = str(admitted_dynamic_factor["admission_label"])
        options[f"Dynamic factor（已准入：{label}）"] = dict(
            admitted_dynamic_factor
        )
    return options


def load_admitted_dynamic_factor() -> dict[str, object] | None:
    """Load the latest dynamic model backed by an admitted, frozen version."""
    store = ResearchStore()
    try:
        statement = (
            select(admission_runs.c.results_json, strategy_versions.c.version)
            .select_from(
                admission_runs.join(
                    strategy_versions,
                    strategy_versions.c.version == admission_runs.c.strategy_version,
                )
            )
            .where(
                admission_runs.c.status == "admitted",
                strategy_versions.c.status == "frozen",
                admission_runs.c.runtime_hash == strategy_versions.c.runtime_hash,
            )
            .order_by(admission_runs.c.id.desc())
        )
        with store.engine.connect() as connection:
            results = connection.execute(statement).all()
        for result, version in results:
            try:
                manifest = store.governance.load_frozen_runtime(version)
            except ValueError:
                continue
            parsed = parse_admitted_dynamic_factor(result)
            if parsed is not None and manifest.config["risk_model"] == "dynamic_factor":
                return {**parsed, "strategy_version": version}
    finally:
        store.close()

    return None


def synced_numeric_parameter(
    label: str,
    key: str,
    *,
    min_value: Numeric,
    max_value: Numeric,
    value: Numeric,
    step: Numeric,
    number_format: str,
    language: str = "zh",
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
            translate("{label} slider", language, label=label),
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
            translate("{label} direct input", language, label=label),
            min_value=min_value,
            max_value=max_value,
            step=step,
            format=number_format,
            key=input_key,
            on_change=sync_from_input,
            label_visibility="collapsed",
        )
    return st.session_state[input_key]


def load_runs(limit: int) -> pd.DataFrame:
    store = ResearchStore()
    try:
        df = store.get_experiment_runs(limit)
        verified_versions = {}
        for version in df.get("strategy_version", pd.Series(dtype=object)).dropna().unique():
            try:
                verified_versions[version] = store.governance.load_frozen_runtime(version).runtime_hash
            except ValueError:
                verified_versions[version] = None
        if not df.empty:
            df["runtime_verified"] = df.apply(lambda row: bool(
                row.get("runtime_hash") and verified_versions.get(row.get("strategy_version")) == row.get("runtime_hash")), axis=1)
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


@st.cache_data(show_spinner=False)
def load_monte_carlo_monitor(run_id: int):
    store = ResearchStore()
    try:
        portfolio = store.get_run_portfolio(run_id)
    finally:
        store.close()
    return build_monte_carlo_monitor(
        portfolio,
        simulations=3000,
        horizon=252,
        block_length=20,
        seed=20260715 + int(run_id),
    )


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
    risk_model: str,
    ewma_half_life_days: int,
    pca_stress_multiplier: float,
    frozen_strategy_version: str | None = None,
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
        risk_model=risk_model,
        ewma_half_life_days=ewma_half_life_days,
        pca_stress_multiplier=pca_stress_multiplier,
    )
    if frozen_strategy_version is not None:
        store = ResearchStore(db_url=config.db_url)
        try:
            manifest = store.governance.load_frozen_runtime(frozen_strategy_version)
            frozen = manifest.to_config(db_url=config.db_url)
        finally:
            store.close()
        editable_fields = (
            "start_date", "rebalance_frequency", "top_n", "min_momentum_threshold",
            "target_annual_vol", "max_asset_weight", "risk_off_cash_weight",
            "vix_risk_off_threshold", "vix_high_threshold", "trading_cost_bps",
            "slippage_bps", "risk_model", "ewma_half_life_days", "pca_stress_multiplier",
        )
        changed = [field for field in editable_fields if getattr(config, field) != getattr(frozen, field)]
        if changed:
            raise ValueError("Frozen strategy fields cannot change in the dashboard: " + ", ".join(changed))
        config = frozen

    loader = TrustedMarketDataLoader(config)
    data = loader.load()

    fe = FeatureEngineer(data, config)
    prices = fe.make_price_frame()
    execution_prices = fe.make_open_frame().reindex(prices.index)
    raw_close_prices = fe.make_raw_close_frame().reindex(prices.index)
    median_dollar_volume = fe.make_median_dollar_volume_frame().reindex(prices.index)
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
        execution_prices=execution_prices,
        raw_close_prices=raw_close_prices,
        corporate_actions=fe.corporate_actions(),
        median_dollar_volume=median_dollar_volume,
    )
    results = bt.run()
    portfolio = results["portfolio"]
    orders = results["orders"]

    reporter = ReportGenerator(config)
    try:
        risk_free = FredRiskFreeProvider().fetch_daily_returns(
            config.start_date, config.end_date
        )
    except ProviderError:
        risk_free = None
    summary = reporter.summarize(
        portfolio,
        risk_free_returns=risk_free,
        risk_free_source="FRED DGS3MO",
        benchmark_returns=build_benchmark_returns(prices),
        orders=orders,
        asset_returns=returns,
    )

    signal_service = SignalService(config, loader=loader)
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
            dataset_snapshot_id=loader.dataset_snapshot_id,
            universe_version=config.universe_version,
            strategy_version=config.strategy_version,
        )
    finally:
        store.close()

    return run_id



def main() -> None:
    baseline_config = Config()
    if "dashboard_language" not in st.session_state:
        requested_language = st.query_params.get("lang", "zh")
        st.session_state["dashboard_language"] = (
            requested_language if requested_language in {"zh", "en"} else "zh"
        )

    def refresh_option_labels() -> None:
        # Streamlit keeps the selected display string when format_func changes.
        # Re-send the same canonical values to refresh their translated labels.
        for key in ("rebalance_frequency", "risk_model"):
            if key in st.session_state:
                st.session_state[key] = st.session_state[key]

    language = st.sidebar.selectbox(
        "Language / 语言",
        options=["zh", "en"],
        format_func=lambda value: {"zh": "中文", "en": "English"}[value],
        key="dashboard_language",
        on_change=refresh_option_labels,
    )
    # Keep refreshes and shared dashboard URLs in the selected language.
    if st.query_params.get("lang") != language:
        st.query_params["lang"] = language
    t = partial(translate, language=language)
    st.set_page_config(page_title=t("Quant Research DB Dashboard v1.1"), layout="wide")
    st.title(t("Quant Research DB Dashboard v1.1"))
    st.caption(
        t("正式研究入口：读取SQLite历史实验，并保存受治理标记约束的新实验。")
    )

    with st.sidebar:
        st.header(t("数据库设置"))
        limit = int(
            synced_numeric_parameter(
                t("读取最近实验数量"),
                "history_limit",
                min_value=5,
                max_value=100,
                value=20,
                step=5,
                number_format="%d",
                language=language,
            )
        )
        st.write(t("当前数据库文件：`{path}`", path=DB_PATH))

        st.header(t("新实验参数"))
        scenario_name = st.text_input(
            t("Scenario Name"), value="dashboard_manual_run", key="scenario_name"
        )
        start_date = st.text_input(
            t("Start Date"), value=baseline_config.start_date, key="start_date"
        )
        frequency_options = ["D", "W", "M"]
        if "rebalance_frequency" not in st.session_state:
            st.session_state["rebalance_frequency"] = baseline_config.rebalance_frequency
        rebalance_frequency = st.selectbox(
            t("Rebalance Frequency"),
            frequency_options,
            key="rebalance_frequency",
            format_func=lambda value: (
                f"{value} — {t('仅供探索')}"
                if value in EXPLORATORY_FREQUENCIES
                else f"{value} — {t('准入协议')}"
            ),
        )
        if frequency_governance_status(rebalance_frequency) == "exploratory_only":
            st.warning(
                t("EXPLORATORY_ONLY：日频/周频结果不得进入准入排名或称为正式候选。")
            )
        top_n = int(
            synced_numeric_parameter(
                t("Top N Assets"),
                "top_n",
                min_value=1,
                max_value=6,
                value=baseline_config.top_n,
                step=1,
                number_format="%d",
                language=language,
            )
        )
        min_momentum_threshold = float(
            synced_numeric_parameter(
                t("Min Momentum Threshold"),
                "min_momentum_threshold",
                min_value=-0.10,
                max_value=0.20,
                value=baseline_config.min_momentum_threshold,
                step=0.01,
                number_format="%.2f",
                language=language,
            )
        )
        target_annual_vol = float(
            synced_numeric_parameter(
                t("Target Annual Vol"),
                "target_annual_vol",
                min_value=0.05,
                max_value=0.30,
                value=baseline_config.target_annual_vol,
                step=0.01,
                number_format="%.2f",
                language=language,
            )
        )
        max_asset_weight = float(
            synced_numeric_parameter(
                t("Max Asset Weight"),
                "max_asset_weight",
                min_value=0.10,
                max_value=baseline_config.max_asset_weight,
                value=baseline_config.max_asset_weight,
                step=0.05,
                number_format="%.2f",
                language=language,
            )
        )
        risk_off_cash_weight = float(
            synced_numeric_parameter(
                t("Risk-Off Cash Weight"),
                "risk_off_cash_weight",
                min_value=0.00,
                max_value=1.00,
                value=baseline_config.risk_off_cash_weight,
                step=0.05,
                number_format="%.2f",
                language=language,
            )
        )
        vix_risk_off_threshold = float(
            synced_numeric_parameter(
                t("VIX Risk-Off Threshold"),
                "vix_risk_off_threshold",
                min_value=15.0,
                max_value=50.0,
                value=baseline_config.vix_risk_off_threshold,
                step=1.0,
                number_format="%.1f",
                language=language,
            )
        )
        vix_high_threshold = float(
            synced_numeric_parameter(
                t("VIX High Threshold"),
                "vix_high_threshold",
                min_value=12.0,
                max_value=40.0,
                value=baseline_config.vix_high_threshold,
                step=1.0,
                number_format="%.1f",
                language=language,
            )
        )
        trading_cost_bps = float(
            synced_numeric_parameter(
                t("Trading Cost (bps)"),
                "trading_cost_bps",
                min_value=0.0,
                max_value=30.0,
                value=baseline_config.trading_cost_bps,
                step=0.5,
                number_format="%.1f",
                language=language,
            )
        )
        slippage_bps = float(
            synced_numeric_parameter(
                t("Slippage (bps)"),
                "slippage_bps",
                min_value=0.0,
                max_value=30.0,
                value=baseline_config.slippage_bps,
                step=0.5,
                number_format="%.1f",
                language=language,
            )
        )
        admitted_dynamic_factor = load_admitted_dynamic_factor()
        risk_model_options = dashboard_risk_model_options(admitted_dynamic_factor)
        risk_model_label = st.selectbox(
            t("Risk Model"),
            options=list(risk_model_options),
            index=0,
            key="risk_model",
            format_func=lambda value: (
                t("Sample covariance（基准）")
                if value == "Sample covariance（基准）"
                else t(
                    "Dynamic factor（已准入：{label}）",
                    label=risk_model_options[value]["admission_label"],
                )
            ),
        )
        risk_model_settings = risk_model_options[risk_model_label]
        if admitted_dynamic_factor is None:
            st.caption(
                t(
                    "当前仅可选择sample covariance基准；数据库没有可验证的"
                    "dynamic_factor已准入记录。"
                )
            )
        else:
            st.caption(
                t(
                    "dynamic_factor选项来自已准入记录及对应的冻结策略版本；"
                    "未被记录选中的参数不会显示。"
                )
            )

        auto_name = (
            f"dashboard_{rebalance_frequency}"
            f"_top{top_n}"
            f"_mom{min_momentum_threshold:.2f}"
            f"_vol{target_annual_vol:.2f}"
            f"_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"
        )

        use_auto_name = st.checkbox(
            t("自动生成实验名称"), value=True, key="use_auto_name"
        )
        final_scenario_name = auto_name if use_auto_name else scenario_name
        st.caption(t("当前将保存为：{name}", name=final_scenario_name))

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
                t("请修正以下参数：\n\n")
                + "\n".join(
                    f"- {translate_warning(message, language)}"
                    for message in validation_errors
                )
            )

        if st.button(
            t("保存当前参数为新实验"),
            type="primary",
            width="stretch",
            disabled=bool(validation_errors),
            key="save_experiment",
        ):
            with st.spinner(t("正在回测并写入数据库...")):
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
                        risk_model=str(risk_model_settings["risk_model"]),
                        ewma_half_life_days=int(
                            risk_model_settings["ewma_half_life_days"]
                        ),
                        pca_stress_multiplier=float(
                            risk_model_settings["pca_stress_multiplier"]
                        ),
                        frozen_strategy_version=risk_model_settings.get("strategy_version"),
                    )
                    st.cache_data.clear()
                    st.success(t("保存成功，run_id = {run_id}", run_id=run_id))
                except Exception as exc:
                    st.error(t("保存失败：{error}", error=exc))

    effective_limit = limit if 5 <= limit <= 100 else 20
    try:
        runs = load_runs(effective_limit)
    except Exception as exc:
        st.error(t("读取数据库失败：{error}", error=exc))
        return

    if runs.empty:
        st.warning(
            t("数据库里还没有实验记录。请使用左侧按钮保存一条新实验。")
        )
        return

    st.subheader(t("最近实验记录"))
    st.dataframe(localize_frame(runs, language), width="stretch")

    run_id_list = runs["id"].tolist()
    selected_run_id = st.selectbox(
        t("选择 run_id"), options=run_id_list, index=0, key="selected_run_id"
    )
    selected_row = runs[runs["id"] == selected_run_id].iloc[0]

    st.subheader(t("实验摘要 · run_id={run_id}", run_id=selected_run_id))
    metric_snapshot = selected_row.get("summary_json")
    if not isinstance(metric_snapshot, dict):
        metric_snapshot = {}
    st.caption(t("结果状态：{status}；指标状态：{metric_status}；运行身份：{runtime_status}",
        status=t(str(selected_row.get("status", "LEGACY_UNVERIFIED"))),
        metric_status=t(str(metric_snapshot.get("Metric Status", "LEGACY_UNVERIFIED"))),
        runtime_status=t("VERIFIED" if runtime_is_verified(selected_row.get("runtime_verified")) else "LEGACY_UNVERIFIED")))
    if eligible_comparison_runs(runs[runs["id"] == selected_run_id]).empty:
        st.warning(t("该结果不满足有效比较条件：可能未准入、已失效、缺少运行身份或指标样本不足。"))
    if selected_row.get("invalidated_reason"):
        st.caption(str(selected_row["invalidated_reason"]))
    st.caption(t("收益为税前估计；卖出订单胜率不等同于完整往返交易胜率。当前投资池回溯不代表已消除幸存者偏差。"))
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(t("Scenario"), str(selected_row.get("scenario_name", "N/A")))
    c2.metric(t("CAGR"), format_pct(selected_row.get("cagr")))
    c3.metric(t("Sharpe"), f"{selected_row['sharpe']:.2f}" if pd.notna(selected_row.get("sharpe")) else "N/A")
    c4.metric(t("Sortino"), f"{selected_row['sortino']:.2f}" if pd.notna(selected_row.get("sortino")) else "N/A")
    c5.metric(t("Max Drawdown"), format_pct(selected_row.get("max_drawdown")))

    c6, c7, c8, c9, c10 = st.columns(5)
    c6.metric(t("Annual Vol"), format_pct(selected_row.get("annual_vol")))
    c7.metric(t("Avg Turnover"), f"{selected_row['avg_turnover']:.4f}" if pd.notna(selected_row.get("avg_turnover")) else "N/A")
    c8.metric(t("Rebalance"), str(selected_row.get("rebalance_frequency", "N/A")))
    c9.metric(t("Top N"), str(selected_row.get("top_n", "N/A")))
    c10.metric(t("Latest Regime"), str(selected_row.get("latest_regime", "N/A")))

    st.subheader(t("参数快照"))
    if metric_snapshot:
        with st.expander(t("完整指标与计算口径")):
            metrics_frame = pd.DataFrame([
                {"Parameter": t(key), "Value": format_parameter_display_value(value)}
                for key, value in metric_snapshot.items()])
            st.dataframe(localize_frame(metrics_frame, language), width="stretch")
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
                "Parameter": t(col),
                "Value": format_parameter_display_value(
                    selected_row.get(col)
                    if pd.notna(selected_row.get(col))
                    else config_snapshot.get(col)
                ),
            }
            for col in param_cols
        ]
    )
    st.dataframe(localize_frame(param_df, language), width="stretch")

    try:
        portfolio, orders, signals = load_run_details(int(selected_run_id))
    except Exception as exc:
        st.error(t("读取 run 详情失败：{error}", error=exc))
        return

    if not portfolio.empty:
        portfolio = portfolio.copy()
        portfolio["date"] = pd.to_datetime(portfolio["date"])
        portfolio = portfolio.sort_values("date")

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
        [
            t("净值曲线"),
            t("订单日志"),
            t("信号快照"),
            t("因子监控"),
            t("蒙特卡洛监控"),
            t("原始数据"),
        ]
    )

    with tab1:
        st.subheader(t("净值曲线"))
        if portfolio.empty:
            st.info(t("该 run 没有 portfolio_daily 数据。"))
        else:
            chart_df = equity_chart(portfolio, metric_snapshot)
            st.line_chart(localize_frame(chart_df, language))

            d1, d2, d3, d4 = st.columns(4)
            opening_nav = metric_snapshot.get("Start Equity") if metric_snapshot.get("Metric Schema Version") == 2 else None
            d1.metric(t("Start Equity"), f"${safe_float(opening_nav):,.2f}" if opening_nav is not None else "N/A")
            d2.metric(t("End Equity"), f"${safe_float(portfolio['equity'].iloc[-1]):,.2f}" if not portfolio.empty else "N/A")
            d3.metric(t("Rows"), str(len(portfolio)))
            d4.metric(t("Last Regime"), str(portfolio['regime'].iloc[-1]) if 'regime' in portfolio.columns and not portfolio.empty else "N/A")

            st.subheader(t("Regime 分布"))
            if "regime" in portfolio.columns:
                regime_counts = portfolio["regime"].value_counts().rename_axis("regime").reset_index(name="count")
                st.bar_chart(
                    localize_frame(regime_counts.set_index("regime"), language)
                )

    with tab2:
        st.subheader(t("订单日志"))
        if orders.empty:
            st.info(t("该 run 没有订单记录。"))
        else:
            st.dataframe(localize_frame(orders, language), width="stretch")

    with tab3:
        st.subheader(t("信号快照"))
        if signals.empty:
            st.info(t("该 run 没有 signals 数据。"))
        else:
            signals = signals.copy()
            st.dataframe(localize_frame(signals, language), width="stretch")
            if {"ticker", "weight"}.issubset(signals.columns):
                signal_chart = signals[["ticker", "weight"]].copy()
                signal_chart = signal_chart.set_index("ticker")
                st.bar_chart(localize_frame(signal_chart, language))

    with tab4:
        st.subheader(t("因子诊断与监控"))
        st.info(t("当前为只读诊断层：不会修改策略信号、风险引擎或目标仓位。"))
        if portfolio.empty:
            st.info(t("该 run 没有可用于因子归因的日收益数据。"))
        else:
            try:
                monitor = load_factor_monitor(int(selected_run_id))
            except Exception as exc:
                st.warning(t("暂时无法生成因子监控：{error}", error=exc))
            else:
                summary = monitor.rolling_summary
                regression = monitor.static_regression
                residual_share = regression.variance_contribution.get(
                    "residual", float("nan")
                )
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric(t("滚动 OOS R²"), format_pct(summary["oos_r_squared"]))
                m2.metric(
                    t("年化回归 Alpha"),
                    format_pct(regression.coefficients["alpha"] * 252.0),
                )
                m3.metric(t("Alpha t 值"), f"{regression.t_statistics['alpha']:.2f}")
                m4.metric(t("残差风险占比"), format_pct(residual_share))
                m5.metric(t("归因观测数"), str(summary["observations"]))

                if monitor.status == "normal":
                    st.success(t("当前因子暴露处于本次实验的历史正常区间。"))
                else:
                    st.warning(t("当前监控状态：需要观察。"))
                    for message in monitor.warnings:
                        st.write(f"- {translate_warning(message, language)}")

                st.subheader(t("最新暴露与历史区间"))
                st.dataframe(
                    localize_frame(
                        monitor.exposure_table.reset_index(drop=True),
                        language,
                        value_columns=("因子", "状态"),
                    ),
                    width="stretch",
                )

                st.subheader(t("最近两年滚动因子暴露"))
                exposure_chart = monitor.rolling_attribution.exposures[
                    list(FACTOR_LABELS)
                ].rename(
                    columns={key: t(value) for key, value in FACTOR_LABELS.items()}
                )
                st.line_chart(exposure_chart.tail(504))

                component_labels = {
                    "cash": t("现金基线"),
                    "alpha": t("回归 Alpha"),
                    "residual": t("回归残差"),
                    **{key: t(value) for key, value in FACTOR_LABELS.items()},
                }
                st.subheader(t("年化算术收益贡献"))
                return_contribution = monitor.return_contribution.rename(
                    index=component_labels
                ).rename(t("贡献"))
                st.bar_chart(return_contribution)

                st.subheader(t("收益波动风险贡献"))
                risk_contribution = monitor.risk_contribution.rename(
                    index=component_labels
                ).rename(t("占比"))
                st.dataframe(
                    risk_contribution.to_frame(), width="stretch"
                )

                if abs(regression.t_statistics["alpha"]) < 1.96:
                    st.caption(
                        t("当前 Alpha 未达到 |t| ≥ 1.96，不能视为统计显著的独立超额收益。")
                    )

    with tab5:
        st.subheader(t("蒙特卡洛尾部风险监控"))
        st.info(t("当前为只读诊断层：模拟结果不会修改策略信号、风控参数或目标仓位。"))
        if portfolio.empty:
            st.info(t("该 run 没有可用于蒙特卡洛监控的日收益数据。"))
        else:
            try:
                monte_carlo = load_monte_carlo_monitor(int(selected_run_id))
            except Exception as exc:
                st.warning(t("暂时无法生成蒙特卡洛监控：{error}", error=exc))
            else:
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric(
                    t("未来{horizon}日亏损概率", horizon=monte_carlo.horizon),
                    format_pct(monte_carlo.probability_of_loss),
                )
                c2.metric(
                    t("5%尾部最大回撤"),
                    format_pct(monte_carlo.tail_max_drawdown),
                )
                c3.metric(
                    t("中位最大回撤"),
                    format_pct(monte_carlo.median_max_drawdown),
                )
                c4.metric(
                    t("中位总收益"),
                    format_pct(monte_carlo.median_total_return),
                )
                c5.metric(t("中位 Sharpe"), f"{monte_carlo.median_sharpe:.2f}")

                if monte_carlo.status == "normal":
                    st.success(t("当前模拟尾部风险未触发观察阈值。"))
                else:
                    st.warning(t("当前蒙特卡洛状态：需要观察。"))
                    for message in monte_carlo.warnings:
                        st.write(f"- {translate_warning(message, language)}")

                st.caption(
                    t(
                        "使用 {observations} 个历史观测、{simulations} 条路径、"
                        "{block_length} 日区块；模拟中位换手 {median_turnover:.2f}，"
                        "中位估算成本 {median_cost:.2%}。",
                        observations=monte_carlo.observations,
                        simulations=monte_carlo.simulations,
                        block_length=monte_carlo.block_length,
                        median_turnover=monte_carlo.median_turnover,
                        median_cost=monte_carlo.median_cost,
                    )
                )

                st.subheader(t("净值路径分位"))
                st.line_chart(localize_frame(monte_carlo.equity_quantiles, language))

                st.subheader(t("模拟分布"))
                distribution_display = monte_carlo.distribution_table.copy()
                for column in ("5%", "中位数", "95%"):
                    distribution_display[column] = distribution_display[
                        column
                    ].astype(object)
                for row_index, row in distribution_display.iterrows():
                    for column in ("5%", "中位数", "95%"):
                        value = float(row[column])
                        distribution_display.at[row_index, column] = (
                            f"{value:.2%}"
                            if row["单位"] == "percent"
                            else f"{value:.3f}"
                        )
                st.dataframe(
                    localize_frame(
                        distribution_display.drop(columns="单位"),
                        language,
                        value_columns=("指标",),
                    ),
                    width="stretch",
                )

                st.subheader(t("区块长度敏感性"))
                sensitivity_display = monte_carlo.sensitivity_table.copy()
                for column in ("亏损概率", "5%尾部回撤", "中位总收益"):
                    sensitivity_display[column] = sensitivity_display[column].map(
                        lambda value: f"{value:.2%}"
                    )
                sensitivity_display["中位Sharpe"] = sensitivity_display[
                    "中位Sharpe"
                ].map(lambda value: f"{value:.3f}")
                st.dataframe(
                    localize_frame(sensitivity_display, language), width="stretch"
                )
                st.caption(
                    t(
                        "这里监控所选 run 自身的净收益分布；正式的同区间、"
                        "同成本基线比较继续使用研究准入脚本。"
                    )
                )

    with tab6:
        st.subheader(t("portfolio_daily"))
        st.dataframe(portfolio, width="stretch")
        st.subheader(t("orders"))
        st.dataframe(orders, width="stretch")
        st.subheader(t("signals"))
        st.dataframe(signals, width="stretch")

    st.subheader(t("实验横向比较"))
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
    comparison_runs = eligible_comparison_runs(runs)
    st.caption(t("仅比较已准入、运行身份有效且指标完整的实验；其余 {count} 条保留在历史记录中。",
                 count=len(runs) - len(comparison_runs)))
    st.dataframe(localize_frame(comparison_runs[existing_compare_cols], language), width="stretch")


if __name__ == "__main__":
    main()
