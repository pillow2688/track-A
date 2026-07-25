# Phase B1.1：Anchor Token 预算修正与五类真实验收重跑验收报告

## 验收结论

- 阶段：Phase B1.1
- 阶段状态：**PARTIAL**
- 验收结论：**PARTIALLY_ACCEPTED**
- 28 题矩阵准入：**NOT_READY_FOR_28_TASK_MATRIX**

预算计划修正验收通过；五类能力验收通过 4/5。结构目标任务没有稳定复现 baseline CoSim failure，实际 Mode 为 OPTIMIZE，因此 STRUCTURAL_FIX 能力没有在本阶段被执行。不得把 5/5 fresh final 三件套通过等同于 5/5 Mode Gate 通过。

## 验收范围

本报告验收：

1. B1 历史预算缺陷是否被正确保留；
2. 五类 Anchor 的离线 Token 预算预检；
3. revised frozen plan 是否在公开约束内；
4. 当前 HEAD 下五个公开 Anchor 的真实运行；
5. actual Mode、Planner、Token、Vitis 和 terminal；
6. fresh final CSim、Synth、CoSim；
7. Ledger、manifest、digest、接口安全和信息隔离；
8. 28 题矩阵准入。

本报告不验收 28 题矩阵、多模型矩阵、Continuation enforce、Experience guided、learned Strategy Ranker 或 hidden 最终成绩。

## 冻结基线验收

| 项目 | 结果 |
|---|---:|
| Branch | `feat/track-a-empty-stub-generation-smoke` |
| Initial / Final HEAD | `4a05763b593a527878a0056f64763126c58ee63b` |
| HEAD 不变 | PASS |
| 产品代码 / 测试不变 | PASS |
| Plan-only change | PASS |
| Revised plan digest | `330c6e7334e59408e14db51a40a1ee3b35d4bc45b95d5460c61c3b0750e5ee5a` |
| 自动 commit / push / PR | 无 |

Phase B1 三份报告没有被覆盖，结束时 SHA-256 与阶段开始记录一致。

## Gate 1 验收

| 能力 | 最低需求 | Frozen cap | 公开上限 | 结果 |
|---|---:|---:|---:|---|
| REPAIR | 8,511 | 8,639 | 32,768 | PASS |
| SYNTH_FIX | 9,073 | 9,201 | 32,768 | PASS |
| STRUCTURAL_FIX | 10,563 | 10,691 | 32,768 | PASS |
| OPTIMIZE | 10,674 | 10,802 | 32,768 | PASS |
| 空 Stub | 9,654 | 9,782 | 32,768 | PASS |

| Gate | 结果 |
|---|---:|
| Token 下界计算可追溯 | PASS |
| Fixed policy 与 Tool Credit reserve 分账 | PASS |
| 五题公开预算可满足 | PASS |
| 完整测试 | 556/556 PASS |
| `compileall` | PASS |
| `git diff --check` | PASS |
| Gate 1 决定 | `FROZEN_GATE1_PASS` |

## 五类 Anchor 验收

| 能力 | 任务 | Token Cap | 最低需求 | 实际 Mode | Planner | Token | Final CSim | Final Synth | Final CoSim | Credit | Terminal | 结论 |
|---|---|---:|---:|---|---:|---:|---|---|---|---:|---|---|
| REPAIR | `v3d_fast_002` | 8,639 | 8,511 | REPAIR | 3 dispatch / 1 response | 1,521 | PASS | PASS | PASS | 32 | DONE_AFTER_ALLOWED_RETRY | PASS |
| SYNTH_FIX | `v3d_fast_010` | 9,201 | 9,073 | SYNTH_FIX | 1 | 1,743 | PASS | PASS | PASS | 35 | DONE | PASS |
| STRUCTURAL_FIX | `v3d_fast_018` | 10,691 | 10,563 | OPTIMIZE | 0 | 0 | PASS | PASS | PASS | 50 | DONE | FAIL：MODE_MISMATCH |
| OPTIMIZE | `v3d_fast_022` | 10,802 | 10,674 | OPTIMIZE | 1 | 2,573 | PASS | PASS | PASS | 35 | DONE | PASS |
| 空 Stub | `track_a_empty_stub_generation` | 9,782 | 9,654 | REPAIR | 1 | 2,109 | PASS | PASS | PASS | 31 | DONE | PASS |

### REPAIR 重试验收

- 初次失败类别：`TRANSIENT_MODEL_TRANSPORT`
- 初次 Provider Token：0
- 初次证据保留：PASS
- 是否满足 frozen plan 的一次外部重试规则：是
- Retry dir 是否全新：是
- Retry terminal：DONE
- 最终结论：PASS

### STRUCTURAL_FIX 差异验收

- 预期 baseline：CSim/Synth PASS、CoSim fail/timeout；
- B1 实际：CoSim 600 秒 TIMEOUT，Mode=STRUCTURAL_FIX；
- B1.1 实际：CoSim 约 25.989 秒 PASS，Mode=OPTIMIZE；
- Router 是否遵循当次 baseline facts：是；
- 是否允许为获得预期 Mode 而重跑：否；
- 是否执行 STRUCTURAL_FIX Planner/Patch：否；
- 失败分类：`ANCHOR_PRECONDITION_NOT_REPRODUCED / MODE_MISMATCH`；
- 是否为共享框架缺陷：否；
- 本项结论：FAIL。

## Fresh final 和 digest 验收

| 项目 | 结果 |
|---|---:|
| 五题 terminal run | 5/5 PASS |
| Fresh final CSim | 5/5 PASS |
| Fresh final Synth | 5/5 PASS |
| Fresh final CoSim | 5/5 PASS |
| Final source digest | 5/5 PASS |
| 实际 Mode 正确 | 4/5 FAIL |

