# Phase B1.1：Anchor Token 预算修正与五类真实验收重跑状态

## 阶段结论

- 阶段状态：**PARTIAL**
- 验收结论：**PARTIALLY_ACCEPTED**
- 28 题矩阵准入：**NOT_READY_FOR_28_TASK_MATRIX**
- Gate 1 Token 预算预检：**DONE / PASS**
- Revised frozen budget：**DONE / PASS**
- 计划 Anchor / 实际启动 / Terminal：5 / 5 / 5
- PASS / FAIL / NOT_RUN：4 / 1 / 0
- 预期实际 Mode 命中：4/5
- Fresh final CSim / Synth / CoSim：5/5 / 5/5 / 5/5

Phase B1 的历史负结果保持不变：固定 8,000 Token 计划不能满足 REPAIR 的 8,511 Token 下界和 STRUCTURAL_FIX 的 10,562 Token 下界。本阶段只修订运行计划，没有修改产品代码、测试、Prompt、Router 或候选比较逻辑。

修订后的五题 Token cap 全部通过离线预检，真实运行中没有 terminal `FINAL_RESERVE_FAILURE`。REPAIR、SYNTH_FIX、OPTIMIZE 和空 Stub 四类按预期 Mode 完成了 fresh final 三件套。结构 Anchor `v3d_fast_018` 本次 baseline CoSim 在约 25.989 秒内 PASS，而不是像 B1 中达到 600 秒 timeout；Router 因而依据当前事实正确选择 `OPTIMIZE`，没有进入 `STRUCTURAL_FIX`。该项记为 `ANCHOR_PRECONDITION_NOT_REPRODUCED / MODE_MISMATCH`，不能算作 STRUCTURAL_FIX 验收通过。

## 冻结基线

- 分支：`feat/track-a-empty-stub-generation-smoke`
- HEAD：`4a05763b593a527878a0056f64763126c58ee63b`
- Revised plan SHA-256：`330c6e7334e59408e14db51a40a1ee3b35d4bc45b95d5460c61c3b0750e5ee5a`
- 运行期间 HEAD 变化：无
- 产品代码或测试变化：无
- 运行计划之外的实现变化：无
- 自动 commit / push / PR：无

上一阶段 B1 三份负证据 SHA-256 在阶段结束时保持：

- 状态：`96e4c43531be186a4142119338272e493b10881f36d0bad818332c0fa53e49fa`
- 实验报告：`665b13331dbdfbfa186370cff57f0deecdb209ef602423d1f6d98ca1e18c945b`
- 验收报告：`a3f03ce5774e82e608d3f1b33ffd466244d81d0de699f1eef219d5ce7644f557`

## Gate 1：Token 预算预检

当前运行使用 `token-budget-policy=fixed`。Planner 可负担性门禁按：

```text
required_tokens = estimated_input_tokens + configured_output_cap
allowed = tokens_remaining >= required_tokens
```

进行判断；configured output cap 为 4,096。固定策略下，fresh final CSim/Synth/CoSim 由 25 Tool Credits 保留，不额外预留 Token。每题在离线 `prepare()` 结果或 B1 实测下界上增加现有 CLI 的 128 Token 安全余量。

| 能力 | 任务 | 最低需求 | 修订 Token Cap | 公开上限 | 预检 |
|---|---|---:|---:|---:|---|
| REPAIR | `v3d_fast_002` | 8,511 | 8,639 | 32,768 | PASS |
| SYNTH_FIX | `v3d_fast_010` | 9,073 | 9,201 | 32,768 | PASS |
| STRUCTURAL_FIX | `v3d_fast_018` | 10,563 | 10,691 | 32,768 | PASS |
| OPTIMIZE | `v3d_fast_022` | 10,674 | 10,802 | 32,768 | PASS |
| 空 Stub | `track_a_empty_stub_generation` | 9,654 | 9,782 | 32,768 | PASS |

五题总 cap 为 49,115 Token。预算计划可满足公开单题上限，Gate 1 判定 `FROZEN_GATE1_PASS`。

## 统一真实运行配置

| 配置 | 冻结值 |
|---|---|
| Planner / 模型 | `openai-compatible` / `deepseek-v4-pro` |
| Backend / 工具链 | `vitis` / Vitis 2025.2 |
| Validation / final policy | `fast-experiment` / `full_internal_audit` |
| Token policy | `fixed` |
| 最大 Planner 轮数 | 2 |
| Provider 最大输出 | 4,096 |
| temperature / top_p | 0.0 / 1.0 |
| CSim / Synth / CoSim cost | 1 / 4 / 20 |
| CSim / Synth / CoSim timeout | 300 / 900 / 600 秒 |
| Final reserve | 25 Tool Credits |
| Continuation / Experience | `shadow` / `shadow` |
| Strategy Ranker | `bayesian_shadow`，仅 advisory |

## 五类 Anchor 结果

