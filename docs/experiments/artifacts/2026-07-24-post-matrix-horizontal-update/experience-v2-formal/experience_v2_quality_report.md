# V3-E Experience v2 数据质量报告

- 原始记录：31
- 成功迁移：31
- 隔离记录：0
- 可排名真实 Candidate：26
- 命中实际 Patch/运行 Artifact：31
- Planner 声明与 Patch 观测完全一致率：25.81%
- OTHER_* 策略比例：10.64%

## 分布

```json
{
  "algorithm_family": {
    "FIR_CONVOLUTION": 1,
    "MATRIX_MULTIPLICATION": 1,
    "OTHER": 2,
    "REDUCTION": 3,
    "STENCIL": 1,
    "STREAM_PIPELINE": 8,
    "VECTOR_ELEMENTWISE": 15
  },
  "bottleneck_subtype": {
    "HIGH_TRANSACTION_LATENCY_WITH_II_ONE": 3,
    "OPTIMIZATION_BOTTLENECK_OTHER": 9,
    "SERIAL_REDUCTION": 1,
    "UNKNOWN": 18
  },
  "excluded": {
    "CANDIDATE_NOT_CREATED": 5,
    "PATCH_INVALID": 3,
    "V1_NOT_RANKING_ELIGIBLE": 5
  },
  "failure_subtype": {
    "DYNAMIC_ALLOCATION": 1,
    "FIFO_DEPTH_INSUFFICIENT": 3,
    "FUNCTIONAL_MISMATCH_OTHER": 4,
    "OFF_BY_ONE": 1,
    "SYNTHESIS_ERROR_OTHER": 4,
    "UNKNOWN": 13,
    "UNSUPPORTED_STL": 1,
    "WRONG_ARRAY_INDEX": 1,
    "WRONG_BRANCH_CONDITION": 1,
    "WRONG_COEFFICIENT": 2
  },
  "missing": {
    "problem.bottleneck_subtype": 18,
    "problem.failure_subtype": 13
  },
  "mode": {
    "OPTIMIZE": 13,
    "REPAIR": 9,
    "STRUCTURAL_FIX": 3,
    "SYNTH_FIX": 6
  },
  "outcome": {
    "FAILURE": 11,
    "NO_IMPROVEMENT": 16,
    "SUCCESS": 4
  },
  "provider": {
    "openai-compatible-fast-experiment": 13,
    "openai-compatible-task-aware": 18
  },
  "strategy": {
    "ARRAY_PARTITION": 3,
    "DATAFLOW": 2,
    "FIX_ARRAY_INDEX": 3,
    "FIX_BRANCH_CONDITION": 3,
    "FIX_COEFFICIENT": 2,
    "FIX_LOOP_BOUND": 3,
    "INCREASE_FIFO_DEPTH": 3,
    "LOCAL_LOOP_RESTRUCTURE": 8,
    "LOOP_PIPELINE": 1,
    "LOOP_UNROLL": 8,
    "MEMORY_BANKING": 1,
    "OTHER_FUNCTIONAL_REPAIR": 2,
    "OTHER_SYNTHESIS_REPAIR": 3,
    "REMOVE_DYNAMIC_ALLOCATION": 1,
    "REMOVE_RECURSION": 1,
    "REPLACE_UNSUPPORTED_STL": 2,
    "STREAMING": 1
  },
  "toolchain": {
    "Vitis 2025.2": 31
  }
}
```

本报告只描述相关性与数据质量，不把 Oracle、golden 或 fixture 记作 Agent 成功。
