# 搜索控制最小重构设计

## 目标

将“连续无改进”从单一计数改为可归因的搜索控制：机械性失败不提前耗尽语义搜索，真实工具失败要求下一个修复实验可区分。

## 已实现

- 新增 `llm4hls_agent/v3_search_control.py`，只处理已持久化事实，没有工具、Ledger、Candidate 或 LLM 权限。
- 每轮 Planner 输入的 `round.search_control` 记录：主 Obligation、规范化失败事实、已尝试实验、Verified Best / fallback、下一步要求和静态 stream 事实。
- 每个 Proposal experiment 绑定其输入 failure signature；下一轮显式记录
  `new_evidence_since_last_planner`，因此“是否有新的真实工具事实”不再由
  prompt 文本或人工猜测决定。
- 同一内容还会封存在 `control/search_control/round_XXX.json`，并由 `search_control_ref` 绑定到终态结果。
- `no_improvement_rounds` 保留兼容统计；新增 `semantic_no_improvement_rounds` 与 `mechanical_recovery_rounds`。预算停止门只读取语义计数。
- 对真实 Live Planner，已在工具证据证明终端失败后重复同一 `hypothesis`
  **且**同一 `action_family` 的输出记录为
  `DUPLICATE_HYPOTHESIS_ACTION_FAMILY`，不会创建 Candidate 或运行 HLS。
  只改变其中一个维度的提案仍可以作为可证伪 follow-up；脚本化 fixture
  不启用此拒绝，以保留既有回放/恢复测试的语义。

## 行为边界

补丁 hunk/Provider 输出问题会产生 `CONTINUE_WITHOUT_LLM` 建议，表示可以先走现有确定性 header/count/relocation 修复；无法安全归一化时仍必须让 Planner 重新生成 diff，绝不静默改写模型 Patch。CSim、Synth、CoSim 的真实新失败则为 `CONTINUE_WITH_LLM`；有更优已验证 incumbent 时记录 `SWITCH_PARENT` 候选语义。最后是否放行仍由 B1 预算和 A2 Admission 控制。

## 没有做的事

未增加默认 Planner 次数、Credits、Token 或并发 Probe；未把失败 Candidate 作为默认父节点；未改变 B2、100 MHz、Unified Diff、Interface Guard 或 Candidate Registry。
