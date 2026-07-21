# V3-E Experience v2 数据质量报告

- 原始记录：103
- 成功迁移：103
- 隔离记录：0
- 可排名真实 Candidate：76
- 命中实际 Patch/运行 Artifact：77
- Planner 声明与 Patch 观测完全一致率：49.51%
- OTHER_* 策略比例：18.12%

## 分布

```json
{
  "algorithm_family": {
    "DOT_PRODUCT": 11,
    "FIR_CONVOLUTION": 3,
    "MATRIX_MULTIPLICATION": 3,
    "OTHER": 13,
    "REDUCTION": 12,
    "STREAM_PIPELINE": 18,
    "VECTOR_ELEMENTWISE": 43
  },
  "bottleneck_subtype": {
    "HIGH_TRANSACTION_LATENCY_WITH_II_ONE": 17,
    "OPTIMIZATION_BOTTLENECK_OTHER": 22,
    "SERIAL_REDUCTION": 3,
    "UNKNOWN": 61
  },
  "excluded": {
    "CANDIDATE_NOT_CREATED": 27,
    "PATCH_INVALID": 19,
    "V1_NOT_RANKING_ELIGIBLE": 27
  },
  "failure_subtype": {
    "DYNAMIC_ALLOCATION": 3,
    "FIFO_DEPTH_INSUFFICIENT": 1,
    "FUNCTIONAL_MISMATCH_OTHER": 21,
    "OFF_BY_ONE": 3,
    "STREAM_ORDER_MISMATCH": 2,
    "STRUCTURAL_ERROR_OTHER": 5,
    "SYNTHESIS_ERROR_OTHER": 13,
    "UNKNOWN": 42,
    "UNSUPPORTED_STL": 3,
    "WRONG_ARRAY_INDEX": 1,
    "WRONG_BRANCH_CONDITION": 3,
    "WRONG_COEFFICIENT": 6
  },
  "missing": {
    "problem.bottleneck_subtype": 61,
    "problem.failure_subtype": 42,
    "source.model": 26,
    "source.prompt_version": 26,
    "source.provider": 26
  },
  "mode": {
    "OPTIMIZE": 42,
    "REPAIR": 34,
    "STRUCTURAL_FIX": 8,
    "SYNTH_FIX": 19
  },
  "outcome": {
    "FAILURE": 32,
    "NO_IMPROVEMENT": 9,
    "SUCCESS": 62
  },
  "provider": {
    "UNKNOWN": 26,
    "openai-compatible-fast-experiment": 31,
    "openai-compatible-task-aware": 46
  },
  "strategy": {
    "ARRAY_PARTITION": 15,
    "DATAFLOW": 4,
    "FIX_ARRAY_INDEX": 7,
    "FIX_BRANCH_CONDITION": 9,
    "FIX_COEFFICIENT": 6,
    "FIX_LOOP_BOUND": 9,
    "FIX_STREAM_ORDER": 2,
    "INCREASE_FIFO_DEPTH": 2,
    "LOCAL_LOOP_RESTRUCTURE": 18,
    "LOOP_PIPELINE": 4,
    "LOOP_UNROLL": 24,
    "MEMORY_BANKING": 5,
    "MULTI_PARTIAL_SUM": 2,
    "OTHER_FUNCTIONAL_REPAIR": 15,
    "OTHER_STRUCTURAL_REPAIR": 4,
    "OTHER_SYNTHESIS_REPAIR": 10,
    "PARALLEL_REDUCTION": 10,
    "REMOVE_DYNAMIC_ALLOCATION": 3,
    "REMOVE_RECURSION": 3,
    "REORDER_STREAM_OPERATIONS": 1,
    "REPLACE_UNSUPPORTED_STL": 6,
    "STREAMING": 1
  },
  "toolchain": {
    "Vitis 2025.2": 103
  }
}
```

本报告只描述相关性与数据质量，不把 Oracle、golden 或 fixture 记作 Agent 成功。
