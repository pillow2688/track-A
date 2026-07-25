# Phase D1/D2 中文验收报告

日期：2026-07-24  
验收对象：终态 Candidate 绑定与无效 Latency 安全降级  
验收结论：`ACCEPTED`

## 1. 验收摘要

四个指定任务均在全新目录完成一次 DeepSeek + Vitis 2025.2 正式协议运行：

- 唯一终态：4/4
- DONE：2
- 真实 FAILED：2
- ERROR：0
- primary retry：0
- P0 错误标记：0

016/020 的 HLS 代码没有修复成功，不影响本阶段 P0 验收：它们已经从错误的执行器 ERROR 恢复为 provenance 完整的真实 FAILED。

## 2. 验收 Gate

| Gate | 结果 | 证据 |
|---|---|---|
| 四题唯一明确终态 | PASS | terminal coverage 4/4 |
| 016/020 无 Candidate binding ERROR | PASS | FAILED / MAX_TASK_REPAIR_ROUNDS |
| 016/020 Candidate ID/source digest 可绑定 | PASS | candidate_000 + source SHA + committed journal |
| 021/022 无 invalid latency ERROR | PASS | 两题 DONE；P0 标记为 0 |
| 无效 latency 明确安全降级 | PASS | 旧 replay：0 → INVALID/NOT_COMPARABLE；18 条专项测试 |
| 正确性事实保留 | PASS | 四题终态 binding 中 CSim/Synth 均 PASS |
| 不伪造 acceleration | PASS | 不可比值为 null；4.5× 仅来自有效正 latency |
| Ledger 全部对账 | PASS | 4/4 |
| Manifest/digest 全部可绑定 | PASS | 4/4，316 条 Manifest Artifact |
| 完整测试 | PASS | 605/605 |
| 任务代码未修改 | PASS | 冻结 task SHA 运行前后相同 |
| 横向组件未启用 | PASS | Continuation/Experience/Ranker off |
| hidden/reference/golden 未访问 | PASS | 无受限 Manifest 路径 |
| 运行期间冻结 HEAD 不变 | PASS | 前后均为 `4a05763b...` |

## 3. P0-1 验收

### 016

- 终态：FAILED
- 原因：MAX_TASK_REPAIR_ROUNDS
- Candidate：candidate_000
- binding source：PRESERVED_INCUMBENT
- source SHA-256：`56df635d7e811d72dac01f5e6f911d8af3a0b0566c66aa64589990894532e0fe`
- Registry/result/decision journal：一致
- correctness：CSim PASS、Synth PASS、CoSim FAIL
- Manifest：PASS
- Ledger：PASS

### 020

- 终态：FAILED
- 原因：MAX_TASK_REPAIR_ROUNDS
- Candidate：candidate_000
- binding source：PRESERVED_INCUMBENT
- source SHA-256：`b8274124e9c9a124864a32f24721aa68d467290d6ecab58639b00f06f8ebf3b4`
- state proposal ref 已重绑定到 committed Candidate journal
- correctness：CSim PASS、Synth PASS、CoSim TIMEOUT
- Manifest：PASS
- Ledger：PASS

P0-1 结论：PASS。

## 4. P0-2 验收

### 旧 Artifact 决定性证据

021/022 旧候选的 raw worst latency=0。修复后的只读 replay 输出：

```text
latency_status       INVALID
performance          NOT_COMPARABLE
acceleration         null
correctness          CSim PASS / Synth PASS
replay terminal      PRETERMINAL_NOT_COMPARABLE
replay decision      INSUFFICIENT_ARTIFACT
```

旧 run 在异常点后没有终态 Artifact，所以 replay 不补造 DONE/FAILED。这是正确的失败闭合行为。

### 021 新运行

- 终态：DONE / BASELINE_FINALIZED_NO_IMPROVEMENT
- Candidate：candidate_000 / FINAL_VERIFIED
- final CSim/Synth：PASS/PASS
- 本次 latency：VALID 18
- 候选无严格改善，baseline 正常 final
- Manifest/Ledger：PASS/PASS

