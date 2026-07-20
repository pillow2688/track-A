# V3-D FAST Corpus Oracle 证据（2026-07-20）

## 结论

- 当前 Corpus 是 28 题：REPAIR 8、SYNTH_FIX 6、STRUCTURAL_FIX 6、OPTIMIZE 8。
- 当前 manifest SHA-256 为 `9e374fa49e4c68315080ab93ba2f395deea5ae682dd145b791e1db52c2198691`，对应 commit `807579b`。
- 新 deterministic Oracle `v3d-oracle-deterministic-diverse-final-20260720-r02` 是 28/28 accepted、134/134 checks PASS；它是 fixture，真实 Vitis anchor 计数为 0。
- 真实 Vitis 2025.2 证据有 12 个有效唯一 anchor，每种 mode 3 题，59/59 门链 check 与预期一致。
- 12 题的 task fingerprint 均与当前已提交 Corpus 逐题相同（12/12）。因此虽然 A01/A02 运行时的整体 manifest 不同，这 12 个具体任务仍能通过 task fingerprint 与当前 Corpus 绑定。

Oracle 验证的是“baseline 在指定门失败、golden 在指定门通过”，不是 Agent 或 LLM 自动修复成功率。A01/A02 中没有 LLM 调用。

## 真实 Vitis anchors

| Mode | Tasks | 来源 | 结果 |
|---|---|---|---|
| REPAIR | `001, 002, 003` | A01 | 3/3 accepted |
| SYNTH_FIX | `009, 010` / `011` | A01 / A02 | 3/3 accepted |
| STRUCTURAL_FIX | `015, 016, 017` | A01 | 3/3 accepted |
| OPTIMIZE | `022, 028` / `021` | A01 / A02 | 3/3 accepted |

两次真实串行运行的 execution elapsed 合计为 561.604 s；包含 A01 中保留的两次失败尝试。最终 12 个有效 anchor 的四种 mode 各为 3 题。

OPTIMIZE 真实指标：

| Task | Baseline latency | Golden latency | Acceleration |
|---|---:|---:|---:|
| `v3d_fast_021` | 18 | 6 | 3.0× |
| `v3d_fast_022` | 9 | 6 | 1.5× |
| `v3d_fast_028` | 39 | 6 | 6.5× |

## 保留的失败证据

A01 原始结果是 10 accepted / 2 rejected，没有删除：

1. `v3d_fast_011`：旧 function-pointer mutation 被 Vitis 综合通过，不是有效 SYNTH_FIX。A02 改成真正会触发 `Indirect function call is not supported` 的 `std::function` 后 4/4 checks PASS。
2. `v3d_fast_021`：旧 baseline 仍保留足够并行 pragma，baseline/golden 都是 6 cycles。A02 改成真正串行 baseline 后得到 18→6 cycles，6/6 checks PASS。

失败 run 与 A02 修复 run 都保留在本地 `runs/`；release JSON 保留每个有效 anchor 的 task fingerprint、门链数和 wall time，不将 deterministic、真实 Vitis Oracle 与 LLM Agent 实验混算。

## 可提交 provenance receipts

原始 A01/A02 Vitis 工作目录包含大量生成物、本机绝对路径和日志，因此仍由 `runs/` 忽略。可提交目录
`v3d-real-vitis-anchor-receipts-2026-07-20/` 为 12 个有效 anchor 保存脱敏 receipt：每题绑定当前 task tree fingerprint、真实 Vitis backend fingerprint、原始 `oracle_result.json` SHA-256、全部门链 checks，以及提取时逐个从原始文件复核过的关键 artifact SHA-256。Receipt 明确标记为 `REAL_VITIS_2025_2_NO_LLM`，不能当成 Agent 或 LLM 成功。

clean clone 可执行以下命令 fail-closed 校验 receipt 文件、release 绑定和当前 task tree；它不声称在缺少原始 ignored run 时重新计算 Vitis artifact 内容：

```bash
cd llm4hls_harness
python -m llm4hls_agent.v3d_anchor_receipts verify \
  --corpus task_corpus/v3d-fast \
  --receipts releases/v3d-real-vitis-anchor-receipts-2026-07-20 \
  --release releases/v3d-corpus-oracle-anchors-2026-07-20.json
```