| 能力 | 任务 | Token Cap | 最低需求 | 实际 Mode | Planner | Token | Final CSim | Final Synth | Final CoSim | Credit | Terminal | 结论 |
|---|---|---:|---:|---|---:|---:|---|---|---|---:|---|---|
| REPAIR | `v3d_fast_002` | 8,639 | 8,511 | REPAIR | 3 次 dispatch / 1 次有效响应 | 1,521 | PASS | PASS | PASS | 32 | DONE_AFTER_ALLOWED_RETRY | PASS |
| SYNTH_FIX | `v3d_fast_010` | 9,201 | 9,073 | SYNTH_FIX | 1 | 1,743 | PASS | PASS | PASS | 35 | DONE | PASS |
| STRUCTURAL_FIX | `v3d_fast_018` | 10,691 | 10,563 | OPTIMIZE | 0 | 0 | PASS | PASS | PASS | 50 | DONE | FAIL：MODE_MISMATCH |
| OPTIMIZE | `v3d_fast_022` | 10,802 | 10,674 | OPTIMIZE | 1 | 2,573 | PASS | PASS | PASS | 35 | DONE | PASS |
| 空 Stub | `track_a_empty_stub_generation` | 9,782 | 9,654 | REPAIR | 1 | 2,109 | PASS | PASS | PASS | 31 | DONE | PASS |

REPAIR 首次物理运行的两次模型调用都以 `URLError` 立即结束，没有 request id 和 Token 消耗。它属于冻结计划允许的一次 `TRANSIENT_MODEL_TRANSPORT` 外部重试；原失败目录和 23 项 manifest 证据完整保留，retry1 在全新目录完成。上表 Credit 32 和 Planner dispatch 3 包含原失败尝试。

## OPTIMIZE 真实性能结果

`v3d_fast_022` 的 candidate_001 完成 fresh final CSim、Synth、CoSim：

- latency：9 → 2 cycles；
- acceleration：4.5×；
- transaction interval：8 → 3；
- estimated clock：1.481 → 0.731 ns；
- LUT：309 → 1,380；
- FF：11 → 3；
- Performance-Area：`PERFORMANCE_AREA_TRADEOFF`；
- Pareto：`NON_DOMINATED`；
- 资源压力：LOW → LOW；
- Power：`UNSUPPORTED`，Artifact 原值为 `NOT_COLLECTED`；
- public validation proxy score：1.475 → 1.7375。

这是以 LUT 增加换取显著 latency、II 和 clock 改善的真实性能闭环，不应写成“面积也改善”。

## 预算与 Artifact 对账

以下总数包含首次 REPAIR 传输失败和一次获准重试：

| 指标 | 实际 | Phase cap | 结果 |
|---|---:|---:|---|
| Planner dispatch | 6 | 10 | PASS |
| 有 Token 用量的 Provider 响应 | 4 | — | — |
| Input / Output / Total Token | 5,693 / 2,253 / 7,946 | 49,115 total | PASS |
| CSim / Synth / CoSim | 15 / 12 / 6 | 20 / 16 / 8 | PASS |
| Tool Credits | 183 | 244 | PASS |
| Ledger runtime | 358.174622 秒 | — | PASS |
| Ledger 对账 | 6/6 | 6/6 | PASS |
| Terminal final reserve failure | 0 | 0 | PASS |
| Manifest | 6/6 | 6/6 | PASS |
| Manifest Artifact SHA/size | 382/382 | 382/382 | PASS |
| Accepted final source digest | 5/5 | 5/5 | PASS |

`ROUND_SKIPPED_FINAL_RESERVE` 在 OPTIMIZE 和结构目标 run 中只是“停止下一轮探索”的原因；两题 terminal final closure gate 都为 allowed，fresh final 三件套均 PASS，不能误分类成 `FINAL_RESERVE_FAILURE`。

## 接口和信息隔离

- 四个实际 Patch candidate 的 `TopInterfaceGuard.allowed=true`，风险均为 LOW；
- `v3d_fast_018` 没有 Patch，baseline digest 与 final digest 相同；
- 没有接口 mutation；
- 没有读取或向 Planner 注入 hidden、reference、golden；
- 证据中没有 API Key 或 Authorization；
- Continuation 保持 shadow；
- Experience 保持 shadow；
- Bayesian Strategy Ranker 保持 advisory-only shadow，没有训练或准入。

## 运行前后 Gate

| 检查 | 运行前 | 运行后 |
|---|---:|---:|
| 完整单元测试 | 556/556 PASS | 556/556 PASS |
| Failure / Error | 0 / 0 | 0 / 0 |
| `compileall` | PASS | PASS |
| `git diff --check` | PASS | PASS |
| HEAD 不变 | — | PASS |
| 产品代码 / 测试不变 | — | PASS |

## 28 题矩阵准入

**NOT_READY_FOR_28_TASK_MATRIX**

已满足：5/5 terminal、5/5 fresh final 三件套、5/5 final digest、Ledger/预算/Manifest/接口/隔离/HEAD Gate。

未满足：五个实际 Mode 只有 4/5 正确；STRUCTURAL_FIX 没有被执行和验收。当前不得启动 28 题正式矩阵。

## 下一步

1. 在不改 Router、不注入预期 Mode 的前提下，选择或构造一个公开、稳定复现 baseline CoSim 失败的 STRUCTURAL_FIX Anchor；
2. 先做重复 baseline 复现，确认结构故障不是一次性 timeout，再冻结新的单题验收计划；
3. 只补齐 STRUCTURAL_FIX 能力证据，并复核五类 Gate；
4. Gate 全部通过后，再单独批准 28 题矩阵；
5. Continuation、Experience 和 Strategy Ranker 继续保持 shadow，不在本阶段训练或升级准入。

本阶段已停止，不启动 28 题矩阵，不自动提交、push 或创建 PR。
