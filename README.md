# 新能源接入承诺清算服务

面向区域电网多场站（风电、分布式光伏聚合商、储能站）同一馈线并发接入的场景，
实现**接入承诺申报、分阶段冻结清算、安全限发、可追溯修订、结算与可解释查询**的完整服务。
仅依赖 Python 3.11 标准库，持久化使用 SQLite 单文件，无需外部数据库或浏览器。

## 覆盖的业务规则

- **按计量点申报**：可用区间 `[min, max]`、上下爬坡速率、合同优先级 `priority_rank`（数字越小越优先保电）；
  储能额外声明充/放电最大功率，且一个时段只能处于 `DISCHARGE` / `CHARGE` / `IDLE` 之一，
  充放电不会同时占用馈线容量。
- **版本化网络边界**：馈线容量按 `ConstraintVersion(effective_from)` 生效；
  检修窗口可修订，命中时段容量取约束与检修残余容量的较小值。
- **三阶段冻结**：日前 `DAY_AHEAD` → 日内 `INTRADAY` → 实时 `REALTIME`，权威度递增且不可回退。
  每次清算先对全部输入（资源、申报、预测、生效约束、检修）做只读快照并计算 SHA-256 指纹，
  相同冻结输入的重复清算直接复用既有运行。
- **确定性、可解释的分配**：愿望出力先经爬坡夹取，再按馈线安全边界分配。
  越限时按约定顺序先限低优先级资源；同优先级内按**累计被限发电量公平注水**（历史被限越多越晚被限）。
  每条分配明细带 `Trace`（约束版本、检修、爬坡夹取、限发、反向压减等）。
  所有资源压到有效最小出力仍越限时，不编造可行解，登记 `SECURITY_INFEASIBLE` 争议。
- **可追溯修订**：预测迟到、申报修订、约束/检修换版、计划撤回、重复请求、计量更正
  都只生成 `RevisionRecord`（含原因码与载荷指纹），**不回改已冻结或已结算的结论**；
  新结论只能以更高阶段的新版本承诺出现（旧版本显式标记 `SUPERSEDED`）。
- **结算冻结**：时段结算后拒绝重新清算与撤回；撤回已结算时段只留痕并产生争议。
  结算依据权威阶段计划明细与最新计量，区分调度指令限发与场站欠发（容忍带外欠发冲减补偿）。
  结算后的计量更正只追加 `SettlementAdjustment`（补偿差额）与争议，原结算不变。
- **幂等**：所有写接口支持 `Idempotency-Key`；同键不同载荷返回 `IDEMPOTENCY_CONFLICT`。
- **重启恢复**：承诺写入发件箱（outbox），投递失败保留 `PENDING`；
  服务启动（及 `POST /recover`）自动继续投递尚未确认的承诺并留痕 `RECOVERED_ON_STARTUP`。
- **审计回放**：持久化完整冻结输入与初始累计台账，`GET /runs/{run_id}/replay`
  可离线重算并逐行核对分配结论与指纹。
- **场站 API**：`GET /explain?resource_code=&period_code=` 返回
  为何获配 / 被限发 / 进入争议（`why` + 全部 trace）、版本历史、最终电量、结算与补偿依据、调整与争议。

## 目录结构

```
src/grid_commitment/
  domain.py        # 领域模型、枚举、错误码、稳定指纹
  contracts.py     # 初始基础契约（稳定标识/去重）
  freeze.py        # 清算输入只读冻结与指纹
  engine.py        # 纯函数清算引擎（优先级+累计公平注水+爬坡+储能互斥+检修）
  settlement.py    # 结算、容忍带、计量更正调整
  repository.py    # SQLite 适配器（幂等表、版本表、发件箱、结算锁定）
  service.py       # 应用服务编排（唯一业务入口）
  serialization.py # 清算结果 JSON 序列化
  api.py           # 标准库 HTTP API
  server.py        # 服务启动入口
tests/             # 契约/引擎/服务集成/HTTP 端到端测试
demo.py            # 端到端业务场景演示
run_cli.py         # 基础契约冒烟
```

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py demo.py
```

## 端到端演示

```bash
PYTHONPATH=src python3 demo.py
```

覆盖：多资源申报 → 日前冻结清算（光伏因低优先级先被限、检修时段生效）→
重复请求幂等/去重 → 日内迟到预测 + 边界收紧换版 → 实时再冻结 →
确认/撤回 → 计量上报与结算 → 结算后计量更正追加调整 → explain 与修订台账。

## 启动 HTTP 服务

```bash
PYTHONPATH=src python3 -m grid_commitment.server --host 0.0.0.0 --port 8080 --db grid_commitment.db
```

### 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/resources` | 注册/更新资源（计量点、馈线、优先级、补偿费率、储能功率） |
| POST | `/declarations` | 申报可用区间/爬坡/储能模式（`revision_seq` 递增） |
| POST | `/forecasts` | 功率预测（`sequence` 递增；迟到只产生修订） |
| POST | `/constraints` | 馈线约束版本 |
| POST | `/maintenance` | 检修窗口（可修订） |
| POST | `/clearings` | 触发清算 `{stage, period_codes}`，返回冻结指纹、明细与争议 |
| POST | `/commitments/confirm` | 场站确认承诺 |
| POST | `/commitments/withdraw` | 撤回计划（已开始/已结算拒绝并留痕） |
| POST | `/meters` | 计量上报/更正（`sequence` 递增） |
| POST | `/settlements` | 时段结算并冻结 |
| POST | `/recover` | 手动触发未确认承诺恢复投递 |
| GET | `/explain?resource_code=&period_code=` | 可解释查询 |
| GET | `/runs/{run_id}/replay` | 冻结输入回放核对 |
| GET | `/settlements?period_code=` | 时段结算结果 |
| GET | `/disputes`、`/revisions` | 争议与修订台账（可按资源/时段过滤） |
| GET | `/health` | 健康检查 |

写接口均可携带请求头 `Idempotency-Key: <唯一键>`。
错误响应统一为 `{"error": {"code", "message", "details"}}`，
冲突类错误（幂等冲突、阶段回退、结算锁定、序号不符）返回 HTTP 409。

### 请求示例

```bash
curl -s localhost:8080/resources -H 'Content-Type: application/json' -d '{
  "resource_code":"PV-AGG","resource_type":"SOLAR",
  "meter_point_code":"MP-PV1","feeder_code":"FDR-A",
  "priority_rank":2,"compensation_rate":90.0}'

curl -s localhost:8080/clearings -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: da-2026-10-06' \
  -d '{"stage":"DAY_AHEAD","period_codes":["2026-10-06T00:00"]}'

curl -s 'localhost:8080/explain?resource_code=PV-AGG&period_code=2026-10-06T00:00'
```

## 命令行冒烟

```bash
python3 run_cli.py
```
