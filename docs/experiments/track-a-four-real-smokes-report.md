# Track A 四类真实 Smoke 执行报告

日期：2026-07-22

分支：`feat/track-a-vitis-run-task-final-smoke`

执行起点：`e880e3d`
真实 run 根目录：`llm4hls_harness/runs/track_a_real_smoke_20260722_A01/`

## 结论

四类任务都已经完成 **真实 DeepSeek Planner + 真实 Vitis 2025.2** 的独立运行。所有
Planner、Candidate、CSim/Synth/CoSim 和 fresh final 都来自本次新的 run 目录；没有复用
旧 Planner response、Candidate 或 final 结果。

- 官方 repair：成功修复，fresh final `CSim + Synth` PASS；
- 官方 optimize：从 `1027` 降至 `38` cycles，raw acceleration=`27.03x`，达到 8x
  acceleration cap 后停止纯 latency 搜索；fresh final `CSim + Synth` PASS；
- 官方 structural：baseline CoSim 真实 deadlock，修复后 Candidate CoSim 及 fresh final
  `CSim + Synth + CoSim` 全部 PASS；
- 公开开发 generate task：以 `generation_required=true` 进入 REPAIR，fresh final
  `CSim + Synth` PASS。

这证明当前安全基线可实际完成四条入口的端到端闭环。它**不是** hidden 评测，也不构成
最终官方分数主张；Power 依然是 `UNSUPPORTED`。

## 1. 历史与运行环境

先前预检因 Codex 进程没有继承 Provider 环境而标记 `REAL_SMOKE_BLOCKED`。本次改为仅在
实际运行子进程中加载已 Git-ignore 的本地 Provider 环境文件：密钥未读出、未打印、未写入
Prompt、run artifact 或本报告。

最小不含任务源码的 Provider JSON 预检通过：

| 项目 | 实际结果 |
|---|---|
| Provider/model | OpenAI-compatible / `deepseek-v4-pro` |
| 请求结果 | PASS |
| input / output Token | 45 / 5 |
| finish reason | `stop` |

所有真实工具 receipt 选择同一工具链：

| 项目 | 结果 |
|---|---|
| executable | `vitis-run` |
| invocation | `vitis-run --mode hls --tcl run_hls.tcl` |
| version | Vitis `2025.2`，SW Build `6295257` |
| receipt schema | `v3.vitis-toolchain-receipt.v1` |
| receipt preflight | `READY` |

## 2. 固定运行配置与隔离边界

所有 run 均使用：

- `backend=vitis`、`validation-profile=fast-experiment`、
  `final-validation-policy=task_contract`；
- `continuation-policy=shadow`、`experience-mode=shadow`；两者只留下审计 artifact，
  不改变 route 或 Planner Prompt；
- CSim/Synth/CoSim 的 credit cost=`1/4/20`；
- `max-planner-rounds=3`、`max-no-improvement-rounds=2`、run Token limit=`32768`；
- public task source、fixed header 和 public description 允许进入 Planner；
  hidden/reference/golden/testbench 不允许进入 Prompt；
- kernel-only Patch Validator 和 top/interface guard 全程开启；
- Power=`UNSUPPORTED`，没有制造 Power proxy；External grader cost 均为 `0`。

安全基线配置为
[track_a_safe_baseline_v1.json](../../llm4hls_harness/llm4hls_agent/config/track_a_safe_baseline_v1.json)，
SHA-256：`ca1ca142f3488062184069e0fc434f3c680687dfed561b10d5bd20aaedd27e48`。

## 3. 四类真实结果

| Task | 来源 / mode | Baseline 路径 | Planner | Search / Internal final / Total credit | Token (in/out) | Fresh final | 结果 |
|---|---|---|---:|---:|---:|---|---|
| `projection_bugfix` | official / REPAIR | CSim FAIL → REPAIR | 1 | 6 / 5 / 11 | 1617 / 423 | CSim PASS；Synth PASS；CoSim NOT_RUN（非必需） | PASS |
| `dotProduct_optimize` | official / OPTIMIZE | CSim PASS；Synth PASS | 3 | 15 / 5 / 20 | 5602 / 1927 | CSim PASS；Synth PASS；CoSim NOT_RUN（非必需） | PASS |
| `residual_stream_deadlock` | official / STRUCTURAL_FIX | CSim PASS；Synth PASS；CoSim FAIL | 1 | 46 / 25 / 71 | 1611 / 609 | CSim PASS；Synth PASS；CoSim PASS | PASS |
| `v3d_fast_004` | public development / REPAIR, `generation_required=true` | CSim FAIL → REPAIR | 1 | 6 / 5 / 11 | 1348 / 413 | CSim PASS；Synth PASS；CoSim NOT_RUN（非必需） | PASS |

四条运行的 wall time 依次为 `56.28s`、`134.15s`、`153.28s`、`42.30s`。所有 final
CSim/Synth/CoSim action 均是 fresh（`cached=false`）。

### 3.1 `projection_bugfix`：功能修复

- baseline CSim 的 public test 失败，PhaseRouter 正确选择 `REPAIR`；
- Planner 定位 `angle==0` 分支漏掉第三个 `z` 顶点项；
- 一次小 Patch 后 Candidate CSim、Synth PASS；
- final 按 `requires_cosim=false` 的 task contract 执行新鲜 CSim+Synth，均 PASS；
- 无 baseline Synth latency，因此没有声称 PPA acceleration。

