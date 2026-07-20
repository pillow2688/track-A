# V3-D 实验表（事实快照）

> 自动生成于 2026-07-20T17:29:13+00:00，事实来源：`evidence_manifest.json` 指定的 run JSON。
> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。

## 当前能力矩阵

| Mode | 当前证据 | Run IDs |
| --- | --- | --- |
| REPAIR | 真实 Vitis replay 闭环；真实 LLM 待补 | v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE, v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE, v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE, v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE |
| SYNTH_FIX | 真实 Vitis replay 闭环；真实 LLM 待补 | v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM |
| STRUCTURAL_FIX | 真实 Vitis replay 闭环；真实 LLM 待补 | v3d-residual-structural-replay-r04-NOT_REAL_LLM |
| OPTIMIZE | 真实 LLM + Vitis 闭环 | v3b_fast_dotproduct_live_deepseek_retry2 |

## 模型 × 任务运行

| Model | Task | Mode | Patch provider | Run status | Fresh final | Run ID |
| --- | --- | --- | --- | --- | --- | --- |
| deepseek-v4-pro | dotProduct_optimize | OPTIMIZE | REAL_LLM | DONE | PASS | v3b_fast_dotproduct_live_deepseek_retry2 |
| deepseek-v4-pro | projection_bugfix | REPAIR | REAL_LLM | FAILED | FAIL/NOT RUN | v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE |
| deepseek-v4-pro | projection_bugfix | REPAIR | REAL_LLM | FAILED | FAIL/NOT RUN | v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE |
| deepseek-v4-pro | projection_bugfix | REPAIR | REAL_LLM | FAILED | FAIL/NOT RUN | v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE |
| operator-supplied-patch-v1 | projection_bugfix | REPAIR | SCRIPTED_PATCH_REPLAY | DONE | PASS | v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE |
| operator-supplied-patch-v1 | residual_stream_deadlock | STRUCTURAL_FIX | SCRIPTED_PATCH_REPLAY | DONE | PASS | v3d-residual-structural-replay-r04-NOT_REAL_LLM |
| operator-supplied-patch-v1 | u55c_synthesis_repair | SYNTH_FIX | SCRIPTED_PATCH_REPLAY | DONE | PASS | v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM |

说明：`SCRIPTED_PATCH_REPLAY` 只证明图、Patch 应用和真实 Vitis 闭环，不证明模型能够自主修复。

### Evidence notes

- `v3b_fast_dotproduct_live_deepseek_retry2` [primary]：Real DeepSeek planner and real Vitis 2025.2; fresh final CSim/Synth/CoSim passed.
- `v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE` [failure]：Real model attempt; Patch policy rejected the proposal. Local 40-credit override, not official 20-credit compliance.
- `v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE` [failure]：Real model attempt; Patch policy rejected the proposal. Local 40-credit override, not official 20-credit compliance.
- `v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE` [failure]：Real model attempt; Patch policy rejected the proposal. Local 40-credit override, not official 20-credit compliance.
- `v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE` [closure_replay]：Historical model Patch replay after relocation fix; real Vitis fresh closure, but no model call in this run.
- `v3d-residual-structural-replay-r04-NOT_REAL_LLM` [closure_replay]：Known structural Patch replay; real Vitis deadlock evidence and fresh closure, but no model call.
- `v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM` [closure_replay]：Known dynamic-allocation Patch replay; real Vitis synthesis failure evidence and fresh closure, but no model call.

## 分组成功率

| Patch provider | Mode | Runs | Success | Failure | Success rate |
| --- | --- | --- | --- | --- | --- |
| REAL_LLM | OPTIMIZE | 1 | 1 | 0 | 100.0% |
| REAL_LLM | REPAIR | 3 | 0 | 3 | 0.0% |
| SCRIPTED_PATCH_REPLAY | REPAIR | 1 | 1 | 0 | 100.0% |
| SCRIPTED_PATCH_REPLAY | STRUCTURAL_FIX | 1 | 1 | 0 | 100.0% |
| SCRIPTED_PATCH_REPLAY | SYNTH_FIX | 1 | 1 | 0 | 100.0% |

## Token、Credit 与工具次数

