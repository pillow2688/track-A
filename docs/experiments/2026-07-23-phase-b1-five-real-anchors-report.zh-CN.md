# Phase B1：当前冻结版本五类真实 Anchor 实验报告

## 实验目的

Phase B1 计划以五个代表任务验证当前冻结版本的端到端能力边界：`STRUCTURAL_FIX`、`REPAIR`、`SYNTH_FIX`、`OPTIMIZE` 和空 Stub Generation。实验要求使用真实 OpenAI-compatible Planner 环境、真实 Vitis 2025.2、统一预算和 `full_internal_audit` fresh final CSim/Synth/CoSim。

本实验不是 28 题矩阵，也不允许在发现产品、预算或 Ledger 缺陷后修改冻结配置继续运行。

## 冻结基线

- Branch：`feat/track-a-empty-stub-generation-smoke`
- HEAD：`4a05763b593a527878a0056f64763126c58ee63b`
- Router fix：`e5ba32a032432579bb9daa8015d2f705cff93498`
- 配置 digest：`c5dd5c6c99805b54ebb129b67d06c60248be4184da10bc77c24be3ed2ab594f2`
- 工作代码范围：clean
- 运行期间 HEAD：未变化
- 运行期间产品代码/测试：未变化

## 统一配置

实验冻结为：

```text
planner=openai-compatible
model_alias=deepseek-v4-pro
backend=vitis
vitis=2025.2
validation_profile=fast-experiment
final_validation_policy=full_internal_audit
continuation_policy=shadow
experience_mode=shadow
ranker_mode=bayesian_shadow
max_planner_rounds=2
token_cap_per_task=8000
llm_max_output_tokens=4096
temperature=0.0
top_p=1.0
cost(csim/synth/cosim)=1/4/20
timeout(csim/synth/cosim)=300/900/600 seconds
runtime_limit=3600 seconds/task
final_reserve=25 credits
external_rerun=false
```

`ranker_mode=bayesian_shadow` 的实际仓库实现不是独立 CLI 参数，而是由 `experience-mode=shadow` 构造默认 `BayesianStrategyRanker`。其 authority 为 `ADVISORY_ONLY`，不会注入 Planner。运行使用 run-local empty seed，不把 Phase A 的描述性 81 条 Candidate 误当作可直接加载的训练集。

## 预算上限

| 能力 | 任务 | Planner cap | Token cap | CSim cap | Synth cap | CoSim cap | Credit cap |
|---|---|---:|---:|---:|---:|---:|---:|
| STRUCTURAL_FIX | `v3d_fast_018` | 2 | 8,000 | 4 | 2 | 4 | 92 |
| REPAIR | `v3d_fast_002` | 2 | 8,000 | 4 | 3 | 1 | 36 |
| SYNTH_FIX | `v3d_fast_010` | 2 | 8,000 | 4 | 4 | 1 | 40 |
| OPTIMIZE | `v3d_fast_022` | 2 | 8,000 | 4 | 4 | 1 | 40 |
| 空 Stub | `track_a_empty_stub_generation` | 2 | 8,000 | 4 | 3 | 1 | 36 |

Phase 上限为 10 个 Planner calls、40,000 token、20 CSim、16 Synth、8 CoSim 和 244 credits。

## Anchor 选择与顺序

按冻结提案顺序执行：

1. `v3d_fast_018`，预期 `STRUCTURAL_FIX`；
2. `v3d_fast_002`，预期 `REPAIR`；
3. `v3d_fast_010`，预期 `SYNTH_FIX`；
4. `v3d_fast_022`，预期 `OPTIMIZE`；
5. `track_a_empty_stub_generation`，预期带 generation context 的 `REPAIR`。

前两题均真实启动且形成 terminal Artifact。第二题再次复现共享 token reserve 矛盾后，按协议触发全局停止。

## 运行结果总表

| 能力 | 任务 | 实际 Mode | Terminal | Final CSim | Final Synth | Final CoSim | Token | Credit | Latency变化 | 结论 |
|---|---|---|---|---|---|---|---:|---:|---|---|
| STRUCTURAL_FIX | `v3d_fast_018` | STRUCTURAL_FIX | FAILED | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 25 | UNKNOWN | FINAL_RESERVE_FAILURE |
| REPAIR | `v3d_fast_002` | REPAIR | FAILED | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 1 | UNKNOWN | FINAL_RESERVE_FAILURE |
| SYNTH_FIX | `v3d_fast_010` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 全局停止后未启动 |
| OPTIMIZE | `v3d_fast_022` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 全局停止后未启动 |
| 空 Stub | `track_a_empty_stub_generation` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 全局停止后未启动 |

## STRUCTURAL_FIX 验收

### Baseline 事实

