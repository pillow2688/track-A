# Track A 四类真实 Smoke 执行报告

日期：2026-07-22

分支：`feat/track-a-vitis-run-task-final-smoke`

执行起点：`cfeb72e`

Vitis receipt 修复提交：`cb0033e`

## 结论

本次没有把 scripted/demo backend 当成真实 Smoke。Vitis 2025.2 的真实 preflight 已经
通过；但当前 Codex 执行环境及其交互 shell 都没有继承 OpenAI-compatible Provider
配置。因此最小真实 Provider 请求无法安全发起，四类真实 Planner + Vitis Smoke 按规则
全部标记为 `REAL_SMOKE_BLOCKED`。这属于全局 `PROVIDER_FAILURE`，不是 Agent、Patch、
Vitis 或 task-contract 故障。

所以本报告不声称任何 public/hidden correctness、Token 效率、PPA 或比赛分数提升；也不
建议启动 28 题单次覆盖。

## 1. 仓库与安全基线

开始时分支正确、工作树干净，HEAD 为 `cfeb72e`。执行期间仅修复了 Vitis receipt 对
`vitis-run v2025.2` 的版本提取；没有新增 Agent、LangGraph 节点、学习模型或
Continuation/Experience 路由。

安全基线保持：

- continuation=`shadow`；experience=`shadow`；ranker=`bayesian_shadow`；
  comparator=`latency_first`；
- final policy=`task_contract`；
- hidden/reference/golden access=false；Power=`UNSUPPORTED`；
- CSim/Synth/CoSim cost=`1/4/20`。

[track_a_safe_baseline_v1.json](../../llm4hls_harness/llm4hls_agent/config/track_a_safe_baseline_v1.json)
SHA-256：`ca1ca142f3488062184069e0fc434f3c680687dfed561b10d5bd20aaedd27e48`。

## 2. Provider 预检

只检查变量是否存在，未输出或持久化任何值：

```text
OPENAI_BASE_URL=UNSET
OPENAI_API_KEY=UNSET
LLM4HLS_MODEL=UNSET
```

普通 login shell 与交互 shell 的结论一致。本线程没有附着的 app terminal session，
无法从用户的独立终端继承 export。因此没有构造网络请求、没有 Authorization header、
没有 Provider usage、也没有生成 Planner input/output artifact。

| 项目 | 结果 |
|---|---|
| 最小真实 OpenAI-compatible JSON 请求 | 未发送 |
| Provider/model | `UNKNOWN` |
| usage | `UNKNOWN` |
| failure class | `PROVIDER_FAILURE_ENVIRONMENT` |
| 可复现性 | 是：当前 shell 的三项变量均未设置 |

## 3. Vitis 2025.2 真实预检

| 项目 | 结果 |
|---|---|
| selected executable | `vitis-run`（root 优先） |
| invocation mode | `vitis-run --mode hls --tcl run_hls.tcl` |
| version | `2025.2`，SW Build `6295257` |
| preflight result | `READY` |
| 真实无 LLM synthesis | PASS |
| baseline latency | 1027 cycles |
| preflight elapsed | 17.182 s |

无 LLM preflight 对公开 `dotProduct_optimize` baseline 执行真实 `csynth_design`，产生
`tcl`、stdout/stderr、`csynth.xml` 和 `vitis_toolchain.json`。本地未提交 artifact：
`/tmp/track_a_vitis_2025_2_preflight_A02/`（约 17 MiB）。receipt 绑定的可执行文件
SHA-256 为 `4d1bf95564e127673e3fde65bdaa7f93dc35220d15af85c794d148517fd91fb3`。

Docker daemon 对当前用户不可访问，但本机 Vitis 直跑不依赖 Docker，不构成此次
Provider 阻塞的原因。

## 4. 四类 Smoke 计划与实际结果

公开 generation/stub 采用开发 corpus 的 `v3d_fast_004`；它是公开开发任务，不声称
属于官方 Track A 分布。

| Task | Type | Mode | requires_cosim | Planner Calls | Token | Search Credits | Final Stages | Final Credits | Result |
|---|---|---|---:|---:|---|---:|---|---:|---|
| `task_corpus/official/fpt26-harness-public/projection_bugfix` | repair | REPAIR | false | 0 | N/A | 0 | 未运行；contract 应为 CSim+Synth | 0 | `REAL_SMOKE_BLOCKED` |
| `task_corpus/official/fpt26-harness-public/dotProduct_optimize` | optimize | OPTIMIZE | false | 0 | N/A | 0 | 未运行；contract 应为 CSim+Synth | 0 | `REAL_SMOKE_BLOCKED` |
| `task_corpus/official/fpt26-harness-public/residual_stream_deadlock` | structural | STRUCTURAL_FIX | true | 0 | N/A | 0 | 未运行；contract 应为 CSim+Synth+CoSim | 0 | `REAL_SMOKE_BLOCKED` |
| `task_corpus/v3d-fast/tasks/v3d_fast_004` | generate | REPAIR | false | 0 | N/A | 0 | 未运行；contract 应为 CSim+Synth | 0 | `REAL_SMOKE_BLOCKED` |

generation task 的 `generation_required=true` 已由 Task Loader 验证；Planner 未启动，
所以没有 kernel patch、Candidate、final、Token、Search credit 或 Agent ledger 可审计。
四个 fresh run-dir 也没有创建，避免留下“未真实执行”的伪 run。

## 5. Contract、隔离与分账

实现与 deterministic 回归已验证：

- `requires_cosim=false` 的 `task_contract` fresh final 是 CSim+Synth，内部 final
  cost=5；
- `requires_cosim=true` 的 fresh final 是 CSim+Synth+CoSim，内部 final cost=25；
- `full_internal_audit` 才无条件要求 final CoSim；
- Agent Search、Internal final 和 External grader 分账保持独立；External grader 永远
  是 `0 / NOT_RUN_BY_AGENT`；
- shadow continuation 只落 decision artifact，不能改 route；shadow Experience 不进入
  Planner prompt；
- kernel-only Patch Validator、top/interface guard、hidden/reference/golden 隔离没有
  改动。

此次没有 Agent run，故不存在可审计的 Candidate parent-child、fresh final source hash 或
Planner token usage。Vitis preflight 不属于 Agent Search 或 Internal final，未把它记入
任一任务的 competition credit。

## 6. 回归与准入

| 检查 | 结果 |
|---|---|
| Vitis entry-point/receipt tests | PASS |
| task-contract final tests | PASS |
| complete unittest discover | **550 PASS** |
| compileall | PASS |
| git diff --check | PASS |
| LangGraph nodes/edges | 未新增 |

`V3` 的默认 CLI final policy 仍为 `task_contract`；安全配置中的 shadow/default、
Power status 与 8x stop 没有变化。

## 7. 最小 Smoke 是否通过、是否进入 28 题

**未通过最小 Smoke，不能进入 28 题单次覆盖。**

唯一需要修复的前置条件是：让运行本任务的 Codex shell 继承真实 Provider 配置。
例如在启动 Codex 前或其同一进程环境中设置：

```bash
export OPENAI_BASE_URL='https://<provider>/v1'
export OPENAI_API_KEY='<secret>'
export LLM4HLS_MODEL='<model>'
```

恢复后，先按本表四个任务以全新独立 run-dir 运行，不复用本次任何 Planner/Candidate/final
artifact；只有四类 fresh final 都满足 task contract、分账正确且无隔离泄漏，才评估
28 题单次覆盖。