| Run ID | Tokens | Credits | LLM | CSim | Synth | CoSim | Wall time (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| v3b_fast_dotproduct_live_deepseek_retry2 | 8128 | 35 | 3 | 3 | 3 | 1 | 130.78 |
| v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE | 2034 | 1 | 1 | 1 | 0 | 0 | 9.81 |
| v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE | 2027 | 1 | 1 | 1 | 0 | 0 | 9.45 |
| v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE | 2033 | 1 | 1 | 1 | 0 | 0 | 9.63 |
| v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE | 0 | 31 | 0 | 3 | 2 | 1 | 67.17 |
| v3d-residual-structural-replay-r04-NOT_REAL_LLM | 0 | 71 | 0 | 3 | 2 | 3 | 114.93 |
| v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM | 0 | 35 | 0 | 3 | 3 | 1 | 67.95 |

## Latency 与 acceleration

| Run ID | Baseline cycles | Final cycles | Acceleration | Profile | Evidence |
| --- | --- | --- | --- | --- | --- |
| v3b_fast_dotproduct_live_deepseek_retry2 | 1027 | 38 | 27.026 | fast-experiment | REAL_VITIS_VALIDATED |
| v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE | — | 0 | — | fast-experiment | REAL_VITIS_VALIDATED |
| v3d-residual-structural-replay-r04-NOT_REAL_LLM | 135 | 68 | 1.985 | fast-experiment | REAL_VITIS_VALIDATED |
| v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM | — | 39 | — | fast-experiment | REAL_VITIS_VALIDATED |

## strict / fast-experiment

- fast-experiment：本表已有事实见上表。
- strict：**TODO**。需要在相同任务、模型、预算和 Prompt 下新增 strict run，当前不能比较。

## 消融入口

- 无结构化 Evidence：**TODO**。需要固定模型、任务、随机性和预算，关闭 Evidence 后重复运行。
- 无 CoSim risk gate：**TODO**。需要固定其他配置并记录 CoSim 次数、Credit 与最终通过率。

## Batch benchmark（真实与 deterministic 不混算）

| Summary | Fingerprint | Tasks | Records | Real runs | Real E2E rate | Real fresh-final rate |
| --- | --- | --- | --- | --- | --- | --- |
| docs/submission/benchmark_snapshots/v3d-benchmark-deterministic-diverse-final-20260720-r03.json | bdcd3a67692721f7 | 28 | 28 | 0 | — | — |

| Summary | Mode | Runs | Success | Success rate | Failure stages |
| --- | --- | --- | --- | --- | --- |
| docs/submission/benchmark_snapshots/v3d-benchmark-deterministic-diverse-final-20260720-r03.json | OPTIMIZE | 8 | 0 | — | {} |
| docs/submission/benchmark_snapshots/v3d-benchmark-deterministic-diverse-final-20260720-r03.json | REPAIR | 8 | 0 | — | {} |
| docs/submission/benchmark_snapshots/v3d-benchmark-deterministic-diverse-final-20260720-r03.json | STRUCTURAL_FIX | 6 | 0 | — | {} |
| docs/submission/benchmark_snapshots/v3d-benchmark-deterministic-diverse-final-20260720-r03.json | SYNTH_FIX | 6 | 0 | — | {} |

### Deterministic full-corpus 明细（只验证编排）

| Summary | Runs | Router | E2E | Fresh final | C/S/Co/L | Optimize N | Optimize accel mean | Resumed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| docs/submission/benchmark_snapshots/v3d-benchmark-deterministic-diverse-final-20260720-r03.json | 28 | — | — | — | 0/0/0/0 | 0 | — | 28 |

## Corpus Oracle 与真实 Vitis anchors

`docs/submission/oracle_snapshots/v3d-oracle-deterministic-diverse-final-20260720-r02.json`：accepted=28，rejected=0，pending=0，real anchors=0，resume=28；backend=deterministic（fixture only）。 Release `llm4hls_harness/releases/v3d-corpus-oracle-anchors-2026-07-20.json` 记录 deterministic checks=134，accepted/rejected=28/0。 可提交 receipt bundle 绑定 12 题、238 个 artifact hashes；它是 REAL_VITIS_NO_LLM 证据，不是 Agent 成绩。

| Mode | Task | Run ID | Status | Checks | Baseline | Golden | Acceleration | Wall (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| REPAIR | v3d_fast_001 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 3 | — | — | — | 16.183 |
| REPAIR | v3d_fast_002 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 3 | — | — | — | 16.369 |
| REPAIR | v3d_fast_003 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 3 | — | — | — | 16.431 |
| SYNTH_FIX | v3d_fast_009 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 4 | — | — | — | 25.758 |
| SYNTH_FIX | v3d_fast_010 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 4 | — | — | — | 25.729 |
| SYNTH_FIX | v3d_fast_011 | v3d-oracle-vitis-opaque-anchors-a02 | ACCEPTED | 4 | — | — | — | 27.514 |
| STRUCTURAL_FIX | v3d_fast_015 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 6 | — | — | — | 82.190 |
| STRUCTURAL_FIX | v3d_fast_016 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 6 | — | — | — | 81.811 |
| STRUCTURAL_FIX | v3d_fast_017 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 6 | — | — | — | 81.407 |
| OPTIMIZE | v3d_fast_021 | v3d-oracle-vitis-opaque-anchors-a02 | ACCEPTED | 6 | 18 | 6 | 3 | 26.655 |
| OPTIMIZE | v3d_fast_022 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 6 | 9 | 6 | 1.500 | 26.701 |
| OPTIMIZE | v3d_fast_028 | v3d-oracle-vitis-opaque-anchors-a01 | ACCEPTED | 8 | 39 | 6 | 6.500 | 80.351 |

### 已降级的历史 anchor

| Run ID | Task | Reason |
| --- | --- | --- |
| v3d-oracle-vitis-opaque-anchors-a01 | v3d_fast_011 | The former function-pointer mutation synthesized successfully; A02 replaced it with an actual unsupported indirect std::function call. |
| v3d-oracle-vitis-opaque-anchors-a01 | v3d_fast_021 | The former baseline retained enough pragmas to match golden latency; A02 replaced it with a genuinely serial baseline. |

## 模型矩阵缺口

- TODO：配置真实 endpoint/key/model 后，运行 DeepSeek 官方三题各3次及 Qwen 同配置矩阵；当前没有可报告的新矩阵平均值或成功率。
