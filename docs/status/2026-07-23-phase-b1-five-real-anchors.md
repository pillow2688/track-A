# Phase B1：当前冻结版本五类真实 Anchor 状态

## 阶段结论

- 阶段状态：**BLOCKED**
- 验收结论：**BLOCKED**
- 28 题矩阵准入：**NOT_READY_FOR_28_TASK_MATRIX**
- 计划 Anchor：5
- 实际启动：2
- Terminal：2
- PASS / FAIL / NOT_RUN：0 / 2 / 3

两类独立 Mode 的真实运行都在 Planner 调用前触发同一项冻结预算门禁：

- `STRUCTURAL_FIX`：可用 8,000 token，小于候选加 fresh final 闭环所需的 10,562 token；
- `REPAIR`：可用 8,000 token，小于候选加 fresh final 闭环所需的 8,511 token。

这证明 Phase B1 冻结的每题 8,000-token 计划与当前仓库的 final reserve 策略不兼容。根据本阶段“预算缺陷不得改参数后继续、共享缺陷立即停止全部真实运行”的规则，后三类 Anchor 未启动，未修改产品代码，也未进行外部重跑。

## 阶段目标

本阶段原计划在同一冻结提交、同一 DeepSeek 模型、同一 Vitis 2025.2 工具链和同一预算策略下，验收：

1. `STRUCTURAL_FIX`；
2. `REPAIR`；
3. `SYNTH_FIX`；
4. `OPTIMIZE`；
5. 空 Stub Generation。

只有五题全部形成 terminal run，且五题 fresh final CSim、Synth、CoSim 全部 PASS，才允许进入当前版本 28 题正式矩阵。本次未满足该 Gate。

## 冻结基线

- 分支：`feat/track-a-empty-stub-generation-smoke`
- HEAD：`4a05763b593a527878a0056f64763126c58ee63b`
- Router 修复 commit：`e5ba32a032432579bb9daa8015d2f705cff93498`
- Router 修复是冻结 HEAD 的祖先：是
- 冻结配置 SHA-256：`c5dd5c6c99805b54ebb129b67d06c60248be4184da10bc77c24be3ed2ab594f2`
- 运行期间 HEAD 是否变化：否
- 运行期间产品代码或测试是否变化：否

Phase B1 开始前已有的 README、交接文档和 Phase A.1 未跟踪证据继续保留，不属于本阶段产品代码修改。

## 统一模型和工具配置

| 配置 | 冻结值 |
|---|---|
| Planner | `openai-compatible` |
| 模型 alias | `deepseek-v4-pro` |
| Backend | `vitis` |
| Vitis | 2025.2 |
| Validation profile | `fast-experiment` |
| Final policy | `full_internal_audit` |
| Continuation | `shadow` |
| Experience | `shadow` |
| Strategy Ranker | `bayesian_shadow` 等价实现 |
| 最大 Planner 轮数 | 2 |
| 每题 token cap | 8,000 |
| Provider 最大输出 | 4,096 |
| temperature / top_p | 0.0 / 1.0 |
| CSim / Synth / CoSim cost | 1 / 4 / 20 |
| CSim / Synth / CoSim timeout | 300 / 900 / 600 秒 |
| 每题 runtime limit | 3,600 秒 |
| Final reserve | 25 credits |
| 外部自动重试 | 禁止 |

当前 CLI 没有独立的 Ranker 参数。实际等价配置是 `--experience-mode shadow` 构造默认 `BayesianStrategyRanker`，保持 `ADVISORY_ONLY`，不把推荐注入 Planner。本批次 Planner 调用为 0，因此不能据此评价 Ranker 的推荐收益，也没有改变 `BAYESIAN_SHADOW / TRAINING_NOT_READY` 状态。

## 运行前 Gate

| 检查 | 结果 |
|---|---:|
| 完整单元测试 | 556/556 PASS；0 failure；0 error |
| `compileall` | PASS |
| `git diff --check` | PASS |
| DeepSeek 环境 | AVAILABLE |
| Vitis 2025.2 | AVAILABLE |
| 运行目录可写 | AVAILABLE |
| 磁盘空间 | AVAILABLE |
| Secret 写入检查 | PASS |

环境证据只记录 `SET / AVAILABLE / UNAVAILABLE` 状态，没有记录 API Key、Authorization 或 endpoint credential。

## 五类 Anchor 结果

| 能力 | 任务 | 实际 Mode | Terminal | Final CSim | Final Synth | Final CoSim | Token | Credit | Latency 变化 | 结论 |
|---|---|---|---|---|---|---|---:|---:|---|---|
| STRUCTURAL_FIX | `v3d_fast_018` | STRUCTURAL_FIX | FAILED | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 25 | UNKNOWN | FINAL_RESERVE_FAILURE |
| REPAIR | `v3d_fast_002` | REPAIR | FAILED | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 1 | UNKNOWN | FINAL_RESERVE_FAILURE |
| SYNTH_FIX | `v3d_fast_010` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 全局停止后未启动 |
| OPTIMIZE | `v3d_fast_022` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 全局停止后未启动 |
| 空 Stub | `track_a_empty_stub_generation` | NOT_EVALUATED | NOT_RUN_GLOBAL_BUDGET_DEFECT | NOT_RUN | NOT_RUN | NOT_RUN | 0 | 0 | UNKNOWN | 全局停止后未启动 |

