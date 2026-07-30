# A2/A3 运行时合同

## A2

A2 继续只拥有搜索期的准入/停止权限：B1 先检查预算和 closeout，A2 Enforce 才能收窄后续 Planner 调用；A2 不生成 Patch、不执行工具。新搜索控制向 A2/Planner 提供同一份规范化失败和重复实验信息，但不放松 Admission、Provenance 或 8× 合法停止。

## A3

A3 仍为 soft-guided：最多一条 Top-1 Strategy Card、低置信度 ABSTAIN、不复用 Candidate、不覆盖当前工具事实。A2 放行前，A3 advice 只在内存准备；只有放行的 Planner action 才持久化它。

## 关闭隔离

`A2=off` 不调用 Continuation，也不加载 Admission；`A3=off` 不加载 Ranker、Store 或 A3 Admission，也不生成 Card。基础 Planner、Candidate 安全、B1/B2 仍可运行。现有隔离测试保持通过。

## 当前冻结边界

仓库 HEAD 为 RC2，但现有正式 A2 Admission 仍绑定 RC1 commit，因此 Enforce 正确 fail-closed。它不能被当作本次代码的正式授权；在代码冻结并生成新的、独立可审计 Gate/Admission 前，不能用它运行正式 Enforce 定向回归。
