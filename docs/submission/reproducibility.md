# V3-D 可复现性说明

> 自动生成于 2026-07-20T16:31:43+00:00，事实来源：`evidence_manifest.json` 指定的 run JSON。
> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。

## 证据分级

- `REAL_LLM + REAL_VITIS_VALIDATED`：模型真实生成 Patch，fresh CSim/Synth/CoSim 全通过。
- `SCRIPTED_PATCH_REPLAY + REAL_VITIS_VALIDATED`：真实 Vitis 验证已知 Patch，只验证执行闭环。
- `DETERMINISTIC/DEMO`：只验证编排和报告，不作为 HLS 成绩。
- `REAL_VITIS_ATTEMPT_FAILED`：保留失败事实，不能计入成功率分子。

## 当前审计 run

- `v3b_fast_dotproduct_live_deepseek_retry2`：REAL_LLM / REAL_VITIS_VALIDATED
- `v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE`：REAL_LLM / REAL_VITIS_ATTEMPT_FAILED
- `v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE`：REAL_LLM / REAL_VITIS_ATTEMPT_FAILED
- `v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE`：REAL_LLM / REAL_VITIS_ATTEMPT_FAILED
- `v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE`：SCRIPTED_PATCH_REPLAY / REAL_VITIS_VALIDATED
- `v3d-residual-structural-replay-r04-NOT_REAL_LLM`：SCRIPTED_PATCH_REPLAY / REAL_VITIS_VALIDATED
- `v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM`：SCRIPTED_PATCH_REPLAY / REAL_VITIS_VALIDATED

Replay release 的证据边界：`llm4hls_harness/releases/v3d-real-vitis-replay-acceptance-2026-07-20.json`：Vitis=REAL_VITIS_VALIDATED；successful replay planner=NOT_REAL_LLM；atomic real LLM acceptance=False。

## 生成报告

```bash
cd "$PROJECT_ROOT/llm4hls_harness"
python -m submission_tools.cli generate \
  --repo-root "$PROJECT_ROOT" \
  --manifest "$PROJECT_ROOT/docs/submission/evidence_manifest.json" \
  --output-dir "$PROJECT_ROOT/docs/submission"
```

Batch 原始 summary 可能含本机输出路径，先生成只保留指标与 SHA-256 的脱敏快照，再把快照相对路径加入 `evidence_manifest.json`：

```bash
python -m submission_tools.cli snapshot-benchmark \
  --input "$BENCHMARK_DIR/summary.json" \
  --output "$PROJECT_ROOT/docs/submission/benchmark_snapshots/my-benchmark.json"

python -m submission_tools.cli snapshot-oracle \
  --input "$ORACLE_RUN_DIR/summary.json" \
  --output "$PROJECT_ROOT/docs/submission/oracle_snapshots/my-oracle.json"
```

## 快速回归与真实环境检查

```bash
cd "$PROJECT_ROOT/llm4hls_harness"
python -m unittest discover -s tests -p 'test_*.py'
scripts/v3d-reproduce.sh demo-smoke
LLM4HLS_VITIS_HLS_ROOT="$VITIS_ROOT" scripts/v3d-reproduce.sh real-preflight
```

`real-preflight` 只探测外部 Vitis 2025.2，不声称运行了 CSim/Synth/CoSim。真实模型还必须在运行时提供 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和 `LLM4HLS_MODEL`，密钥不得写入文件。

## 生成并检查非最终 staging

```bash
cd "$PROJECT_ROOT/llm4hls_harness"
SOURCE_REVISION="$(git -C "$PROJECT_ROOT" rev-parse HEAD)" \
SOURCE_TREE_STATE="CLEAN_AFTER_MANUAL_GIT_CHECK" \
python -m submission_tools.cli stage \
  --source-root "$PROJECT_ROOT" \
  --output-root "$PROJECT_ROOT/build/submission-staging-NOT-FINAL" \
  --spec "$PROJECT_ROOT/docs/submission/staging_spec.json"

python -m submission_tools.cli scan \
  --root "$PROJECT_ROOT/build/submission-staging-NOT-FINAL"
```

Planner 输入可以独立专项检查：`python -m submission_tools.cli scan --root "$RUN_DIR/planner/inputs"`。
只有确认 `git status` 干净后才能把 `SOURCE_TREE_STATE` 设为 `CLEAN_AFTER_MANUAL_GIT_CHECK`；否则省略该变量，manifest 会标为 `DIRTY_OR_UNVERIFIED`。每个 staging 文件仍有独立 SHA-256。

## 当前外部阻塞

- 当前 Codex 进程未设置 OPENAI_BASE_URL、OPENAI_API_KEY、LLM4HLS_MODEL；因此本轮无法完成 projection A04、STRUCTURAL_FIX 和 SYNTH_FIX 的新原子真实 LLM+Vitis 成功 run。

## 可复现性边界

- 本文不声称 hidden grader 通过。
- 本文不声称 Vitis 可以合法打包进容器；使用主机外部 runtime。
- Run 目录是本地证据，不进入提交 staging；提交前只保留脱敏汇总和必要源码。
