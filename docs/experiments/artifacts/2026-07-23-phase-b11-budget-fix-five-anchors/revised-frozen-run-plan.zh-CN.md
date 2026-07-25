# Phase B1.1 修正后冻结运行计划

## 冻结结论

- Gate 1：PASS
- 代码 commit：`4a05763b593a527878a0056f64763126c58ee63b`
- 产品代码修改：无
- 修正范围：仅逐题 `run-token-limit`
- 预算策略：fixed V3-D compatibility
- 重跑原因：`FROZEN_BUDGET_PLAN_CORRECTED`

本计划不删除 final reserve，不改变 Router、Planner Prompt、Candidate comparator、Vitis 语义、task corpus 或测试。

## 统一配置

```text
planner=openai-compatible
model=deepseek-v4-pro
backend=vitis
vitis=2025.2
validation-profile=fast-experiment
final-validation-policy=full_internal_audit
continuation-policy=shadow
experience-mode=shadow
ranker=bayesian_shadow (advisory only)
max-planner-rounds=2
llm-max-output-tokens=4096
temperature=0.0
top_p=1.0
cost(csim/synth/cosim)=1/4/20
final-reserve-credits=25
```

## Anchor 顺序与逐题预算

| 顺序 | 能力 | 任务 | Token cap | 最低需求 | Headroom | Credit cap |
|---:|---|---|---:|---:|---:|---:|
| 1 | REPAIR | `v3d_fast_002` | 8,639 | 8,511 | 128 | 36 |
| 2 | SYNTH_FIX | `v3d_fast_010` | 9,201 | 9,073 | 128 | 40 |
| 3 | OPTIMIZE | `v3d_fast_022` | 10,802 | 10,674 | 128 | 40 |
| 4 | 空 Stub | `track_a_empty_stub_generation` | 9,782 | 9,654 | 128 | 36 |
| 5 | STRUCTURAL_FIX | `v3d_fast_018` | 10,691 | 10,563 | 128 | 92 |

先验证相对低成本的 Repair、Synth 和 Optimize 链路，最后运行已有 600 秒 baseline CoSim 风险的 STRUCTURAL_FIX。

## Phase 最大预算

| 指标 | 上限 |
|---|---:|
| Planner calls | 10 |
| Token | 49,115 |
| CSim | 20 |
| Synth | 16 |
| CoSim | 8 |
| Tool Credits | 244 |
| 单题 runtime 上限之和 | 18,000 秒 |
| Phase 真实运行墙钟窗口 | 9,600 秒 |

Token cap 是硬上限，不是消耗目标。达到 160 分钟窗口后不再启动新题。

## 新运行目录

所有目录均使用 `phase-b11-` 前缀和新的批次时间 `20260723T120331Z`，不续跑、不覆盖任何 `phase-b1-` 目录。

## 停止条件

- 代码 HEAD 变化；
- 产品或测试代码出现未提交修改；
- secret 可能进入 Artifact；
- 预算、Ledger 或产品共享缺陷；
- 达到 160 分钟；
- 非允许类型的失败不得外部重跑。

完成五类 Anchor 后立即停止，不启动 28 题矩阵。
