# 2026-07-26 E2E correctness 收口状态

## 当前结论

本轮没有重构既定 Graph，也没有启动 28 题矩阵。工作集中在旧正式
`28×1` 的 5 个失败 slot，以及 Continuation、Experience、Strategy Ranker
等横向轻量工具的真实状态。

当前可以确认：

1. `v3d_fast_006`、`v3d_fast_016` 已有此前当前实现的定向真实 PASS；
2. `v3d_fast_020` 已在本轮完成真实 Vitis 2025.2
   `STRUCTURAL_FIX` 两轮闭环，最终 `DONE`；
3. `v3d_fast_021`、`v3d_fast_022` 已真实复现
   `WORST_LATENCY_ZERO`，当前 HEAD 均不再崩溃，而是拒绝不可比较候选、
   保留合法 incumbent 并返回 `DONE`；
4. Candidate CoSim 失败现在会生成候选级 bounded evidence，并以 ref/hash
   绑定到 Candidate Registry、Graph State 和下一轮 Planner 输入；
5. 完整快速单元测试为 `648 tests / PASS`；
6. Continuation V2 与 Strategy Ranker V3 的固定协议 Gate 均已 PASS，
   admission 文件均已生成。

以上证明旧 5 个失败对应的已知产品缺陷均有当前实现的定向证据，但**不能**
据此宣称当前工作区已经完成新的正式 `28×1`。当前 HEAD 的统一单次矩阵、
重复统计、多模型矩阵和 hidden 成绩仍未运行。

## 本轮产品修改

修改位置：

- `llm4hls_harness/llm4hls_agent/v3_prototype.py`
- `llm4hls_harness/tests/test_v3_candidate_failure_evidence.py`

修改内容：

- `_candidate_cosim` 失败后调用既有
  `_candidate_failure_evidence_update(...)`；
- 生成 `evidence/failures/candidate_<id>_cosim.json`；
- 把 evidence ref/hash 写入当前 State；
- Candidate 被拒绝时把同一 ref/hash 提交到 Registry；
- 下一轮 Planner 同时看到 baseline failure 和最新 Candidate failure；
- 新增直接覆盖 `_candidate_cosim` 正式调用路径的回归测试。

冻结 HEAD 为 `c76f6e114fb71cf6eb4d7e3f26d8f602a341af97`，上述修改是该
HEAD 上尚未提交的工作区补丁。产品文件当前 SHA-256：

```text
13c2c9dd3b07cba46f67aa63e7e3b0a94d51bce29e490dba7b21021858e90c37
```

## 根因与修复验证

### 修复前

公开任务 `v3d_fast_020` 的真实 DeepSeek + Vitis 运行中：

1. baseline CoSim 超时，进入 `STRUCTURAL_FIX`；
2. DeepSeek 第一轮把 `feedback_stream` 深度从 1 改到 16；
3. Candidate CSim PASS，但 Candidate CoSim 仍真实超时；
4. Candidate Registry 只有 `CANDIDATE_COSIM_FAILED`，没有 failure
   evidence ref/hash；
5. 第二轮 Planner 的 `round.failure_evidence` 仍是 baseline evidence；
6. DeepSeek 不知道 d16 已经失败，原样重复同一 Patch；
7. Proposal 被 `DUPLICATE_PATCH` 拒绝，任务最终 FAILED。

### 修复后

宿主 Vitis 定向运行：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260725_a04/
  runs/v3d_fast_020--scripted--r001--host-vitis/
```

真实结果：

| 阶段 | Candidate | 结果 |
|---|---|---|
| baseline CSim | `candidate_000` | PASS |
| baseline Synth | `candidate_000` | PASS |
| baseline CoSim | `candidate_000` | TIMEOUT |
| 第 1 轮 CSim | `candidate_001`，FIFO depth=16 | PASS |
| 第 1 轮 CoSim | `candidate_001` | TIMEOUT |
| 第 2 轮 CSim | `candidate_002`，删除无消费者回写 | PASS |
| 第 2 轮 CoSim | `candidate_002` | PASS |
| final CSim/Synth/CoSim | `candidate_002` | PASS/PASS/PASS |

终态：

```text
status                    DONE
stop_reason               STRUCTURAL_FIX_FINALIZED
exploration_stop_reason   STRUCTURAL_FIX_VALIDATION_PASS
best_candidate_id         candidate_002
final_candidate_id        candidate_002
credits_used              92
tool_calls                CSim 4 / Synth 2 / CoSim 4
evidence                  REAL_VITIS_VALIDATED
```

第二轮 Gate 原因包含：

```text
NEW_ACTIONABLE_EVIDENCE
NOVEL_STRUCTURAL_STRATEGY
```

`round_002` 明确绑定：

```text
evidence/failures/candidate_001_cosim.json
fd7355780dc69d089765b9b6b22c325043380f874c337a65b25493b8d68b6e6b
```

最终三个 validation stage 均绑定 code hash：

```text
f87a31e9b45ce4731dbc216ba4028f3d916b66738e276e402c390614c775b446
```

## `021/022` invalid latency 终态

两个任务均使用旧失败候选的最终源码变化进行单候选真实 Vitis 复现。

| Task | Candidate latency | Candidate 决策 | 最终 Candidate | 终态 |
|---|---:|---|---|---|
| `v3d_fast_021` | `worst=0` | `LATENCY_NOT_COMPARABLE` | `candidate_000` | DONE |
| `v3d_fast_022` | `worst=0` | `LATENCY_NOT_COMPARABLE` | `candidate_000` | DONE |

两题均满足：

- `latency_status=INVALID`；
- reason code 为 `WORST_LATENCY_ZERO`；
- `acceleration_vs_baseline=null`，没有制造虚假加速；
- Candidate Registry 将不可比较候选标为 `REJECTED`；
- final CSim/Synth 重新执行并 PASS；
- `task_contract` 不要求 CoSim，因此 final CoSim 为 `NOT_RUN`，不是漏跑。

证据目录：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260726_a06/
llm4hls_harness/experiments/e2e_correctness_recovery_20260726_a07/
```