结构目标 run 的 final 三件套验证的是 actual OPTIMIZE/baseline finalization，不是 STRUCTURAL_FIX 修复候选，因此 actual Mode Gate 仍失败。

## 预算验收

| 项目 | 实际 | 上限 | 结果 |
|---|---:|---:|---|
| Planner dispatch | 6 | 10 | PASS |
| Input / Output / Total Token | 5,693 / 2,253 / 7,946 | 49,115 total | PASS |
| CSim | 15 | 20 | PASS |
| Synth | 12 | 16 | PASS |
| CoSim | 6 | 8 | PASS |
| Tool Credits | 183 | 244 | PASS |
| Ledger runtime | 358.174622 秒 | — | PASS |
| Ledger reconciliation | 6/6 | 6/6 | PASS |
| Terminal final reserve failure | 0 | 0 | PASS |

所有总数包含 REPAIR 初次传输失败。`ROUND_SKIPPED_FINAL_RESERVE` 是停止额外探索，不是 terminal final reserve failure。

## OPTIMIZE 验收

| 指标 | 结果 |
|---|---:|
| Final correctness | CSim/Synth/CoSim PASS |
| Latency | 9 → 2 |
| Acceleration | 4.5× |
| Transaction interval | 8 → 3 |
| Clock | 1.481 → 0.731 ns |
| LUT / FF | 309/11 → 1380/3 |
| Performance-Area | `PERFORMANCE_AREA_TRADEOFF` |
| Pareto | `NON_DOMINATED` |
| Power | `UNSUPPORTED（NOT_COLLECTED）` |
| 结论 | PASS |

## Artifact 与安全验收

| 项目 | 结果 |
|---|---:|
| Terminal manifests | 6/6 PASS |
| Manifest Artifact SHA/size | 382/382 PASS |
| Manifest mismatch | 0 |
| Ledger mismatch | 0 |
| Patch candidate TopInterfaceGuard | 4/4 allowed，LOW |
| Interface mutation | 0 |
| hidden accessed | false |
| reference accessed | false |
| golden accessed | false |
| Secret value recorded | false |
| Continuation | SHADOW / INSUFFICIENT_EVIDENCE |
| Experience | SHADOW |
| Strategy Ranker | BAYESIAN_SHADOW / TRAINING_NOT_READY |

## 运行后 Gate

| Gate | 结果 |
|---|---:|
| 完整测试 | 556/556 PASS |
| Failure / Error | 0 / 0 |
| `compileall` | PASS |
| `git diff --check` | PASS |
| HEAD 不变 | PASS |
| 产品代码 / 测试不变 | PASS |
| B1 负证据不变 | PASS |

## 28 题准入逐项判定

| 条件 | 结果 |
|---|---:|
| 五个 Anchor 全部 terminal | PASS |
| 五个实际 Mode 正确 | **FAIL（4/5）** |
| 五个 Planner 预算门禁正常 | **FAIL / NOT_EVALUATED（实际调用路径 4/4 正常；STRUCTURAL_FIX 路径未到达 Planner）** |
| 五个 final CSim PASS | PASS |
| 五个 final Synth PASS | PASS |
| 五个 final CoSim PASS | PASS |
| 五个 final source digest | PASS |
| Ledger 全对账 | PASS |
| 公开 Token 预算内 | PASS |
| 无 terminal final reserve failure | PASS |
| 无共享框架缺陷 | PASS |
| 无接口修改 | PASS |
| 无 hidden/reference/golden 泄漏 | PASS |
| 运行期间 HEAD 不变 | PASS |

有一项硬条件失败，最终准入为：

```text
NOT_READY_FOR_28_TASK_MATRIX
```

## 最终验收

**Phase B1.1：PARTIALLY_ACCEPTED**

接受的部分：

- B1 预算缺陷已准确修正；
- 五题离线预算全部可满足；
- Revised frozen plan 已冻结；
- 5/5 terminal、5/5 fresh final 三件套、5/5 digest；
- REPAIR、SYNTH_FIX、OPTIMIZE、空 Stub 四类能力通过；
- 预算、Ledger、manifest、接口、安全和运行后 Gate 通过。

不接受的部分：

- STRUCTURAL_FIX 前置条件未复现；
- 五个 actual Mode 只命中 4/5；
- 当前版本五类 Anchor Gate 未完全闭合；
- 不允许进入 28 题矩阵。

## 后续 Gate

下一阶段只应补齐一个稳定的 STRUCTURAL_FIX 公开 Anchor，并先验证 baseline 故障可重复。不得为触发 STRUCTURAL_FIX 修改 Router、强制 Mode、注入 oracle facts 或重复运行直到偶发 timeout。结构能力真实通过后，再复核五类 Gate。

本验收到此停止，不启动 28 题矩阵，不自动提交、push 或创建 PR。

## 证据索引

- 状态：`docs/status/2026-07-23-phase-b11-budget-fix-five-anchors.md`
- 实验报告：`docs/experiments/2026-07-23-phase-b11-budget-fix-five-anchors-report.zh-CN.md`
- 证据目录：`docs/experiments/artifacts/2026-07-23-phase-b11-budget-fix-five-anchors/`
- 预算预检：`token-budget-preflight.json`
- 冻结计划：`revised-frozen-run-plan.json`
- Run index：`anchor-run-index.jsonl`
- 结果：`anchor-result-summary.json`
- 预算对账：`anchor-budget-reconciliation.json`
- 失败分类：`anchor-failure-taxonomy.json`
- Artifact 完整性：`anchor-artifact-integrity.json`
