# LLM4HLS Agent — 内部里程碑 V0–V2

[English](README.md) | 简体中文

V0 至 V4 是本项目的内部工程里程碑，不是比赛官方阶段。比赛提供的示例名为 **Reference Agent & Evaluation Harness**。两者的范围、接口和实现差异见[中文对比文档](../doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md)。

V0 提供不可变、受预算审计的 `csim -> synth -> cosim` baseline。V1 增加确定性失败诊断、紧凑修复上下文、一次受限 unified diff、隔离候选、真实验证、晋级与安全回滚。修复建议可以来自 OpenAI-compatible API，也可以来自静态补丁测试夹具。V2 增加持久 Candidate 树、确定性验证/约束/PPA/成本比较、每轮一种优化类、best 保留、最终复验和安全拒绝。V3 LangGraph 编排、隐藏评分和参考答案调用尚不包含在内。

`runs/v1-deepseek-final` 是 DeepSeek 曾成功修复 `FUNCTIONAL_MISMATCH` 的历史证据，但它早于严格 Candidate/action 绑定和 Artifact Manifest，必须重新生成，不能代表 V1 完成。只有 `FUNCTIONAL_MISMATCH`、`COMPILE_ERROR`、`SYNTHESIS_ERROR` 都具备真实 DeepSeek/Vitis 证据，并且独立的 `PATCH_INVALID` 安全负例通过确定性验收器，才可宣布 V1 完成。

运行时是自包含的，仅依赖 Python 3.11 及以上版本的标准库，不会导入 reference harness。任务加载器只读取 `task.toml`、存在时的 `description.md`、配置指定的 kernel、header 和公开 testbench。任何进入 `hidden/` 或 `reference/` 的路径都会被拒绝。

## V1 OpenAI-compatible 修复

API Provider 默认模型为 `deepseek-v4-pro`。DeepSeek 官方 OpenAI-compatible 地址为 `https://api.deepseek.com`。对 DeepSeek V4 修复请求显式关闭默认 thinking mode，使有限输出预算用于最终 JSON Patch；成功和失败请求都必须记录 Provider 报告的输入/输出 Token。端点和密钥通过环境变量配置；API key 不会写入运行配置、trace、Prompt、动作结果或 Provider 指纹。

```bash
cd /home/ying/CompetitionTrackA/track-A/llm4hls_harness
export LLM4HLS_VITIS_HLS_ROOT=/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis
export OPENAI_BASE_URL=https://api.deepseek.com
export OPENAI_API_KEY=your-secret-value
export LLM4HLS_MODEL=deepseek-v4-pro

python3 -m llm4hls_agent repair examples/u55c_repair_task \
  --run-dir runs/v1-deepseek-v4-pro \
  --clock-ns 10 --minimum-frequency-mhz 100
```

仅用于确定性离线回归时：

```bash
python3 -m llm4hls_agent repair examples/u55c_repair_task \
  --run-dir runs/v1-static \
  --provider static --patch-file examples/u55c_repair.diff \
  --clock-ns 10 --minimum-frequency-mhz 100
```

`--allow-deterministic-fallback` 仅用于调试指定的 vector-add fixture，默认关闭。fallback 通过 Vitis 只能证明候选验证闭环有效，不能作为 LLM-based V1 的模型验收证据。真实 V1 验收要求候选 provider 为 `openai-compatible`、Token 用量来自 API usage，且该候选通过 Vitis `csim/synth/cosim`。

模型只接收结构化失败证据、局部 kernel 行、公开约束和预算摘要，并且必须返回只含一个 unified diff 的严格 JSON 对象。只有配置指定的 kernel `.cpp` 可以修改。`CANDIDATE_VERIFIED` 表示隔离候选已通过 csim、synth、cosim 和最低时钟约束。Provider、Patch、验证或预算失败都会产生明确停止原因，并保持 baseline/best 不受污染。

Patch proposal 必须先完成解析、策略检查和针对不可变源码的 dry-run，之后才允许
分配 Candidate ID。非法 Patch 因而不会创建 Candidate 目录或 registry 记录；合法
Patch 才会原子物化、把验证状态重置为 `NOT_RUN`、注册并以自己的 Candidate ID
绑定 Vitis action。V2 已加入持久 Candidate 树、按验证/硬约束/PPA/成本的确定性比较、
每轮一种优化类、best 保留、最终复验和独立安全拒绝；V3 LangGraph 编排仍未包含。