### STRUCTURAL_FIX

`v3d_fast_018` 使用真实 Vitis 2025.2 完成 baseline：

- CSim：PASS，3.437 秒；
- Synth：PASS，10.276 秒；
- baseline latency：21 cycles；
- baseline II：22；
- estimated clock：2.278 ns；
- LUT / FF：398 / 157；
- CoSim：TIMEOUT，600.017 秒。

Synth 和 CoSim 证据都出现 FIFO / dataflow / deadlock 相关信息，Router 正确选择 `STRUCTURAL_FIX`，原因是 `REQUIRED_BASELINE_COSIM_FAILED`。随后 final reserve gate 在 Planner 前阻断：`tokens:8000<10562`。因此没有 Patch、Candidate promotion、final candidate 或 fresh final 验证。

### REPAIR

`v3d_fast_002` 使用真实 Vitis 2025.2 得到 baseline CSim FAIL，公开测试首个差异为 `expected -8, got -3`。Router 正确选择 `REPAIR`，原因是 `BASELINE_CSIM_FAILED`。随后 final reserve gate 在 Planner 前阻断：`tokens:8000<8511`。同样没有 Patch、Candidate promotion、final candidate 或 fresh final 验证。

### SYNTH_FIX、OPTIMIZE 与空 Stub

三题均未启动。原因不是总时间窗口，也不是环境故障，而是前两种独立 Mode 已证明同一冻结 token cap 与当前候选加 fresh final 预算策略不兼容。继续运行只会消耗 baseline 工具预算，且违反冻结阶段的共享预算缺陷停止规则。

因此：

- OPTIMIZE latency：`UNKNOWN`
- OPTIMIZE acceleration：`UNKNOWN`
- OPTIMIZE 结果分类：`NOT_EVALUATED`
- 空 Stub generation 能力：本阶段没有形成新证据

## Token、工具和时间

| 指标 | 实际值 | Phase 上限 |
|---|---:|---:|
| Planner calls | 0 | 10 |
| Provider requests | 0 | 10 |
| Token | 0 | 40,000 |
| CSim | 2 | 20 |
| Synth | 1 | 16 |
| CoSim | 1 | 8 |
| Tool Credits | 26 | 244 |
| Ledger runtime | 618.966 秒 | — |
| 外部重试 | 0 | 仅允许基础设施故障一次 |

没有预算超限；真正的问题是冻结 token cap 低于开始 Planner 前必须预留的最低 token。两题 Ledger 都完整对账，`token_usage_complete=true`。

## 失败类型

主要失败分类为 `FINAL_RESERVE_FAILURE`：

- 已启动且触发：2；
- 因共享缺陷传播而未运行：3；
- 次要 baseline 观测：1 个 `COSIM_TIMEOUT`、1 个 `CSIM_FAIL`；
- 外部重试：0。

`v3d_fast_018` 的 baseline CoSim timeout 是进入 `STRUCTURAL_FIX` 的真实依据，但 terminal 主因仍是 Planner 前的 final reserve failure。不得把 baseline 工具失败误写成 final 验收失败，也不得把 0 token 写成“模型调用成功”。

## Artifact、接口与信息隔离

- 两个 terminal run 都生成 `v3a.package-manifest.v1`；
- canonical manifest digest 与 terminal 记录一致：2/2；
- manifest 列出的 Artifact：46/46 通过 SHA-256 和 byte-size 复核；
- Ledger：2/2 对账；
- baseline source digest：2/2 可绑定；
- final source digest：0/5，因为没有 final candidate；
- Planner input/output、Patch Validator、TopInterfaceGuard：未到达；
- 接口 mutation：未发生；
- 运行任务来自公共 V3-D task corpus；
- 未读取或注入 hidden、reference、golden 内容；
- 未写入 API Key、Authorization、license 或 endpoint credential。

## 运行后检查

| 检查 | 结果 |
|---|---:|
| 完整单元测试 | 556/556 PASS；0 failure；0 error |
| `compileall` | PASS |
| `git diff --check` | PASS |
| HEAD | 仍为 `4a05763b593a527878a0056f64763126c58ee63b` |
| 产品代码 / 测试变化 | 无 |

## 是否允许进入 28 题正式矩阵

**NOT_READY_FOR_28_TASK_MATRIX**

不满足的硬条件包括：

- 只有 2/5 Anchor 形成 terminal run；
- 0/5 final CSim PASS；
- 0/5 final Synth PASS；
- 0/5 final CoSim PASS；
- 0/5 final source digest；
- 存在共享冻结预算计划缺陷；
- 存在 final reserve failure。

## 未完成项

1. 修正并重新冻结与当前 reserve policy 一致的 per-task token 计划；
2. 在新的阶段、全新 run 目录中重新执行五类 Anchor；
3. 完成五题 fresh final CSim / Synth / CoSim；
4. 得到 OPTIMIZE 的真实 latency 与 acceleration 分类；
5. 得到空 Stub 的本版本真实生成闭环；
6. 五题全部通过后，才重新判断 28 题矩阵准入。

本阶段已停止，不启动 28 题矩阵，不修改产品代码，不自动提交、push 或创建 PR。
