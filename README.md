# Quant System v3

语言：中文 | [English](README_EN.md)

面向个人研究的 ETF 动量轮动平台。项目重点不是生成一条漂亮的历史净值，
而是让每个研究结论都能追溯到代码提交、不可变数据快照、ETF 宇宙版本、
预注册参数、成本假设和执行记录；任一关键证据缺失时，系统默认失败关闭
（fail closed）。

> 当前状态（2026-08-26）：工程链路已经实现并通过 270 项测试，但 5 个数据
> 快照仍全部为 `BLOCKED`，最新快照还有 706 个未裁决阻断问题；`UV-001`
> 为 `draft`，策略版本、准入运行和本地模拟成交均为 0。因此项目当前可以
> 做诊断和工程验证，不能声称策略已准入、broker paper 已验证或可以实盘。

## 文档入口

| 文档 | 用途 |
|---|---|
| [项目简介](PROJECT_OVERVIEW.md) | 项目定位、策略轮廓、成熟度和当前结论 |
| [架构说明](quant_system_architecture_overview.md) | 数据流、治理状态机、存储模型和执行边界 |
| [运行手册](docs/upgrade_v3_runbook.md) | 数据事故、准入、本地模拟、停机和恢复流程 |
| [个人研究运行档案](docs/personal_research_operating_profile.md) | 账户、税务、数据源、告警和历史宇宙约束 |
| [2026-07-28 历史审计](reports/quant_system_audit_2026-07-28.md) | 历史问题与修正记录，不代表当前准入状态 |

## 核心能力

- Tiingo ETF 原始日线/公司行动为主源，Yahoo 为 ETF 交叉校验；VIX 使用
  CBOE/FRED 路径。
- 原始数据、公司行动、供应商修订和使用结果均保存为可追溯的不可变快照。
- 数据质量裁决绑定具体快照、问题指纹、原始数据哈希、ticker、日期、证据和
  操作人；禁止整个 ticker 或整段历史通配放行。
- 月频 ETF 动量轮动、市场区制、波动率缩放和 sample covariance 风险模型。
- 持股数量与现金账本、T+1 原始开盘执行、自然权重漂移、T+1 结算和成本/冲击
  建模。
- 固定 135 个候选的嵌套扩展窗口准入；所有候选和失败结果必须持久化。
- SQLite 持久化的本地 `REPLAY_OPEN`：信号、人工批准、幂等成交、结算、对账、
  风险事故和 Pushover 重试。
- 只读因子归因、Monte Carlo、Robinhood 镜像和正式 Streamlit Dashboard。

## 能力边界

| 能力 | 当前状态 | 不能据此证明 |
|---|---|---|
| 双源数据与裁决 | 已实现；当前快照仍 `BLOCKED` | 数据已可用于准入 |
| 回测和 135 候选准入引擎 | 已实现、可恢复 | 已产生可信策略结论 |
| 本地 `REPLAY_OPEN` | 已实现并通过重启幂等测试 | 真实 bid/ask、限价成交或 7 bp 真实成本 |
| Pushover | 事故先落库、发送失败可重试 | 事故已对账或已恢复 |
| IBKR 适配器边界 | 默认断开、双开关保护 | broker paper 或实盘可用 |
| 税务与历史 ETF 宇宙 | 仅记录约束 | 税后绩效或无幸存者偏差 |
| 实时新闻与实时行情 | 未实现 | 盘中事件驱动交易能力 |

所有历史 ETF 结果必须标记为 `CURRENT_UNIVERSE_BACKCAST`，并保留
`historical_universe_integrity=false`。当前绩效口径为税前。

## 环境安装

项目验证环境为 Windows 和 CPython 3.14.3（见 `.python-version`）。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.lock
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints.lock
.\.venv\Scripts\python.exe -m scripts.check_environment
```

`requirements.txt` 固定 9 个直接运行依赖，`constraints.lock` 固定当前验证过的
68 个运行/测试依赖。依赖升级时必须同步更新根依赖和完整约束，并重跑全套验证。

## 数据库与凭据

运行数据库默认为 `sqlite:///quant_research.db`，由 Alembic 管理且不进入 Git。
SQLite 是当前唯一完成迁移、完整性、外键和重启恢复验证的后端；其他数据库 URL
属于未验证扩展，不应仅通过修改配置直接投入使用。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

