# Phase B1.2：单一 STRUCTURAL_FIX 真实补证报告

## 1. 目标和边界

本阶段只完成一个缺失能力：从现有公开任务中选择 baseline CSim/Synth PASS、baseline CoSim 可稳定 FAIL/TIMEOUT 的任务，确认 Router 进入 `STRUCTURAL_FIX`，再运行一次真实 DeepSeek + Vitis `full_internal_audit`。

明确禁止并实际未执行：

- 重跑 B1.1 的 REPAIR、SYNTH_FIX、OPTIMIZE 和空 Stub；
- 启动 28 题正式矩阵；
- 启动多模型矩阵；
- 修改 Router、Prompt、产品代码、测试或公开任务；
- 强制 Mode 或重复运行直到出现偶发 timeout。

## 2. 公开任务选择

选择：

```text
llm4hls_harness/task_corpus/official/fpt26-harness-public/residual_stream_deadlock
```

任务契约：

- `task_type=structural`
- `requires_cosim=true`
- budget=80 credits
- baseline 设计为 CSim 正确、Synth 可综合，但 bounded RTL FIFO 在 CoSim 中形成循环等待。

该任务已有历史真实 STRUCTURAL_FIX 证据，但本阶段不复用历史 validation 或 Candidate，只用它作为筛选依据。

## 3. 当前 HEAD 稳定性验证

冻结分支 `feat/track-a-empty-stub-generation-smoke`，HEAD `4a05763b593a527878a0056f64763126c58ee63b`。

两次独立 V0 baseline 和正式 run 自带 baseline 均为 fresh、`cached=false`：

| 样本 | CSim | Synth | CoSim | CoSim elapsed |
|---|---|---|---|---:|
| stability1 | PASS | PASS | FAIL | 25.332574 秒 |
| stability2 | PASS | PASS | FAIL | 26.188471 秒 |
| formal baseline | PASS | PASS | FAIL | 27.347160 秒 |

三次 baseline source digest 均为 `a0f40b47dea175d761e22f26edb42ac72f2ba5e25e21fdc460887fe2c639457b`。CoSim 返回码为 1，证据包含 deadlock detector、bounded FIFO 模块和 `C/RTL co-simulation finished: FAIL`。

纯 Router 预判和正式 Graph 决策一致：

```json
{
  "mode": "STRUCTURAL_FIX",
  "reason": "REQUIRED_BASELINE_COSIM_FAILED",
  "requires_cosim": true,
  "validation_status": {
    "csim": "PASS",
    "synth": "PASS",
    "cosim": "FAIL"
  }
}
```

因此该任务满足“稳定结构 Anchor”准入，不依赖偶发 timeout。

## 4. 冻结配置

| 配置 | 值 |
|---|---|
| Model | `deepseek-v4-pro` |
| Planner | OpenAI-compatible |
| Vitis | 2025.2 |
| Token policy / cap | fixed / 32,768 |
| Credit cap | 80 |
| CSim / Synth / CoSim cost | 1 / 4 / 20 |
| CSim / Synth / CoSim timeout | 300 / 900 / 300 秒 |
| Max Planner rounds | 2 |
| Final reserve | 25 credits |
| Validation profile | `fast-experiment` |
| Final policy | `full_internal_audit` |
| Continuation / Experience | shadow / shadow |

仅启动一个真实 Planner run：

```text
llm4hls_harness/runs/phase-b12-4a05763-residual-structural-20260723T124154Z
```

## 5. Planner 和 Patch

Router 在 baseline 后进入 `STRUCTURAL_FIX`。DeepSeek Planner 调用一次，使用：

- input tokens：1,611；
- cached input tokens：1,536；
- output tokens：591；
- total tokens：2,202。

Planner 识别到 stageA 先写完整 `s_main`，再写 `s_skip`，导致 depth=2 的 RTL FIFO 形成循环等待。Patch 将两条 burst loop 合并为一条 loop，对 `s_main` 和 `s_skip` 逐项交错写。

Patch：

- 只修改 candidate 隔离副本；
- baseline 保持只读；
- top signature `void residual(data_t*, data_t*)` 不变；
- TopInterfaceGuard allowed；
- 风险 LOW；
- candidate digest：`9b88f96f98526f06dca992c9524ae4052d77360623f3f11732d070a86d1d5cd8`。

## 6. 真实验证结果

| 阶段 | CSim | Synth | CoSim | cached |
|---|---|---|---|---|
| Baseline | PASS | PASS | FAIL | false |
| Candidate exploration | PASS | NOT_RUN（structural gate 不要求） | PASS | false |
| Fresh final | PASS | PASS | PASS | false |

Candidate exploration CoSim 用时 26.974 秒；fresh final CoSim 用时 27.344 秒，latency 为 97 cycles。

Fresh final Synth：

| 指标 | Baseline | Final |
|---|---:|---:|
| Latency | 135 | 68 |
| Interval | 136 | 64 |
| Clock ns | 2.796 | 2.796 |
| LUT | 539 | 406 |
| FF | 248 | 231 |
| BRAM / DSP / URAM | 0 / 0 / 0 | 0 / 0 / 0 |

性能变化是伴随结果；本题验收主张是结构死锁被修复并通过 fresh final 三件套。Power 未采集，保持 `UNSUPPORTED`。

## 7. Terminal、预算和 Artifact

- status：`DONE`
- stop reason：`STRUCTURAL_FIX_FINALIZED`
- exploration stop：`STRUCTURAL_FIX_VALIDATION_PASS`
- final candidate：`candidate_001 / FINAL_VERIFIED`
- credits：71/80
- tools：CSim 3、Synth 2、CoSim 3、Planner 1
- runtime：122.698775 秒
- final reserve gate：allowed
- pending token/credits：0
- manifest：85/85 Artifact SHA-256 验证通过
- canonical manifest digest：`db1dd938e28bbaff4198d0249e65b2870a842ea3569af2cffdc23af5278987f8`

## 8. 安全和禁止项复核

- 只使用公开任务源码、header、metadata 和 public testbench；
- 没有 hidden、reference、golden；
- 没有把 API Key 或 Authorization 写入 Artifact；
- 没有产品代码或测试 diff；
- HEAD 未变化；
- 另外四题没有 run；
- 28 题矩阵没有启动；
- 多模型矩阵没有启动。

## 9. 运行后回归

- 556/556 unittest PASS；
- `compileall` PASS；
- `git diff --check` PASS；
- 产品代码/测试差异为 0。

## 10. 验收结论

单一 STRUCTURAL_FIX Anchor：**ACCEPTED**

Phase B1.1 的四个已通过 Mode 加上本阶段同 HEAD 的 STRUCTURAL_FIX 补证，五类能力 Gate 已补齐：

```text
READY_FOR_28_TASK_MATRIX
```

这只表示准入证据已就绪。本阶段没有、也不会自动启动 28 题矩阵。

## 11. 证据

- `docs/experiments/artifacts/2026-07-23-phase-b12-single-structural-anchor/baseline-stability.json`
- `docs/experiments/artifacts/2026-07-23-phase-b12-single-structural-anchor/frozen-single-run-plan.json`
- `docs/experiments/artifacts/2026-07-23-phase-b12-single-structural-anchor/single-anchor-result.json`
- `llm4hls_harness/runs/phase-b12-4a05763-residual-structural-20260723T124154Z/`
