# LLM4HLS Agent — 内部里程碑 V0–V3-A1

[English](README.md) | 简体中文

V0 至 V4 是本项目的内部工程里程碑，不是比赛官方阶段。比赛提供的示例名为 **Reference Agent & Evaluation Harness**。两者的范围、接口和实现差异见[中文对比文档](../doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md)。

V0 提供不可变、受预算审计的 `csim -> synth -> cosim` baseline。V1 增加确定性失败诊断、紧凑修复上下文、一次受限 unified diff、隔离候选、真实验证、晋级与安全回滚。修复建议可以来自 OpenAI-compatible API，也可以来自静态补丁测试夹具。V2 增加持久 Candidate 树、确定性验证/约束/PPA/成本比较、每轮一种优化类、best 保留、最终复验和安全拒绝。V3-A1 目前包含独立的确定性多轮 LangGraph、版本化 Planner 输入/输出/action 契约、循环级综合证据、完整 Planner 溯源日志和哈希封存的终态包。Candidate 被拒绝后可继续下一条 scripted 提议，晋升/拒绝/final 选择仍可恢复。自主 LLM Planner 和隐藏评分尚未包含。

`runs/v1-deepseek-final` 是 DeepSeek 曾成功修复 `FUNCTIONAL_MISMATCH` 的历史证据，但它早于严格 Candidate/action 绑定和 Artifact Manifest，必须重新生成，不能代表 V1 完成。只有 `FUNCTIONAL_MISMATCH`、`COMPILE_ERROR`、`SYNTHESIS_ERROR` 都具备真实 DeepSeek/Vitis 证据，并且独立的 `PATCH_INVALID` 安全负例通过确定性验收器，才可宣布 V1 完成。

V0–V2 运行时是自包含的，仅依赖 Python 3.11 及以上版本的标准库；V3-A1 的可选依赖固定在 `.[v3]` extra 中。系统不会导入 reference harness。任务加载器只读取 `task.toml`、存在时的 `description.md`、配置指定的 kernel、header 和公开 testbench。任何进入 `hidden/` 或 `reference/` 的路径都会被拒绝。

## V3-A1 LangGraph 原型

V3-A1 不改变原有 `run`、`repair` 或 `optimize`（V2）入口。它通过独立命令把 baseline CSim/Synth/CoSim、scripted Planner、Candidate 物化、Candidate CSim/Synth、CoSim 价值门控、晋升/拒绝、轮次继续/停止、final CSim/Synth/CoSim 和团队报告拆成可 checkpoint 的动作节点。重复传入 `--patch-file` 可按顺序运行多个确定性提议；只传一个文件时保持原单轮行为。`--max-no-improvement-rounds` 控制连续无提升停止线；只有显式传入 `--enable-final-fallback` 且预算充足时，才最多再尝试一次完整 final 闭环。

每一轮 Planner 现在都会持久化 canonical input、STARTED 日志、版本化 output、旧报告投影和 COMPLETED 日志；Candidate 元数据与最终 Manifest 通过哈希绑定整条链。封包前还会从 Ledger 绑定的工具结果重新计算全部 score，并校验 Candidate 决策的 prepared/committed 配对、operation ID、revision 链和最终 Registry 绑定。若 `csynth.xml` 提供循环信息，综合证据会记录 loop `PipelineII`、TripCount、loop latency 和调度 violation，并明确区分 top-level transaction interval 与 loop II。任何缺失或被修改的溯源证据都会 fail closed。

严格 happy path 的工具成本是 `25 + 25 + 25 = 75 credits`：baseline 完整闭环、Candidate
完整闭环和一套全新的 final 闭环。如果启动 Candidate 后无法保留 final 所需的 25 credits，
原型会跳过这一轮优化，改为最终验证已通过的 baseline。

```bash
cd /home/ying/CompetitionTrackA/track-A
python3 -m venv .venv
.venv/bin/python -m pip install -e 'llm4hls_harness[v3]'

# 只证明 LangGraph、路由、账本、Candidate 和报告数据流
.venv/bin/llm4hls-v3-prototype \
  --task-dir llm4hls_harness/examples/u55c_v2_optimize_task \
  --patch-file llm4hls_harness/examples/u55c_v3_prototype.diff \
  --run-dir runs/v3a1-prototype-demo --backend demo
```

