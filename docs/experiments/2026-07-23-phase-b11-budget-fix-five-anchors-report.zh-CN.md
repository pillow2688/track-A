# Phase B1.1：Anchor Token 预算修正与五类真实验收重跑实验报告

## 1. 实验问题

Phase B1 已形成不可覆盖的历史负证据：统一固定 8,000 Token 计划在 Planner 前被 final reserve 门禁阻断，其中 REPAIR 最低需要 8,511 Token，STRUCTURAL_FIX 最低需要 10,562 Token。B1 的结论是运行计划与当前预算语义不兼容，不是 Planner、Patch 或 fresh final 能力失败。

Phase B1.1 的目标是：

1. 离线计算五类 Anchor 的真实 Planner 输入加输出上限；
2. 在公开 Token 约束内只修订 per-task 运行计划；
3. 保持代码 HEAD 和产品实现不变；
4. 用全新目录真实重跑五类 Anchor；
5. 核验 actual Mode、真实 Vitis 结果、fresh final 三件套、Ledger、manifest、digest、接口与信息隔离；
6. 给出是否允许进入 28 题矩阵的明确结论。

本实验不运行 28 题矩阵，不运行多模型矩阵，不启用 Continuation enforce、Experience guided 或 learned Strategy Ranker。

## 2. 冻结对象

- Branch：`feat/track-a-empty-stub-generation-smoke`
- HEAD：`4a05763b593a527878a0056f64763126c58ee63b`
- Plan digest：`330c6e7334e59408e14db51a40a1ee3b35d4bc45b95d5460c61c3b0750e5ee5a`
- Planner：OpenAI-compatible
- Model alias：`deepseek-v4-pro`
- Vitis：2025.2
- Backend：`vitis`
- Validation：`fast-experiment`
- Final validation：`full_internal_audit`
- Token policy：`fixed`
- Continuation：`shadow`
- Experience：`shadow`
- Ranker：默认 `BayesianStrategyRanker` 的 `bayesian_shadow / ADVISORY_ONLY`

运行期间没有修改 HEAD、产品代码、测试、Prompt、Router、Candidate comparator、Vitis 语义或 task corpus。

## 3. Gate 1：离线 Token 预算预检

### 3.1 当前代码的门禁语义

`PreparedPlannerCall.estimated_tokens` 为 estimated input 与 configured max output 的和；固定策略的 affordability 检查为 `tokens_remaining >= required_tokens`。本实验 configured output cap 固定为 4,096。

fresh final CSim/Synth/CoSim 的硬保护是 25 Tool Credits。固定策略没有独立 future-token reserve，因此本阶段不能把 dynamic `TokenEnvelope` 的 reserve 语义混入固定计划，也不能将 25 credits 错换算成 Token。

### 3.2 预检数据来源

- REPAIR：采用 B1 同任务实测门禁下界 8,511；
- STRUCTURAL_FIX：采用 B1 同任务实测下界，并对输入位数增长留一 Token，得到 10,563；
- SYNTH_FIX、OPTIMIZE、空 Stub：在不调用网络和 Vitis 的情况下，对公开 decision-time snapshot 执行纯 `prepare()`；
- 所有 cap 在最低需求上增加现有 CLI 使用的 128 Token safety margin；
- 单题公开 live Token 上限采用 CLI/source 可核验的 32,768。

### 3.3 修订结果

| 能力 | Estimated input / 来源 | Output cap | 最低需求 | Headroom | Frozen cap | 公开上限 | 判定 |
|---|---:|---:|---:|---:|---:|---:|---|
| REPAIR | B1 实测合计 | 4,096 | 8,511 | 128 | 8,639 | 32,768 | PASS |
| SYNTH_FIX | 4,977 | 4,096 | 9,073 | 128 | 9,201 | 32,768 | PASS |
| STRUCTURAL_FIX | B1 实测上界 | 4,096 | 10,563 | 128 | 10,691 | 32,768 | PASS |
| OPTIMIZE | 6,578 | 4,096 | 10,674 | 128 | 10,802 | 32,768 | PASS |
| 空 Stub | 5,558 | 4,096 | 9,654 | 128 | 9,782 | 32,768 | PASS |

