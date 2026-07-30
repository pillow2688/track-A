# HLS Agent 搜索框架根因审计（阶段 0）

> 日期：2026-07-30
> 审计范围：仅只读检查已封存的 fresh 28 题主实验中 `012/016/017/020` 的正式 run。
> 基线提交：`d2cc309de44d6b74d86576aac34d43d65ffc9cd3`，分支 `release/track-a-rc2-candidate`。
> 本文不改写历史 Candidate、Ledger、Planner Artifact 或工具结果。

## 阶段状态

- 已完成：现场保护、四题历史轨迹审计、012 成功/失败轨迹比较、当前快测和 Vitis version 检查。
- 真实测试：`python -m unittest tests.test_v3_task_aware_smoke tests.test_vitis tests.test_executor_runtime tests.test_final_certification tests.test_v3_batch_benchmark -v`，86 通过、1 个历史 020 Artifact 缺失测试跳过。
- 未完成：统一 failure schema、Obligation/Proposal、Continuation/Portfolio、通用 DATAFLOW evidence、定向 fresh 回归。
- Git 状态：开始时只有本项目说明文件未跟踪；`git diff --check` 通过，tracked diff SHA-256 为空。
- 环境：`vitis-run v2025.2`，SW Build 6295257；DeepSeek V4 Pro/openai-compatible 历史 run；A1 on、A2 enforce、A3 guided、fixed token、B1/B2 on。
- 风险：`scripts/vitis-2025.2-preflight.sh --version` 因本机 `/outputs` 挂载不可创建而返回 `OUTPUT_MOUNT_UNAVAILABLE`；这次没有启动 HLS action，后续定向真实回归前必须用可写 output 目录重新 preflight。

## 现场和证据位置

主实验根目录：

```text
llm4hls_harness/runs/full-agent-current-fresh-28-20260729-a01/
```

| Task | 历史 run_id 后缀 | 历史终态 | 主汇总 failure_stage |
|---|---|---|---|
| 012 | `002e0d7640f9` | `TASK_REPAIR_NO_IMPROVEMENT_LIMIT` | `SYNTH` |
| 016 | `f681865b0450` | `TASK_REPAIR_NO_IMPROVEMENT_LIMIT` | `COSIM` |
| 017 | `e63adc6892d4` | `TASK_REPAIR_NO_IMPROVEMENT_LIMIT` | `COSIM` |
| 020 | `5d31f9f4573e` | `TASK_REPAIR_NO_IMPROVEMENT_LIMIT` | `UNKNOWN`（错误汇总） |

每个 run 都存在 `planner/inputs/`、`planner/proposal_*.json` 或 provider failure、`planner/call_gates/`、`candidate_registry.json`、`evidence/failures/`、`trace.jsonl`、`budget_ledger.jsonl` 和工具 `actions/*/result.json`。因此下述结论来自已持久化 Artifact，而不是对日志的猜测。

## 共同根因

旧框架把三种彼此不同的事情压缩成 `no_improvement_rounds`：

1. provider/patch 格式拒绝；
2. 候选经工具验证后失败；
3. 真实、重复的修复假设已经被证伪。

该计数达到 2 后，统一由 `task_repair_no_improvement_limit` 拒绝后续搜索，blocker 为 `no_improvement:2>=2`。它不要求第二次 Proposal 具有不同的 hypothesis、action family、fallback 或 parent。因此 A2 能安全放行/停止，但无法保证 Planner 在做不同实验。

另一个共性问题是 failure 的语义被混合：工具 phase、failure kind、evidence fingerprint、continuation reason 和 terminal stop reason 不在稳定字段中分别保存。020 的真实最后工具事实是 candidate CoSim no-progress timeout，但主汇总最终得到 `UNKNOWN`，说明汇总层没有把 `phase=timeout`/`failure_kind=TIMEOUT` 映射为 CoSim failure stage。

## 逐题轨迹结论

### v3d_fast_012：SYNTH_FIX

- baseline：CSim PASS；Synth 因 `std::vector` 的 `operator new/delete` 不可综合而 FAIL。
- round 1：`SYNTHESIS_REPAIR`，假设为“用固定数组替代 vector”；parent=`candidate_000`；统一 diff 因 `PATCH_HUNK_NEW_START_MISMATCH` 被拒绝，未创建 candidate。
- round 2：仍为 `SYNTHESIS_REPAIR`，根因和方案仍为“固定数组替代 vector”；再次发生同一 hunk 坐标错误，未创建 candidate。
- terminal：baseline synth evidence 仍是最后真实工具失败；但终态写为 `TASK_REPAIR_NO_IMPROVEMENT_LIMIT`。

