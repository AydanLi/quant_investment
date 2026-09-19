"""Display-only English and Simplified Chinese text for the research dashboard.

Stored parameters, result frames, and free-form user content remain unchanged.
The source text is the catalog key; unfamiliar messages retain their original text.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

import pandas as pd


# Chinese source text already supplies the Chinese display; only its English
# counterpart is needed here. English labels have the inverse catalog below.
_EN = {
    "正式研究入口：读取SQLite历史实验，并保存受治理标记约束的新实验。":
        "Research dashboard: browse historical SQLite experiments and save new experiments under research governance controls.",
    "数据库设置": "Database settings",
    "读取最近实验数量": "Recent experiment limit",
    "当前数据库文件：`{path}`": "Current database: `{path}`",
    "新实验参数": "New experiment parameters",
    "仅供探索": "Exploratory only",
    "准入协议": "Admission protocol",
    "EXPLORATORY_ONLY：日频/周频结果不得进入准入排名或称为正式候选。":
        "EXPLORATORY_ONLY: daily and weekly results cannot enter admission rankings or be treated as formal candidates.",
    "Sample covariance（基准）": "Sample covariance (baseline)",
    "Dynamic factor（已准入：{label}）": "Dynamic factor (admitted: {label})",
    "当前仅可选择sample covariance基准；数据库没有可验证的dynamic_factor已准入记录。":
        "Only the sample covariance baseline is available; the database has no verifiable admitted dynamic_factor record.",
    "dynamic_factor选项来自已准入记录及对应的冻结策略版本；未被记录选中的参数不会显示。":
        "The dynamic_factor option comes from an admitted record and its frozen strategy version; only the selected parameters are shown.",
    "自动生成实验名称": "Generate experiment name automatically",
    "当前将保存为：{name}": "Will save as: {name}",
    "请修正以下参数：\n\n": "Please correct these parameters:\n\n",
    "保存当前参数为新实验": "Save current parameters as a new experiment",
    "正在回测并写入数据库...": "Running backtest and saving to the database...",
    "保存成功，run_id = {run_id}": "Saved successfully, run_id = {run_id}",
    "保存失败：{error}": "Save failed: {error}",
    "读取数据库失败：{error}": "Failed to read the database: {error}",
    "数据库里还没有实验记录。请使用左侧按钮保存一条新实验。":
        "There are no experiments in the database yet. Use the sidebar button to save a new experiment.",
    "最近实验记录": "Recent experiments",
    "选择 run_id": "Select run_id",
    "实验摘要 · run_id={run_id}": "Experiment summary · run_id={run_id}",
    "参数快照": "Parameter snapshot",
    "读取 run 详情失败：{error}": "Failed to read run details: {error}",
    "净值曲线": "Equity curve",
    "订单日志": "Order log",
    "信号快照": "Signal snapshot",
    "因子监控": "Factor monitor",
    "蒙特卡洛监控": "Monte Carlo monitor",
    "原始数据": "Raw data",
    "该 run 没有 portfolio_daily 数据。": "This run has no daily portfolio data.",
    "Regime 分布": "Regime distribution",
    "该 run 没有订单记录。": "This run has no order records.",
    "该 run 没有 signals 数据。": "This run has no signal data.",
    "因子诊断与监控": "Factor diagnostics and monitoring",
    "当前为只读诊断层：不会修改策略信号、风险引擎或目标仓位。":
        "Read-only diagnostics: strategy signals, the risk engine, and target positions are not changed.",
    "该 run 没有可用于因子归因的日收益数据。":
        "This run has no daily returns available for factor attribution.",
    "暂时无法生成因子监控：{error}": "Factor monitoring is currently unavailable: {error}",
    "滚动 OOS R²": "Rolling OOS R²",
    "年化回归 Alpha": "Annualized regression alpha",
    "Alpha t 值": "Alpha t-statistic",
    "残差风险占比": "Residual risk share",
    "归因观测数": "Attribution observations",
    "当前因子暴露处于本次实验的历史正常区间。":
        "Current factor exposures are within this experiment's normal historical range.",
    "当前监控状态：需要观察。": "Current monitor status: watch.",
    "最新暴露与历史区间": "Latest exposures and historical ranges",
    "最近两年滚动因子暴露": "Rolling factor exposures over the last two years",
    "现金基线": "Cash baseline",
    "回归 Alpha": "Regression alpha",
    "回归残差": "Regression residual",
    "年化算术收益贡献": "Annualized arithmetic return contribution",
    "贡献": "Contribution",
    "收益波动风险贡献": "Return variance risk contribution",
    "占比": "Share",
    "当前 Alpha 未达到 |t| ≥ 1.96，不能视为统计显著的独立超额收益。":
        "Alpha does not meet |t| ≥ 1.96 and cannot be treated as statistically significant independent excess return.",
    "蒙特卡洛尾部风险监控": "Monte Carlo tail risk monitor",
    "当前为只读诊断层：模拟结果不会修改策略信号、风控参数或目标仓位。":
        "Read-only diagnostics: simulations do not change strategy signals, risk parameters, or target positions.",
    "该 run 没有可用于蒙特卡洛监控的日收益数据。":
        "This run has no daily returns available for Monte Carlo monitoring.",
    "暂时无法生成蒙特卡洛监控：{error}": "Monte Carlo monitoring is currently unavailable: {error}",
    "未来{horizon}日亏损概率": "Loss probability over {horizon} days",
    "5%尾部最大回撤": "5th-percentile maximum drawdown",
    "中位最大回撤": "Median maximum drawdown",
    "中位总收益": "Median total return",
    "中位 Sharpe": "Median Sharpe",
    "当前模拟尾部风险未触发观察阈值。":
        "Simulated tail risk has not reached the watch thresholds.",
    "当前蒙特卡洛状态：需要观察。": "Current Monte Carlo status: watch.",
    "使用 {observations} 个历史观测、{simulations} 条路径、{block_length} 日区块；模拟中位换手 {median_turnover:.2f}，中位估算成本 {median_cost:.2%}。":
        "Based on {observations} historical observations, {simulations} paths, and {block_length}-day blocks; median simulated turnover {median_turnover:.2f}, median estimated cost {median_cost:.2%}.",
    "净值路径分位": "Equity path quantiles",
    "模拟分布": "Simulation distributions",
    "区块长度敏感性": "Block length sensitivity",
    "这里监控所选 run 自身的净收益分布；正式的同区间、同成本基线比较继续使用研究准入脚本。":
        "This monitors the selected run's own net return distribution; use the research admission scripts for formal baseline comparisons with matching periods and costs.",
    "实验横向比较": "Experiment comparison",
    "市场": "Equity market",
    "成长": "Growth",
    "规模": "Size",
    "久期": "Duration",
    "黄金": "Gold",
    "能源": "Energy",
    "防御": "Defensive",
    "因子": "Factor",
    "最新暴露": "Latest exposure",
    "历史10%": "Historical 10th percentile",
    "历史中位数": "Historical median",
    "历史90%": "Historical 90th percentile",
    "状态": "Status",
    "高于历史90%分位": "Above historical 90th percentile",
    "低于历史10%分位": "Below historical 10th percentile",
    "正常区间": "Normal range",
    "指标": "Metric",
    "中位数": "Median",
    "单位": "Unit",
    "总收益": "Total return",
    "最大回撤": "Maximum drawdown",
    "换手": "Turnover",
    "估算成本": "Estimated cost",
    "区块长度": "Block length",
    "亏损概率": "Loss probability",
    "5%尾部回撤": "5th-percentile drawdown",
    "中位Sharpe": "Median Sharpe",
    "5%路径": "5th-percentile path",
    "中位路径": "Median path",
    "95%路径": "95th-percentile path",
    "交易日": "Trading day",
    "Scenario Name 不能为空。": "Scenario Name cannot be empty.",
    "Scenario Name 不能超过 200 个字符。": "Scenario Name cannot exceed 200 characters.",
    "Start Date 必须使用 YYYY-MM-DD 格式。": "Start Date must use YYYY-MM-DD format.",
    "Start Date 不能晚于今天。": "Start Date cannot be later than today.",
    "Rebalance Frequency 必须是 D、W 或 M。": "Rebalance Frequency must be D, W, or M.",
    "读取最近实验数量必须是整数。": "Recent experiment limit must be an integer.",
    "Top N Assets 必须是整数。": "Top N Assets must be an integer.",
    "VIX High Threshold 必须小于 VIX Risk-Off Threshold。":
        "VIX High Threshold must be less than VIX Risk-Off Threshold.",
    "{label} 必须是数字。": "{label} must be a number.",
    "{label} 不能是 NaN 或无穷值。": "{label} cannot be NaN or infinite.",
    "{label} 必须在 {minimum} 到 {maximum} 之间。": "{label} must be between {minimum} and {maximum}.",
    "{factor}暴露 {value} 高于本次实验的历史90%分位。":
        "{factor} exposure {value} is above this experiment's historical 90th percentile.",
    "{factor}暴露 {value} 低于本次实验的历史10%分位。":
        "{factor} exposure {value} is below this experiment's historical 10th percentile.",
    "滚动样本外解释力低于40%，当前归因结果应谨慎使用。":
        "Rolling out-of-sample explanatory power is below 40%; use the current attribution results with caution.",
    "代理因子存在较强共线性，单个暴露系数可能不稳定。":
        "Proxy factors have strong collinearity; individual exposure coefficients may be unstable.",
    "成本后回归Alpha显著为负，需要检查换手和择时损耗。":
        "Regression alpha after costs is significantly negative; review turnover and timing losses.",
    "该 run 缺少换手数据，换手分布按0处理。":
        "This run has no turnover data; the turnover distribution is treated as zero.",
    "该 run 缺少成本数据，成本分布按0处理。":
        "This run has no cost data; the cost distribution is treated as zero.",
    "历史数据少于252个交易日，年度尾部估计可信度有限。":
        "Fewer than 252 trading days of history are available; annual tail estimates have limited reliability.",
    "未来{horizon}日模拟亏损概率达到 {probability}。":
        "Simulated loss probability over {horizon} days has reached {probability}.",
    "5%尾部路径最大回撤达到 {drawdown}。":
        "The 5th-percentile path maximum drawdown has reached {drawdown}.",
    "模拟中位总收益不为正，需要继续观察。":
        "Median simulated total return is not positive; continued monitoring is needed.",
    "区块长度变化会改变中位收益方向，结果对参数敏感。":
        "Changing block length changes the sign of median returns; results are sensitive to this parameter.",
    "存在换手但没有记录成本，成本后结果可能被高估。":
        "Turnover exists without recorded costs; results after costs may be overstated.",
}

_ZH = {
    "Quant Research DB Dashboard v1.1": "量化研究数据库仪表盘 v1.1",
    "Scenario Name": "实验名称",
    "Start Date": "开始日期",
    "End Date": "结束日期",
    "Rebalance Frequency": "调仓频率",
    "Top N Assets": "入选资产数量",
    "Min Momentum Threshold": "最低动量阈值",
    "Target Annual Vol": "目标年化波动率",
    "Max Asset Weight": "单资产最大权重",
    "Risk-Off Cash Weight": "避险现金权重",
    "VIX Risk-Off Threshold": "VIX 避险阈值",
    "VIX High Threshold": "VIX 高位阈值",
    "Trading Cost (bps)": "交易成本（基点）",
    "Slippage (bps)": "滑点（基点）",
    "Risk Model": "风险模型",
    "Sample covariance（基准）": "样本协方差（基准）",
    "Dynamic factor（已准入：{label}）": "动态因子（已准入：{label}）",
    "{label} slider": "{label} 滑块",
    "{label} direct input": "{label} 直接输入",
    "Scenario": "实验",
    "CAGR": "复合年化收益率",
    "Sharpe": "夏普比率",
    "Sortino": "索提诺比率",
    "Max Drawdown": "最大回撤",
    "Annual Vol": "年化波动率",
    "Avg Turnover": "平均换手率",
    "Rebalance": "调仓频率",
    "Top N": "入选数量",
    "Latest Regime": "最新市场状态",
    "Parameter": "参数",
    "Value": "数值",
    "Start Equity": "期初净值",
    "End Equity": "期末净值",
    "Rows": "记录数",
    "Last Regime": "最近市场状态",
    "Regime 分布": "市场状态分布",
    "portfolio_daily": "每日投资组合（portfolio_daily）",
    "orders": "订单（orders）",
    "signals": "信号（signals）",
    "Scenario Name 不能为空。": "实验名称不能为空。",
    "Scenario Name 不能超过 200 个字符。": "实验名称不能超过 200 个字符。",
    "Start Date 必须使用 YYYY-MM-DD 格式。": "开始日期必须使用 YYYY-MM-DD 格式。",
    "Start Date 不能晚于今天。": "开始日期不能晚于今天。",
    "Rebalance Frequency 必须是 D、W 或 M。": "调仓频率必须是 D（日频）、W（周频）或 M（月频）。",
    "Top N Assets 必须是整数。": "入选资产数量必须是整数。",
    "VIX High Threshold 必须小于 VIX Risk-Off Threshold。": "VIX 高位阈值必须小于 VIX 避险阈值。",
}

# Database keys are translated for display only, never changed in result frames
# used by calculations or persistence. The labels share the UI terminology.
_COLUMN_LABELS = {
    "id": ("ID", "编号"),
    "run_id": ("Run ID", "实验编号"),
    "scenario_name": ("Scenario Name", "实验名称"),
    "created_at": ("Created At", "创建时间"),
    "start_date": ("Start Date", "开始日期"),
    "end_date": ("End Date", "结束日期"),
    "benchmark": ("Benchmark", "基准"),
    "rebalance_frequency": ("Rebalance Frequency", "调仓频率"),
    "top_n": ("Top N Assets", "入选资产数量"),
    "min_momentum_threshold": ("Min Momentum Threshold", "最低动量阈值"),
    "target_annual_vol": ("Target Annual Vol", "目标年化波动率"),
    "max_asset_weight": ("Max Asset Weight", "单资产最大权重"),
    "risk_off_cash_weight": ("Risk-Off Cash Weight", "避险现金权重"),
    "vix_risk_off_threshold": ("VIX Risk-Off Threshold", "VIX 避险阈值"),
    "vix_high_threshold": ("VIX High Threshold", "VIX 高位阈值"),
    "trading_cost_bps": ("Trading Cost (bps)", "交易成本（基点）"),
    "slippage_bps": ("Slippage (bps)", "滑点（基点）"),
    "risk_model": ("Risk Model", "风险模型"),
    "ewma_half_life_days": ("EWMA Half-Life (days)", "EWMA 半衰期（天）"),
    "pca_stress_multiplier": ("PCA Stress Multiplier", "PCA 压力乘数"),
    "start_equity": ("Start Equity", "期初净值"),
    "end_equity": ("End Equity", "期末净值"),
    "total_return": ("Total Return", "总收益"),
    "cagr": ("CAGR", "复合年化收益率"),
    "annual_vol": ("Annual Vol", "年化波动率"),
    "sharpe": ("Sharpe", "夏普比率"),
    "sortino": ("Sortino", "索提诺比率"),
    "max_drawdown": ("Max Drawdown", "最大回撤"),
    "avg_turnover": ("Avg Turnover", "平均换手率"),
    "latest_signal_date": ("Latest Signal Date", "最新信号日期"),
    "latest_regime": ("Latest Regime", "最新市场状态"),
    "status": ("Status", "状态"),
    "notes": ("Notes", "备注"),
    "tags": ("Tags", "标签"),
    "dataset_snapshot_id": ("Dataset Snapshot ID", "数据集快照编号"),
    "universe_version": ("Universe Version", "资产池版本"),
    "strategy_version": ("Strategy Version", "策略版本"),
    "admissible": ("Admissible", "可准入"),
    "invalidated_reason": ("Invalidation Reason", "失效原因"),
    "config_json": ("Configuration Snapshot", "配置快照"),
    "config_hash": ("Configuration Hash", "配置哈希"),
    "date": ("Date", "日期"),
    "equity": ("Equity", "净值"),
    "gross_return": ("Gross Return", "成本前收益"),
    "daily_return": ("Daily Return", "日收益"),
    "regime": ("Regime", "市场状态"),
    "count": ("Count", "数量"),
    "turnover": ("Turnover", "换手率"),
    "est_trading_cost": ("Estimated Trading Cost", "估算交易成本"),
    "est_slippage": ("Estimated Slippage", "估算滑点"),
    "est_impact": ("Estimated Market Impact", "估算市场冲击"),
    "est_cost": ("Estimated Cost", "估算成本"),
    "cost_dollars": ("Cost ($)", "成本（美元）"),
    "cash": ("Cash", "现金"),
    "settled_cash": ("Settled Cash", "已结算现金"),
    "unsettled_cash": ("Unsettled Cash", "未结算现金"),
    "drawdown": ("Drawdown", "回撤"),
    "high_water": ("High Water Mark", "净值高水位"),
    "risk_status": ("Risk Status", "风险状态"),
    "stop_triggered": ("Stop Triggered", "止损已触发"),
    "maximum_adv_fraction": ("Maximum ADV Fraction", "最大日均成交额占比"),
    "order_date": ("Order Date", "订单日期"),
    "signal_date": ("Signal Date", "信号日期"),
    "ticker": ("Ticker", "证券代码"),
    "side": ("Side", "买卖方向"),
    "weight": ("Weight", "权重"),
    "weight_change": ("Weight Change", "权重变动"),
    "quantity": ("Quantity", "数量"),
    "notional": ("Notional", "名义金额"),
    "price": ("Price", "价格"),
    "trading_cost_dollars": ("Trading Cost ($)", "交易成本（美元）"),
    "slippage_dollars": ("Slippage ($)", "滑点（美元）"),
    "impact_cost_dollars": ("Market Impact Cost ($)", "市场冲击成本（美元）"),
    "adv_fraction": ("ADV Fraction", "日均成交额占比"),
    "average_entry_cost": ("Average Entry Cost", "平均建仓成本"),
    "gross_realized_pnl": ("Gross Realized P&L", "成本前已实现盈亏"),
    "realized_pnl": ("Realized P&L", "已实现盈亏"),
    "momentum": ("Momentum", "动量"),
    "volatility": ("Volatility", "波动率"),
    "score": ("Score", "评分"),
}


def translate(message: str, language: str, **values: object) -> str:
    """Translate a catalog key; format named values only for recognized text."""
    if language not in {"en", "zh"}:
        raise ValueError(f"Unsupported dashboard language: {language!r}")
    if message in _COLUMN_LABELS:
        text = _COLUMN_LABELS[message][0 if language == "en" else 1]
    elif message in _EN or message in _ZH:
        text = (_EN if language == "en" else _ZH).get(message, message)
    else:
        return message
    return text.format(**values) if values else text


_NUMBER_LABELS = (
    "读取最近实验数量", "Top N Assets", "Min Momentum Threshold",
    "Target Annual Vol", "Max Asset Weight", "Risk-Off Cash Weight",
    "VIX Risk-Off Threshold", "VIX High Threshold", "Trading Cost (bps)",
    "Slippage (bps)",
)
_LABEL_PATTERN = "(?P<label>" + "|".join(map(re.escape, _NUMBER_LABELS)) + ")"
_NUMBER_PATTERN = r"-?\d+(?:\.\d+)?"
_FACTOR_PATTERN = r"(?P<factor>市场|成长|规模|久期|黄金|能源|防御)"
_WARNING_PATTERNS = (
    (re.compile(_LABEL_PATTERN + r" 必须是数字。"), "{label} 必须是数字。"),
    (re.compile(_LABEL_PATTERN + r" 不能是 NaN 或无穷值。"), "{label} 不能是 NaN 或无穷值。"),
    (re.compile(_LABEL_PATTERN + rf" 必须在 (?P<minimum>{_NUMBER_PATTERN}) 到 (?P<maximum>{_NUMBER_PATTERN}) 之间。"),
     "{label} 必须在 {minimum} 到 {maximum} 之间。"),
    (re.compile(_FACTOR_PATTERN + rf"暴露 (?P<value>{_NUMBER_PATTERN}) 高于本次实验的历史90%分位。"),
     "{factor}暴露 {value} 高于本次实验的历史90%分位。"),
    (re.compile(_FACTOR_PATTERN + rf"暴露 (?P<value>{_NUMBER_PATTERN}) 低于本次实验的历史10%分位。"),
     "{factor}暴露 {value} 低于本次实验的历史10%分位。"),
    (re.compile(rf"未来(?P<horizon>\d+)日模拟亏损概率达到 (?P<probability>{_NUMBER_PATTERN}%)。"),
     "未来{horizon}日模拟亏损概率达到 {probability}。"),
    (re.compile(rf"5%尾部路径最大回撤达到 (?P<drawdown>{_NUMBER_PATTERN}%)。"),
     "5%尾部路径最大回撤达到 {drawdown}。"),
)


def translate_warning(message: str, language: str) -> str:
    """Translate known backend warnings without rewriting arbitrary error data."""
    for pattern, template in _WARNING_PATTERNS:
        match = pattern.fullmatch(message)
        if match is not None:
            values = match.groupdict()
            for key in ("label", "factor"):
                if key in values:
                    values[key] = translate(values[key], language)
            return translate(template, language, **values)
    return translate(message, language)


def localize_frame(
    frame: pd.DataFrame,
    language: str,
    *,
    value_columns: Iterable[str] = (),
) -> pd.DataFrame:
    """Copy a display frame and translate labels plus explicitly selected cells.

    ``value_columns`` uses original column names. Index values and all other
    cells remain untouched so identifiers and user experiment names are safe.
    """
    def label(value: object) -> object:
        return translate(value, language) if isinstance(value, str) else value

    result = frame.copy(deep=True)
    for column in value_columns:
        if column in result.columns:
            result[column] = result[column].map(label)
    result = result.rename(columns=label)
    result.index.names = [label(name) for name in result.index.names]
    result.columns.names = [label(name) for name in result.columns.names]
    return result