## V1 三类错误统一验收

| 场景 | Baseline 阶段边界 | 必须达到的结果 |
|---|---|---|
| `FUNCTIONAL_MISMATCH` | public csim mismatch | 真实 DeepSeek Candidate 通过 csim/synth/cosim/clock |
| `COMPILE_ERROR` | csim 编译失败 | 真实 DeepSeek Candidate 通过 csim/synth/cosim/clock |
| `SYNTHESIS_ERROR` | csim PASS、synth 失败 | 真实 DeepSeek Candidate 通过 csim/synth/cosim/clock |
| `PATCH_INVALID` | 越权修改 testbench | workflow 在 Candidate 分配前安全失败 |

不调用 LLM 即可预检查两个新增 baseline：

```bash
python3 -m llm4hls_agent run examples/u55c_compile_repair_task \
  --run-dir runs/v1-compile-preflight --clock-ns 10

python3 -m llm4hls_agent run examples/u55c_synthesis_repair_task \
  --run-dir runs/v1-synthesis-preflight --clock-ns 10
```

三个 HLS 任务分别使用前述 API 环境运行 `repair`。独立安全负例不调用 API：

```bash
python3 -m llm4hls_agent repair examples/u55c_repair_task \
  --run-dir runs/v1-patch-invalid \
  --provider static --patch-file examples/u55c_patch_invalid.diff \
  --clock-ns 10 || test $? -eq 2
```

最后对四个运行目录执行统一验收。验收器只读证据目录，并把结果写入独立目录：

```bash
python3 -m llm4hls_agent accept-v1 \
  --functional-run runs/v1-functional-final-2 \
  --compile-run runs/v1-compile-final \
  --synthesis-run runs/v1-synthesis-final-2 \
  --patch-invalid-run runs/v1-patch-invalid \
  --output-dir runs/v1-acceptance
```

只有规范要求的真实证据才能得到 `overall_status=PASS`。单元测试和 fake backend
证据只能得到 `TEST_PASS`，不能据此宣布 V1 完成。命令会同时生成机器可读的
`acceptance_result.json`，以及包含四场景矩阵、报告/Manifest 链接和完整复现命令的
英文 `acceptance_report.md` 与中文 `acceptance_report_CN.md`。

需要直接人工审核时，可在不重新运行模型或 Vitis、也不修改任何 JSON、ledger、Trace、
Manifest、action 或 Candidate 产物的前提下，离线聚合现有证据：

```bash
python3 -m llm4hls_agent review-v1 --runs-root runs
```

命令直接在 `runs/` 根目录平铺生成两个文件：

- `V1_ACCEPTANCE_REPORT_CN.md`：完整中文单文件审核入口；
- `V1_ACCEPTANCE_REPORT.md`：完整英文单文件审核入口。

报告会重新计算并交叉核对机器验收、Baseline 错误、模型/fallback、Patch 范围与接口、
Final gates、Candidate 提升/回滚、Tokens、工具调用、Credits、Ledger、Trace、action
记录和 Manifest hash。所有原始证据链接都相对于 `runs/`。

## V2 Candidate 与 PPA 循环

V2 使用自包含的 256 元素 U55C `vector_add` fixture。Baseline 功能正确，但故意使用
保守的 `PIPELINE II=16`。最多执行四个主轮次，每轮只允许一种优化类；连续两轮无改进
即停止探索。Candidate 只有在 CSim、综合、CoSim、时钟和资源硬约束全部通过后才有资格
成为 best。比较顺序固定为验证等级、硬约束、以 latency/II 为主的 PPA、Token/Credit
成本和稳定 Candidate ID。

配置好前述 API 与 Vitis 环境后运行真实优化：

```bash
python3 -m llm4hls_agent optimize examples/u55c_v2_optimize_task \
  --run-dir runs/v2-optimize-final \
  --vitis-root "$LLM4HLS_VITIS_HLS_ROOT" \
  --clock-ns 10 --minimum-frequency-mhz 100 \
  --credit-limit 160 --max-optimization-rounds 4 \
  --max-no-improvement-rounds 2 --max-final-attempts 2 \
  --final-reserve-credits 25 \
  --csim-timeout 180 --synth-timeout 900 --cosim-timeout 900
```