真实 Vitis 运行应先进入本地开发用 Distrobox（前提是本机已经创建该容器），再用显式
task 和 Patch 启动同一 CLI：

```bash
distrobox enter vitis-2025-2
cd /home/ying/CompetitionTrackA/track-A
.venv/bin/llm4hls-v3-prototype \
  --task-dir llm4hls_harness/examples/u55c_v2_optimize_task \
  --patch-file llm4hls_harness/examples/u55c_v3_prototype.diff \
  --run-dir runs/v3a1-prototype-vitis --backend vitis \
  --vitis-root /home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis
```

`demo` 后端必须标记为 `ORCHESTRATION_SMOKE_ONLY`，不能冒充 HLS 或模型能力证据。`vitis` 后端会真实运行三类工具；结果保存在 `v3_prototype_result.json`，逐节点复盘保存在 `v3_team_report.md`，LangGraph checkpoint 保存在 `graph_checkpoints.sqlite`，`control/package_manifest.json` 与 Candidate/Planner 决策日志负责恢复和防篡改。下一步是 V3-B：把确定性适配器替换为受预算计费、可恢复的自主 LLM Planner，同时不授予模型直接工具或文件系统权限。
结果 JSON 最后提交；终态再入时可以仅根据它重建缺失的生成式 Markdown 报告，不会重跑
任何 HLS 工具。
`vitis` 模式会在启动 Graph 前检查 `<vitis-root>/settings64.sh`，缺失时不会调用工具或消耗
Credit。完整真实闭环成功标记为 `REAL_VITIS_VALIDATED`，已经启动但失败标记为
`REAL_VITIS_ATTEMPT_FAILED`。`vitis-2025-2` 是当前本地开发环境，不是比赛最终提交的 Docker 镜像；最终比赛
Docker 的构建与验收属于 V4，目前尚未完成。

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
每轮一种优化类、best 保留、最终复验和独立安全拒绝；V3-A1 已提供独立的确定性多轮 LangGraph 与证据绑定 Planner 边界，但尚未替代 V2 默认流程。

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
保守的 `PIPELINE II=16`。最多允许六次 Candidate 尝试，每轮只允许一种优化类；连续两轮
无改进即停止探索。每个 Candidate 先运行 CSim 和 Synth，只有 Synth PPA 严格优于当前
best 才允许消耗探索 CoSim Credits；通过门控的 Candidate 还必须通过 CoSim、时钟和资源
硬约束后才能成为 best。最终 best 必须再独立完整运行 CSim、Synth 和 CoSim。比较顺序
仍固定为验证等级、硬约束、以 latency/II 为主的 PPA、Token/Credit 成本和稳定 Candidate
ID。

`runs/v2-optimize-final` 中已验收的 V2 证据已经冻结，禁止覆盖。使用该门控策略时必须选择
新的运行目录。

V2 的最终发布记录为
[`releases/v2-ppa-gated-final_CN.md`](releases/v2-ppa-gated-final_CN.md)，机器可读元数据为
[`releases/v2-ppa-gated-final.json`](releases/v2-ppa-gated-final.json)。全局冠军固定为
`v2-optimize-final/candidate_004`；后续真实门控回归 Candidate 仅作为成本策略证据，不能
替换该冠军。V2 发布完成后进入 V3 LangGraph 编排阶段。

配置好前述 API 与 Vitis 环境后运行真实优化：

```bash
python3 -m llm4hls_agent optimize examples/u55c_v2_optimize_task \
  --run-dir runs/next-ppa-gated \
  --vitis-root "$LLM4HLS_VITIS_HLS_ROOT" \
  --clock-ns 10 --minimum-frequency-mhz 100 \
  --credit-limit 160 --max-optimization-rounds 6 \
  --max-no-improvement-rounds 2 --max-final-attempts 2 \
  --max-csim-calls 8 --max-synth-calls 8 --max-cosim-calls 8 \
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

2026-07-18 的两次真实 Vitis 团队复盘已按“只发布核心 Markdown、不提交生产运行环境”
整理到 [`releases/v2-team-reports-2026-07-18/`](releases/v2-team-reports-2026-07-18/README.md)。
阅读顺序、数据流、证据边界和本地重建方法见
[《V2 核心报告阅读与更新指南》](../doc/materials/07_experiments/2026-07-18-v2-core-report-guide.md)。

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
