# V3-E Experience v2 数据质量报告

- 原始记录：11
- 成功迁移：11
- 隔离记录：0
- 可排名真实 Candidate：11
- 命中实际 Patch/运行 Artifact：11
- Planner 声明与 Patch 观测完全一致率：36.36%
- OTHER_* 策略比例：20.00%

## 分布

```json
{
  "algorithm_family": {
    "DOT_PRODUCT": 1,
    "STENCIL": 1,
    "STREAM_PIPELINE": 6,
    "VECTOR_ELEMENTWISE": 3
  },
  "bottleneck_subtype": {
    "HIGH_TRANSACTION_LATENCY_WITH_II_ONE": 1,
    "OPTIMIZATION_BOTTLENECK_OTHER": 2,
    "UNKNOWN": 8
  },
  "excluded": {},
  "failure_subtype": {
    "FUNCTIONAL_MISMATCH_OTHER": 2,
    "STREAM_ORDER_MISMATCH": 1,
    "STRUCTURAL_ERROR_OTHER": 1,
    "SYNTHESIS_ERROR_OTHER": 2,
    "UNKNOWN": 3,
    "UNSUPPORTED_STL": 1,
    "WRONG_BRANCH_CONDITION": 1
  },
  "missing": {
    "problem.bottleneck_subtype": 8,
    "problem.failure_subtype": 3
  },
  "mode": {
    "OPTIMIZE": 3,
    "REPAIR": 3,
    "STRUCTURAL_FIX": 2,
    "SYNTH_FIX": 3
  },
  "outcome": {
    "FAILURE": 4,
    "NO_IMPROVEMENT": 3,
    "SUCCESS": 4
  },
  "provider": {
    "openai-compatible-fast-experiment": 3,
    "openai-compatible-task-aware": 8
  },
  "strategy": {
    "ARRAY_PARTITION": 1,
    "DATAFLOW": 1,
    "FIX_BRANCH_CONDITION": 2,
    "FIX_COEFFICIENT": 1,
    "FIX_STREAM_ORDER": 2,
    "INCREASE_FIFO_DEPTH": 1,
    "LOCAL_LOOP_RESTRUCTURE": 1,
    "LOOP_PIPELINE": 1,
    "LOOP_UNROLL": 1,
    "OTHER_FUNCTIONAL_REPAIR": 1,
    "OTHER_SYNTHESIS_REPAIR": 2,
    "REPLACE_UNSUPPORTED_STL": 1
  },
  "toolchain": {
    "Vitis 2025.2": 11
  }
}
```

本报告只描述相关性与数据质量，不把 Oracle、golden 或 fixture 记作 Agent 成功。