审计问答：

| 问题 | 结论 |
|---|---|
| 第二次是否看到新工具 Evidence | 否。它只看到了 round 1 patch 机械拒绝；call gate 的 `new_actionable_evidence=false`、`same_failure_signature=true`。 |
| hypothesis / action family 是否相同 | 是，均为 vector→fixed-array 的 `SYNTHESIS_REPAIR`。 |
| candidate_002 从何处分支 | 不存在；两次 proposal 都从 immutable baseline 提出。 |
| 是否叠加在失败 Candidate | 否，候选尚未物化；但同一失败实验被白白重复。 |
| 失败 signature 是否相同 | baseline synth signature 相同；两次 patch 坐标失败同类。 |
| 是否应 SWITCH_PARENT | 没有已验证替代父节点；应切换 **fallback/action family**（例如仅修正 diff 坐标或选择另一局部实现），而非直接 STOP。 |

### v3d_fast_016：STRUCTURAL_FIX

- baseline：CSim/Synth PASS；CoSim DEADLOCK。证据显示 producer/consumer 的 main/side stream 顺序和 depth-1 FIFO 形成阻塞。
- round 1：`STRUCTURAL_REPAIR`，将 producer 写顺序改为与 consumer 读顺序一致；parent=`candidate_000`。candidate_001 通过 CSim/Synth，但 CoSim 再次 DEADLOCK；阻塞的空/满 FIFO 方向发生变化。
- round 2：call gate 正确标记 `NEW_ACTIONABLE_EVIDENCE`；但 Planner 再次提出同一“重排 writes”的 `STRUCTURAL_REPAIR`。该 proposal 被 duplicate-patch gate 拒绝。
- terminal：仍由固定 no-improvement=2 停止。

审计问答：

| 问题 | 结论 |
|---|---|
| 第二次是否看到新 Evidence | 是，candidate_001 CoSim deadlock evidence 已进入 history；failure fingerprint 改变。 |
| hypothesis / action family 是否相同 | 是，均为 write-order reordering / `STRUCTURAL_REPAIR`。 |
| candidate_002 从何处分支 | 不存在；round 2 parent 已是 baseline（安全），不是 candidate_001。 |
| 是否叠加失败 Candidate | 否；当前 parent 选择实际保留 baseline。 |
| failure signature 是否相同 | 工具 kind 都是 DEADLOCK，但 FIFO empty/full 方向改变，属于新而相关的结构 Evidence。 |
| 是否应 SWITCH_PARENT | 已隐式回到 baseline，却没有把它表达为 `SWITCH_PARENT`，也没有要求不同 action family（如 topology/read-write balance）。这是缺口。 |

### v3d_fast_017：STRUCTURAL_FIX

- baseline：CSim/Synth PASS；CoSim DEADLOCK，main/side stream 的 FIFO 空/满阻塞可读。
- round 1：provider output 被拒绝为 `PROVIDER_OUTPUT_REJECTED:PATCH_INCOMPLETE`；没有可解析 Proposal 或 candidate。
- round 2：产生 `STRUCTURAL_REPAIR`（interleave writes 或增加 depth），parent=`candidate_000`；candidate_001 通过 CSim/Synth，但 CoSim 再次 DEADLOCK。
- terminal：第二个有效 proposal 后立即受 no-improvement=2 限制，不存在第三个不同实验。

审计问答：

| 问题 | 结论 |
|---|---|
| 第二次是否看到新工具 Evidence | 否，没有新的工具结果；它应被视为 provider-format recovery，而不是“第二个已验证假设”。 |
| hypothesis / action family 是否相同 | 只有第二次存在可用 hypothesis，无法比较两条有效 hypothesis。 |
| candidate_002 从何处分支 | 不存在；candidate_001 从 baseline 分支。 |
| 是否叠加失败 Candidate | 否。 |
| failure signature 是否相同 | baseline/candidate 都是 DEADLOCK，具体阻塞证据相近。 |
| 是否应 SWITCH_PARENT | 当前 active parent 已是 baseline；真正需要的是在 candidate_001 failure 后保留 fallback 并允许一次不同结构实验。 |

### v3d_fast_020：STRUCTURAL_FIX / DATAFLOW

