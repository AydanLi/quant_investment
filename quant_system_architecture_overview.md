# Quant System Architecture Overview

下面是当前量化研究平台的整体架构总览，包括：
- 系统分层图
- 数据流图
- 关键模块职责
- 当前版本边界
- 后续升级方向

---

## 1. 系统分层图

```text
┌──────────────────────────────────────────────────────────────┐
│                        Presentation Layer                   │
│  Streamlit Dashboards                                       │
│  - streamlit_dashboard_v1.py                               │
│  - streamlit_dashboard_v1_1.py                             │
│  - streamlit_dashboard_v1_2.py                             │
│  - streamlit_dashboard_db.py                               │
│  - streamlit_dashboard_db_v1_1_save_experiment.py          │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                        Application Layer                    │
│  Entry Points / Services                                    │
│  - main.py                                                  │
│  - main_with_db.py                                          │
│  - services/signal_service.py                               │
│                                                              │
│  Responsibilities:                                          │
│  - Coordinate modules                                       │
│  - Run backtest                                             │
│  - Generate latest signal                                   │
│  - Save experiment results                                  │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                         Research Layer                      │
│  Strategy / Risk / Backtest / Report                        │
│  - strategy/regime.py                                       │
│  - strategy/momentum_rotation.py                            │
│  - risk/engine.py                                           │
│  - backtest/engine.py                                       │
│  - report/reporter.py                                       │
│  - utils/metrics.py                                         │
│                                                              │
│  Responsibilities:                                          │
│  - Detect market regime                                     │
│  - Rank tradable assets                                     │
│  - Build target portfolio                                   │
│  - Apply risk constraints                                   │
│  - Simulate portfolio evolution                             │
│  - Compute performance metrics                              │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                           Data Layer                        │
│  Market Data + Features                                     │
│  - data/loader.py                                           │
│  - data/features.py                                         │
│                                                              │
│  Responsibilities:                                          │
│  - Download raw market data                                 │
│  - Build price frame                                        │
│  - Compute momentum / vol / MA / drawdown features          │
└──────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
┌───────────────────────────────┐   ┌──────────────────────────┐
│      External Data Source     │   │     Persistence Layer    │
│  yfinance / market prices     │   │  SQLite                  │
│                               │   │  - storage/sqlite_store.py│
│                               │   │  - quant_research.db     │
└───────────────────────────────┘   └──────────────────────────┘
                                                │
                                                ▼
                                  ┌──────────────────────────┐
                                  │  Stored Research Assets  │
                                  │  - experiment_runs       │
                                  │  - portfolio_daily       │
                                  │  - orders                │
                                  │  - signals               │
                                  └──────────────────────────┘
```

---

## 2. 数据流图

```text
[1] User starts run
    ├─ python main.py
    ├─ python main_with_db.py
    └─ Streamlit dashboard button click

                │
                ▼
[2] Load configuration
    └─ config/settings.py

                │
                ▼
[3] Download market data
    └─ data/loader.py -> yfinance

                │
                ▼
[4] Build research features
    └─ data/features.py
       - mom_20
       - mom_60
       - mom_120
       - vol_20
       - ma_50
       - ma_200
       - drawdown_200

                │
                ▼
[5] Detect market regime
    └─ strategy/regime.py
       Output:
       - bull_trend
       - neutral
       - risk_off
       - bear_high_vol

                │
                ▼
[6] Generate strategy weights
    └─ strategy/momentum_rotation.py
       - asset scoring
       - top_n selection
       - defensive overlay

                │
                ▼
[7] Apply risk controls
    └─ risk/engine.py
       - target vol scaling
       - max weight constraint
       - weight normalization
       - pre-trade checks

                │
                ▼
[8] Simulate portfolio / orders
    └─ backtest/engine.py
       + execution/broker.py (MockBroker)

                │
                ▼
[9] Generate outputs
    ├─ report/reporter.py
    │  - summary metrics
    │  - latest allocation snapshot
    │  - equity curve
    └─ services/signal_service.py
       - current signal only

                │
        ┌───────┴────────┐
        ▼                ▼
[10A] Display            [10B] Persist
      Streamlit                SQLite
      dashboards               storage/sqlite_store.py

                               │
                               ▼
[11] Query historical runs later
     └─ streamlit_dashboard_db.py
        streamlit_dashboard_db_v1_1_save_experiment.py
```

---

## 3. 关键模块职责

## 3.1 `config/settings.py`
统一管理实验参数。

负责内容：
- 标的池
- benchmark
- fear gauge
- 调仓频率
- 因子权重
- risk-off 参数
- target vol
- 权重约束
- 交易成本

作用：
- 参数与逻辑分离
- 方便回测实验
- 方便 dashboard 调参

---

## 3.2 `data/loader.py`
原始行情入口。

负责内容：
- 从 yfinance 拉历史数据
- 输出统一格式的 DataFrame

当前局限：
- 适合研究原型
- 不适合机构级生产环境

---

## 3.3 `data/features.py`
研究特征工厂。

当前输出：
- 短中长期动量
- 20日波动率
- 50/200日均线
- 相对 200 日均线偏离

作用：
- 给策略和 regime 提供输入

---

## 3.4 `strategy/regime.py`
市场状态识别器。