### 022 新运行

- 终态：DONE / CANDIDATE_PROMOTED_AND_FINALIZED
- Candidate：candidate_001 / FINAL_VERIFIED
- final CSim/Synth：PASS/PASS
- 本次 latency：VALID 2
- acceleration：4.5×，数值来源有效
- Manifest/Ledger：PASS/PASS

本次实时随机样本没有重复旧零 latency。无效分支由旧 replay 和自动化测试验收，实时结果只按实际有效 latency 报告。

P0-2 结论：PASS。

## 5. 预算验收

| Task | LLM | Token | CSim | Synth | CoSim | Credits | Ledger |
|---|---:|---:|---:|---:|---:|---:|---|
| 016 | 2 | 4320 | 2 | 1 | 2 | 46 | PASS |
| 020 | 2 | 4796 | 2 | 1 | 2 | 46 | PASS |
| 021 | 2 | 4408 | 3 | 3 | 0 | 15 | PASS |
| 022 | 2 | 5029 | 4 | 4 | 0 | 20 | PASS |
| 合计 | 8 | 18,553 | 11 | 9 | 4 | 127 | PASS |

所有 pending 均为 0；所有题均在 32,768 Token 和 100 Credits 上限内。

## 6. Provenance 与 Manifest 验收

| Task | Manifest Artifact | Candidate binding | Manifest |
|---|---:|---|---|
| 016 | 66 | PASS | PASS |
| 020 | 64 | PASS | PASS |
| 021 | 82 | PASS | PASS |
| 022 | 104 | PASS | PASS |

逐条验证包括：路径安全、文件存在、size、SHA-256、source 覆盖、decision journal 覆盖、逻辑 manifest digest 与 batch receipt 一致。

## 7. 冻结与安全验收

- Git HEAD 前后相同：PASS。
- 产品和任务冻结文件 49/49 相同：PASS。
- task code/testbench/header/metadata 未修改：PASS。
- Planner/Router/Token policy 未修改：PASS。
- Continuation/Experience/Ranker runtime off：PASS。
- secret 未落盘：PASS。
- hidden/reference/golden Artifact 路径：0。
- 旧 Artifact 改写：0。
- 非指定任务新运行：0。

## 8. 最终结论

`ACCEPTED`

满足 Phase D1/D2 的全部 P0 条件。成功率不是本验收的唯一 Gate；两题真实 FAILED 是合法结果。

## 9. 是否允许启动修复后 28×1

允许进入 Phase D3 的冻结准备，不在本阶段直接启动。

前置条件：

1. 用户授权提交 D1 产品代码和专项测试；
2. 形成新的明确 Git HEAD；
3. 基于新 HEAD 重做正式 freeze 和 per-task Token 预检；
4. 保持 Continuation/Experience/Ranker off；
5. primary retry disabled，failure-isolating/resumable；
6. 不在同一正式数据上调横向组件阈值。

## 10. 证据索引

- 状态：`docs/status/2026-07-24-phase-d1-terminal-latency-fix.md`
- 实验报告：`docs/experiments/2026-07-24-phase-d1-terminal-latency-fix-report.zh-CN.md`
- 冻结快照：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/phase-d2-frozen-snapshot.json`
- 旧 Artifact 复盘：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/old-artifact-reconciliation.json`
- 旧 Artifact replay：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/old-artifact-replay-after-fix.json`
- 聚焦测试：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/focused-tests.txt`
- 完整测试：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/full-tests.txt`
- 静态验证：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/static-verification.txt`
- 预启动 Gate：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/phase-d2-prelaunch.json`
- 运行后 Gate：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/phase-d2-postrun.json`
- 机器可读验收：`docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/phase-d2-real-run-acceptance.json`
- 新运行根目录：`llm4hls_harness/runs/phase-d2-4a05763-d1cad2cd88-20260724T124625Z`
