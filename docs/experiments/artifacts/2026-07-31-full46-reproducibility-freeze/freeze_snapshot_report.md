# RC2 FULL46 最终恢复快照报告

## 结论

本报告建立 RC2 46 题 FULL_REF 的唯一可恢复基线。证据基线提交为
`21df31499fd0b8af1f616f5ee6d30fa067a2e5f1`；该提交已包含 46/46 E2E 成功的结果、统计报告和完整性审计。
本次冻结提交只增加快照、公开扩展任务包、评分器副本和恢复检查，不重写任何历史结果。

## 快照内容

| 类别 | 已冻结内容 | 恢复方式 |
| --- | --- | --- |
| Benchmark | v3d-fast 的既有公开运行输入、18 个 v3d-expanded 公开任务、46 task ID/Mode/Difficulty 清单与 328 个公开文件 SHA-256 | clone 后运行 `check_full46_repro_snapshot.py` |
| Scoring | 官方 `scoring.py` 精确副本，SHA-256 `5f65aaa27bfc3cd488ae2294aa4cf463b6be4399306ffb847c44cdd3861d33fe` | 使用 `snapshot/official_scoring.py` 作版本比对；不据此伪造隐藏评分 |
| Experiment Config | DeepSeek V4 Pro 名称、固定 Token 策略、任务级预算、最多 4 次 Planner、Vitis 2025.2/U55C/5 ns、100 MHz Gate、A1/A2/A3/B1/B2 状态 | `snapshot/experiment_config_snapshot.json` 与 `benchmark_plan.json` |
| Runtime Artifact | FULL Agent Manifest、A2 Gate/Admission、A3 Store/Gate/Admission，全部哈希绑定 | `snapshot/runtime_full_agent_manifest.json` 与 `snapshot/runtime_artifacts/` |
| Reports | 46 题报告、JSONL、汇总、scorecards、失败诊断表、完整性审计及其 SHA-256 | `snapshot/snapshot_manifest.json` 的 `frozen_report_hashes` |

## 证据口径

- FULL_REF 原始运行目录为
  `llm4hls_harness/runs/rc2-full-ref-46-20260731T063617Z`，终态标记为 `FULL46_EXIT_CODE=0`。
- 完整性审计确认 46/46 task ID 唯一、Package/Certification receipt/B2 Ledger-unchanged 证据为 46/46，
  且 `git diff --check` 通过。
- 公开验证 proxy 依据官方公式和公开 B2/latency Artifact 计算；没有隐藏评分 receipt，因此任何任务的
  `official_score` 都不是已执行的官方分数。
- 完整快照不包含密钥、provider token、Vitis work tree、缓存、历史 Candidate、hidden/reference/golden 输入。

## 防漂移约束

后续 A2/A3 消融以此快照为唯一 FULL_REF 基线。其代码、Prompt、任务、Manifest、Admission、Store、预算、
Vitis 版本和运行时 fingerprint 必须一致；每一消融配置仅允许改变 A2/A3 的真实开关，并使用全新 run
目录。若复现检查出现 hash mismatch，必须停止实验，先重新建立新的基线快照，不能把不同指纹的结果合并。

## 验收

执行 `experiment_reproducibility_check.md` 中的只读命令后，本快照应返回 PASS。随后才允许进入
`A2/A3 Ablation`；本报告本身不启动模型、CSim、Synth、CoSim 或 Vitis。
