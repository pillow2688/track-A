# 回退完整性与 Track A 最小差异审计

日期：2026-07-22  
审计基线：`d9c5d76` (`fix: bind continuation policy to run identity`)  
审计分支：`feat/track-a-post-rollback-baseline`

## 结论

回退完整：当前工作树干净，`HEAD` 与 V3-F 分支共同指向
`d9c5d76dcaaa9bd25b740171d4a66abf2558ac2b`。V3-G 的 6 个实现提交和最终
文档提交没有位于当前分支历史中；它们仅保存在本地保护引用
`backup/v3g-before-rollback-20260722`（`4ec09dd`），没有合并或推送。

本审计以当前源代码、当前 Git 图和已保存的 run/experiment artifact 为准。它不把
V3-G 文档、Chat 记录或未重新运行的历史结果当作当前能力。

## 实际检查结果

| 项目 | 当前事实 | 结论 |
|---|---|---|
| 分支 / HEAD | `feat/track-a-post-rollback-baseline` / `d9c5d76` | 正确回到 V3-F |
| 工作树 | 审计开始时 clean | 无未提交 V3-G 残留 |
| 最近历史 | `d9c5d76 → a67aa9c → 1f5ad2b → a2b3489 ...` | V3-F 历史可见 |
| 回退前后差异 | V3-G 的 config、generation fixture、numeric/generalization/Pareto/follow-up 模块、V3-G tests、实验和提交材料均不在当前 tree | 回退完整 |
| 当前文档 | 当前阶段文档和 V3-F 报告仍描述 V3-F；V3-G 报告不在当前 tree | 无已回退能力的当前声明 |
| 真实 artifact | repository 中仍保存历史 `REAL_LLM_VITIS` experiment artifact 与本地 `runs/`；它们是历史证据，不等于 post-rollback fresh run | 不作新的性能/分数声明 |

### 已回退的内容（通过真实 diff 核验）

`d9c5d76..backup/v3g-before-rollback-20260722` 的文件差异包括：

- `competition_safe_v1.json`、V3-G capability/replay/follow-up artifacts；
- generation fixtures、`v3_generation.py`、numeric/generalization guards；
- PPA evidence、Pareto archive、V3-G follow-up/recovery/shared-feature modules；
- V3-G tests、V3-G 状态记录和新的 submission scaffold；
- 对 `task.py`、`repair.py`、`workflow.py`、`v3_openai_planner.py`、
  `v3_prototype.py` 的 V3-G 修改。

这些文件均不在当前 checkout；当前不会加载它们。

## 当前 V3-F 真实能力与最小差异

| Track A 对齐项 | 当前代码证据 | 状态 | 最小动作 |
|---|---|---|---|
| 四种 phase mode | `v3_phase_router.py` 有 `REPAIR/SYNTH_FIX/STRUCTURAL_FIX/OPTIMIZE` | 已有 | 保持 |
| task type 元数据 | `task.py` 读取任意字符串，默认 `generate`，未明确校验/导出 `generation_required` | 部分 | 限定公开支持类型并导出生成标记 |
| kernel-only / top guard | `repair.py` + `top_interface_guard.py` | 已有 | 保持 |
| generate 大 patch | V3 默认 `PatchLimits(30,4)`；whole-file replacement 禁止 | 缺失 | 仅对 public `generation_required` 允许受限的大 kernel patch |
| 工具成本 | BudgetLedger 支持 config；V3 CLI 却写死 `1/4/20` | 缺失 | V3 CLI 加配置化参数/env 默认 |
| 预算分账 | ledger 有 action、token、credit、final scope；终端只有合计 | 部分 | 只增加由 ledger/result 推导的三类成本展示 |
| `requires_cosim` | PhaseRouter 对 required baseline CoSim fail-closed；fast profile 对普通优化不盲跑；fresh final 总跑 CoSim | 已有 | 测试确认 |
| local reference score proxy | `scoring.py` 有 hidden/public failure = 0、8× cap 和 acceleration | 已有 | difficulty 缺失标记 UNKNOWN，非官方结论 |
| 8× latency 停止 | score cap 存在，但 V3 continuation/stop 没有显式 cap-stop | 缺失 | 仅在正确、时钟/资源满足后停止纯 latency follow-up |
| Token/runtime | BudgetLedger 和 V3 CLI 已分别记录 input/output/total、time limit、无静默重试 | 已有 | 安全配置固定默认 |
| Power | V3-F 仅有 performance-area proxy，明确不声称 Power | 部分 | 显式输出 `UNSUPPORTED`，不构造 proxy |

## 当前主 Graph

当前 `build_v3_prototype_graph()` 仍是 V3-F 的既有 action graph；本阶段不增加或重构
节点/边。`PhaseRouter` 仍位于 baseline CSim/Synth/(required CoSim) 后。Continuation
的 `shadow` 模式只持久化决定，不改写既有路径。

## 外部 Smoke 前置条件

本执行环境中 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`LLM4HLS_MODEL` 和
`LLM4HLS_VITIS_HLS_ROOT` 均未设置；默认 Vitis root 为
`/opt/xilinx/2025.2/Vitis`。因此没有启动任何伪造的“真实” Planner/Vitis run。
历史真实 artifact 会保留，但不能代替本阶段的 fresh smoke。

## 下一步

仅实现上表中标为“缺失”或“部分”的最小补丁，随后运行 package-aware 完整 unittest、
compileall、diff check 和可用的 smoke 前置检查。