五题 cap 合计 49,115。Gate 1 测试 556/556 PASS、`compileall` PASS、`git diff --check` PASS，修订计划状态为 `FROZEN_GATE1_PASS`，因此放行 Gate 2。

## 4. Gate 2：真实运行协议

五题统一使用：

- temperature 0.0、top_p 1.0；
- 最大 Planner 轮数 2；
- CSim/Synth/CoSim cost 1/4/20；
- timeout 300/900/600 秒；
- per-task runtime 3,600 秒；
- final reserve 25 credits；
- 全新 run dir；
- 公共 task corpus；
- 同一冻结 HEAD；
- 只允许 `TRANSIENT_MODEL_TRANSPORT` 等明确基础设施故障进行一次外部重试；
- 不允许为获得预期 Mode 而重跑。

运行顺序为 REPAIR、SYNTH_FIX、OPTIMIZE、空 Stub、STRUCTURAL_FIX，以先验证较低工具成本路径，再运行高成本 CoSim 路径。

## 5. 真实运行明细

### 5.1 REPAIR：`v3d_fast_002`

第一次运行：

- baseline CSim FAIL；
- actual Mode = REPAIR；
- 8,511 Token 门禁需求小于 8,639 cap，预算门禁已修复；
- 两轮 Provider 调用都立即 `URLError`；
- request id 为 null，Token 用量为 0；
- Terminal = FAILED，`MAX_TASK_REPAIR_ROUNDS`；
- manifest 23/23 完整。

该失败满足一次 `TRANSIENT_MODEL_TRANSPORT` 重试条件。retry1 使用全新目录：

- actual Mode = REPAIR；
- 1 次有效 Planner 响应；
- Input / Output / Total Token = 1,172 / 349 / 1,521；
- candidate digest：`472d38864df233b3ca17929aa857abdb0448eac9201bfc70075f13e6da40e8ea`；
- fresh final CSim / Synth / CoSim = PASS / PASS / PASS；
- Terminal = DONE，`REPAIR_FINALIZED`；
- manifest 72/72 完整；
- TopInterfaceGuard = allowed，LOW。

REPAIR 最终验收 PASS；原失败证据未删除或覆盖。

### 5.2 SYNTH_FIX：`v3d_fast_010`

- baseline CSim PASS、Synth FAIL；
- actual Mode = SYNTH_FIX；
- Planner 1 次；
- Input / Output / Total Token = 1,327 / 416 / 1,743；
- candidate digest：`ebb302260db5d30b33506bae587107663e36ef91c8f6b8d5da8e5c95282e04d5`；
- fresh final CSim / Synth / CoSim = PASS / PASS / PASS；
- Terminal = DONE，`SYNTH_FIX_FINALIZED`；
- manifest 77/77 完整；
- TopInterfaceGuard = allowed，LOW。

SYNTH_FIX 验收 PASS。

### 5.3 OPTIMIZE：`v3d_fast_022`

- baseline CSim、Synth PASS；
- actual Mode = OPTIMIZE；
- Planner 1 次；
- Input / Output / Total Token = 1,756 / 817 / 2,573；
- candidate digest：`708fd2d1b3e224ec9ddafaa6910f83a51ad2abb95b56091247971859f2e2aab6`；
- fresh final CSim / Synth / CoSim = PASS / PASS / PASS；
- Terminal = DONE，`CANDIDATE_PROMOTED_AND_FINALIZED`；
- manifest 82/82 完整；
- TopInterfaceGuard = allowed，LOW。

性能结果：

| 指标 | Baseline | Final | 变化 |
|---|---:|---:|---:|
| Latency | 9 | 2 | 4.5× acceleration |
| Transaction interval | 8 | 3 | 改善 |
| Clock ns | 1.481 | 0.731 | 改善 |
| LUT | 309 | 1,380 | +1,071 |
| FF | 11 | 3 | -8 |
| BRAM / DSP / URAM | 0 / 0 / 0 | 0 / 0 / 0 | 不变 |

