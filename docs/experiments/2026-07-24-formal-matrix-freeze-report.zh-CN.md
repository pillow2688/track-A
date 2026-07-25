# 当前冻结 HEAD：28 Tasks × 1 正式矩阵冻结报告

## 结果

矩阵配置已冻结，28/28 逐题 Token 预检通过。真实启动预检验证 DeepSeek 与
Vitis 路径可用，并暴露出 Batch 终态验收器与 `task_contract` 的旧语义冲突；
兼容修正已限定在编排层、加入回归测试并哈希冻结。随后正式 28×1 已从干净目录
完成：28/28 Slot 有唯一明确终态，23 题端到端成功，1 题 FAILED，4 题 ERROR。

## 固定协议

| 项目 | 冻结值 |
| --- | --- |
| HEAD | `4a05763b593a527878a0056f64763126c58ee63b` |
| Corpus | `v3d-fast/tasks` 的 28 个公开 Task |
| Model | `deepseek-v4-pro` |
| Repeat | 1 |
| Backend | Vitis 2025.2 |
| Validation profile | `fast-experiment` |
| Fresh final | `task_contract` |
| Continuation | `off` |
| Experience | `off` |
| Ranker runtime | `off` |
| Token policy | `fixed` |
| Run Token limit | 32,768 |
| Planner output cap | 4,096 |
| Planner rounds | 最多 2 |
| Credit limit | 每题 `task.toml` 的 60/100 |
| Primary retry | 禁止 |
| Resume | 开启 |

Graph、Planner、BudgetLedger、Router 和 Vitis 执行文件仍与冻结 HEAD 完全一致。
唯一 Runtime 差异是 Batch 终态 Provenance 验收器：`task_contract` 模式始终要求
fresh CSim/Synth，只在公开 Task 的 `requires_cosim=true` 时要求 fresh CoSim。
这不会改变任何 Planner 输入或 Agent 主路径。

## 为什么与最初计划不同

最初计划写的是 Experience Shadow / Ranker Shadow。但 C1.1 的硬 Gate 要求它们
完全不改变 Planner 输入和主路径。行为测试证明 Experience Shadow 不满足：

- 固定策略的 Planner fingerprint 与 off 不同；
- 动态策略的 TokenEnvelope、Dispatch Context 和 Provider Request 不同。

所以冻结器执行了用户预先指定的 fallback：正式矩阵关闭它们，矩阵结束后做离线
replay。

## 可恢复性与明确终态

Runner 复用已有 BatchBenchmarkRunner 的 Slot 指纹、JSONL 持久化、Artifact
Provenance 校验和异常隔离。完成后另写 `terminal_coverage.json`，验收条件为：

```text
expected_slots = 28
terminal_slots = 28
每个 task_id 恰好出现一次
每条记录具有非空 status 和 run_id
```

失败是合法的实验终态，但缺 Slot 不是。若宿主中断，使用同一 Runner Resume，
直到所有 Slot 有终态。

## 启动前环境与真实预检

Secret 不写入命令、脚本或 Artifact。启动进程只从环境读取 Key。Vitis Root 冻结为：

`/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis`

该目录下已确认存在 `bin/vitis-run`。

环境文件就绪后，DeepSeek 只读连通性检查返回 HTTP 200 且目标模型可见。真实启动
预检中 `task001_repair_syntax` 与 `task002_repair_oob` 均由 Agent 完成，分别使用
1,558 和 1,517 tokens，且 fresh CSim/Synth PASS。旧 Batch 验收器错误地把公开
合约不要求的 CoSim 当作必需项，导致外层记录为 ERROR；相关中止目录完整保留，
不会混入正式矩阵。兼容修正已用 23 条 Batch 测试验证。

## 已发生的预检开销

截至兼容修正冻结：

- 有效 Provider call：2；
- 有效 Token：3,075；
- 两题均完成真实 CSim/Synth；
- 该开销只记为启动预检，不进入正式 28×1 统计。

## 启动前验证

- 修正前全量测试：585/585 PASS；
- Batch 聚焦回归：23/23 PASS；
- `compileall`：PASS；
- `git diff --check`：PASS；
- 新增 Artifact Secret 扫描：0 命中。

## 正式矩阵验收

| 指标 | 实测 |
| --- | ---: |
| 预期 Slot | 28 |
| 唯一明确终态 | 28 |
| DONE | 23 |
| FAILED | 1 |
| ERROR | 4 |
| E2E 成功率 | 82.14% |
| Primary retry | 0 |
| LLM 调用 | 39 |
| Tokens | 85,768 |
| CSim / Synth / CoSim | 83 / 68 / 21 |
| Tool credits | 775 |

五个未成功任务不是缺失记录：

- `v3d_fast_006`：REPAIR 两轮均未通过 Patch Policy，达到
  `MAX_TASK_REPAIR_ROUNDS`；
- `v3d_fast_016`、`v3d_fast_020`：终态未绑定最后一个 Candidate；
- `v3d_fast_021`、`v3d_fast_022`：候选 worst latency 无效。

性能方面，`v3d_fast_025` 至 `028` 分别取得约 2.03×、8.7×、5.06×、2.17×
加速，其中 8.7× 样本验证了 8× acceleration stop。

Router 的 24 个可比较结果中 23 个符合期望；`v3d_fast_018` 本轮 baseline CoSim
实测 PASS，因而实际路由 OPTIMIZE，而不是静态标签中的 STRUCTURAL_FIX。这是输入
事实变化，不应伪装成 Router 逻辑失败。

正式结果审计位于：
`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/formal-matrix-result-audit.json`。
矩阵后的 Experience、Continuation 与 Ranker 更新及完整中文验收另见
`docs/experiments/2026-07-24-formal-matrix-results-and-horizontal-update-acceptance.zh-CN.md`。
