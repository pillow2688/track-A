# 当前冻结版本预算消耗型真实运行提案

> **Proposal only / 尚未批准执行 / AWAITING_USER_APPROVAL**
>
> 本文件不是运行授权。Phase A.1 没有调用真实 LLM、CSim、Synth 或 CoSim。

主文件：`current-head-paid-run-proposal.md`。两份文件结论和预算数字一致。

## 1. 提案目标与冻结对象

- 产品代码基线：`e5ba32a032432579bb9daa8015d2f705cff93498`
- 公共题目范围：V3-D 28 题 + 独立空 Stub fixture
- 真实后端（批准后）：OpenAI-compatible DeepSeek + Vitis 2025.2
- 当前状态：只完成预算计算与决策文件，未执行任何 run

正式执行前，用户必须确认 frozen commit、模型 alias、Planner/Token/Budget 配置和 final policy。任一字段变化都要求重新计算本文件全部 cap。

## 2. 计算依据

所有数字来自当前 task specs、`requires_cosim`、现有预算配置和确定性状态机，不使用 LLM/Vitis 估算：

- 28 题 inventory：REPAIR 8、SYNTH_FIX 6、STRUCTURAL_FIX 6、OPTIMIZE 8
- `requires_cosim=true`：6 个 STRUCTURAL_FIX + `v3d_fast_028`，合计 7
- Planner hard cap：每 run 2 calls
- Token hard cap：每 run 8,000
- Tool cost：CSim 1、Synth 4、CoSim 20 Credits
- 每 run final reserve：25 Credits
- 每 run runtime hard cap：3,600 秒
- 自动/选择性重试：0

详细冻结字段见：

- `artifacts/2026-07-23-phase-a1-freeze/proposed-frozen-run-config.json`
- `artifacts/2026-07-23-phase-a1-freeze/next-real-run-options-decision.json`

Tool call cap 由 `max_planner_rounds=2` 下的 baseline、两个候选 round 和 fresh final 路径逐项相加：

| Mode / Final policy | CSim | Synth | CoSim | Credit cap |
|---|---:|---:|---:|---:|
| REPAIR / `task_contract` | 4 | 3 | 0 | 16 |
| REPAIR / `full_internal_audit` | 4 | 3 | 1 | 36 |
| SYNTH_FIX / `task_contract` | 4 | 4 | 0 | 20 |
| SYNTH_FIX / `full_internal_audit` | 4 | 4 | 1 | 40 |
| STRUCTURAL_FIX / 两种 final policy | 4 | 2 | 4 | 92 |
| OPTIMIZE、无需 CoSim / `task_contract` | 4 | 4 | 0 | 20 |
| OPTIMIZE、无需 CoSim / `full_internal_audit` | 4 | 4 | 1 | 40 |
| OPTIMIZE、需要 CoSim / 两种 final policy | 4 | 4 | 2 | 60 |

Credit 公式为：

```text
CSim × 1 + Synth × 4 + CoSim × 20
```

## 3. 公共冻结控制

批准后的所有 run 必须统一：

```text
provider=openai-compatible
model_alias=deepseek-v4-pro
backend=vitis
toolchain=Vitis 2025.2
validation_profile=fast-experiment
continuation=shadow
experience=shadow
ranker=bayesian_shadow
max_planner_rounds=2
run_token_limit=8000
final_reserve_credits=25
每个 slot 使用全新 run-dir
禁止自动选择性重跑
```

访问控制保持 `hidden=false`、`reference=false`、`golden=false`。

## 4. Gate B1：五类真实 Anchor

Gate B1 使用 `final=full_internal_audit`：

| 能力 | 任务 | Mode | Planner cap | Token cap | CSim/Synth/CoSim cap | Credit cap |
|---|---|---|---:|---:|---|---:|
| REPAIR | `v3d_fast_002` | REPAIR | 2 | 8,000 | 4/3/1 | 36 |
| SYNTH_FIX | `v3d_fast_010` | SYNTH_FIX | 2 | 8,000 | 4/4/1 | 40 |
| STRUCTURAL_FIX | `v3d_fast_018` | STRUCTURAL_FIX | 2 | 8,000 | 4/2/4 | 92 |
| OPTIMIZE | `v3d_fast_022` | OPTIMIZE | 2 | 8,000 | 4/4/1 | 40 |
| 真正空 Stub | `track_a_empty_stub_generation` | REPAIR | 2 | 8,000 | 4/3/1 | 36 |
| **Gate B1 合计** | **5 个 slot** |  | **10** | **40,000** | **20/16/8** | **244** |

- summed per-run final reserve：5 × 25 = 125 Credits；它已包含在 244 的 Tool Credit cap 内，不再重复相加。
- 历史 72-run 最近秩 P25–P90 推算的串行规划区间：约 5.63–10.45 分钟。
- 配置级串行 hard upper bound：5 × 3,600 秒 = 5 小时。

Gate B1 目的：

- 验证冻结 commit 的真实闭环；
- 确认 Router 修复未破坏四种 Mode；
- 验证真正空 Stub Generation；
- 在大矩阵前发现全局框架问题；
- 形成五类 fresh CSim/Synth/CoSim 证据。

**任一 Anchor 未通过时，不得启动 Gate B2。**