真实凭据只能放在环境变量、忽略的本地 `.env` 或操作系统密钥存储中。仓库中的
`.env.example` 只能保留空值。开始无人值守任务前，应轮换任何曾在聊天、截图或
命令参数中暴露过的 Tiingo/Pushover 凭据。

数据源授权测试和完整快照构建：

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_data_sources
.\.venv\Scripts\python.exe -m scripts.build_trusted_snapshot
```

HTTP 429 或供应商失败不会静默切换主源。构建结果可以是 `BLOCKED`；这表示证据
已保存，不表示任务失败得可以忽略。裁决和派生快照流程见
[运行手册](docs/upgrade_v3_runbook.md)。

## 运行入口

### 正式 Dashboard

双击：

```text
Open Quant Dashboard.cmd
```

正式入口是 `streamlit_dashboard_db_v1_1_save_experiment.py`，默认使用 sample
covariance 和 35% 风险资产上限。只有数据库存在绑定冻结策略的正式风险模型准入
记录时，才会显示 `dynamic_factor`。日频/周频实验始终标记为
`exploratory_only`。

侧栏顶部的 **Language / 语言** 可在 **中文**（默认）和 **English** 之间切换。
界面标签、提示、诊断表格及图表随之切换；已输入参数和当前选中的实验保持不变。
所选语言通过地址中的 `?lang=zh` 或 `?lang=en` 保留，刷新或分享该地址仍使用同一语言。
原始数据页保留数据库字段名，用户输入、存储值和底层异常详情保持原样。

`streamlit_dashboard_db.py`、`main.py`、`main_with_db.py` 和 `DNU/` 仅作历史兼容
或审计，不是正式准入入口。

### Robinhood 只读镜像

双击：

```text
Open Robinhood Mirror.cmd
```

镜像只显示导入的持仓快照和诊断性 walk-forward 结果，不连接下单链路，也不授权
仓位变化。

## 研究准入

只有在存在可执行快照、人工批准的宇宙版本和冻结前研究协议时，才应运行以下流程：

```powershell
.\.venv\Scripts\python.exe -m scripts.approve_universe `
  --version <universe-version> --approved-by <operator>

$commit = git rev-parse HEAD
.\.venv\Scripts\python.exe -m scripts.create_research_protocol `
  --version <protocol-version> `
  --code-commit $commit `
  --dataset-snapshot-id <snapshot-id> `
  --universe-version <universe-version> `
  --output .runtime\core_protocol.json

.\.venv\Scripts\python.exe -m scripts.run_core_admission `
  --protocol .runtime\core_protocol.json `
  --strategy-version <strategy-version>
```

准入命令必须留下恰好 135 个最终候选结果。全部候选被拒绝是有效研究结论；不得
在查看结果后临时扩展参数网格。当前数据库尚无可执行快照，因此不应运行正式准入。

## 本地模拟

本地模拟仅在策略已准入、冻结并启动未来时钟后使用：

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_cycle `
  --strategy-version <strategy-version> --help
```

它使用 T+1 原始开盘价加预注册成本做确定性回放，并持久化决策、订单、成交、三类
现金、结算、对账和风险事故。它不是 broker paper；不能验证 09:35 限价单、盘口、
真实冲击成本或碎股路由。

## 验证

```powershell
.\.venv\Scripts\python.exe -m scripts.check_environment
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q backtest config data execution report research risk scripts services storage strategy tests utils
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check
```

2026-08-26 的当前提交验证结果为：270 项测试通过、依赖一致、SQLite
`integrity_check=ok`、外键检查为空、Alembic 位于 `5f74c1a9d2b0 (head)`。

## 目录结构

| 路径 | 职责 |
|---|---|
| `config/` | 策略、风险、执行模式和 ETF 宇宙默认值 |
| `data/` | 供应商、交易日历、复权、质量检查和可信加载 |
| `strategy/` | 动量轮动和市场区制 |
| `risk/` | 协方差、仓位约束、风险控制和敞口 |
| `backtest/` | T+1 回测引擎与数量/现金账本 |
| `research/` | 协议、嵌套 walk-forward、准入和诊断 |
| `services/` | 信号、Dashboard 视图、本地模拟和 Pushover |
| `execution/` | 预交易检查、OMS 和 broker 隔离边界 |
| `storage/` | SQLAlchemy 表结构和仓储 |
| `scripts/` | 唯一可审计的运维/研究 CLI |
| `tests/` | 单元、集成、迁移、前视、治理和重启测试 |

本项目仅供研究与软件工程验证，不构成投资、税务或法律建议。
