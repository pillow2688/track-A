# Track A `vitis-run` 与 task-contract final Smoke 报告

日期：2026-07-22

分支：`feat/track-a-vitis-run-task-final-smoke`

起点：`032ca0f`（post-rollback 安全基线）

实现提交：`3f98a8c`

## 结论

本阶段只完成两项 reference-harness 对齐：Vitis 2025.2 的 `vitis-run` 发现/
调用，以及 fresh final 的 task-contract 策略。没有恢复 V3-G，没有增加 LangGraph
节点、Agent、学习模型或 enforce/guided 路由。

宿主机已经确认存在 Vitis 2025.2 的 `vitis-run`，所以先前把旧版
`vitis_hls` 缺失视为 Vitis 不可用的判断是错误的。四个真实 Smoke 仍未运行：唯一
核心阻塞是本会话没有 OpenAI-compatible Provider 配置（`OPENAI_BASE_URL`、
`OPENAI_API_KEY`、`LLM4HLS_MODEL` 都为 `UNSET`）。没有用 demo 或 scripted
backend 伪造真实 Planner/Vitis 结果。

## 1. Vitis 探测根因与修复

旧检查只把 `settings64.sh` / `vitis_hls` 当作 Vitis 可用条件；这不符合 2025.2
reference harness 的调用方式。现在统一选择顺序为：

1. `<root>/bin/vitis-run`
2. `PATH` 中的 `vitis-run`
3. `<root>/bin/vitis_hls`（旧版本兼容）
4. `PATH` 中的 `vitis_hls`（旧版本兼容）

只有四项都不存在时才给出 `TOOLCHAIN_UNAVAILABLE`。选择 `vitis-run` 时实际命令为：

```text
vitis-run --mode hls --tcl run_hls.tcl
```

旧入口仍使用 `vitis_hls -f run_hls.tcl`。每次实际工具调用都会生成内部
`vitis_toolchain.json` receipt，保存选择来源、可执行文件哈希、调用模式、版本、root
和 preflight 结论；receipt 是 run artifact，不在本报告中披露本机绝对路径。

实际 preflight：

| 项目 | 结果 |
|---|---|
| root 下 `vitis-run` | 可执行，已选中 |
| root 下 `vitis_hls` | 不存在；不再是阻塞条件 |
| `vitis-run --version` | `v2025.2`，SW Build `6295257` |
| PATH 中 `vitis-run` | 未配置 |
| Docker daemon | 当前用户无 socket 权限；本机 Vitis 直跑不依赖 Docker |

## 2. Fresh final：task-contract

新增 `--final-validation-policy`：

| policy | `requires_cosim=false` | `requires_cosim=true` | 用途 |
|---|---|---|---|
| `task_contract` | fresh CSim + Synth（5 credits） | fresh CSim + Synth + CoSim（25 credits） | 比赛安全默认 |
| `full_internal_audit` | fresh CSim + Synth + CoSim（25 credits） | fresh CSim + Synth + CoSim（25 credits） | 额外内部审计 |

该策略复用既有 final 条件边：非 CoSim contract 在既有 `final_cosim` action 内记录
`FINAL_COSIM_NOT_REQUIRED`，不会执行或计费 CoSim，也没有新增 LangGraph 节点。
`STRUCTURAL_FIX` 和 `requires_cosim=true` 仍不能跳过 CoSim；探索阶段的高风险 CoSim
规则也未改变。

Credit 展示仍来自账本和 result receipts：

- `agent_search_cost`：探索中的 Planner/工具调用；
- `internal_final_validation_cost`：本 Agent 实际 fresh final 调用，按 task contract
  为 5 或 25；
- `external_grader_cost=0`，`NOT_RUN_BY_AGENT`：外部 grader 从未被本 Agent 执行。

## 3. 冻结配置

[track_a_safe_baseline_v1.json](../../llm4hls_harness/llm4hls_agent/config/track_a_safe_baseline_v1.json)

- SHA-256：`ca1ca142f3488062184069e0fc434f3c680687dfed561b10d5bd20aaedd27e48`
- `final_validation.policy=task_contract`
- continuation=`shadow`、experience=`shadow`、ranker=`bayesian_shadow`
- comparator=`latency_first`、fresh final required
- hidden/reference/golden access=false
- Power=`UNSUPPORTED`
- acceleration cap=8

## 4. 四类真实 Smoke

| task | task type / mode | Planner | Vitis | result |
|---|---|---|---|---|
| `projection_bugfix` | repair / REPAIR | 未启动 | 可用 | `REAL_SMOKE_BLOCKED` |
| `dotProduct_optimize` | optimize / OPTIMIZE | 未启动 | 可用 | `REAL_SMOKE_BLOCKED` |
| `residual_stream_deadlock` | structural / STRUCTURAL_FIX | 未启动 | 可用 | `REAL_SMOKE_BLOCKED` |
| public generation/stub 开发任务 | generate / REPAIR | 未启动 | 可用 | `REAL_SMOKE_BLOCKED` |

阻塞项是 Provider 环境配置缺失，而不是 Vitis：

```bash
export OPENAI_BASE_URL='https://<provider>/v1'
export OPENAI_API_KEY='<secret>'
export LLM4HLS_MODEL='<model>'
export LLM4HLS_VITIS_HLS_ROOT='<Vitis-2025.2-root>'
```

环境恢复后，每题必须使用新的 `--run-dir`，并固定：

```text
--backend vitis --planner openai-compatible
--final-validation-policy task_contract
--continuation-policy shadow --experience-mode shadow
--cost-csim 1 --cost-synth 4 --cost-cosim 20
```

## 5. 验证

| 检查 | 结果 |
|---|---|
| Vitis selection / command tests | PASS |
| task-contract final tests | PASS |
| 完整 `unittest discover` | **549 PASS** |
| `compileall` | PASS |
| `git diff --check` | PASS |

测试覆盖：仅 `vitis-run`、仅 `vitis_hls`、二者同时存在时优先 `vitis-run`、全部缺失、
2025.2/legacy 命令构造、task-contract 的 5-credit final、required CoSim 的 25-credit
final，以及安全配置的 policy 声明。

## 6. 当前边界与唯一下一优先项

**唯一下一优先项：配置真实 OpenAI-compatible Provider 后，对四个 task 使用四个新
run-dir 执行真实 Smoke。**

在这四份 sealed receipt 出现前，不报告 Token、PPA 或比赛分数改进，也不启动 28 题、
ranker 训练、Continuation enforce 或 Experience guided。
