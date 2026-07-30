# STRUCTURAL_FIX 语义进展与停止策略

## 修改前审计

`v3_prototype._advance_round` 过去把每个非机械失败直接计入
`semantic_no_improvement_rounds`。`_evaluate_task_round_budget` 随后在该数值达到
`max_no_improvement_rounds` 时，无条件给出
`TASK_REPAIR_NO_IMPROVEMENT_LIMIT`。该路径没有比较失败签名、策略族、拓扑变化或
新的结构证据，因此 020 的“容量调整无效”和“残余反馈环死锁”会被错误折算为两次
无改进。

## 新合同

此合同只作用于 `STRUCTURAL_FIX`；REPAIR、SYNTH_FIX、OPTIMIZE 保持原有计数和
预算语义。以下任一项都构成语义进展，并将语义无改进连续计数清零：

- 当前规范化 Failure Signature 不同于本轮输入签名；
- 本轮使用了此前未尝试的结构策略族；
- Structural Guard 发现新的未满足结构条件；
- 候选声明且实际具有可审计的拓扑进展、假设证伪或更精确义务。

只有没有上述进展，且不是既有机械 Patch 恢复时，才增加
`semantic_no_improvement_rounds`。普通 `no_improvement_rounds` 仍保留作报告，
不能单独导致 STRUCTURAL_FIX 停止。

达到旧阈值时，运行时从持久化候选、证据和 proposal 只读重建策略族前沿：若存在
高价值、适用、未尝试的策略族，且原有“候选验证＋搜索期收口”预算门允许，则继续；
否则才安全停止。预算、Token、Credits、closeout reserve 和 B2 均未改变。

## 020 的预期

深度调整会把 `capacity_adjustment` 标记为已尝试而非“验证过的失败候选”；残余
feedback cycle 的 CoSim deadlock 会产生新的 Failure Signature。两者都不会仅因
候选失败而使搜索在第二轮停止。
