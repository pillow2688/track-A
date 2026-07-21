# V3-E Shadow Pilot 报告

更新时间：2026-07-21

## 状态

`BLOCKED / NOT_RUN`。12 个计划 slot 全部保留在机器结果中，没有启动任何真实模型请求，也没有启动对应的 Vitis run，因此不能计算 shadow 成功率、建议一致率或 Token/Credit/时间均值。

计划任务：`001, 002, 003, 009, 010, 011, 015, 016, 017, 021, 022, 028`。

## 阻塞原因

本地已有模型配置，Vitis 2025.2 也可用；但当前运行平台拒绝把标记为 `hidden_like` 的工作区任务内容发送给外部 OpenAI-compatible 模型。该限制发生在进程启动前。我们没有把 split 改成 train/dev 绕过，也没有生成伪造 run。

因此：

- `experience_recommendations.jsonl` 当前为 0 条真实推荐；
- `pilot_results.jsonl` 中 12 个 shadow slot 都是 `execution_started=false`、`attempt_status=NOT_RUN`；
- 0 Token、0 Credit、0 工具调用不是实验结果，只表示未执行。

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