| 项目 | 结果 |
|---|---|
| Baseline source SHA-256 | `6f2db94db472c9ec24b6af69045d3b3be13def04967976285d9533de1c50702f` |
| CSim | PASS；3.437369615 秒 |
| Synth | PASS；10.275541245 秒 |
| CoSim | TIMEOUT；600.016591386 秒 |
| Router Mode | STRUCTURAL_FIX |
| Router reason | REQUIRED_BASELINE_COSIM_FAILED |

Synth 报告给出 latency 21 cycles、II 22、estimated clock 2.278 ns、LUT 398、FF 157。日志提示 `main_stream`、`side_stream` FIFO 深度为 1，建议加深以避免 deadlock；CoSim 编译了 deadlock detector 后在 600 秒边界超时。这些是真实结构风险证据。

### 闭环结果

Planner 前预算门禁要求：

```text
policy=structural_fix_candidate_plus_final_closure
required_tokens=10562
available_tokens=8000
blocker=tokens:8000<10562
```

因此：

- Planner input/output：没有生成；
- Patch Validator：未到达；
- TopInterfaceGuard：未到达；
- Candidate registry：只有 immutable baseline candidate；
- Promote/Reject：没有 candidate operation；
- final candidate：不存在；
- final source digest：不存在；
- fresh final CSim/Synth/CoSim：全部 NOT_RUN。

## REPAIR 验收

### Baseline 事实

| 项目 | 结果 |
|---|---|
| Baseline source SHA-256 | `94afd2fd4aba3d896ddd7d0478a14f1bdbe7b1a5624391ec17521ebd50dd7be7` |
| CSim | FAIL；3.192355360 秒 |
| 首个公开差异 | `expected -8, got -3` |
| Router Mode | REPAIR |
| Router reason | BASELINE_CSIM_FAILED |

Router 与预期一致，但 Planner 前预算门禁要求：

```text
policy=repair_candidate_plus_final_closure
required_tokens=8511
available_tokens=8000
blocker=tokens:8000<8511
```

因此同样没有 Planner、Patch、新 Candidate、final candidate 或 fresh final 验证。

## SYNTH_FIX 验收

状态：`NOT_RUN_GLOBAL_BUDGET_DEFECT`。

没有形成 baseline、实际 Mode、Planner、Patch、Candidate 或 final 证据。不得用历史 run 替代本次冻结提交的验收结果。

## OPTIMIZE 验收

状态：`NOT_RUN_GLOBAL_BUDGET_DEFECT`。

- 正确性验收：`NOT_EVALUATED`
- 性能结果：`NOT_EVALUATED`
- latency：`UNKNOWN`
- acceleration：`UNKNOWN`

不得将 UNKNOWN 写成 0，也不得把历史优化结果当作本阶段性能收益。

## 空 Stub 验收

状态：`NOT_RUN_GLOBAL_BUDGET_DEFECT`。

本阶段没有读取或注入 golden 实现，也没有形成当前冻结版本的真实 generation Patch 与 final 验证。

## 正确性结果

以 `full_internal_audit` fresh final 为验收口径：

```text
Final CSim PASS  = 0/5
Final Synth PASS = 0/5
Final CoSim PASS = 0/5
```

STRUCTURAL_FIX baseline 的 CSim/Synth PASS 不可计入 final；REPAIR baseline CSim FAIL 是路由事实，不是修复后的正确性结果。

## Performance–Area 结果

只有 `v3d_fast_018` 的 baseline Synth 产生可解析数据：

| 指标 | Baseline |
|---|---:|
| Latency best/avg/worst | 21 / 21 / 21 cycles |
| Interval min/max | 22 / 22 |
| Estimated clock | 2.278 ns |
| LUT | 398 |
| FF | 157 |
| BRAM / DSP / URAM | 0 / 0 / 0 |

因为不存在 final candidate，不能计算 performance-area 变化、acceleration 或资源增减。OPTIMIZE 未运行，性能分类为 `NOT_EVALUATED`。

## Token 与工具预算

| 任务 | Planner | Token | CSim | Synth | CoSim | Credits | Ledger runtime |
|---|---:|---:|---:|---:|---:|---:|---:|
| `v3d_fast_018` | 0 | 0 | 1 | 1 | 1 | 25 | 614.802952 秒 |
| `v3d_fast_002` | 0 | 0 | 1 | 0 | 0 | 1 | 4.163111 秒 |
| 其余三题 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| **总计** | **0** | **0** | **2** | **1** | **1** | **26** | **618.966063 秒** |

两个 started run 的 Ledger 均对账，未发生 credit 或 token 超限。`token_usage_complete=true`，实际 Provider request 为 0；0 token 来自门禁在请求前停止，不代表一次零成本模型响应。

## 失败分析

### 主失败：FINAL_RESERVE_FAILURE