- baseline：CSim/Synth PASS；300 秒 CoSim probe 触发 timeout。该修复已避免旧的 1800 秒占用。
- round 1：识别 `feedback_stream`/`forward_stream` DATAFLOW cycle，假设 depth=1 缓冲不足，修改 `feedback_stream depth=1→2`；parent=`candidate_000`。candidate_001 CSim/Synth PASS，CoSim 触发 `COSIM_NO_RTL_TEST_PROGRESS_TIMEOUT`。
- round 2：call gate 标为 `NEW_ACTIONABLE_EVIDENCE`，但又生成相同的 hypothesis、相同 action family、**相同 patch SHA-256**，由 duplicate-patch gate 拒绝。
- terminal：`TASK_REPAIR_NO_IMPROVEMENT_LIMIT`；主汇总错误把真实 CoSim timeout 写为 `UNKNOWN`。

审计问答：

| 问题 | 结论 |
|---|---|
| 第二次是否看到新 Evidence | 是，candidate_001 no-progress evidence 已持久化；但只形成了新的 hash，并未形成通用 topology obligation。 |
| hypothesis / action family 是否相同 | 是；均为“增大 feedback FIFO 深度”的 `STRUCTURAL_REPAIR`，patch 完全相同。 |
| candidate_002 从何处分支 | 不存在；round 2 parent 已是 baseline。 |
| 是否叠加失败 Candidate | 否。 |
| failure signature 是否相同 | 表面 fingerprint 不同；语义上都属于 DATAFLOW feedback 无 RTL progress。 |
| 020 为何为 UNKNOWN | 汇总器未将 CoSim `phase=timeout`、`failure_kind=TIMEOUT`/no-progress evidence 映射为 `COSIM`。不是缺少工具证据。 |
| 是否应 SWITCH_PARENT | parent 已安全回到 baseline，但需要正式 `SWITCH_PARENT` 原因和不同 action family，例如 process graph/topology、initial token、read/write balance，而不是继续加 FIFO depth。 |

## 012 的 fresh 成功/失败差异

本次比较使用随后 6×4 fresh 消融中同一公开任务的四条独立运行，作为轨迹说明而非因果实验：

- 旧主实验和 `MINUS_A3`：前两次均因 `PATCH_HUNK_NEW_START_MISMATCH` 被拒绝，均未物化 candidate；失败。
- `FULL_REF`：round 1 同样 hunk 拒绝，但 round 2 生成可应用的 fixed-array patch，candidate_001 最终认证通过。
- `MINUS_A2`：第一轮就生成可应用 fixed-array patch，最终认证通过。
- `MINUS_A2_A3`：round 1 hunk 拒绝、round 2 可应用，最终认证通过。

因此 012 的差异主要是 Provider 输出的 unified-diff 坐标质量，而不是已经证明 A2/A3 的稳定因果效果。框架应把机械 hunk error 变成精确 Evidence，并阻止它被误算成“没有修复能力”的等价 no-improvement round。

## 下一阶段入口

阶段 1–5 的最小闭环应先实现：

1. 将 terminal reason 与 last failure stage/kind/signature 分离；
2. 将 proposal/hypothesis/action family 和 patch rejection 纳入小型状态；
3. 根据语义证据而不是 fingerprint hash 决定第二次 LLM 是否允许；
4. 将 baseline/Verified Best/Active Probe/Fallback Parent 明确化，并在失败后记录 `SWITCH_PARENT`；
5. 用通用 DATAFLOW/stream analysis 生成 016/017/020 可读的结构 Evidence。

在这些行为有单元测试前，不启动任何新的 HLS 定向回归。

## 阶段 1–8 实施结果（2026-07-30）

- 已完成：统一终态失败字段、规范化 failure signature、轻量 Obligation/Proposal Experiment、语义/机械失败分账、三引用 Portfolio 表达、保守 DATAFLOW/stream 事实、CoSim progress 字段、020 CoSim 汇总归类修复。
- 关键安全结论：失败 Candidate 没有被作为默认父节点；hunk/provider 机械问题不再消耗 `semantic_no_improvement_rounds`；Live Planner 的终端失败重复 action family 会在 Candidate 分配前被拒绝。
- 本地验证：147 项 V3/task-aware/terminal/搜索控制测试通过。完整套件中的剩余失败与本次调用链分离，详见 `test_results.md`。
- 定向 HLS 回归尚未启动：当前 RC1 Admission 与 RC2 HEAD commit 不匹配，Enforce 正确 fail-closed。必须在冻结后重新签发 Admission，不能用旧 Artifact 绕过。
