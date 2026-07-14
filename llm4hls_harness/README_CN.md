# LLM4HLS Agent — 内部里程碑 V0：无 LLM 的确定性验证闭环

[English](README.md) | 简体中文

V0 至 V4 是本项目的内部工程里程碑，不是比赛官方阶段。比赛提供的示例名为 **Reference Agent & Evaluation Harness**。两者的范围、接口和实现差异见[中文对比文档](../doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md)。

本目录仅包含 V0 纵向闭环：从兼容 reference harness 的公开任务包中加载不可变 baseline，随后按预算计费依次执行 `csim -> synth -> cosim`。V0 不包含 LLM、LangGraph、补丁生成、优化循环、隐藏测试评分或参考答案调用。

运行时是自包含的，仅依赖 Python 3.11 及以上版本的标准库，不会导入 reference harness。任务加载器只读取 `task.toml`、存在时的 `description.md`、配置指定的 kernel、header 和公开 testbench。任何进入 `hidden/` 或 `reference/` 的路径都会被拒绝。

## 官方参考实现对应内部哪个阶段

官方实现不能直接归入一个内部阶段，需要分别看“功能宽度”和“工程深度”：

| 内部阶段 | 官方覆盖程度 | 判断 |
| --- | --- | --- |
| V0 确定性 Harness | 核心流程部分覆盖 | 有 task loader、csim/synth/cosim、结构化结果和 credit 计费；没有不可变 baseline、持久 ledger、trace、candidate registry、幂等计费与恢复，因此不满足本项目完整 V0 验收 |
| V1 最小修复循环 | 原型级覆盖 | 有 LLM 修复循环，但直接返回完整 `.cpp`，没有受限 Patch、候选隔离、结构化诊断和安全回滚 |
| V2 Candidate/PPA 循环 | 部分覆盖 | 有 synth、latency 比较和 best code；没有 candidate tree、完整验证等级、约束优先排序和多目标 PPA |
| V3 Budget-Aware LangGraph | 基本没有 | 没有 LangGraph、checkpointer、持久 State、多维预算和可恢复副作用节点 |
| V4 Competition Hardening | 只有样例素材 | 有 hidden grading、deadlock task、scorecard 和 Dockerfile，但没有完成竞赛级硬化，辅助脚本还存在 2023.2/2025.2 版本漂移 |

因此，按“能演示哪些功能”看，官方实现大约达到 V2 原型；按“能否可靠复现、审计和恢复”看，它不能替代我们的 V0 工程底座。当前策略是保留本地 V0，在 V1/V2 中参考官方循环，在 V3 中自行实现持久化 LangGraph，并只把官方评分、deadlock task 和容器文件作为 V4 测试素材。

## 在 WSL 中复现

已验证的开发环境为 Ubuntu WSL，Vitis 2025.2 安装在 `/opt/xilinx/2025.2/Vitis`。下面假设已将官方公开 task 快照放入仓库根目录的 `_external/fpt26-harness/`；该目录仅用于本地验证，不进入 Git。在 WSL 中运行：

```bash
REPO=/mnt/c/path/to/track-A
PROJECT="$REPO/llm4hls_harness"
TASKS="$REPO/_external/fpt26-harness/tasks"
cd "$PROJECT"

python3 -m llm4hls_agent run "$TASKS/dotProduct_optimize" \
  --run-dir "$PROJECT/runs/v0-dotproduct" \
  --csim-timeout 180 --synth-timeout 600 --cosim-timeout 600

python3 -m llm4hls_agent run "$TASKS/projection_bugfix" \
  --run-dir "$PROJECT/runs/v0-projection" \
  --csim-timeout 180 --synth-timeout 600 --cosim-timeout 600 || test $? -eq 2

python3 -m llm4hls_agent run "$TASKS/residual_stream_deadlock" \
  --run-dir "$PROJECT/runs/v0-residual" \
  --csim-timeout 180 --synth-timeout 600 --cosim-timeout 300 || test $? -eq 2
```

退出码 `0` 表示 baseline 已通过 csim、synth、cosim 和配置的最低频率检查。退出码 `2` 表示确定性工作流已捕获 baseline 验证失败。退出码 `3` 表示任务包、预算、运行产物或配置错误。

使用相同的 `--run-dir` 重复执行同一命令即可验证幂等恢复：已完成的 action ID 会从持久化结果中复用，`trace.jsonl` 会记录 `TOOL_CACHE_HIT`，预算 ledger 不会重复计费。

## 配置

默认使用任务包中的 `part`、`clock_ns` 和 credit 预算，也可以通过 CLI 参数覆盖。主要环境变量如下：

- `LLM4HLS_VITIS_HLS_ROOT`：Vitis 根目录，默认为 `/opt/xilinx/2025.2/Vitis`；
- `LLM4HLS_TOOLCHAIN_ID`：工具链标识，默认为 `Vitis 2025.2`，参与缓存键计算；
- `LLM4HLS_PART`、`LLM4HLS_CLOCK_NS`；
- `LLM4HLS_CREDIT_BUDGET`；
- `LLM4HLS_COST_CSIM`、`LLM4HLS_COST_SYNTH`、`LLM4HLS_COST_COSIM`；
- `LLM4HLS_CSIM_TIMEOUT_S`、`LLM4HLS_SYNTH_TIMEOUT_S`、`LLM4HLS_COSIM_TIMEOUT_S`；
- `LLM4HLS_TOKEN_BUDGET`：无 LLM 的 V0 中，已用 Token 始终记录为 0；
- `LLM4HLS_RUNTIME_LIMIT_S`、`LLM4HLS_MIN_FREQUENCY_MHZ`。

运行 `python3 -m llm4hls_agent run --help` 可查看全部覆盖参数和各工具调用次数限制。

## 持久化产物

每个运行目录包含：

```text
task_spec.json                 公开任务元数据和公开文件哈希
run_config.json                本次运行的完整预算与工具配置
workflow_result.json           最终结构化状态和停止原因
candidate_registry.json        不可变 candidate_000 及其验证引用
budget_ledger.jsonl            带锁的 STARTED/COMPLETED/AMBIGUOUS 计费账本
budget_state.json              从权威 ledger 派生的预算快照
trace.jsonl                    工作流、工具、恢复和缓存事件
baseline/source/<kernel>.cpp   与原始文件逐字节一致的只读 baseline
actions/<action_id>/result.json
actions/<action_id>/work/      Tcl、stdout、stderr、XML、报告和 Vitis 工作目录
```

每个 action 使用完整的 SHA-256 ID，并绑定 candidate、代码、完整公开任务 fixture、工具配置、工具链标识和 backend 指纹。每条 `COMPLETED` ledger 事件都会保存对应 `result.json` 的精确摘要；所有引用的 Tcl、日志、XML 和报告也分别保存摘要，并在缓存命中时重新校验。

单次工具调用的有效超时不会超过剩余总运行时间预算。同一运行目录不允许多个写入进程并发执行。

与 reference harness 兼容的 phase 包括 `pass`、`compile_error`、`runtime_fail`、`synth_error`、`cosim_fail` 和 `timeout`；基础设施异常记录为 `tool_error`。综合报告缺失或无法解析时，系统绝不会伪造 PASS。

## 快速测试

```bash
python3 -m unittest discover -s tests -v
```

快速测试使用确定性的 fake process/tool backend，不需要 Vitis。真实 Vitis 证据必须使用前述命令单独生成。