### 3.2 `dotProduct_optimize`：真实 PPA 闭环

| 阶段 | Candidate | 策略 / 结果 | Latency | 说明 |
|---|---|---|---:|---|
| baseline | `candidate_000` | 已正确、已流水化 | 1027 | top transaction interval=1025，不把它误当 loop II |
| round 1 | `candidate_001` | `LOOP_UNROLL + MULTI_PARTIAL_SUM + PARALLEL_REDUCTION` | 518 | CSim/Synth PASS，严格改善，低风险延后 CoSim |
| round 2 | — | `PAR_FACTOR_TUNING` Patch | — | Patch context 不唯一，Patch Validator 拒绝；未创建 Candidate、未调用 Vitis |
| round 3 | `candidate_002` | `ARRAY_PARTITION + LOOP_UNROLL + LOOP_PIPELINE` | 38 | CSim/Synth PASS，晋升并 fresh final PASS |

最终 raw acceleration 为 `1027 / 38 = 27.03x`；公开 proxy 的 acceleration cap 是 `8x`，
所以系统正确产生 `ACCELERATION_CAP_REACHED` 并停止后续纯 latency follow-up。final clock
estimated period=`3.17 ns`，满足 100 MHz 要求。此任务 `requires_cosim=false`，因此没有为
非必需 CoSim 消耗 20 credit。

### 3.3 `residual_stream_deadlock`：结构死锁修复

- baseline 的 CSim、Synth 都 PASS，但强制 baseline CoSim 失败；
- CoSim evidence 指向 `s_main` 满而 `s_skip` 空的 FIFO 循环等待；
- PhaseRouter 选择 `STRUCTURAL_FIX`；Planner 用一次 Patch 将 stageA 对 main/skip 的
  burst 写改为交错写；
- Candidate CSim PASS、CoSim PASS；fresh final CSim、Synth、CoSim 均 PASS；
- 资源允许的伴随时延改善为 baseline `135` → final `68` cycles（约 `1.99x`），但本次
  主张是结构正确性修复，不把它写作官方最终评分。

### 3.4 `v3d_fast_004`：generation 入口

该任务是公开 development corpus，不能称为官方 Track A 分布。Task Loader 读取到
`task_type=generate`、`generation_required=true`，且 PhaseRouter 如设计映射至 `REPAIR`。
真实 Planner 修复了 kernel 内的循环索引，Candidate 与 fresh final CSim/Synth PASS。

本题的真实 Patch 恰好是小修复，而不是大范围 kernel-body 替换；因此它验证了
`generation_required` 的 **入口、接口保护和真实工具闭环**，但尚未单独证明复杂空 stub
的大 Patch 生成质量。

## 4. `requires_cosim` 与预算行为

本次真实运行直接验证了 task contract，而非“所有任务强制三件套”：

- `projection_bugfix`、`dotProduct_optimize`、`v3d_fast_004` 的
  `requires_cosim=false`：最终只有 fresh CSim+Synth，CoSim 状态是
  `NOT_RUN / FINAL_COSIM_NOT_REQUIRED`；
- `residual_stream_deadlock` 的 `requires_cosim=true`：baseline CoSim 是 routing gate，
  Candidate CoSim 是 structural gate，fresh final CoSim 也是必需 gate；
- dotProduct 的两个低/中风险、且 Synth 严格改善 Candidate 均延后 CoSim，避免无必要的
  20-credit 调用；
- structural run 的 credit 分布精确反映为 baseline CSim+Synth+CoSim=`25`、Candidate
  CSim+CoSim=`21`、final CSim+Synth+CoSim=`25`，总计 `71`。

## 5. Artifact 与可复核性

每个 task 都有不可变 run 目录，包含：`trace.jsonl`、`budget_ledger.jsonl`、
`candidate_registry.json`、Planner input/output、Patch、Vitis action result、工具 log、
`vitis_toolchain.json`、final result 和 team report。运行产物按项目规则不提交 Git。

可从以下位置复核：

- [projection run](../../llm4hls_harness/runs/track_a_real_smoke_20260722_A01/projection_repair/v3_prototype_result.json)
- [dotProduct run](../../llm4hls_harness/runs/track_a_real_smoke_20260722_A01/dotproduct_optimize/v3_prototype_result.json)
- [residual run](../../llm4hls_harness/runs/track_a_real_smoke_20260722_A01/residual_structural/v3_prototype_result.json)
- [generation run](../../llm4hls_harness/runs/track_a_real_smoke_20260722_A01/generation_stub/v3_prototype_result.json)

这些链接只在本机 workspace 内有效；报告没有复制任何 API key、hidden/reference/golden 或
完整 testbench 内容。

## 6. 回归与下一步

本轮只创建真实 run artifact 与本报告，没有修改产品代码、LangGraph 节点、BudgetLedger、
CandidateManager、ToolServer、Checkpoint 或 fresh-final 逻辑。执行前工作树干净，
`git diff --check` PASS；最近完整回归保持 **550 unittest PASS**、`compileall` PASS。

最值得做的一项下一步工作是：在保持此安全基线不变的前提下，补一个真正的空函数/TODO
公开 generation fixture 的真实 Planner+Vitis 验收，专门测量合法大 kernel-body Patch，而
不是把当前 `v3d_fast_004` 的小索引修复误称为完整 generation 能力。

本报告提交后应以该提交 hash 作为此次真实四类 Smoke 的代码/documentation 边界。
