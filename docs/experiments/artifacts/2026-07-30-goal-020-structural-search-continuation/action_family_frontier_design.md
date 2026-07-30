# STRUCTURAL_FIX Action Family Frontier 设计

每个开放的 RTL liveness obligation 都维护一个可审计前沿：

- `attempted_action_families`：已有持久化 proposal 的族，包含 Guard 拒绝；
- `rejected_action_families`：最终状态为拒绝的族；
- `untried_action_families`：同时满足适用性且尚未尝试的族；
- `family_failure_signatures`：从 Candidate failure evidence 读取的签名；
- `recommended_fallback_family`：根据多生产者、环和流平衡事实选择的下一高价值族。

固定词表为 `capacity_adjustment`、`producer_normalization`、
`topology_elimination`、`sequential_pipeline_fallback`、
`protocol_initialization`、`stream_balance_repair`。它是公共搜索控制数据，不是任务
ID 规则；没有要求每题尝试所有族。

当 Guard 拒绝且存在未尝试高价值族时，内部搜索控制返回
`CONTINUE_WITH_LLM`。失败候选不会成为下一轮父候选；现有 verified incumbent，或
immutable baseline，仍是唯一回退父节点。没有未尝试高价值族时才可 STOP；有已经
验证的 incumbent 时仍可先 SWITCH_PARENT。