独立的确定性语义回归安全场景不调用 LLM。它先完整验证 baseline，Patch 校验通过后才
物化回归 Candidate，只对该 Candidate 运行 CSim，并且必须拒绝它、保持 best/final
不受污染：

```bash
python3 -m llm4hls_agent reject-v2 examples/u55c_v2_optimize_task \
  --run-dir runs/v2-safety-rejection-final \
  --patch-file examples/u55c_v2_regression.diff \
  --vitis-root "$LLM4HLS_VITIS_HLS_ROOT" \
  --clock-ns 10 --minimum-frequency-mhz 100 \
  --credit-limit 160 --csim-timeout 180 \
  --synth-timeout 900 --cosim-timeout 900
```

机器验收会重新计算 Candidate 树、全部 score/comparison、best/final、Provider/Model/
Token、公开输入与仅 kernel Patch 绑定、安全不变量，以及 Ledger/Trace/action/
budget_state 一致性：

```bash
python3 -m llm4hls_agent accept-v2 \
  --optimization-run runs/v2-optimize-final \
  --rejection-run runs/v2-safety-rejection-final \
  --output-dir runs/v2-acceptance

python3 -m llm4hls_agent review-v2 --runs-root runs
```

只有真实 Vitis/DeepSeek 证据能得到 `PASS`；fake 证据只能得到 `TEST_PASS`。
`review-v2` 不调用模型或 Vitis，只在 `runs/` 根目录平铺生成
`V2_ACCEPTANCE_REPORT_CN.md` 和 `V2_ACCEPTANCE_REPORT.md`，不生成 HTML。

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
- `LLM4HLS_TOKEN_BUDGET`：V0 为 0；V1 按 API usage 分别记录输入、输出、缓存输入和总 Token，失败调用也计入；
- `LLM4HLS_RUNTIME_LIMIT_S`、`LLM4HLS_MIN_FREQUENCY_MHZ`；
- `OPENAI_BASE_URL`、`OPENAI_API_KEY`；
- `LLM4HLS_MODEL`（默认 `deepseek-v4-pro`）；
- `LLM4HLS_LLM_TIMEOUT_S`、`LLM4HLS_LLM_MAX_OUTPUT_TOKENS`、
  `LLM4HLS_LLM_TEMPERATURE`、`LLM4HLS_COST_LLM`。

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
diagnostics/<candidate_id>.json
llm_actions/<action_id>/result.json
candidates/<candidate_id>/     只读源码、Patch 和候选元数据
v1_result.json                 V1 决策、验证、回滚和预算结果
optimization_config.json       V2 评分、循环限制、Patch 策略和 Provider 指纹
optimization_rounds/*.json     可恢复的 Selector/提案/Candidate/决策记录
scores/*.json                  可重算的 Candidate PPA 与成本分数
comparisons/*.json             字典序比较证据
v2_result.json                 V2 best/final、轮次、最终验证和预算
v2_rejection_result.json       确定性安全拒绝及全部不变量
experimental_report.md         面向人的 Vitis、Token、credit 与指标报告
artifact_manifest.json         排序后的路径、大小、SHA-256、producer/action 绑定
```

每个 action 使用完整的 SHA-256 ID，并绑定 candidate、代码、完整公开任务 fixture、工具配置、工具链标识和 backend 指纹。每条 `COMPLETED` ledger 事件都会保存对应 `result.json` 的精确摘要；所有引用的 Tcl、日志、XML 和报告也分别保存摘要，并在缓存命中时重新校验。

单次工具调用的有效超时不会超过剩余总运行时间预算。同一运行目录不允许多个写入进程并发执行。

与 reference harness 兼容的 phase 包括 `pass`、`compile_error`、`runtime_fail`、`synth_error`、`cosim_fail` 和 `timeout`；基础设施异常记录为 `tool_error`。综合报告缺失或无法解析时，系统绝不会伪造 PASS。

## 快速测试

```bash
python3 -m unittest discover -s tests -v
```

快速测试使用确定性的 fake process/tool backend，不需要 Vitis。真实 Vitis 证据必须使用前述命令单独生成。