Performance-Area 分类为 `PERFORMANCE_AREA_TRADEOFF`、`NON_DOMINATED`。性能面积代理轻微改善，但纯 area proxy 未改善；资源压力保持 LOW。Power 没有采集，报告为 `UNSUPPORTED（Artifact: NOT_COLLECTED）`。

`ROUND_SKIPPED_FINAL_RESERVE` 只表示不再开始下一轮探索。terminal final gate 的 25-credit reserve 仍然满足，fresh final 三件套全部执行，不能把它写成 final reserve failure。

### 5.4 空 Stub：`track_a_empty_stub_generation`

- baseline CSim FAIL，公开 stub 没有实现目标计算；
- actual Mode = REPAIR；
- Planner 1 次；
- Input / Output / Total Token = 1,438 / 671 / 2,109；
- candidate digest：`b2e2836d4ccf80cdd04682c32a5b3cba5d092715c7d90721053e8d03ae71e647`；
- fresh final CSim / Synth / CoSim = PASS / PASS / PASS；
- Terminal = DONE，`REPAIR_FINALIZED`；
- manifest 72/72 完整；
- TopInterfaceGuard = allowed，LOW。

本版本空 Stub Generation 真实闭环 PASS。

### 5.5 STRUCTURAL_FIX 目标：`v3d_fast_018`

本次真实 baseline：

- CSim PASS；
- Synth PASS；
- CoSim PASS，约 25.989 秒；
- phase decision reason = `BASELINE_VALIDATION_PASSED`；
- actual Mode = OPTIMIZE；
- Planner = 0；
- baseline/final digest 同为 `6f2db94db472c9ec24b6af69045d3b3be13def04967976285d9533de1c50702f`；
- fresh final CSim / Synth / CoSim = PASS / PASS / PASS；
- Terminal = DONE，`BASELINE_FINALIZED_NO_IMPROVEMENT`；
- manifest 56/56 完整。

同一任务在 B1 中 baseline CoSim 达到 600 秒 timeout 并进入 STRUCTURAL_FIX，本次却正常通过。这说明该 Anchor 的结构故障前置条件没有稳定复现。Router 使用当次 baseline facts 选择 OPTIMIZE 是正确行为，不能为满足预期 Mode 强迫 Router 进入 STRUCTURAL_FIX，也不能重跑直到出现 timeout。

因此本项的终态虽然 DONE、fresh final 三件套也全 PASS，但能力验收结论仍是 FAIL：`ANCHOR_PRECONDITION_NOT_REPRODUCED / MODE_MISMATCH`。B1.1 没有实际执行 STRUCTURAL_FIX Planner/Patch 闭环。

## 6. 结果总表

| 能力 | 任务 | Token Cap | 最低需求 | 实际 Mode | Planner | Token | Final CSim | Final Synth | Final CoSim | Credit | Terminal | 结论 |
|---|---|---:|---:|---|---:|---:|---|---|---|---:|---|---|
| REPAIR | `v3d_fast_002` | 8,639 | 8,511 | REPAIR | 3 dispatch / 1 response | 1,521 | PASS | PASS | PASS | 32 | DONE_AFTER_ALLOWED_RETRY | PASS |
| SYNTH_FIX | `v3d_fast_010` | 9,201 | 9,073 | SYNTH_FIX | 1 | 1,743 | PASS | PASS | PASS | 35 | DONE | PASS |
| STRUCTURAL_FIX | `v3d_fast_018` | 10,691 | 10,563 | OPTIMIZE | 0 | 0 | PASS | PASS | PASS | 50 | DONE | FAIL：MODE_MISMATCH |
| OPTIMIZE | `v3d_fast_022` | 10,802 | 10,674 | OPTIMIZE | 1 | 2,573 | PASS | PASS | PASS | 35 | DONE | PASS |
| 空 Stub | `track_a_empty_stub_generation` | 9,782 | 9,654 | REPAIR | 1 | 2,109 | PASS | PASS | PASS | 31 | DONE | PASS |

## 7. Budget Ledger 对账

全阶段包含 6 次物理运行：5 个原计划运行，加 REPAIR 一次获准的传输重试。