## 5. Gate B2 方案 A：预算优先

```text
5 个 Gate B1 full_internal_audit Anchor
+
V3-D 28 题 × 1，final=task_contract
```

四个 V3-D Anchor 会在矩阵中再次出现，但这是 final policy 不同的两个正式协议槽位：

- Gate B1：`full_internal_audit`
- 28 题矩阵：`task_contract`

不允许用 Anchor run 冒充矩阵 slot，也不允许隐去这四个重复 task identity。

### 方案 A 精确上限

| 项目 | 上限 |
|---|---:|
| Gate B1 slot | 5 |
| 28 题矩阵 slot | 28 |
| 唯一 run slot | **33** |
| 唯一 task identity | 29 |
| 最大 Planner calls | **66** |
| 最大 Token | **264,000** |
| CSim cap | **132** |
| Synth cap | **108** |
| CoSim cap | **34** |
| Tool Credit cap | **1,244** |
| summed per-run final reserve | 825 Credits，已包含在 Tool Credit cap 中 |
| 串行经验规划区间 | **37.18–68.98 分钟** |
| 串行配置 hard upper bound | **33 小时** |

28 题 `task_contract` 子矩阵本身的上限为：

```text
CSim=112
Synth=92
CoSim=26
Tool Credits=1000
Planner calls=56
Token=224000
```

正式表必须逐题记录 CoSim 的 PASS/FAIL/NOT_RUN；本方案不得声称 28 题全部完成 full CoSim。

## 6. Gate B2 方案 B：证据优先

```text
V3-D 28 题 × 1，final=full_internal_audit
+
1 个独立空 Stub full_internal_audit
```

`v3d_fast_002/010/018/022` 作为 28 题矩阵首批 slot，不再单独重复。真正空 Stub fixture 不属于 V3-D 28 题，不能伪装成矩阵 slot，因此完整 Gate B1+B2 协议是 28 + 1 = 29 个 run slot。

### 方案 B 精确上限

| 项目 | 上限 |
|---|---:|
| V3-D full-audit 矩阵 slot | 28 |
| 独立空 Stub slot | 1 |
| 唯一 run slot | **29** |
| 唯一 task identity | 29 |
| 最大 Planner calls | **58** |
| 最大 Token | **232,000** |
| CSim cap | **116** |
| Synth cap | **95** |
| CoSim cap | **48** |
| Tool Credit cap | **1,456** |
| summed per-run final reserve | 725 Credits，已包含在 Tool Credit cap 中 |
| 串行经验规划区间 | **32.67–60.62 分钟** |
| 串行配置 hard upper bound | **29 小时** |

其中 V3-D 28 题 full-audit 子矩阵本身为：

```text
CSim=112
Synth=92
CoSim=47
Tool Credits=1420
Planner calls=56
Token=224000
```

方案 B 的 28 题全部产生 fresh CSim/Synth/CoSim，但与方案 A 相比增加 14 次 CoSim 和 212 Tool Credits。

## 7. wall-time 口径

“经验规划区间”不是猜测值，也不是成功保证。计算方法是：

1. 从已审计 72 个历史 run 读取 `wall_time_seconds`；
2. 使用最近秩 P25 = 67.6028966240 秒、P90 = 125.4233122070 秒；
3. 分别乘以方案的唯一 run slot 数；
4. 假设串行执行。

配置级 hard upper bound 则是精确的 `唯一 run slot × 3,600 秒`。实际排队、人工审批或独立手动重跑不计入上述范围。

## 8. 失败和重试规则

- 自动重试：0
- 选择性重跑：禁止
- 失败 slot：保留原 run-dir、Ledger、trace 和全部失败证据
- 手动重跑：必须获得单独用户批准，并使用新的 run-dir
- 手动重跑不包含在本提案 cap 中；批准前必须重新核算总预算
- B1 失败：停止，不进入 B2

## 9. 原 16 题 gap 清单降级

状态：`DROPPED_AS_DEFAULT`

以下 16 题只保留为工程覆盖参考：

```text
v3d_fast_002
v3d_fast_003
v3d_fast_006
v3d_fast_007
v3d_fast_008
v3d_fast_010
v3d_fast_011
v3d_fast_014
v3d_fast_018
v3d_fast_019
v3d_fast_020
v3d_fast_022
v3d_fast_024
v3d_fast_026
v3d_fast_027
v3d_fast_028
```

如果决定运行同一冻结版本的正式 28 题矩阵，不应先独立执行完整 16 题 gap coverage。

## 10. 推荐与待批准状态

### RECOMMENDED_OPTION

`OPTION_A_BUDGET_FIRST`

理由：保留五类 full-audit Anchor 和同 commit 的 28 题正式 `task_contract` 矩阵，同时比方案 B 少 14 次 CoSim、少 212 Tool Credits。代价是多 4 个 run slot、最多多 8 次 Planner 调用和 32,000 Token。

### ALTERNATIVE_OPTION

`OPTION_B_EVIDENCE_FIRST`

适用于论文/提交必须要求 28 题全部 fresh CoSim，且用户明确接受额外 CoSim Credit 与风险的情况。

### 用户批准状态

`AWAITING_USER_APPROVAL`

本阶段立即停止，不执行 Gate B1 或 Gate B2。
