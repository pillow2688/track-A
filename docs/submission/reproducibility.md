# V3-D 可复现性说明

> 自动生成于 2026-07-20T17:29:13+00:00，事实来源：`evidence_manifest.json` 指定的 run JSON。
> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。
> 2026-07-21 的 A03、真实验收和矩阵为人工核对补充；当前 generator 不会重建这些补充，
> 不可变事实入口是 `llm4hls_harness/releases/v3d-overnight-daytime-summary-2026-07-21.{md,json}`。

## 证据分级

- `REAL_LLM + REAL_VITIS_VALIDATED`：模型真实生成 Patch，fresh CSim/Synth/CoSim 全通过。
- `SCRIPTED_PATCH_REPLAY + REAL_VITIS_VALIDATED`：真实 Vitis 验证已知 Patch，只验证执行闭环。
- `DETERMINISTIC/DEMO`：只验证编排和报告，不作为 HLS 成绩。
- `REAL_VITIS_ATTEMPT_FAILED`：保留失败事实，不能计入成功率分子。

## 2026-07-20 历史 curated runs

- `v3b_fast_dotproduct_live_deepseek_retry2`：REAL_LLM / REAL_VITIS_VALIDATED
- `v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE`：REAL_LLM / REAL_VITIS_ATTEMPT_FAILED
- `v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE`：REAL_LLM / REAL_VITIS_ATTEMPT_FAILED
- `v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE`：REAL_LLM / REAL_VITIS_ATTEMPT_FAILED
- `v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE`：SCRIPTED_PATCH_REPLAY / REAL_VITIS_VALIDATED
- `v3d-residual-structural-replay-r04-NOT_REAL_LLM`：SCRIPTED_PATCH_REPLAY / REAL_VITIS_VALIDATED
- `v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM`：SCRIPTED_PATCH_REPLAY / REAL_VITIS_VALIDATED

Replay release 的证据边界：`llm4hls_harness/releases/v3d-real-vitis-replay-acceptance-2026-07-20.json`：Vitis=REAL_VITIS_VALIDATED；successful replay planner=NOT_REAL_LLM；atomic real LLM acceptance=False。

Fail-closed Vitis probe 后的 anchor 重跑：`llm4hls_harness/releases/v3d-vitis-probed-anchor-rerun-2026-07-21.json`：A01=8 accepted/4 rejected；A02 retry=0 accepted/4 rejected；status=PARTIAL_XSIM_BLOCKED。失败来自重复的 XSIM CoSim 启动异常；该结果不是 LLM 成绩。

后续 A03 恢复证据：`docs/submission/oracle_snapshots/v3d-oracle-vitis-probed-anchors-a03.json`：使用全新 run/simulator 工作目录、非沙箱串行重跑，4 accepted / 0 rejected，耗时 328.602 秒；015/016/017 命中预期 baseline deadlock 后 golden CoSim PASS，028 为 39 → 6 cycles。该证据支持瞬态或运行隔离问题，不足以把根因武断归到一个具体缓存文件。

2026-07-21 真实模型矩阵：DeepSeek 官方三题 9/9 DONE、9/9 fresh final 全 PASS；加上 synth-fix 共 12/12 DONE，31258 Tokens、526 Credits。逐 run SHA-256 和源码/镜像基线记录见 `llm4hls_harness/releases/v3d-overnight-daytime-summary-2026-07-21.json`。

## 生成报告

```bash
cd "$PROJECT_ROOT/llm4hls_harness"
python -m submission_tools.cli generate \
  --repo-root "$PROJECT_ROOT" \
  --manifest "$PROJECT_ROOT/docs/submission/evidence_manifest.json" \
  --output-dir "$PROJECT_ROOT/docs/submission"
```

该命令只重建 2026-07-20 generator 管理的基础段落；它不会读取
`execution_summary_release` 或 `xsim_recovery_snapshot`，因此会覆盖本文件中的 2026-07-21
人工补充。需要重生成时，应先保存或随后从不可变 release 恢复这些补充。

Batch 原始 summary 可能含本机输出路径，先生成只保留指标与 SHA-256 的脱敏快照，再把快照相对路径加入 `evidence_manifest.json`：

```bash
python -m submission_tools.cli snapshot-benchmark \
  --input "$BENCHMARK_DIR/summary.json" \
  --output "$PROJECT_ROOT/docs/submission/benchmark_snapshots/my-benchmark.json"

python -m submission_tools.cli snapshot-oracle \
  --input "$ORACLE_RUN_DIR/summary.json" \
  --output "$PROJECT_ROOT/docs/submission/oracle_snapshots/my-oracle.json"
```

## 从零运行确定性 Corpus 与 Batch

下面两条命令会创建全新的输出目录；它们只验证数据集门控和批量编排，
不会被报告成真实 LLM 或真实 Vitis 成绩：

```bash
cd "$PROJECT_ROOT"
PYTHONPATH=llm4hls_harness .venv/bin/python \
  -m llm4hls_agent.v3d_oracle_validator \
  --corpus llm4hls_harness/task_corpus/v3d-fast \
  --output-dir /tmp/v3d-oracle-deterministic-fresh \
  --backend deterministic

PYTHONPATH=llm4hls_harness .venv/bin/python \
  -m llm4hls_agent.v3_batch_benchmark \
  --corpus llm4hls_harness/task_corpus/v3d-fast \
  --output-dir /tmp/v3d-benchmark-deterministic-fresh \
  --models deterministic-fixture-v1 \
  --backend deterministic
```

在相同命令末尾增加 `--resume` 可验证断点复用；不要把 `/tmp` 的原始
run 目录直接放入提交包，应先使用上面的 snapshot 命令脱敏。

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
cd "$PROJECT_ROOT"
PYTHONPATH=llm4hls_harness .venv/bin/python -m submission_tools.cli stage \
  --source-root "$PROJECT_ROOT" \
  --output-root "$PROJECT_ROOT/build/submission-staging-NOT-FINAL" \
  --spec "$PROJECT_ROOT/docs/submission/staging_spec.json"

PYTHONPATH=llm4hls_harness .venv/bin/python -m submission_tools.cli scan \
  --root "$PROJECT_ROOT/build/submission-staging-NOT-FINAL"
```

Planner 输入可以独立专项检查：`python -m submission_tools.cli scan --root "$RUN_DIR/planner/inputs"`。
staging 工具直接读取当前 Git HEAD 和工作树状态；环境变量不能把脏工作树伪装成 clean。每个 staging 文件仍有独立 SHA-256。

## 当前外部阻塞

- DeepSeek 环境和三种真实修复闭环已经完成。当前模型实验的唯一外部阻塞是 Qwen：缺少可达的 OpenAI-compatible `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和服务端实际 `LLM4HLS_MODEL` alias。
- 源码/镜像基线 `a796da3` 的 Agent-only Docker 镜像已重建并通过 demo-smoke 与容器快速测试；专有 Vitis 仍使用宿主外部 runtime。

## 可复现性边界

- 本文不声称 hidden grader 通过。
- 本文不声称 Vitis 可以合法打包进容器；使用主机外部 runtime。
- Run 目录是本地证据，不进入提交 staging；提交前只保留脱敏汇总和必要源码。
