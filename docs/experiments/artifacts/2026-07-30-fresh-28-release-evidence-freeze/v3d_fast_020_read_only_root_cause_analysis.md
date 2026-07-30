# v3d_fast_020 只读根因分析

本报告只读取 fresh 28 题主实验的已冻结 Artifact；未调用模型或 Vitis，未修改产品代码、任务、旧 Artifact 或 Git 状态。

## 结论

`v3d_fast_020` 的失败主因是 DATAFLOW 中 `feedback_stream` 的循环依赖与不安全的多生产者拓扑，不是 Vitis 基础设施、A2 提前拦截、A3 引导或预算耗尽。

置信度：高。

任务要求 `requires_cosim = true`。因此任何 CSim/Synth 通过但 CoSim 未通过的 Candidate 均不能成为 final Candidate，也不会进入 B2 独立认证。

## 证据链

| 结论 | 主要证据 |
|---|---|
| 基线功能和综合正常 | `actions/1c960.../result.json` 为 CSim PASS；`actions/f69f.../result.json` 为 Synth PASS。 |
| 基线 CoSim 超时 | `evidence/failures/baseline_cosim.json`：300 秒 timeout；已生成 RTL/FIFO 与 VRFC 分析日志。 |
| 基线存在拓扑风险 | `baseline/source/kernel.cpp`：`feedback_stream` 有 `v3d_cycle_seed` 与 `v3d_cycle_consume` 两个写入者，`v3d_cycle_forward` 一个读取者，且 forward/consume 形成循环。`control/search_control/round_001.json` 同样记录 `possible_multi_producer=true` 与 stream dependency cycle。 |
| Candidate 001 未修复 | 仅将 feedback FIFO depth 由 1 改为 2；`candidate_001_cosim.json` 仍为 1800 秒 TIMEOUT。 |
| Candidate 002 提供明确 RTL 级根因 | `candidate_002_cosim.json`：AESL deadlock detector 报告 forward 等待空 feedback FIFO，同时 consume 等待空 forward FIFO。 |
| A2 未拦截搜索 | `planner/call_gates/round_001.json` 和 `round_002.json` 均为 `ALLOW`。 |
| A3 未注入策略卡 | `experience/experience_recommendations.jsonl` 两条均为 `ABSTAIN / NO_MATCHING_VERIFIED_SUPPORT`，无推荐 atom 或 supporting record。 |
| 停止不是预算或 closeout | `budget_state.json`：停止时仍有 25 Credits、23,227 Tokens、5,015 秒 runtime；`trace.jsonl` 最终原因为 `TASK_REPAIR_NO_IMPROVEMENT_LIMIT`。 |

## 各阶段判断

### Baseline

基线源码让 seed 和 consume 同时写入 `feedback_stream`，forward 从该 FIFO 读取；forward 还向 `forward_stream` 写入，consume 再从该 FIFO 读取。静态提取器记录两条依赖环。300 秒 CoSim 在 RTL 生成/分析阶段后超时，因此单独的 baseline timeout 不能完全证明具体握手点；但 Candidate 002 的明确死锁检测器输出与这份拓扑风险相互印证。

### Candidate 001

Planner 假设是“深度 1 不足”，补丁确实只将 feedback FIFO 深度改为 2，补丁应用、CSim、Synth 都正确通过。问题在于该补丁保留了两个 writer 和依赖环；它没有解决根因。故此为**Planner 的结构推理不充分**，不是 unified diff 或 Candidate materialization 错误。

### Candidate 002

Planner 正确识别了多生产者问题，并移除了 seed；但它保留了 forward 与 consume 之间的反馈读写闭环。局部变量 `dependency = 0` 在 C 级语义中可用，却没有保证 HLS DATAFLOW 的 RTL 进程获得可启动的 token/握手顺序。CoSim 明确报告两个进程分别等待对方生产的空 FIFO。

故此为**Patch 的 DATAFLOW/协议结构设计错误**：文本补丁已正确应用，CSim 与 Synth 也确实通过；失败发生在 RTL CoSim 中，而不是 Patch 应用或环境。

## 组件与基础设施责任边界

- A1：正确保留了 timeout、结构事实、第一轮失败以及第二轮的显式死锁证据；没有丢失 Candidate 级绑定。
- A2：两次均为 `ALLOW`，第二次的理由为 `NEW_ACTIONABLE_EVIDENCE` 与 `NOVEL_STRUCTURAL_STRATEGY`。没有 false block；未发生 finalization，因此也没有 unsafe finalize。
- A3：两次都 ABSTAIN，未生成或注入 Strategy Card；没有可归因的有害建议。
- B1：Candidate Registry 保留 immutable baseline 与两项 rejected Candidate，未错误提升候选。
- B2：未执行是正确行为，因为没有 CoSim PASS 的 final Candidate；这不是 B2 缺失或失败。
- 基础设施：没有 Vitis 环境故障证据。Candidate 002 的 AESL deadlock detector 直接给出 DUT 级 FIFO 互等的错误，排除了“仅外部进程卡住”作为主因。

## 停止规则

停止符合当前合同。两次结构 Candidate 都在必须的 CoSim 验证中失败；trace 记录 semantic no-improvement 从 1 变为 2，随后按 `TASK_REPAIR_NO_IMPROVEMENT_LIMIT` 终止。预算、closeout reserve、runtime 和 Planner round 上限均不是阻止第三次调用的原因。

因此，不应仅为提高 020 成功率而放宽 A2 或 no-improvement 上限。

## 最小修复候选（未实施）

### 优先：添加极窄的结构证据约束

当 `requires_cosim=true` 且静态提取同时发现 stream dependency cycle 或 multi-producer FIFO 时，向下一轮 Planner 加入一条短的强制约束：候选必须移除全部相关环和多生产者；仅改变 FIFO depth 不足以完成修复。该约束应只作为 Planner Evidence，不改变 A2/A3、预算或验证规则。

需要的测试：

1. 020 baseline 触发该约束；普通无环 DATAFLOW 任务不触发。
2. Candidate 001 样式的“仅 FIFO depth”方案仍被标记为未解决环。
3. Candidate 002 样式的残余 forward/consume 环也被识别。
4. A2 off、A3 off、A3 ABSTAIN 以及非结构任务的 Planner 输入保持不变。

随后值得只进行一次 fresh 020 定向重跑：要求 search CSim/Synth/CoSim、B2 和 100 MHz Gate 全部 PASS；失败不自动重试。

### 备选：保留单向 DATAFLOW，删除 feedback 环

在 Candidate 方案中只保留一个单生产者/单消费者的 forward FIFO，或直接以单一 pipeline loop 实现任务声明的 `output[i] = input[i] * 2 + 1`。这两种源码结构都不保留 feedback FIFO。它是针对 020 的高置信度候选实现思路，但不应直接写入 Corpus 或冒充已验证结果。

需要的测试与优先方案相同，另增加：16 个元素、边界输入值、公开 CSim、Synth、CoSim、B2、100 MHz Gate。

## 建议

建议修改代码，但只实施“优先”方案：将已提取的结构事实以窄范围、可测试的约束传递给 Planner。不要修改 A2 的停止阈值，不要改 A3、预算、任务或 CoSim timeout。完成聚焦测试后，只 fresh 重跑 020 一次。
