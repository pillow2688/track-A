# Post-rollback Track A 安全基线报告

日期：2026-07-22
工作分支：`feat/track-a-post-rollback-baseline`
审计起点：`849b8ba`（审计文档提交；其父为已回退后的 V3-F `d9c5d76`）
最小对齐实现：`2b149bf`

## 结论

本阶段没有恢复 V3-G，也没有新增 LangGraph 节点、Agent、Controller 或学习路由。
在 V3-F 的既有主路径上只完成了 Track A 最小差异对齐，并冻结了一份默认安全
配置。代码回归通过；真实 Planner + Vitis 的四类 fresh Smoke **没有运行**，原因是
当前会话没有模型环境变量，且检查到的两个 Vitis HLS root 都没有 `vitis_hls` 可执行文件。

这意味着本报告不声称新的真实正确率、PPA、Token 效率或比赛分数。

## 1. 回退是否完整

是。审计过程与回退前后源码差异见
[post_rollback_audit.md](post_rollback_audit.md)。当前分支从 V3-F `d9c5d76` 出发，
V3-G 的 generation/numeric/generalization/Pareto/follow-up/replay 大型实现不在当前
checkout 或当前主流程中。本阶段只增加下表的最小补丁。

## 2. 保留与删除的能力

| 类别 | 当前状态 |
|---|---|
| LangGraph 主骨架 | 保留：baseline → PhaseRouter → Planner → Patch → Candidate 验证 → final closure |
| 四种运行 mode | 保留：`REPAIR`、`SYNTH_FIX`、`STRUCTURAL_FIX`、`OPTIMIZE` |
| Candidate / Tool / Budget / checkpoint | 保留，未重构 |
| fresh final | 保留并始终要求 CSim、Synth、CoSim |
| Continuation / Experience / Ranker | 保留现有实现；安全默认是 shadow/advisory，不改变正式路由 |
| 已回退 V3-G 大型能力 | 不恢复：新 generation 子系统、numeric/generalization guard、Pareto/follow-up/replay 数据集、enforce/guided 路由均不在此版本 |

主图未增加节点：当前源码有 **26 个 action node** 和 **27 个 edge/conditional-edge
声明**。PhaseRouter 仍然在 baseline CSim/Synth/(required CoSim) 之后做纯 Python
分流。

## 3. 本阶段的最小对齐补丁

| Track A 项目 | 当前实现 | 边界 |
|---|---|---|
| task type | `task.py` 显式接受 `generate`、`repair`、`optimize`、`synth_fix`，并保留 reference harness 的 `structural` 兼容拼写 | 未知类型 fail-closed |
| generate / stub 映射 | `generate` 默认 `generation_required=true`；仍走现有 `REPAIR` mode | 没有新 GENERATE 图节点 |
| 大 kernel-body patch | 仅 `generation_required=true` 时将现有 PatchLimits 放宽到 1600 行/48 hunks，并允许 whole-file replacement | header、testbench、task metadata、top/interface 仍受 kernel-only 与 TopInterfaceGuard 保护 |
| V3 工具成本 | 新增 `--cost-csim`、`--cost-synth`、`--cost-cosim`，也读取 `LLM4HLS_COST_*`；默认仍为 `1/4/20` | BudgetLedger 仍是唯一硬约束 |
| Credit 分账 | V3 terminal result / team report 从 ledger + tool result 的 `validation_scope` 推导 `agent_search_cost`、`internal_final_validation_cost`、`external_grader_cost` | external grader 从未被 Agent 执行，因此固定为 `0 / NOT_RUN_BY_AGENT` |
| `requires_cosim` | `true` 时 CoSim 是 baseline correctness gate；false 的 fast exploration 不会无条件花 Search CoSim；fresh final 总跑 CoSim | 沿用既有 V3 路由，未改图 |
| score proxy | 仅本地报告。公开/hidden correctness 未通过为 0，acceleration=`baseline/candidate`，cap=8 | task 未声明 difficulty 时，V3 记录 `difficulty_status=UNKNOWN` 且不生成 official-score proxy |
| 8× stop | 仅 OPTIMIZE：当前 incumbent 已 CSim/Synth/(required CoSim) 正确且 clock/resource 合格、加速≥8×时，停止后续纯 latency follow-up | 不阻断 repair；仍进入 fresh final |
| Token/runtime | CLI 和 BudgetLedger 原有 input/output/total、runtime、Token stop 语义保持 | 不静默重试 |
| Power | 安全配置明示 `UNSUPPORTED` | 未构造虚假 Power proxy |

## 4. 当前安全默认配置