当前逻辑主要依赖：
- benchmark vs 200DMA
- VIX threshold
- 相对 200DMA 的 drawdown

输出：
- bull_trend
- neutral
- risk_off
- bear_high_vol

作用：
- 决定系统更进攻还是更防守

---

## 3.5 `strategy/momentum_rotation.py`
策略权重生成器。

负责内容：
- 对资产进行综合打分
- 选 top_n
- 根据 regime 做防御调整

当前核心思想：
- ETF 动量轮动
- 低波动辅助
- 风险环境下降低进攻仓位

---

## 3.6 `risk/engine.py`
风控硬约束层。

负责内容：
- 波动率目标控制
- 单资产权重限制
- 权重归一化
- pre-trade 检查

作用：
- 不让策略权重直接裸奔
- 防止组合失控

---

## 3.7 `execution/broker.py`
模拟执行层。

当前内容：
- MockBroker
- 生成订单日志
- 不进行真实下单

作用：
- 让回测和未来实盘接口解耦

---

## 3.8 `backtest/engine.py`
回测引擎。

负责内容：
- 逐日推进时间
- 在调仓日重建目标仓位
- 计算持仓收益
- 扣除交易成本
- 保存组合状态

输出：
- portfolio
- orders

作用：
- 把策略放到历史里实际演练

---

## 3.9 `report/reporter.py` + `utils/metrics.py`
结果解释层。

负责内容：
- 指标计算
- summary 生成
- 净值曲线
- 最新组合建议

当前指标：
- Start Equity
- End Equity
- Total Return
- CAGR
- Annual Vol
- Sharpe
- Sortino
- Max Drawdown
- Avg Turnover

---

## 3.10 `services/signal_service.py`
实时建议层。

负责内容：
- 在不跑完整研究流程时
- 快速生成最新权重建议

作用：
- dashboard 展示当前信号
- 未来定时任务可以直接复用

---

## 3.11 `storage/sqlite_store.py`
研究资产持久化层。

负责内容：
- 初始化数据库
- 存实验摘要
- 存每日净值
- 存订单
- 存信号
- 查历史 run

当前表：
- experiment_runs
- portfolio_daily
- orders
- signals

作用：
- 实验留痕
- 参数追踪
- 历史对比
- dashboard 直接读历史

---

## 3.12 Streamlit Dashboards

### `streamlit_dashboard_v1.py`
基础展示版。

### `streamlit_dashboard_v1_1.py`
增加策略解释和更多参数。

### `streamlit_dashboard_v1_2.py`
增加双场景参数对比实验。

### `streamlit_dashboard_db.py`
直接从 SQLite 读取历史实验。

### `streamlit_dashboard_db_v1_1_save_experiment.py`
可从界面直接保存当前参数为新实验。

作用：
- 让系统从脚本升级成研究工作台

---

## 4. 当前系统已经具备的能力

### 4.1 研究能力
- ETF 轮动策略回测
- 市场状态识别
- 参数实验
- 策略解释
- 最新信号输出

### 4.2 工程能力
- 多文件模块化结构
- 分层清晰
- 可扩展
- 基础单元测试
- 异常处理比最初版本更稳

### 4.3 产品能力
- Streamlit 可视化
- 参数面板
- 对比实验
- SQLite 历史实验管理
- 从 dashboard 一键保存实验

---

## 5. 当前系统边界

## 已做到
- 个人量化研究平台雏形
- 可跑、可看、可调、可存

## 尚未做到
- 实盘 broker 接入
- 自动定时任务
- 高频/低延迟架构
- 机构级数据源
- 更严谨的 walk-forward / OOS 流程
- 多策略组合框架
- 正式日志监控告警系统
- 更完整测试覆盖率

---

## 6. 当前测试逻辑

## 6.1 单元测试
目录：`tests/`

现有测试：
- `test_metrics.py`
- `test_risk_engine.py`

当前目的：
- 避免基础函数低级错误

## 6.2 研究测试
通过 dashboard 和回测进行：
- 参数对比
- 结果合理性检查
- regime 分布观察
- 是否过度持有 BIL
- 是否出现异常回测曲线

## 6.3 集成测试（非正式）
你现在实际上已经在做：
- 多文件复制后能否跑通
- 回测 → dashboard → 数据库 是否闭环
- dashboard 保存实验后是否能查询到 run_id

---

## 7. 后续最自然的升级方向

### v1.x 研究强化
- run 标签 / 备注 / 收藏
- 删除废实验
- 数据库 dashboard 支持筛选
- 自动跳转到刚生成的 run_id

### v2.x 工程强化
- Streamlit 参数实验自动批量跑
- walk-forward
- out-of-sample
- 更丰富因子
- 更高质量数据源
- PostgreSQL
- broker API
- 定时任务

### v3.x 产品化
- 多策略组合
- 实时信号流水线
- 风险告警
- 用户权限 / 云部署

---

## 8. 一句话总结

这套系统当前是一套：

**基于 Python 的 ETF 动量轮动量化研究平台，具备数据获取、特征工程、市场状态识别、策略打分、风控、回测、信号生成、Streamlit 可视化、SQLite 实验留痕与历史实验管理功能。**

它已经超过“量化脚本”阶段，进入“个人研究平台”阶段。

