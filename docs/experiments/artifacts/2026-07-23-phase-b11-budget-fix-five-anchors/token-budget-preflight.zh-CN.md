# Phase B1.1 五类 Anchor 离线 Token 预算预检

## 结论

Gate 1：**PASS**

五题最低需求均可解释，逐题建议上限均低于本阶段采用的公开 live-run Token 硬约束 32,768。无需修改产品代码，只需修正运行计划中的 per-task `run-token-limit`。

本预检没有发送模型请求，没有运行 CSim、Synth 或 CoSim，也没有读取 hidden、reference、golden 或未来 Candidate。

## 公开 Token 上限

五个公开任务的 `task.toml` 只有 Tool Credit `budget`，没有独立 Token 字段。当前 CLI 明确规定 live Planner 的总 Token 默认值为 32,768，`--run-token-limit` 是覆盖该总量的硬上限；CLI 的 context window 默认同为 32,768。

因此，本阶段把 32,768 作为五题统一的公开 live-run Token 硬约束。修正后的逐题 cap 都显著低于该值。

## 8511 和 10562 的真实组成

Phase B1 使用 `token-budget-policy=fixed`。该路径不构造动态 `TokenEnvelope`，`PreparedPlannerCall` 的 token 预留公式是：

```text
estimated_tokens = estimated_input_tokens + max_output_tokens
```

然后 `_budget_affordability()` 检查：

```text
tokens_remaining >= estimated_tokens
```

所以：

```text
REPAIR:
4415 estimated prompt input
+ 4096 configured Provider output cap
= 8511 required minimum

STRUCTURAL_FIX:
6466 estimated prompt input
+ 4096 configured Provider output cap
= 10562 required minimum
```

其中没有另一个“recovery reserve”，也没有重复计算 final Token reserve。`full_internal_audit` 的 fresh final CSim/Synth/CoSim 由 Tool Credit reserve 保护，当前为 25 credits；fixed Token 路径中的 `final_token_reserve=0`。

STRUCTURAL_FIX 新 cap 从四位数变成五位数后，Prompt 中 `remaining_tokens` 的序列化最多增加一个字符，因此预检采用 6,467 input、10,563 minimum 的保守上界。

## 离线估算方法

- REPAIR、STRUCTURAL_FIX：读取 Phase B1 当前 HEAD 的公开、脱敏、决策时可见 budget gate。
- SYNTH_FIX、OPTIMIZE、空 Stub：使用当前 `OpenAICompatibleV3PlannerAdapter.prepare()` 和只描述请求、不联网的 Provider 路径，对既有公共决策时快照重新计算。
- 将 max Planner rounds 修正为 2，final policy 视为 CSim/Synth/CoSim。
- Experience 保持 shadow；shadow 推荐不注入 Prompt。
- 不读取 acceptance、golden、reference 或 hidden 内容。
- 对 cap 位数变化造成的一个 token/字符差异采用保守上界。

## 五题预检

| 能力 | 任务 | 估算输入 | 输出预留 | 最低需求 | 建议 cap | Headroom | 公开上限 | 状态 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| REPAIR | `v3d_fast_002` | 4,415 | 4,096 | 8,511 | 8,639 | 128 | 32,768 | PASS |
| SYNTH_FIX | `v3d_fast_010` | 4,977 | 4,096 | 9,073 | 9,201 | 128 | 32,768 | PASS |
| STRUCTURAL_FIX | `v3d_fast_018` | 6,467 | 4,096 | 10,563 | 10,691 | 128 | 32,768 | PASS |
| OPTIMIZE | `v3d_fast_022` | 6,578 | 4,096 | 10,674 | 10,802 | 128 | 32,768 | PASS |
| 空 Stub | `track_a_empty_stub_generation` | 5,558 | 4,096 | 9,654 | 9,782 | 128 | 32,768 | PASS |

建议 cap 不是鼓励消耗的目标。128-token headroom 取自现有 CLI `token-safety-margin` 默认值，不删除或放宽 final reserve。

## Tool Credit reserve

沿用 Phase B1 的逐题 Credit cap：

| 任务 | Credit cap | Baseline 后候选加 fresh final 所需 | 结果 |
|---|---:|---:|---|
| `v3d_fast_002` | 36 | 30 | PASS |
| `v3d_fast_010` | 40 | 30 | PASS |
| `v3d_fast_018` | 92 | 46 | PASS |
| `v3d_fast_022` | 40 | 30 | PASS |
| `track_a_empty_stub_generation` | 36 | 30 | PASS |

所有题的 fresh final Credit reserve 均为 CSim 1 + Synth 4 + CoSim 20 = 25。

## Gate 1 验收

- 公开 Token 上限：5/5 已读取；
- 最低需求：5/5 可解释；
- 建议 cap：5/5 不超过 32,768；
- Planner rounds：仍为 2；
- Continuation：shadow；
- Experience：shadow；
- Strategy Ranker：bayesian shadow；
- Final：full internal audit；
- Tool Credit reserve：5/5 可覆盖；
- 产品代码修改：不需要；
- 真实 Provider / CSim / Synth / CoSim：0 / 0 / 0 / 0。

允许进入 Gate 2。
