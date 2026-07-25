# Phase B1：当前冻结版本五类真实 Anchor 验收报告

## 验收范围

本报告验收 HEAD `4a05763b593a527878a0056f64763126c58ee63b` 下五类真实 Anchor 的统一运行协议、真实 Artifact、预算、fresh final 闭环、安全边界和 28 题矩阵准入。

本报告不验收：

- 当前版本 28 题正式矩阵；
- 多模型矩阵；
- Continuation enforce；
- Experience guided；
- learned Strategy Ranker；
- hidden 最终成绩。

## 仓库冻结基线

- Branch：`feat/track-a-empty-stub-generation-smoke`
- HEAD：`4a05763b593a527878a0056f64763126c58ee63b`
- Router commit：`e5ba32a032432579bb9daa8015d2f705cff93498`
- 冻结配置 SHA-256：`c5dd5c6c99805b54ebb129b67d06c60248be4184da10bc77c24be3ed2ab594f2`
- 运行期间 HEAD 变化：无
- 运行期间产品代码/测试变化：无
- 自动 commit / push / PR：无

## 运行前 Gate

| Gate | 结果 |
|---|---:|
| 完整测试 | 556/556 PASS |
| Failure / Error | 0 / 0 |
| `compileall` | PASS |
| `git diff --check` | PASS |
| DeepSeek 环境 | AVAILABLE |
| Vitis 2025.2 | AVAILABLE |
| 输出目录与磁盘 | AVAILABLE |
| Secret 安全检查 | PASS |
| Router fix 在 HEAD 中 | PASS |

运行前 Gate 全部通过，允许开始第一个真实 Anchor。

## 统一配置指纹

统一配置为 `deepseek-v4-pro`、OpenAI-compatible Planner、Vitis 2025.2、`fast-experiment`、`full_internal_audit`、Continuation shadow、Experience shadow、Bayesian Ranker shadow、两轮 Planner、每题 8,000 token、CSim/Synth/CoSim cost 1/4/20。

实际配置指纹：

```text
c5dd5c6c99805b54ebb129b67d06c60248be4184da10bc77c24be3ed2ab594f2
```

温度为 0.0，top_p 为 1.0，Provider 最大输出为 4,096 token。配置在运行期间未修改。

## 五类 Anchor 结果

| 能力 | 任务 | 实际 Mode | Terminal | Final CSim | Final Synth | Final CoSim | Token | Credit | Latency变化 | 结论 |
|---|---|---|---|---|---|---|---:|---:|---|---|
| STRUCTURAL_FIX | `v3d_fast_018` | STRUCTURAL_FIX | FAILED | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 25 | UNKNOWN | FINAL_RESERVE_FAILURE |
| REPAIR | `v3d_fast_002` | REPAIR | FAILED | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 1 | UNKNOWN | FINAL_RESERVE_FAILURE |
| SYNTH_FIX | `v3d_fast_010` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 未启动 |
| OPTIMIZE | `v3d_fast_022` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 未启动 |
| 空 Stub | `track_a_empty_stub_generation` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 未启动 |

两个实际 Mode 与预期一致，但两题都没有到达 Planner。五类能力的端到端验收均未通过。

## Fresh final 验证

| 指标 | 结果 |
|---|---:|
| Final candidate | 0/5 |
| Final source digest | 0/5 |
| Fresh final CSim PASS | 0/5 |
| Fresh final Synth PASS | 0/5 |
| Fresh final CoSim PASS | 0/5 |

`full_internal_audit` 要求五题都执行 fresh final 三件套。两个 terminal run 的 final 字段均为 `NOT_RUN`，这是硬性验收失败，不能用 baseline 验证代替。

## 预算对账

| 指标 | 实际 |
|---|---:|
| Planner calls | 0 |
| Provider requests | 0 |
| Input / Output / Total token | 0 / 0 / 0 |
| CSim / Synth / CoSim | 2 / 1 / 1 |
| Tool Credits | 26 |
| Ledger runtime | 618.966063 秒 |
| Started run Ledger 对账 | 2/2 |
| 预算超限 | 0 |
| Final reserve 不足 | 2/2 started |
| 外部重试 | 0 |

STRUCTURAL_FIX reserve gate 为 `8000<10562`；REPAIR reserve gate 为 `8000<8511`。门禁发生在模型请求前，所以真实 token 和 Provider requests 都为 0。

## Artifact 完整性

