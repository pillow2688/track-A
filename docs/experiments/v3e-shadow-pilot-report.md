# V3-E Shadow Pilot 报告

更新时间：2026-07-21

## 状态

`BLOCKED / INCOMPLETE`。本轮确实启动了一个新的 A01 批次，但它运行在禁止网络 socket 的沙箱中。前 6 题完成了真实 Vitis baseline 并在 Planner HTTP dispatch 后失败，第 7 题完成 baseline CSim/Synth 后被主动中断；剩余 5 题未启动。没有任何题形成 terminal result，因此不能计算 shadow 成功率、建议一致率或最终 Token/Credit/时间均值。

计划任务：`001, 002, 003, 009, 010, 011, 015, 016, 017, 021, 022, 028`。

## 阻塞原因

本地模型配置和 Vitis 2025.2 均可用。独立的最小 DeepSeek 探测在允许网络后返回 HTTP 200，证明 endpoint、key 和 `deepseek-v4-pro` 有效。A01 的失败原因是沙箱禁止网络 socket，不是模型配置或 Quality Gate。

随后计划使用全新 A02 目录在允许网络的执行环境重跑，但平台拒绝把标记为 `hidden_like` 的本地派生任务上下文发送给外部模型。A02 在创建 run 目录前被拦截。我们没有修改 split 绕过，也没有伪造结果。

因此：

- A01 保留 7 个独立 run 目录：6 个 `PlannerActionAmbiguous` 失败，1 个中断；
- 前 6 题的 Quality Gate 均为 `ABSTAIN`，所以模型 Prompt 没有注入经验；
- 失败的 6 次 LLM ledger 各按非重放动作保守预留 8428–9485 tokens，这不是 provider 返回的实际 usage；
- A01 共完成 7 次 CSim、4 次 Synth、0 次 CoSim，消耗 19 credits；
- A02 没有创建，因此不存在可误解为成功的空 run。

## 已验证的 shadow 工程性质

确定性测试已证明：

- shadow 与 off 发送给模型的 Prompt 相同；
- shadow 不改变 Graph route；
- 经验库损坏、schema 错误或推荐写入失败时 fail-open 到旧 Planner；
- shadow 推荐仍受 train-only 检索、长度上限和安全 schema 限制。

## 可直接运行的本地命令

在允许向配置模型发送这些任务的本地环境中：

```bash
set -a
source llm4hls_harness/.env
set +a

PYTHONPATH=llm4hls_harness .venv/bin/python -m llm4hls_agent.v3_batch_benchmark \
  --corpus llm4hls_harness/task_corpus/v3d-fast/tasks \
  --output-dir llm4hls_harness/runs/v3e_shadow_pilot_20260721 \
  --models "$LLM4HLS_MODEL" --repeats 1 --backend vitis \
  --validation-profile fast-experiment --experience-mode shadow \
  --experience-store llm4hls_harness/experiments/v3e/experience_store.jsonl \
  --experience-task-split hidden_like \
  --task 'v3d_fast_001,v3d_fast_002,v3d_fast_003,v3d_fast_009,v3d_fast_010,v3d_fast_011,v3d_fast_015,v3d_fast_016,v3d_fast_017,v3d_fast_021,v3d_fast_022,v3d_fast_028'
```

不要修改 split 规避数据边界；应在用户自行授权且允许外部模型处理该 corpus 的执行环境中运行。
