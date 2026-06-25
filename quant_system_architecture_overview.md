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
│                        Presentation Layer                    │
│  Streamlit Dashboards                                        │
│  - streamlit_dashboard_v1.py                                 │
│  - streamlit_dashboard_v1_1.py                               │
│  - streamlit_dashboard_v1_2.py                               │
│  - streamlit_dashboard_db.py                                 │
│  - streamlit_dashboard_db_v1_1_save_experiment.py            │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                        Application Layer                     │
│  Entry Points / Services                                     │
│  - main.py                                                   │
│  - main_with_db.py                                           │
│  - services/signal_service.py                                │
│                                                              │
│  Responsibilities:                                           │
│  - Coordinate modules                                        │
│  - Run backtest                                              │
│  - Generate latest signal                                    │
│  - Save experiment results                                   │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                         Research Layer                       │
│  Strategy / Risk / Backtest / Report                         │
│  - strategy/regime.py                                        │
│  - strategy/momentum_rotation.py                             │
│  - risk/engine.py                                            │
│  - backtest/engine.py                                        │
│  - report/reporter.py                                        │
│  - utils/metrics.py                                          │
│                                                              │
│  Responsibilities:                                           │
│  - Detect market regime                                      │
│  - Rank tradable assets                                      │
│  - Build target portfolio                                    │
│  - Apply risk constraints                                    │
│  - Simulate portfolio evolution                              │
│  - Compute performance metrics                               │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                           Data Layer                         │
│  Market Data + Features                                      │
│  - data/loader.py  (cache-aware, Phase 5)                    │
│  - data/features.py                                          │
│                                                              │
│  Responsibilities:                                           │
│  - Read from market_data cache first                         │
│  - Download raw market data via yfinance if missing          │
│  - Build price frame                                         │
│  - Compute momentum / vol / MA / drawdown features           │
└──────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
┌───────────────────────────────┐   ┌──────────────────────────────────────────┐
│      External Data Source     │   │          Persistence Layer               │
│  yfinance / market prices     │   │  SQLAlchemy + Alembic                    │
│  (fallback when not cached)   │   │  storage/db.py      ← engine factory     │
│                               │   │  storage/schema.py  ← table definitions  │
│                               │   │  storage/store.py   ← ResearchStore      │
│                               │   │    (facade over all repositories)        │
│                               │   │  storage/repositories/                   │
│                               │   │    ├── experiment.py                     │
│                               │   │    ├── portfolio.py                      │
│                               │   │    ├── order.py                          │
│                               │   │    ├── signal.py                         │
│                               │   │    └── market_data.py  (Phase 5)         │
└───────────────────────────────┘   └──────────────────────────────────────────┘
                                                │
                                                ▼
                                  ┌──────────────────────────────┐
                                  │  SQLite / PostgreSQL / MySQL  │
                                  │  Tables:                      │
                                  │  - experiment_runs            │
                                  │  - portfolio_daily            │
                                  │  - portfolio_weights  (v2 新) │
                                  │  - orders                     │
                                  │  - signals                    │
                                  │  - market_data        (v2 新) │
                                  └──────────────────────────────┘
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
    └─ config/settings.py  (dataclass with full param set)

                │
                ▼
[3] Load market data  (cache-first, Phase 5)
    └─ data/loader.py
       ├─ Check market_data table via MarketDataRepository
       ├─ If coverage OK → return cached OHLCV
       └─ If missing / force_refresh → download from yfinance
                                     → write to market_data cache
                                     → return OHLCV

                │
                ▼
[4] Build research features
    └─ data/features.py
       - mom_20 / mom_60 / mom_120
       - vol_20
       - ma_50 / ma_200
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
       - asset scoring (momentum composite + inverse-vol)
       - top_n selection
       - defensive overlay (risk_off / bear_high_vol)

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
       Output:
       - portfolio DataFrame (equity, daily_return, regime,
         turnover, est_cost, w_* per-asset columns)
       - orders DataFrame

                │
                ▼
