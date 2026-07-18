# V2 核心团队报告（2026-07-18）

这个目录只发布两次真实 Vitis 运行的核心 Markdown 复盘，不包含生产/运行环境。

| 报告 | 题目 | 结果 | 关键结论 |
|---|---|---|---|
| [官方 dotProduct](official-dotproduct.md) | 官方公开 `dotProduct_optimize` | Candidate 未晋升 | Synth latency `1027 -> 1027`，CoSim latency `1025 -> 1025`，代理分 `2.2125 -> 2.2125` |
| [本地 vector_add](local-vector-add.md) | 自包含 `u55c_v2_optimize_task` | `candidate_001` 晋升并完成 final closure | Synth latency `513 -> 258`，CoSim latency `511 -> 256`，代理分 `1.475 -> 1.5491` |

两次运行都实际调用了 Vitis 2025.2 的 CSim、Synth 和 CoSim。Proposal Provider 是明确标注的 `deterministic-local-smoke / offline-rule`，不是线上 LLM，因此 Token 为 0；这两份报告证明的是 V2 编排、选择、门控、计费和真实 HLS 验证链路，不证明真实模型的优化能力。

Vitis 精确版本来自未提交的源 run 配置与 backend fingerprint；`dotProduct_optimize` 的官方公开题身份由受版本控制的题库 provenance 哈希在 run 外核验。核心 Markdown 记录结论，但单独一份 Git 克隆不能替代这两条本地来源证据。

## 提交边界

本目录没有提交以下内容：

- `runs/` 原始运行目录；
- `actions/*/work/`、Vitis 工程、XSIM 可执行文件和中间文件；
- 原始 JSON/JSONL、工具日志、锁文件和本机绝对路径；
- API 响应、密钥或生产环境配置。

报告中的 Evidence 链接已指向本地 `llm4hls_harness/runs/...`。克隆 Git 仓库后，如果没有相应本地运行目录，这些链接在 GitHub 上返回 404 是预期行为；报告正文已经保留逐轮 Prompt、Patch、工具结果摘要、决策、Token/Credit 和关键指标，足以完成第一轮团队复盘。

完整的阅读、重建和数据边界说明见[《V2 核心报告阅读与更新指南》](../../../doc/materials/07_experiments/2026-07-18-v2-core-report-guide.md)。
