# Task corpus

这个目录集中保存可用于 Harness 回归与后续批量评测的公开题目。选择顺序由
`manifest.json` 固定为：

1. `official/fpt26-harness-public/`：比赛官方公开 PoC 中的三个最小任务包；
2. `v3d-fast/tasks/`：确定性生成的 28 道 V3-D 快速开发题；
3. `../examples/`：本项目自己维护的示例与测试任务。

官方公开集合目前覆盖三类不同问题：

- `dotProduct_optimize`：正确但未优化；
- `projection_bugfix`：C 仿真可发现的功能错误；
- `residual_stream_deadlock`：C 仿真通过、C/RTL CoSim 才暴露的死锁。

每个题目都只含 `task.toml`、kernel、header 和 public testbench，不含 hidden
test 或 reference answer。文件来源、SHA-256 和授权状态见
`official/fpt26-harness-public/provenance.json`。

注意：这些是官方公开 PoC 题目，不是正式 hidden 题库。当前 Harness 会把
baseline 与最终复验也计入 agent credits，因此前两个题目的原始官方预算不能直接
覆盖现有严格流程；具体差额记录在 `manifest.json`，在实现官方预算兼容模式前不要
悄悄改写题目的 `budget`。

当前题库只面向源码 checkout，未放进 Python wheel 的 package data。这样做是有意的：
官方公开快照没有声明许可证，在确认再分发授权前，不应把第三方源码自动塞进安装包。

## V3-D 快速题库

`v3d-fast/` 独立于官方快照，固定覆盖 `REPAIR 8 / SYNTH_FIX 6 /
STRUCTURAL_FIX 6 / OPTIMIZE 8`。每题的公开 Task Loader 输入与离线验收材料分开：

- Planner 可见：`task.toml`、`description.md`、`kernel.cpp`、`kernel.h`、
  `kernel_tb.cpp`；
- 仅离线验收：`hidden_like/`、`golden/`、`mutation_manifest.json`、
  `acceptance.json`。

生成器按 operator、seed、base SHA-256 和 source range 确定性重放：

```bash
python -m llm4hls_agent.v3d_corpus task_corpus/v3d-fast
python -m llm4hls_agent.v3d_corpus --check task_corpus/v3d-fast
```

这些题只提供 deterministic fixture 证据，不宣称通过真实 Vitis。`answer`、
`golden`、`hidden`、`hidden_like`、`reference` 五个路径分量会被 Task Loader 与
V3 Planner 明确拒绝。
