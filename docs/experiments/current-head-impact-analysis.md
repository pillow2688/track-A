# 当前冻结代码基线影响分析

> 中文镜像：`current-head-impact-analysis.zh-CN.md`。两份文件结论一致。

## 比较基线

- 历史 72-run commit：`431f7a65627ab6911bd11d1a4a8ae6f57094ab08`
- Phase A 起始 HEAD：`65ab82c13f28921356b77ea454306d6dc1ca8e17`
- Router 修复产品代码 commit：`e5ba32a032432579bb9daa8015d2f705cff93498`
- 有效产品代码状态：`ROUTER_FIX_COMMITTED`
- 正式可比性：`NOT_SAME_VERSION`

Router 修复后的产品文件与历史 commit 中同路径文件的 SHA-256 相同，因此 Router 本身恢复的是历史 baseline-fact 语义。当前版本仍有多条独立于 Router 的全局执行路径变化。

## 能力影响

| 能力 | 影响分类 | 证据与说明 |
|---|---|---|
| Task loader 与 task contract | GLOBAL_BEHAVIORAL | `llm4hls_harness/llm4hls_agent/task.py` |
| PhaseRouter | NONE | 修复后与历史正确 Router 字节一致；generation 不再覆盖 baseline facts |
| Planner prompt、schema 与 Token Envelope | GLOBAL_BEHAVIORAL | `openai_provider.py`、`v3_openai_planner.py` |
| Patch validator | GLOBAL_BEHAVIORAL | `repair.py` |
| Candidate 创建、比较、晋升和 final | GLOBAL_BEHAVIORAL | `v3_prototype.py` |
| BudgetLedger 与 reserve | GLOBAL_BEHAVIORAL | `budget.py` |
| Vitis 命令与 receipt | GLOBAL_BEHAVIORAL | `vitis.py` |
| validation profile 与 final policy | GLOBAL_BEHAVIORAL | 历史 commit 后新增 `task_contract` / `full_internal_audit` 行为 |
| Continuation | GLOBAL_BEHAVIORAL | 新增 shadow-capable 实现；authority 仍为 `SHADOW` |
| Experience 与 Bayesian Ranker | NONE | 本阶段未启用 guided/trained authority；历史批次使用 `experience=off` |
| V3-D 28 题公开源码、metadata 与 testbench | NONE | 历史 commit 到当前产品代码基线之间没有 V3-D 题目文件变化 |

## 逐题分类

- `DIRECT`：0
- `GLOBAL_BEHAVIORAL`：28
- `NON_BEHAVIORAL`：0
- `NONE`：0
- `UNKNOWN`：0

## 结论

历史 Artifact 可以用于描述性工程覆盖和离线工具候选池，但不能冒充当前冻结版本的正式矩阵。用于论文或提交的同版本结论，需要同一 frozen commit、同一模型 alias、同一 Planner/Token/Budget 配置和同一 final policy 下的 28 题正式矩阵。

Router 修复不被误记为对 28 题的逐题直接修改；`GLOBAL_BEHAVIORAL 28/28` 来自 Planner、task contract/final、Budget 和 Vitis 执行路径变化。
