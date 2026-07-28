# 2026-07-28 Full Agent combined 28-task coverage

`COMBINED_COVERAGE_NOT_SINGLE_BATCH_28X1`

```text
First attempt: 25/28
Latest validated combined coverage: 28/28
Targeted reruns: 012, 018, 020
Single batch 28×1: No
Single-fingerprint causal ablation: No
```

## 状态

- 状态码：`COMPLETE_LATEST_VALIDATED_COMBINED_COVERAGE`。
- 首次严格成功：`25/28`（`89.29%`）。
- latest validated 合并覆盖：`28/28`（`100%`）。
- B2 和 100 MHz Gate：全部 `28/28 PASS`。
- 018 路由差异保持可见：Expected `STRUCTURAL_FIX`，Routed `OPTIMIZE`，Route Match `false`；真实 baseline 三阶段均 PASS。
- 当前判断：进入代码冻结；停止 A1/A2/A3 和 Executor 开发。

## A2 版本

- CLI interface：`v2`。
- Effective policy：`v3.continuation-policy.v3`。
- Decision schema：`v3.continuation-decision.v2`。

## 证据边界

No-harm：latest `28/28`、A2 false block `0`、A2 unsafe finalize `0`、A3 injection mismatch `0`。

Causal benefit：没有同实现、同 Executor fingerprint 的 off/shadow 配对消融；不能把成功率、Token 或 Planner 调用变化归因于 A2/A3。

## 统计

- Planner `48`；Tokens `115125`；Agent Credits 统计和 `884`。
- A2 ALLOW `48`；A3 RECOMMEND `23`、ABSTAIN `25`。

## 边界与输出

本结果不是单批次 28×1；每题 Agent Ledger 独立。仍需统一正式 28×1 和同 fingerprint 配对消融，才能形成对应的统一批次与因果收益证据。

- 机器结果：`llm4hls_harness/runs/full-agent-v3d-fast-combined-28-coverage-20260728-a01/combined_full_agent_28_task_coverage.json`
- 详细报告：`llm4hls_harness/runs/full-agent-v3d-fast-combined-28-coverage-20260728-a01/combined_full_agent_28_task_report.md`
- Release snapshot：`docs/experiments/artifacts/2026-07-28-full-agent-release-snapshot/`
