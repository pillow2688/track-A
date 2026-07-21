# V3-E Guided Pilot 报告

更新时间：2026-07-21

## 状态

`BLOCKED / NOT_RUN`。guided 必须在 shadow 完成后，使用相同 12 题、模型、validation profile、预算和冻结 seed，在独立目录重新请求模型。由于 shadow 的外部模型请求被平台数据边界拦截，guided 未启动，也没有可比较的建议帮助/误导案例。

机器汇总明确记录：

- expected tasks：12
- scheduled guided slots：12
- actual attempts：0
- final successes：不可测
- Token/Credit/wall time：不可测
- comparison status：`NOT_RUN`

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

先完成 shadow，再执行：

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
