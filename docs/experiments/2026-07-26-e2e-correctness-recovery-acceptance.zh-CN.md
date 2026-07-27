# 2026-07-26 E2E correctness 收口验收报告

## 验收结论

本阶段验收结果为：

```text
产品缺陷修复验收          PASS
旧 5 个失败定向收口       PASS
横向组件固定 Gate         PASS
完整当前 HEAD 28×1        NOT RUN
修复后 DeepSeek 在线重验  PASS
```

因此可以验收“已知 E2E correctness 缺陷已定向修复”，不能验收“比赛正式
矩阵已经全部完成”。

## 验收矩阵

| 验收项 | 结果 | 说明 |
|---|---:|---|
| Candidate CoSim 失败落盘 | PASS | 生成 candidate 级 bounded evidence |
| Candidate Registry ref/hash | PASS | 非空且与文件 SHA-256 一致 |
| 下一轮 Planner 使用最新失败 | PASS | `round_002` 指向 `candidate_001` |
| baseline failure 保留 | PASS | history 同时保留 baseline 与 Candidate |
| Continuation V2 Enforce | PASS | 第二轮由新证据和新策略放行 |
| `020` 第 1 候选超时 | PASS | 宿主 XSim 真实 `0/1` 事务停滞 |
| `020` 第 2 候选探索 CoSim | PASS | PASS |
| `020` fresh final | PASS | CSim/Synth/CoSim 全 PASS |
| `021` invalid latency | PASS | `WORST_LATENCY_ZERO`，安全拒绝 |
| `022` invalid latency | PASS | `WORST_LATENCY_ZERO`，安全拒绝 |
| DeepSeek 在线 STRUCTURAL_FIX | PASS | 1 call，探索与 final 均 PASS |
| 无效 latency 虚假 acceleration | PASS | 两题均为 null |
| 无效候选覆盖 incumbent | PASS | 两题均保留 baseline |
| 终态候选绑定 | PASS | final validation 与 source hash 一致 |
| 完整快速单元测试 | PASS | 648 tests |
| `git diff --check` | PASS | 无格式错误 |
| hidden/reference/golden 隔离 | PASS | 未访问 |
| 启动 28 题矩阵 | PASS（未启动） | 本阶段只跑 3 个公开失败任务 |

## 真实运行结果

### `v3d_fast_020`

证据：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260725_a04/
  runs/v3d_fast_020--scripted--r001--host-vitis/
```

结果文件 SHA-256：

```text
a021cead2a66aa88fce43bb60a72fd198542f26e5d235f93e1bb3079437db018
```

终态：

- `status=DONE`；
- `stop_reason=STRUCTURAL_FIX_FINALIZED`；
- `final_candidate_id=candidate_002`；
- `REAL_VITIS_VALIDATED`；
- final CSim/Synth/CoSim 全 PASS；
- 92/100 credits；
- 0 LLM Token。

### `v3d_fast_021`

证据：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260726_a06/
  runs/v3d_fast_021--scripted--r001--invalid-latency-combined/
```

结果文件 SHA-256：

```text
9c2c4596c0a5a2e4a0cc9b27dcdf9d2e6bc77804a28b9a439a4a8aa453c62d29
```

终态：

- `status=DONE`；
- Candidate latency `worst=0`；
- `latency_status=INVALID`；
- `decision_reason=LATENCY_NOT_COMPARABLE`；
- `final_candidate_id=candidate_000`；
- final CSim/Synth PASS。

### `v3d_fast_022`

证据：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260726_a07/
  runs/v3d_fast_022--scripted--r001--invalid-latency-combined/
