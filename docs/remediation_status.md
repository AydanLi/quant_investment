# 量化经济链路修复与验收

语言：中文 | [English](remediation_status_en.md)

本轮基线：`90fd792`，2026-09-22。本轮修改前实际运行 **283 项测试通过**。
本文记录软件修复和验收边界；测试通过不代表数据、策略或券商已准入。

## 缺陷与验证对应

| 问题 | 修改位置 | 验收证据 |
|---|---|---|
| S01 原始价格与公司行动 | `data/features.py`、`execution/accounting.py`、`backtest/ledger.py`、执行仓储 | `test_remediation_accounting.py`：应收、拆股、权益不随卖出消失、未来收盘不改变已执行订单、连续账户 |
| S02、S03 数据与日历 | `data/quality.py`、`data/features.py`、`backtest/engine.py` | `test_remediation_data.py`：异常 OHLCV、共同缺口、拒绝未知价格口径 |
| S04、S05、M03—M05 预算、停机、对账 | `execution/budget.py`、`risk/controls.py`、`services/paper_cycle.py`、执行仓储 | `test_remediation_execution.py`：满仓扣费缓冲、固定前收、停机升级、漂移、未知碎股、非有限估值 |
| S06 冻结身份 | `research/runtime.py`、治理/信号/实验仓储、paper CLI | `test_remediation_research.py`、`test_remediation_reporting.py`：经济参数、代码和依赖身份；每日新行情与研究数据身份分离 |
| S07、S08、M06 研究验证 | `research/core_evaluator.py`、`research/nested_walk_forward.py`、动态风险验收 | 研究协议测试、连续账户测试、首日 -20% 与外层数据扰动测试 |
| M07—M09 指标、原子保存、界面 | `report/reporter.py`、`storage/store.py`、正式 Dashboard | `test_remediation_reporting.py`、`test_dashboard_language.py`：完整覆盖、保存失败回滚、失效结果过滤、语言切换无副作用 |
| M10 镜像 | `scripts/optimize_mirrored_portfolio.py` | 离线完整 OHLCV 镜像计算与只读限制测试 |
| S09、M01、M02 就绪证据 | `scripts/check_readiness.py`、`research/paper_admission.py` | 只读就绪报告、持久化阶段时钟、回放与券商证据分离 |
| O01—O04 诊断和可维护性 | 全链路压力测试、研究诊断、指标口径 | `test_economic_chain_stress.py` 实际经过特征、策略、订单、账本和风险；消融及性能测量保持诊断用途 |

## 共同经济规则

- 总收益序列只用于信号；执行必须显式提供原始 Open，估值必须显式提供原始 Close。无隐式替代。
- 初始账户为现金；既有持仓由显式账户状态输入。净值 = 原始份额市值 + 已结算现金 + 交易结算净额 + 股息应收。
- 除息权益在开盘成交前确认。支付日期或来源未知时，应收保留且不能用于下单；不捏造支付日期。未知成本基础保留未知，安全卖出的已实现盈亏不伪造为零。
- 滑点和冲击进入成交价，佣金单独记账。整批预算和费用检查先于第一笔成交；重启只恢复未成交部分，已批准数量不可偷偷缩放。
- 开盘后配置目标的回测与盘前批准固定数量的 `REPLAY_OPEN` 是不同执行模型。相同成交输入使用同一记账函数，跳空时两种模型的数量并不保证相同。
- 日损失使用上一交易日正式收盘净值。缺前收禁止增险，仍评估可计算的回撤并允许具备可信持仓和行情的减险。停机原因独立保存，解除一项不解除其他项。
- 研究通过仅保存待批准结果；独立人工批准后才能加载冻结版本，参数、代码或依赖改变使旧批准失效。显式初始化账户才开始验证期。
- 未确认股息支付标记为 `PROVISIONAL_CASH_FLOWS`，不能进入有效排名。界面和报告均纳入已知期初净值；旧记录未知值不补造。
- 连续走步结果检验选参程序；不能将其称为最终固定参数版本的同等独立样本外收益。动态风险正式验收需要核心研究未触碰的数据，不足时保留 sample covariance。

## 迁移和恢复

使用增量版本 `6b2e1d9a4f30`。先运行一致备份，在独立副本升级并检查所有旧字段、行数、完整性和外键，然后升级工作数据库。

```powershell
.venv\Scripts\python.exe -m scripts.backup_database
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m scripts.check_readiness
```

旧快照、hash、裁决、实验不重写；旧支付日期、成本、前收和运行身份不补签。新增事件记录时间由数据库产生，旧事件的该字段保持 NULL。没有新事件和冻结身份时迁移可回退；已有新证据时，降级会拒绝删除它，应前向修复或从一致备份恢复并重放。

## 验证命令

```powershell
.venv\Scripts\python.exe -m pytest -q --basetemp .runtime\pytest-remediation
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m scripts.check_environment
.venv\Scripts\python.exe -m scripts.check_readiness
```

2026-09-22 最终实测：

| 检查 | 结果 |
|---|---|
| 全量回归与集成 | **368 passed / 148.95 秒**；修改前基线 283 项，本轮增加 85 项 |
| 运行环境 | Python 3.14.3、68 个锁定包一致；`pip check` 无损坏依赖 |
| 编译、差异与文档 | 编译通过；差异无空白错误；9 份文档本地链接有效 |
| 迁移演练和实际升级 | 一致副本演练后，工作数据库已升级到 `6b2e1d9a4f30` |
| 历史数据保留 | 28 张旧表、1,729,411 行的全部旧列内容逐表哈希一致；完整性通过、外键错误 0、schema drift 0 |
| 就绪检查 | 正确返回 `ready_for_live=false`；外部条件未被软件测试放行 |

升级前一致备份：`.runtime/remediation-before-production-upgrade-20260922.db`。验证日志和迁移、就绪证据分别保存在 `.runtime/integration-final-stable.log`、`.runtime/remediation-production-migration.json` 和 `.runtime/remediation-readiness-final.json`。

离线诊断采用 3,915 行合成输入：特征计算 3 次中位数 0.095 秒；基准和 6 个消融场景各 3.83–4.91 秒，Python 跟踪峰值内存 2.94–5.81 MB。记录在 `.runtime/remediation-performance.json`。这是本机合成规模的观测，不是生产吞吐量承诺，也没有形成策略收益或独立样本外证据。完整链路压力回归覆盖实际特征、信号、订单、账本和风控，以及 SQLite 并发竞争。

修改尚未提交。建议按共同接口与迁移、数据质量、公司行动账本、执行风控、冻结审批、研究验证、指标界面、诊断就绪分别审查和提交。

## 外部关卡

只读核对仍为 5 个 `BLOCKED` 快照；最新快照 5 的未裁决阻断问题为 706；`UV-001` 为 draft，历史宇宙完整性 false，策略、准入和成交履历为空。新的 OHLCV 和公司行动规则下必须重新裁决和重建快照，不能把旧 hash 自动视为新口径认证。

仍需原始供应商差异、公司行动及支付公告、历史候选池/清盘/合并记录、冻结后新样本，以及目标券商的真实 paper 订单和对账证据。前瞻期从对应阶段实际开始时间计时，并核对事件时间和实际入库时间；保留 365 天、12 次调仓、30 笔成交门槛。`REPLAY_OPEN` 始终不能证明券商成交质量或实盘就绪。
