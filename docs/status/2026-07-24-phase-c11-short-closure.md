# Phase C1.1：横向组件边界短收口状态

日期：2026-07-24  
冻结 HEAD：`4a05763b593a527878a0056f64763126c58ee63b`  
状态：`PASS_FAIL_CLOSED`  
真实 LLM / Vitis 消耗：`0 / 0`

## 结论

C1.1 已完成安全白名单、标签分层、Cost Scope、Provenance 分池和
off/shadow 行为等价测试。C2 的固定候选阈值没有调整。

Experience Shadow **未通过“完全不改变 Planner 输入和主路径身份”Gate**：

- 固定 Token 策略下，off 与 shadow 的 Provider Request 和
  Dispatch Context 相同；
- 但 Planner fingerprint 不同，shadow 还会写 Recommendation 审计副作用；
- 动态 Token 策略下，shadow 会参与 guidance Token 预留，实际改变
  TokenEnvelope、Dispatch Context 和 Provider Request；
- Ranker V2 没有被主 Graph、Planner、CLI 或 Batch Runtime 导入，
  因此当前只具备“未接入所以不干扰”的离线属性。

因此正式 28 题矩阵采用：

```text
Continuation = off
Experience = off
Ranker runtime = off
```

Experience 与 Ranker 均移到矩阵完成后的固定协议离线 replay / reevaluation。

## 安全白名单

`v3e.experience-query.v2` 顶层字段和 `structure_features` 均改为显式
allow-list。以下内容不能进入 Query：

- validation / performance / cost / token_policy；
- promoted / rejected / fresh_final_status；
- strict_improvement / acceleration；
- Token、Credit、wall time 等结果成本；
- hidden/reference/golden/answer、Secret 和本地绝对路径。

嵌套 `structure_features` 原先只检查“必须是对象”，现在也会拒绝非白名单字段和
不安全文本。

## 标签分层

| 层 | 用途 | 是否可进入当前 Query |
| --- | --- | --- |
| L0 | 当前决策前公开结构特征 | 是，仅白名单 |
| L1 | 历史 Support 的 validation/performance/cost | 否，仅历史终态 |
| L2 | Held-out evaluation labels | 否，仅离线评分 |
| L3 | 当前运行 terminal attribution | 否，终态后生成 |

## Cost Scope

- Query：不允许 Cost 字段；
- 历史 Support：可在决策后用于固定效用统计；
- Held-out：Cost 只用于评估，不进入 Support；
- 当前真实运行：唯一权威仍是 `BudgetLedger`、`TokenBudgetPolicy` 和工具上限。

## Provenance 分池

C1 的 103 条冻结记录重新按协议分池：

| Pool | 数量 | 权限 |
| --- | ---: | --- |
| `TRAIN_SUPPORT` | 76 | 可作为后续固定协议 Support |
| `TRAIN_OBSERVATION_ONLY` | 27 | 仅观察，不参与 Ranker Support |

此外协议已显式定义 `DEV_EVALUATION_ONLY`、
`HIDDEN_LIKE_EVALUATION_ONLY`、`CURRENT_MATRIX_POST_TERMINAL_ONLY`、
`FIXTURE_EXCLUDED` 和 `QUARANTINE`。正式矩阵新记录在整个矩阵结束前不能回流。

## C2 阈值控制

保持不变：

- Leave-One-Task coverage ≥ 40%；
- Leave-One-Task-Family coverage ≥ 40%；
- overall/per-mode harmful duplicate recommendation rate ≤ 5%；
- 每个 Mode 至少 10 条；
- 不删除负样本，不根据同批 held-out 结果调阈值。

## 验证

- C1.1 行为与边界测试：6/6 PASS；
- C1.1 + KB + Ranker 聚焦回归：26/26 PASS；
- 审计结果：`PASS_FAIL_CLOSED`；
- API Key 未写入任何 Artifact。

## 证据

- `docs/experiments/artifacts/2026-07-24-phase-c11-short-closure/c11-audit-result.json`
- `docs/experiments/artifacts/2026-07-24-phase-c11-short-closure/c11-focused-tests.txt`
- `docs/experiments/artifacts/2026-07-24-phase-c11-short-closure/c11_offline_audit.py`
- `llm4hls_harness/llm4hls_agent/v3_shadow_boundary.py`
- `llm4hls_harness/tests/test_c11_shadow_boundary.py`