```

结果文件 SHA-256：

```text
48f07e2d50f1f3180807ea3a692bdaf7f3f2710316b7bf55d06417a64baf8e97
```

终态与 `021` 相同：不可比较候选被拒绝，baseline 重新 final
CSim/Synth 后提交，任务 `DONE`。

## 横向组件验收

| 组件 | 固定 Gate | Admission | 本轮在线/真实行为 |
|---|---:|---:|---|
| Continuation V2 | PASS | 已生成 | `020` Enforce 第二轮真实放行 |
| Experience V2 | 冻结池已生成 | 由 Ranker admission 约束 | 150 records |
| Strategy Ranker V3 | PASS | 已生成 | 在线 `020` 因无匹配支撑安全 ABSTAIN |

Continuation admission evidence SHA-256：

```text
46a0c7b5571e7a0f741c562228943a0fd1288efa050b8d6d62f8a9ad243cb89d
```

Ranker admission evidence SHA-256：

```text
03577892f4fe86e29de85214c8842d3df00a5a8ba561fc1eeaa636fe7089caf3
```

Ranker 已通过全局固定 Gate，但 STRUCTURAL_FIX per-mode coverage 为 0。
这不是未接入，而是该 Mode 仍会在缺少匹配 verified support 时 abstain。

## 无效运行的处理

一次非提升权限的本地 Vitis run 出现 XSim Tcl 启动异常。该 run 被中止并
保留，不计入 correctness 验收：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260725_a03/
```

随后用宿主权限、相同公开任务和本地 Vitis 重跑，XSim 正常输出 RTL
transaction progress，最终以 `a04` 作为有效证据。

一次 `021` 两 patch scripted 重放中，第二 patch 被
`DUPLICATE_STRATEGY_BUNDLE` 拒绝。该 run 只证明终态可达，不作为
invalid-latency 主证据：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260726_a05/
```

主证据使用 `a06` 合并候选，真实产生 `WORST_LATENCY_ZERO`。

## DeepSeek 在线验收

Codex 宿主曾拒绝由 Agent 直接启动外部 DeepSeek 请求。用户随后在自己的
终端运行同一命令，在线重验已完成：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260726_deepseek_a01/
  runs/v3d_fast_020--deepseek-v4-pro--r001--candidate-cosim-evidence/
```

机器终态：

```text
status                    DONE
stop_reason               STRUCTURAL_FIX_FINALIZED
rounds_completed          1
final_candidate_id        candidate_001
credits_used              71
tokens_used               2948
tool_calls                CSim 3 / Synth 2 / CoSim 3 / LLM 1
evidence                  REAL_VITIS_VALIDATED
```

DeepSeek Patch 删除导致环形依赖的 DATAFLOW/FIFO 结构，改为
`PIPELINE II=1` 的直接单循环。Candidate 探索 CSim/CoSim PASS，随后
fresh final CSim/Synth/CoSim 均 PASS，三项均绑定 code hash：

```text
8ec305b66d51e1fca52386bc7c2773d625bed6b6bfd572522f2cd243357926ab
```

Experience V2 为 `guided/ACTIVE`，admission 允许 prompt injection；
Strategy Ranker V3 对该查询返回
`ABSTAIN/NO_MATCHING_VERIFIED_SUPPORT`。因此本题证明：

- Guided/Ranker 已真实进入在线协调路径；
- Ranker 在证据不足时没有强制推荐；
- 本次正确 Patch 来自 DeepSeek 对 baseline failure evidence 的独立判断；
- 该单题结果可以计入当前模型的定向 Anchor 证据，但不能替代完整多模型
  正式矩阵。

## 回归命令

```bash
PYTHONPATH=llm4hls_harness .venv/bin/python -m unittest discover \
  -s llm4hls_harness/tests -t llm4hls_harness -p 'test_*.py' -q
```

结果：

```text
Ran 648 tests in 113.252s
OK
```

## 最终判定

本阶段达到以下可提交级子目标：

- Candidate CSim/Synth/CoSim 失败证据链完整；
- `STRUCTURAL_FIX` 可跨失败候选继续并最终闭环；
- 无效 latency 不再破坏 batch slot 终态；
- 横向 Gate 与 admission 状态有明确机器证据；
- 旧矩阵已知 5 个失败均有当前实现的定向收口证据。

正式完赛前仍必须：

1. 提交并冻结当前工作区补丁；
2. 运行当前冻结版本 `28 tasks × 1`；
3. 做重复统计与多模型矩阵；
4. 最后才更新正式成绩和提交材料。

## 相关文件

- [当前状态](../status/2026-07-26-e2e-correctness-recovery.md)
- [历史 Shadow 状态](../status/2026-07-25-horizontal-components-online-shadow.md)
- `artifacts/2026-07-25-horizontal-online-admission/continuation-v2-admission.json`
- `artifacts/2026-07-25-horizontal-online-admission/ranker-v3-admission.json`