| 项目 | 结果 |
|---|---:|
| Terminal package manifest | 2/2 |
| Canonical manifest digest 匹配 | 2/2 |
| Manifest Artifact SHA/size | 46/46 |
| Baseline source digest | 2/2 |
| Final source digest | 0/5 |
| Manifest mismatch | 0 |
| Ledger mismatch | 0 |

两个失败 run 均完整保留，没有覆盖、删除或伪装成成功。未启动任务没有伪造 run 目录或 terminal Artifact。

## 失败与重试

主要分类：`FINAL_RESERVE_FAILURE`。

- `v3d_fast_018`：baseline CSim/Synth PASS、CoSim TIMEOUT，正确进入 STRUCTURAL_FIX；随后 token reserve gate 阻断；
- `v3d_fast_002`：baseline CSim FAIL，正确进入 REPAIR；随后 token reserve gate 阻断；
- 其余三题：共享冻结预算计划缺陷触发全局停止，没有启动；
- 外部重试：0。

这里采用 `BLOCKED` 而不是 `REJECTED`：已发现的是冻结运行计划与当前 reserve policy 的确定性不兼容，Planner、Patch 和 final 能力尚未被执行，不能据此判定产品能力本身不合格。但本阶段交付也不能写为 `PARTIALLY_ACCEPTED`，因为五类 Anchor 没有一题形成合格的 fresh final 闭环。

## 安全边界

- 未访问 hidden、reference 或 golden 实现；
- 未把 acceptance/oracle facts 传入 Planner；
- 实际 Planner 请求为 0；
- 未发生 interface mutation；
- API Key、Authorization、license 和 endpoint credential 未写入证据；
- Continuation 保持 `SHADOW / INSUFFICIENT_EVIDENCE`；
- Experience 保持 `SHADOW`；
- Strategy Ranker 保持 `BAYESIAN_SHADOW / TRAINING_NOT_READY`；
- 未修改旧 V3-F 结论；
- 未启动 28 题矩阵。

## 运行后 Gate

| Gate | 结果 |
|---|---:|
| 完整测试 | 556/556 PASS |
| Failure / Error | 0 / 0 |
| `compileall` | PASS |
| `git diff --check` | PASS |
| HEAD 不变 | PASS |
| 产品代码/测试不变 | PASS |

## 验收结论

**Phase B1 验收：BLOCKED**

**阶段状态：BLOCKED**

**28 题矩阵准入：NOT_READY_FOR_28_TASK_MATRIX**

阻断条件：

1. 只有 2/5 Anchor 启动并 terminal；
2. 0/5 Anchor PASS；
3. 0/5 fresh final CSim/Synth/CoSim PASS；
4. 0/5 final source digest；
5. 2/2 started run 出现 final reserve failure；
6. SYNTH_FIX、OPTIMIZE、空 Stub 没有当前 HEAD 下的真实证据。

## 下一阶段准入

在新的阶段开始真实运行前，应：

1. 仅修改运行提案，计算每种 Mode 的最小 candidate + fresh final token reserve；
2. 让 per-task token cap 与该下界一致，并重新冻结配置 digest；
3. 保持 HEAD 与产品代码不变，除非另行立项证明是产品代码缺陷；
4. 对五题使用全新 run 目录，不续跑本次失败目录；
5. 重新执行五类 Anchor；
6. 只有五题 terminal、final 三件套全 PASS 且 Ledger/manifest/digest 全部通过，才允许进入 28 题矩阵。

本报告完成后停止，不自动开始下一阶段。

## 证据索引

- 状态：`docs/status/2026-07-23-phase-b1-five-real-anchors.md`
- 实验报告：`docs/experiments/2026-07-23-phase-b1-five-real-anchors-report.zh-CN.md`
- 证据目录：`docs/experiments/artifacts/2026-07-23-phase-b1-five-real-anchors/`
- Frozen plan：`frozen-run-plan.json`
- Run index：`anchor-run-index.jsonl`
- Result summary：`anchor-result-summary.json`
- Budget reconciliation：`anchor-budget-reconciliation.json`
- Failure taxonomy：`anchor-failure-taxonomy.json`
- Artifact integrity：`anchor-artifact-integrity.json`
- STRUCTURAL_FIX raw run：`llm4hls_harness/runs/phase-b1-4a05763-v3d_fast_018-20260723T050704Z/`
- REPAIR raw run：`llm4hls_harness/runs/phase-b1-4a05763-v3d_fast_002-20260723T050704Z/`