配置文件：[track_a_safe_baseline_v1.json](../../llm4hls_harness/llm4hls_agent/config/track_a_safe_baseline_v1.json)

- SHA-256：`9b2c93a8c194e35ad0e811ca348426be5aa49baa350197ffb019612427059a5f`
- `continuation_policy=shadow`
- `experience_mode=shadow`
- `strategy_ranker=bayesian_shadow`
- `comparator=latency_first`
- `fresh_final=required`
- `hidden_access=false`、`reference_access=false`、`golden_access=false`
- official score proxy 只用于本地报告，Power=`UNSUPPORTED`

该 JSON 是冻结的安全基线声明；它没有把 shadow/enforce 行为绕过现有 CLI 或
BudgetLedger。V3 CLI 的既有默认也保持 `continuation-policy=shadow`、
`experience-mode=shadow`、`validation-profile=strict`。

## 5. 四类真实 Smoke

| task | task type / 预期 mode | fresh run | 结果 |
|---|---|---|---|
| `projection_bugfix` | repair / `REPAIR` | 未创建 | `REAL_SMOKE_BLOCKED` |
| `dotProduct_optimize` | optimize / `OPTIMIZE` | 未创建 | `REAL_SMOKE_BLOCKED` |
| `residual_stream_deadlock` | structural / `STRUCTURAL_FIX` | 未创建 | `REAL_SMOKE_BLOCKED` |
| public generation/stub 开发任务 | generate / `REPAIR` + `generation_required` | 未创建 | `REAL_SMOKE_BLOCKED` |

预检事实（不输出任何 secret）：

- `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`LLM4HLS_MODEL`：均为 `UNSET`；
- `LLM4HLS_VITIS_HLS_ROOT`：`UNSET`；
- `/opt/xilinx/2025.2/Vitis/bin/vitis_hls`：不存在；
- `/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis/bin/vitis_hls`：不存在；
- Docker CLI 可用，但本阶段没有确认带同一 Vitis 版本和许可的可运行镜像，因此不把
  Docker 存在误报成 Vitis 证据。

因此没有使用 demo backend 代替真实 smoke，也没有创建或伪造 Candidate、Token、Credit
或 latency artifact。

在真实环境中，先设置模型与实际 `vitis_hls` root，再对每题使用新的 run 目录，例如：

```bash
export OPENAI_BASE_URL='https://<provider>/v1'
export OPENAI_API_KEY='<secret>'
export LLM4HLS_MODEL='<model>'
export LLM4HLS_VITIS_HLS_ROOT='/absolute/path/to/Vitis'

PYTHONPATH=llm4hls_harness .venv/bin/python -m llm4hls_agent.v3_prototype_cli \
  --task-dir llm4hls_harness/task_corpus/official/fpt26-harness-public/projection_bugfix \
  --planner openai-compatible --backend vitis \
  --run-dir llm4hls_harness/runs/post_rollback_projection_A01 \
  --credit-limit 40 --cost-csim 1 --cost-synth 4 --cost-cosim 20 \
  --validation-profile fast-experiment --continuation-policy shadow \
  --experience-mode shadow --max-planner-rounds 3
```

每个 task 必须换一个全新的 `--run-dir`；真实结果要从该目录的 sealed result、ledger 和
fresh final artifacts 读取。

## 6. 回归测试

所有结果来自当前工作树：

| 检查 | 结果 |
|---|---|
| `compileall -q llm4hls_harness/llm4hls_agent` | PASS |
| `git diff --check` | PASS |
| 回退安全基线聚焦集（含 generate 路由） | 95 PASS |
| 完整 unittest discover | **542 PASS** |

完整 unittest 已在当前补丁后以 `unittest discover` 重跑，为 **542 PASS**。此前为便于
定位曾按稳定批次执行：通用+V2（238）、experience（134）、V3 核心（112）、V3D/
validation/Vitis/workflow（57）；本次新增 generate 路由测试后，以完整 discover 结果为准。

新增测试覆盖：

- generation 默认能力、未知 task type 拒绝；
- generation 大 body patch 的标准拒绝与受控放行；
- 安全配置与工具成本 CLI 覆盖；
- 8× cap 仅停止后续 latency round、仍完成 fresh final；
- terminal ledger 分账与外部 grader=0 的一致性。

## 7. 当前边界与唯一下一优先项

**唯一下一优先项：恢复真实 OpenAI-compatible Provider 与 Vitis 2025.2 可执行环境后，
用四个全新目录跑一次受控真实 Smoke。**

在得到这些 fresh sealed artifact 前，不应启动大规模 benchmark、训练 ranker、开启
Continuation enforce 或 Experience guided，更不能把当前代码测试通过表述成比赛分数。
