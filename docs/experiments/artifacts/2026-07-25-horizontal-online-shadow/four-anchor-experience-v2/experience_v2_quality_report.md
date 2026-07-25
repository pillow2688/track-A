# V3-E Experience v2 数据质量报告

- 原始记录：5
- 成功迁移：5
- 隔离记录：0
- 可排名真实 Candidate：5
- 命中实际 Patch/运行 Artifact：5
- Planner 声明与 Patch 观测完全一致率：20.00%
- OTHER_* 策略比例：12.50%

## 分布

```json
{
  "algorithm_family": {
    "COORDINATE_TRANSFORM": 1,
    "DOT_PRODUCT": 2,
    "FIR_CONVOLUTION": 1,
    "VECTOR_ELEMENTWISE": 1
  },
  "bottleneck_subtype": {
    "HIGH_TRANSACTION_LATENCY_WITH_II_ONE": 1,
    "OPTIMIZATION_BOTTLENECK_OTHER": 1,
    "UNKNOWN": 3
  },
  "excluded": {},
  "failure_subtype": {
    "DYNAMIC_ALLOCATION": 1,
    "FUNCTIONAL_MISMATCH_OTHER": 1,
    "STREAM_ORDER_MISMATCH": 1,
    "UNKNOWN": 2
  },
  "missing": {
    "problem.bottleneck_subtype": 3,
    "problem.failure_subtype": 2
  },
  "mode": {
    "OPTIMIZE": 2,
    "REPAIR": 1,
    "STRUCTURAL_FIX": 1,
    "SYNTH_FIX": 1
  },
  "outcome": {
    "NO_IMPROVEMENT": 2,
    "SUCCESS": 3
  },
  "provider": {
    "openai-compatible-fast-experiment": 2,
    "openai-compatible-task-aware": 3
  },
  "strategy": {
    "ARRAY_PARTITION": 1,
    "FIX_STREAM_ORDER": 1,
    "LOCAL_LOOP_RESTRUCTURE": 1,
    "LOOP_PIPELINE": 1,
    "LOOP_UNROLL": 1,
    "OTHER_FUNCTIONAL_REPAIR": 1,
    "PAR_FACTOR_TUNING": 1,
    "REMOVE_DYNAMIC_ALLOCATION": 1
  },
  "toolchain": {
    "Vitis 2025.2": 5
  }
}
```

本报告只描述相关性与数据质量，不把 Oracle、golden 或 fixture 记作 Agent 成功。