[9] Generate outputs
    ├─ report/reporter.py
    │  - summary metrics
    │  - latest allocation snapshot
    │  - equity curve
    └─ services/signal_service.py
       - current signal only (no full backtest)

                │
        ┌───────┴────────┐
        ▼                ▼
[10A] Display            [10B] Persist  (main_with_db.py only)
      Streamlit                ResearchStore.save_full_run()
      dashboards               ├─ ExperimentRepository  → experiment_runs
                               ├─ PortfolioRepository   → portfolio_daily
                               │                        → portfolio_weights
                               ├─ OrderRepository       → orders
                               └─ SignalRepository      → signals

                               │
                               ▼
[11] Query historical runs later
     └─ Streamlit dashboards read directly from DB
        via ResearchStore / repositories
```

---

## 3. 关键模块职责

### 3.1 `config/settings.py`
统一管理实验参数。

负责内容：
- 标的池（universe）
- benchmark / fear gauge
- 调仓频率（rebalance_frequency）
- 因子权重（mom_20 / 60 / 120 权重，低波动权重）
- risk-off 参数（防御仓位比例）
- target vol / 权重约束
- 交易成本（bps）
- `db_url`（数据库连接字符串，可切换后端）

作用：
- 参数与逻辑分离
- 方便回测实验和 dashboard 调参
- `db_url` 一行换掉即可从 SQLite 升到 PostgreSQL

---

### 3.2 `data/loader.py`
原始行情入口，Phase 5 后增加 cache-first 逻辑。

负责内容：
1. 根据 tickers 检查 `market_data` 表中的覆盖情况
2. 若 cache 命中 → 直接返回 OHLCV，不打 yfinance
3. 若 cache 缺失或 `force_refresh=True` → 从 yfinance 拉取 → 写入 cache
4. 兜底：cache 写入失败时降级为直接下载（不阻断主流程）

好处：
- 同一日期范围的回测完全可重现（不依赖 yfinance 实时可用性）
- 避免重复打 API

---

### 3.3 `data/features.py`
研究特征工厂。

当前输出：
- 短中长期动量（mom_20 / mom_60 / mom_120）
- 20 日波动率（年化）
- 50 / 200 日均线
- 相对 200 日均线偏离（drawdown_200）

作用：给 regime 和 strategy 提供统一输入。

---

### 3.4 `strategy/regime.py`
市场状态识别器。

当前逻辑依赖：
- benchmark vs 200DMA
- VIX threshold
- 相对 200DMA 的 drawdown

输出：`bull_trend` / `neutral` / `risk_off` / `bear_high_vol`

---

### 3.5 `strategy/momentum_rotation.py`
策略权重生成器。

负责内容：
- 对资产综合打分（动量 + 低波动合成）
- 选 top_n
- 根据 regime 做防御调整（risk_off 加现金，bear_high_vol 降权益）

---

### 3.6 `risk/engine.py`
风控硬约束层。

负责内容：
- 波动率目标控制（scale_to_target_vol）
- 单资产权重限制（enforce_weight_limits, 默认 40%）
- 权重归一化
- pre-trade 检查（sum ≈ 1.0，无负值）

---

### 3.7 `execution/broker.py`
模拟执行层（MockBroker）。

当前内容：
- 生成订单日志（side, weight_change, price, est_cost）
- 不进行真实下单

作用：让回测和未来实盘接口解耦。

---

### 3.8 `backtest/engine.py`
回测引擎。

负责内容：
- 逐日推进时间（从暖机期结束到 end_date）
- 在调仓日：计算目标权重 → 应用风控 → 生成 orders
- 每日：按当前权重累计收益，扣除交易成本
- 输出 portfolio DataFrame（含 w_* 每日持仓列）和 orders DataFrame

---

### 3.9 `report/reporter.py` + `utils/metrics.py`
结果解释层。

当前指标：
- Start / End Equity、Total Return、CAGR
- Annual Vol、Sharpe、Sortino
- Max Drawdown、Avg Turnover

---

### 3.10 `services/signal_service.py`
实时建议层。

用途：在不跑完整回测时快速生成最新权重建议，供 dashboard 展示或未来定时任务复用。

---

### 3.11 Persistence Layer（v2 重构）

#### `storage/db.py`
Engine 工厂。负责：
- 根据 `db_url` 创建 SQLAlchemy Engine（SQLite / PostgreSQL / MySQL）
- 启用 WAL 日志（SQLite）和外键约束
- 提供全局单例 engine

#### `storage/schema.py`
权威表定义（Alembic 从此处消费）。含：
- `experiment_runs`、`portfolio_daily`、`portfolio_weights`、`orders`、`signals`、`market_data`

#### `storage/store.py` — ResearchStore
统一门面（Facade），应用层唯一的持久化入口。

核心方法：
- `save_full_run(config, portfolio, orders, signal_date)` → `run_id`
  - 写 experiment_runs（config_json + config_hash + 汇总指标）
  - 写 portfolio_daily（每日净值 + regime + turnover）
  - 写 portfolio_weights（每日各资产权重，长格式）
  - 写 orders（调仓订单）
  - 写 signals（最新仓位快照）
- 提供查询接口（list_runs、get_run、…）

#### `storage/repositories/`
5 个后端无关的 Repository，各自负责单张表：

| Repository | 表 | 核心方法 |
|---|---|---|
| ExperimentRepository | experiment_runs | insert / list / get_by_id |
| PortfolioRepository | portfolio_daily + portfolio_weights | insert_daily / insert_weights / get_equity_curve / get_weights |
| OrderRepository | orders | insert / get_by_run |
| SignalRepository | signals | insert / get_latest |
| MarketDataRepository | market_data | upsert_bars / get_bars / coverage_check |

设计原则：
- 对外只暴露 pandas DataFrame，不把 SQL 泄漏到应用层
- 通用 upsert 逻辑（SQLite / PostgreSQL / MySQL native + 通用兜底）
- 批量写入防 bind-parameter 超限（每批 ≤ 900 行）

---

### 3.12 `alembic/` + `alembic.ini`
数据库 schema 版本管理。

负责内容：
- `alembic upgrade head` 建表或升级
- `alembic/versions/` 存储迁移脚本历史

作用：
- 换后端（SQLite → PostgreSQL）时只需改 URL 再 `upgrade head`
- 后续加字段无需手写 DDL

---

### 3.13 `scripts/migrate_legacy_to_v2.py`
一次性 ETL 脚本（不重复使用）。

用途：将旧 SQLiteStore 手写表中的历史数据迁移到 v2 schema。
注意：仅保留了旧 schema 有的字段；per-day weights 和 market_data 无法回填（旧版本未存储）。

---

### 3.14 Streamlit Dashboards

| 文件 | 版本特点 |
|---|---|
| `streamlit_dashboard_v1.py` | 基础展示版 |
| `streamlit_dashboard_v1_1.py` | 增加策略解释和更多参数 |
| `streamlit_dashboard_v1_2.py` | 增加双场景参数对比实验 |
| `streamlit_dashboard_db.py` | 直接从 DB 读取历史实验 |
| `streamlit_dashboard_db_v1_1_save_experiment.py` | 界面一键保存当前参数为新实验 |

---

## 4. 数据库 Schema

### `experiment_runs`
- PK: `id`
- `config_json`（完整参数快照）、`config_hash`（SHA-256，用于去重）
- 提升字段：start_date / end_date / benchmark / rebalance_frequency / top_n 等
- 汇总指标：start_equity / end_equity / total_return / cagr / annual_vol / sharpe / sortino / max_drawdown / avg_turnover
- 元数据：scenario_name / latest_signal_date / latest_regime / status / notes / tags

### `portfolio_daily`
- FK → experiment_runs（CASCADE DELETE）
- 一行 = 一个 run 的一天：date / equity / daily_return / regime / turnover / est_cost
- Unique: (run_id, date)

### `portfolio_weights`（v2 新增）
- FK → experiment_runs
- 长格式：(run_id, date, ticker, weight)
- Unique: (run_id, date, ticker)
- 作用：完整还原任意历史 run 的每日持仓

### `orders`
- FK → experiment_runs
- 调仓订单日志：order_date / ticker / side / weight_change / price / est_cost

### `signals`
- 最新仓位快照：(run_id, signal_date, ticker, weight, regime)

### `market_data`（v2 新增，Phase 5）
- 共享 OHLCV 缓存，PK: (ticker, date)
- 字段：open / high / low / close / volume / auto_adjusted / source / fetched_at
- Upsert：重新拉取同一根 bar 时原地更新

---

## 5. 当前系统已经具备的能力

### 5.1 研究能力
- ETF 轮动策略回测（2018 至今）
- 市场状态识别（4 种 regime）
- 参数实验与对比
- 策略解释与最新信号输出

### 5.2 工程能力
- 多层模块化结构（config / data / strategy / risk / backtest / report / storage）
- 后端无关持久化（SQLite / PostgreSQL / MySQL 切换只改 URL）
- Schema 版本管理（Alembic）
- 市场数据缓存（可重现、省 API 调用）
- 批量写入 + upsert 防冲突
- 单元测试（metrics / risk_engine / loader_cache）

### 5.3 产品能力
- Streamlit 可视化与参数面板
- 对比实验
- 历史实验管理（按 run_id 查询、对比）
- 从 dashboard 一键保存实验
- 每日持仓权重历史（portfolio_weights）

---

## 6. 当前系统边界

### 已做到
- 个人量化研究平台
- 可跑、可看、可调、可存、可重现

### 尚未做到
- 实盘 broker 接入（MockBroker only）
- 自动定时任务
- Walk-forward / out-of-sample 验证框架
- 多策略组合框架
- 机构级数据源
- 高频 / 低延迟架构
- 正式日志监控告警系统
- 完整测试覆盖率

---

## 7. 当前测试逻辑

### 7.1 单元测试
目录：`tests/`

现有测试：
- `test_metrics.py` — 指标计算准确性
- `test_risk_engine.py` — 权重归一化、裁剪、pre-trade 检查
- `test_loader_cache.py` — cache 命中 / 旁路逻辑（无网络依赖，Phase 5 新增）

### 7.2 研究测试（通过 dashboard 和回测）
- 参数对比与结果合理性
- regime 分布观察
- 是否过度持有 BIL
- 是否出现异常回测曲线

### 7.3 集成测试（非正式）
- 回测 → dashboard → 数据库 闭环
- dashboard 保存实验后能否按 run_id 查询
- 新旧 schema 数据是否迁移完整

---

## 8. 后续最自然的升级方向

### v1.x 研究强化
- run 标签 / 备注 / 收藏 / 删除废实验
- 数据库 dashboard 支持筛选与排序
- 自动跳转到刚生成的 run_id

### v2.x 工程强化
- Streamlit 参数实验自动批量跑
- Walk-forward / out-of-sample 验证
- 更丰富因子库
- 切换到 PostgreSQL（仅改 `db_url`）
- Broker API 接入（替换 MockBroker）
- 定时任务 / 自动信号推送

### v3.x 产品化
- 多策略组合
- 实时信号流水线
- 风险告警系统
- 用户权限 / 云部署

---

## 9. 一句话总结

这套系统当前是一套：

**基于 Python 的 ETF 动量轮动量化研究平台，具备 cache-first 数据获取、特征工程、市场状态识别、策略打分、风控、回测、信号生成、Streamlit 可视化，以及基于 SQLAlchemy + Alembic 的后端无关持久化层（experiment tracking、每日持仓权重历史、市场数据缓存）。**

它已从"量化脚本"阶段升级到"可重现的个人研究平台"阶段，并为未来切换到 PostgreSQL 和接入实盘 broker 打好了基础。
