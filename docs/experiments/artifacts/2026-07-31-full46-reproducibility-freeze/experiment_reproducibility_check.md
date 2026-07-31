# RC2 46题实验可复现性检查

## 冻结对象与范围

本检查对应的已完成证据提交为 `21df31499fd0b8af1f616f5ee6d30fa067a2e5f1`，其父提交链固定
RC2 Agent 实现（核心实现提交为 `6f1e31a86159dd26fa8f7c161b77e3aaaa25a0b4`）。本快照只补齐
可恢复输入和配置，不改变 Agent、Prompt、A1/A2/A3、Verifier、任务语义或已有 46 题结果。

快照目录为 `docs/experiments/artifacts/2026-07-31-full46-reproducibility-freeze/snapshot/`：

- `official_scoring.py`：来自团队提供的官方 `scoring.py` 的逐字节副本，SHA-256 为
  `5f65aaa27bfc3cd488ae2294aa4cf463b6be4399306ffb847c44cdd3861d33fe`。
- `runtime_full_agent_manifest.json`、`runtime_artifacts/`：FULL_REF 使用的 Manifest、A2 Gate/
  Admission、A3 Store/Gate/Admission 的副本及哈希绑定。
- `benchmark_plan.json`：本次真实 46 题批次的解析后配置与逐题 identity。
- `experiment_config_snapshot.json`：无密钥的模型、预算来源、超时、Vitis、器件、时钟和组件状态快照。
- `snapshot_manifest.json`：46 个任务、328 个允许进入运行时的公开输入文件及所有哈希。

公开任务输入的唯一允许范围为 `task.toml`、描述、kernel 源/头文件、公开 testbench、公开 metadata
和 schema。`answer`、`golden`、`hidden`、`hidden_like`、`reference` 路径明确排除；它们不在快照
文件清单中，也不能进入 Agent、Planner 或复现实验。

## 复现前检查

在 clone 后、配置 provider 或启动 Vitis 前，执行：

```bash
cd llm4hls_harness
python tools/check_full46_repro_snapshot.py \
  --repo .. \
  --harness . \
  --snapshot ../docs/experiments/artifacts/2026-07-31-full46-reproducibility-freeze/snapshot
```

预期输出：`{"status":"PASS","tasks":46,"public_task_files":328,"failures":[]}`。
该检查只读文件与计算 SHA-256：确认 46 个唯一 task ID、Mode 分布
`REPAIR=14 / SYNTH_FIX=10 / STRUCTURAL_FIX=11 / OPTIMIZE=11`、评分器副本、Manifest、冻结报告和
所有允许公开输入均可恢复且未漂移；它不会调用模型或 Vitis。

## 环境与运行合同

- Provider/模型：`openai-compatible` / `deepseek-v4-pro`；密钥仅从本机环境变量读取，绝不写入快照。
- Backend：Vitis `2025.2`，`vitis-run v2025.2`，SW Build `6295257`。
- Vitis 根目录：`/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis`，运行时变量为
  `LLM4HLS_VITIS_HLS_ROOT`，不使用 `VITIS_ROOT`。
- FPGA：`xcu55c-fsvh2892-2L-e`；开发时钟为 `5.0 ns`；最终认证额外要求 100 MHz Gate。
- Agent：A1=on、A2=enforce（CLI interface `v2`，有效实现
  `v3.continuation-policy.v3`）、A3=guided/ranker v3、Fixed Token、B1/B2=on。
- Planner：每题最多 4 次；Token/Credit 上限来自各任务 `task.toml`，运行不得向上覆盖；B2 位于
  Agent Ledger 之外。

## 重新运行（产生新的 fresh evidence）

以下命令用于在已经通过上节只读检查、且本机已正确配置 DeepSeek provider 凭据和 Vitis 授权的前提下，
重新运行一个**全新的** 46 题批次。它不会覆盖 `rc2-full-ref-46-20260731T063617Z` 的原始 Artifact。

```bash
cd llm4hls_harness
export LLM4HLS_VITIS_HLS_ROOT=/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis
python -m llm4hls_agent.v3_batch_benchmark \
  --corpus task_corpus/v3d-fast \
  --corpus task_corpus/v3d-expanded \
  --output-dir runs/full46-rerun-$(date -u +%Y%m%dT%H%M%SZ) \
  --models deepseek-v4-pro \
  --backend vitis \
  --validation-profile fast-experiment \
  --final-validation-policy task_contract \
  --evidence-memory on \
  --max-planner-rounds 4 \
  --max-no-improvement-rounds 2 \
  --continuation-policy enforce \
  --continuation-policy-version v2 \
  --continuation-admission-manifest ../docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/a2/continuation-v3-admission.json \
  --full-agent-manifest llm4hls_agent/config/track_a_rc2_structural_search_continuation_020.json \
  --experience-mode guided \
  --experience-store ../docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/a3/train-only-experience-store.jsonl \
  --experience-ranker-version v3 \
  --experience-admission-manifest ../docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/a3/admission.json \
  --experience-task-split hidden_like
```

重新生成 46 题汇总报告时，必须把新 run 目录传给冻结版本的
`tools/generate_full_46_reports.py`，并把输出写入新目录；不得覆盖现有
`2026-07-31-experiment-framework-enhancement/` 证据。结果中的 `official_score` 仍须保持
`NOT_EXECUTED_NO_HIDDEN_RECEIPT`，除非独立取得合规的隐藏评分 receipt。

## 当前恢复结论

所有必要 Manifest、公开任务输入、评分器副本、运行配置、A2/A3 runtime Artifact 及七份已冻结报告均由
`snapshot_manifest.json` 哈希绑定。通过上述只读检查后，可以在同一提交上开始 A2/A3 Ablation；Ablation
必须使用新的 fresh run 目录，不能修改或复用 FULL_REF Candidate。