| 指标 | 物理运行总计 | 仅 accepted Anchor runs | Phase cap |
|---|---:|---:|---:|
| Planner dispatch | 6 | 4 | 10 |
| 有 usage 的 Provider 响应 | 4 | 4 | — |
| Input Token | 5,693 | 5,693 | — |
| Output Token | 2,253 | 2,253 | — |
| Total Token | 7,946 | 7,946 | 49,115 |
| CSim | 15 | 14 | 20 |
| Synth | 12 | 12 | 16 |
| CoSim | 6 | 6 | 8 |
| Tool Credits | 183 | 182 | 244 |
| Ledger runtime 秒 | 358.174622 | 353.586637 | — |

6/6 Ledger 对账通过，pending credits/tokens 均为 0，`token_usage_complete=true`。所有任务都低于公开单题 Token 上限，所有 phase cap 均未超过。

## 8. Artifact、digest 与接口

- 6/6 terminal run 具有 package manifest；
- 382/382 manifest 条目通过 SHA-256 和 size 复核；
- 5/5 accepted Anchor final source digest 可绑定；
- 4 个 Patch candidate 均通过 TopInterfaceGuard；
- 结构目标 run 未产生 Patch，接口保持 baseline；
- interface mutation = false；
- Ledger mismatch = 0；
- manifest mismatch = 0。

## 9. 信息隔离和轻量工具

- hidden / reference / golden 均未访问、未注入 Planner；
- API Key 和 Authorization 未写入证据；
- Continuation 保持 shadow，未 enforce；
- Experience 保持 shadow，未 guided；
- Strategy Ranker 由 `--experience-mode shadow` 使用仓库默认 Bayesian shadow 实现，权限仍是 advisory-only；
- 本阶段没有训练 learned Ranker，也没有改变 `BAYESIAN_SHADOW / TRAINING_NOT_READY`；
- 本阶段的 Anchor 成功不能作为 Ranker 收益证据，因为没有受控 ranker on/off 对照。

## 10. 运行后检查

| Gate | 结果 |
|---|---:|
| 完整测试 | 556/556 PASS |
| Failure / Error | 0 / 0 |
| `compileall` | PASS |
| `git diff --check` | PASS |
| HEAD | `4a05763b593a527878a0056f64763126c58ee63b`，未变化 |
| 产品代码 / 测试变化 | 无 |
| B1 负证据变化 | 无 |

## 11. 结论和 28 题 Gate

阶段状态：**PARTIAL**

验收结论：**PARTIALLY_ACCEPTED**

矩阵准入：**NOT_READY_FOR_28_TASK_MATRIX**

预算修正目标已经完成：Gate 1 通过，所有真实运行在公开预算内，没有 terminal final reserve failure。五题也都有 terminal、fresh final 三件套和 final digest。

但“五个实际 Mode 正确”这一硬条件只完成 4/5。STRUCTURAL_FIX 能力在本阶段没有被执行，因此不能进入 28 题正式矩阵，也不能写成五类 Anchor 全部通过。

## 12. 建议

下一阶段应只补结构能力证据：

1. 优先从公开任务中寻找可重复产生 baseline CoSim failure 的结构 Anchor；
2. 在正式冻结前对 baseline 至少重复验证，排除一次性 timeout；
3. 不修改 Router 事实优先级，不向 Planner 注入 acceptance/oracle facts；
4. 不复用本次 run dir；
5. STRUCTURAL_FIX terminal、fresh final 三件套、Ledger、manifest、digest、接口和隔离全部通过后，再复核 5/5 Gate；
6. 未得到明确批准前，不启动 28 题或多模型矩阵。

## 13. 证据索引

- Gate 1：`token-budget-preflight.json`、`token-budget-preflight.zh-CN.md`
- Revised plan：`revised-frozen-run-plan.json`、`revised-frozen-run-plan.zh-CN.md`
- Run index：`anchor-run-index.jsonl`
- Result summary：`anchor-result-summary.json`
- Budget reconciliation：`anchor-budget-reconciliation.json`
- Failure taxonomy：`anchor-failure-taxonomy.json`
- Artifact integrity：`anchor-artifact-integrity.json`
- 证据目录：`docs/experiments/artifacts/2026-07-23-phase-b11-budget-fix-five-anchors/`