## 横向轻量工具状态

### Continuation V2

固定 Gate：PASS。Admission 已生成：

```text
docs/experiments/artifacts/2026-07-25-horizontal-online-admission/
  continuation-v2-admission.json
```

关键指标：

- 四类 Mode 各 1 个有效样本；
- false blocks：0；
- leakage violations：0；
- STRUCTURAL essential retention：1.0；
- beneficial retention：1.0。

本轮 `020` 宿主运行使用 `enforce/v2`。第一轮失败后，第二轮因
`NEW_ACTIONABLE_EVIDENCE` 和 `NOVEL_STRUCTURAL_STRATEGY` 被正式放行。

### Experience V2 / Strategy Ranker V3

Ranker 固定 Gate：PASS。Admission 已生成：

```text
docs/experiments/artifacts/2026-07-25-horizontal-online-admission/
  ranker-v3-admission.json
```

冻结池：

- 150 条 Experience V2；
- 99 条 verified；
- OPTIMIZE 25、REPAIR 35、STRUCTURAL_FIX 11、SYNTH_FIX 28；
- seed SHA-256：
  `dcbdc7dbdf4f424c528715864588d9357ea54ceb840616e3565dc16b2846dabf`。

LOTO admission 指标：

- coverage：44.44%；
- harmful rate：0；
- global positive hit：37.63%；
- leakage：0。

必须保留的限制：

- STRUCTURAL_FIX 当前 per-mode coverage 仍为 0；
- 修复前的 `020` 在线 guided 运行中，Ranker 已获得 prompt injection
  权限，但因 `COSIM_TIMEOUT_UNKNOWN` 没有匹配 verified support 而
  `ABSTAIN`；
- 因此不能声称 Ranker 已帮助解决 `020`，只能确认 Ranker 已接入、
  已通过全局固定 Gate，并在证据不足时安全 abstain；
- scripted Planner + Vitis 首先完成了两轮产品链路验证；随后用户在宿主
  终端完成了独立的 DeepSeek 在线重验；
- 在线重验中 Ranker 仍为 `ABSTAIN`，因此成功归因于 DeepSeek 对 baseline
  failure evidence 的诊断和现有 Graph/Vitis Gate，而不是 Ranker 推荐。

## DeepSeek 在线重验

用户已明确允许把公开任务代码和诊断发送给 DeepSeek。尝试在修复后启动
新的在线 `020` 重验时，Codex 宿主租户外发策略曾在进程创建前拒绝执行。
用户随后在自己的终端使用同一工作区和冻结配置启动运行，最终成功：

```text
llm4hls_harness/experiments/e2e_correctness_recovery_20260726_deepseek_a01/
  runs/v3d_fast_020--deepseek-v4-pro--r001--candidate-cosim-evidence/
```

结果：

- `status=DONE`；
- `stop_reason=STRUCTURAL_FIX_FINALIZED`；
- DeepSeek calls：1；
- Tokens：2,948；
- Credits：71；
- Candidate：`candidate_001`；
- 探索 CSim/CoSim：PASS/PASS；
- fresh final CSim/Synth/CoSim：PASS/PASS/PASS；
- 证据等级：`REAL_VITIS_VALIDATED`。

DeepSeek 没有采用旧运行的“FIFO depth=16”策略，而是删除循环 DATAFLOW
反馈结构，将顶层改为保持公开语义的 `PIPELINE II=1` 单循环。最终源码
SHA-256：

```text
8ec305b66d51e1fca52386bc7c2773d625bed6b6bfd572522f2cd243357926ab
```

运行结果文件 SHA-256：

```text
c51c6f4a552e084c0c7084a17733bdf6eb23930a837904a1fac6f99138144897
```

## 尚未完成

1. 当前工作区补丁尚未提交；
2. 当前 HEAD 完整 `28 tasks × 1` 尚未重跑；
3. 当前 HEAD 多次重复统计尚未完成；
4. 多模型正式矩阵尚未完成；
5. hidden 最终成绩未知；
6. Ranker 的 STRUCTURAL_FIX per-mode coverage 仍需独立公开任务补强。

## 验收入口

- [E2E correctness 收口验收报告](../experiments/2026-07-26-e2e-correctness-recovery-acceptance.zh-CN.md)
- `llm4hls_harness/experiments/e2e_correctness_recovery_20260725_a04/`
- `llm4hls_harness/experiments/e2e_correctness_recovery_20260726_a06/`
- `llm4hls_harness/experiments/e2e_correctness_recovery_20260726_a07/`
- `llm4hls_harness/experiments/e2e_correctness_recovery_20260726_deepseek_a01/`
- `docs/experiments/artifacts/2026-07-25-horizontal-online-admission/`