冻结计划把每题 token cap 固定为 8,000，但当前仓库的候选加 fresh final closure 在两个独立 Mode 上分别要求 10,562 和 8,511。该冲突会阻止至少已观测的 STRUCTURAL_FIX 与 REPAIR 进入 Planner。

这是“冻结运行计划与现有策略不一致”的预算计划缺陷。它没有证明 Planner、Patch 或 final validation 的产品逻辑错误，因为这些路径尚未执行；也不能通过本阶段中途增加 token cap 来规避，否则前后 run 不再属于同一冻结配置。

### 次要 baseline 观测

- `v3d_fast_018`：`COSIM_TIMEOUT`，并有结构 deadlock 相关证据；
- `v3d_fast_002`：`CSIM_FAIL`，符合 REPAIR 路由预期。

两者均保留原始失败 Artifact，没有删除失败 run，也没有进行选择性外部重跑。

## Artifact 完整性

| 任务 | Package manifest | Listed / Verified | Baseline digest | Final digest |
|---|---|---:|---|---|
| `v3d_fast_018` | `48ad67a...5e633` | 29 / 29 | 可绑定 | 不存在 |
| `v3d_fast_002` | `1c8f6cdf...d6ce5` | 17 / 17 | 可绑定 | 不存在 |

两份 canonical package-manifest digest 都与 terminal result 中的记录一致；合计 46/46 个 Artifact 的 SHA-256 和 byte-size 复核通过，manifest mismatch 为 0。

## 安全与信息隔离

- 只运行公共 V3-D 任务目录；
- 没有 Planner 请求，因此没有模型上下文泄漏路径；
- 未访问 hidden、reference、golden 实现；
- 未把 acceptance/oracle facts 注入 Planner；
- 未记录 API Key、Authorization、license 或 endpoint credential；
- 命令索引仅记录环境变量名；
- 没有 interface mutation；由于无 Patch，TopInterfaceGuard 未到达；
- Continuation 保持 shadow；
- Experience 保持 shadow；
- Bayesian Strategy Ranker 保持 advisory-only shadow，未训练、未 guided、未准入。

## 与历史证据的关系

Phase A 的 72-run 离线审计和旧 V3-F 结果继续保留为历史描述性证据。本次是 HEAD `4a05763b...` 下的独立真实 Anchor 尝试，不能用历史结果填补三个 NOT_RUN，也不能用历史 final 替代本次缺失的 fresh final。

本次新增事实是：当前冻结的 8,000-token Phase B1 计划不能通过现行 final reserve gate。它不覆盖 Phase A 的 ACCEPTED，也不改变 Continuation、Experience 和 Strategy Ranker 的 shadow 准入状态。

## 是否进入 28 题正式矩阵

结论：**NOT_READY_FOR_28_TASK_MATRIX**。

原因：

1. terminal run 只有 2/5；
2. final CSim/Synth/CoSim 均为 0/5 PASS；
3. final source digest 为 0/5；
4. 两个 started run 都发生 final reserve failure；
5. 冻结预算计划存在共享缺陷；
6. OPTIMIZE 和空 Stub 没有当前版本证据。

本阶段结束后立即停止，不启动 28 题矩阵。

## 证据索引

- 冻结计划：`docs/experiments/artifacts/2026-07-23-phase-b1-five-real-anchors/frozen-run-plan.json`
- 运行索引：`docs/experiments/artifacts/2026-07-23-phase-b1-five-real-anchors/anchor-run-index.jsonl`
- 结果摘要：`docs/experiments/artifacts/2026-07-23-phase-b1-five-real-anchors/anchor-result-summary.json`
- 预算对账：`docs/experiments/artifacts/2026-07-23-phase-b1-five-real-anchors/anchor-budget-reconciliation.json`
- 失败分类：`docs/experiments/artifacts/2026-07-23-phase-b1-five-real-anchors/anchor-failure-taxonomy.json`
- 完整性复核：`docs/experiments/artifacts/2026-07-23-phase-b1-five-real-anchors/anchor-artifact-integrity.json`
- STRUCTURAL_FIX run：`llm4hls_harness/runs/phase-b1-4a05763-v3d_fast_018-20260723T050704Z/`
- REPAIR run：`llm4hls_harness/runs/phase-b1-4a05763-v3d_fast_002-20260723T050704Z/`

## 实验结论

**Phase B1 状态：BLOCKED**

本次验证了真实 Vitis baseline 路径、Router 的两个代表 Mode、BudgetLedger 的预请求门禁和 terminal package 绑定；没有完成任何一个 Anchor 的端到端 fresh final 闭环。下一阶段应先形成新的预算修正提案并重新冻结，然后在全新 run 目录中重做五类 Anchor。不能复用本次 FAILED run 作为修正后配置的成功样本。
