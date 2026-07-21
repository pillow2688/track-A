# V3-E Guided Pilot 报告

更新时间：2026-07-21

## 状态

`SKIPPED BY QUALITY GATE / NOT_RUN`。guided 未启动有两个独立原因：真实 shadow 没有完成；更重要的是 Leave-One-Run-Out 离线门槛明确给出 `SKIP_GUIDED`，因为 18 条真实经验中没有达到阈值的高置信注入案例。此时继续运行 guided 不符合本阶段预先规定的启动条件。

离线汇总明确记录：

- coverage：22.22%
- harmful recommendation rate：0%
- duplicate-failure suppression：100%
- average injected guidance：108.17 tokens（ABSTAIN 按实际 Prompt 计 0）
- high-confidence injections：0
- guided decision：`SKIP_GUIDED`

不得把上述缺失值解释为 0% 成功率。

## Guided 仍受哪些约束

即使后续运行 guided，经验层也只能增加 Planner Context：

- Planner 仍自行输出 hypothesis、evidence、strategy bundle、risk 和 Patch；
- BudgetLedger 仍是硬约束；
- Patch Validator/TopInterfaceGuard 仍可拒绝 Patch；
- Candidate 仍必须按原规则通过 CSim/Synth/CoSim gate；
- final 仍必须 fresh CSim + Synth + CoSim 全部 PASS；
- 空建议不会改变旧 Prompt。

## 可直接运行的本地命令

只有离线门槛和 shadow 都通过后，才允许执行：

```bash
set -a
source llm4hls_harness/.env
set +a

PYTHONPATH=llm4hls_harness .venv/bin/python -m llm4hls_agent.v3_batch_benchmark \
  --corpus llm4hls_harness/task_corpus/v3d-fast/tasks \
  --output-dir llm4hls_harness/runs/v3e_guided_pilot_20260721 \
  --models "$LLM4HLS_MODEL" --repeats 1 --backend vitis \
  --validation-profile fast-experiment --experience-mode guided \
  --experience-store llm4hls_harness/experiments/v3e/experience_store.jsonl \
  --experience-task-split hidden_like \
  --task 'v3d_fast_001,v3d_fast_002,v3d_fast_003,v3d_fast_009,v3d_fast_010,v3d_fast_011,v3d_fast_015,v3d_fast_016,v3d_fast_017,v3d_fast_021,v3d_fast_022,v3d_fast_028'

PYTHONPATH=llm4hls_harness .venv/bin/python -m llm4hls_agent.v3_experience_pilot \
  --shadow-dir llm4hls_harness/runs/v3e_shadow_pilot_20260721 \
  --guided-dir llm4hls_harness/runs/v3e_guided_pilot_20260721 \
  --output-dir llm4hls_harness/experiments/v3e/pilot
```

当前机器产物 `pilot_results.jsonl`、`pilot_summary.json`、`pilot_summary.csv` 是诚实的 blocked 占位证据，确保 12 个未执行 slot 不会从分母中消失。
