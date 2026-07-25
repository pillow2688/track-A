# 当前 Continuation V1 审计

## 结论

V1 已实现并可在主路径以 `shadow` 运行，但运行时默认值仍为 `off`，正式接受权限只到 Shadow，Enforce 未准入。V1 采用一套跨 Mode 共用的分数规则；仅对 STRUCTURAL_FIX 和 OPTIMIZE 有少量加减分，未显式分离 correctness、structural、performance 三类进展。

## 输入与规则

- 输入：Mode、正确 incumbent、严格 latency 改善、Performance-Area、Evidence Delta、Strategy Novelty、Continuation Cost、剩余轮数。
- Evidence Delta：比较清洗后的前后 fingerprint，统计 failure stage/subtype、location、affected object、bottleneck、critical loop、scheduling 和 resource pressure 等变化。
- Strategy Novelty：静态扫描已生成 Patch 的 pragma/结构行为；Patch 扫描优先，声明标签只作 fallback。
- 决策：预算/最终保留先形成硬原因；其余事实进入一个通用 value score，再产生 `ALLOW/BLOCK/DEFER_TO_FINAL`。

## Mode 差异不足

- REPAIR、SYNTH_FIX 没有独立停止规则。
- STRUCTURAL_FIX 只有 subtype refinement 奖励，尚未把 FIFO、producer/consumer、拓扑和 mismatch specificity 分开。
- OPTIMIZE 有 bottleneck 与 Performance-Area 条件，但未把 latency、II、interval、clock、8× cap 和低边际收益组织为独立决策规则。

## 信息泄漏审计

主图调用点使用当前 Ledger、已完成 Candidate 历史和当前 Evidence，未发现读取未来 Candidate/outcome。旧 V3-F R02 replay builder 把终态 `result.budget` 放进了 `pre_state`，属于严格 V2 口径下的 future-information leakage 风险；旧回放估计成本为 0，因此该字段未改变当时 11 条 V1 决策，但 Phase C0 数据已经将它完全剔除并保留为 `null/UNKNOWN`，没有用终态值回填。
