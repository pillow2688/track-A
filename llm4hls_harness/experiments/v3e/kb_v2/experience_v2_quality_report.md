# V3-E Experience v2 数据质量报告

- 原始记录：26
- 成功迁移：26
- 隔离记录：0
- 可排名真实 Candidate：18
- 命中实际 Patch/运行 Artifact：26
- Planner 声明与 Patch 观测完全一致率：15.38%
- OTHER_* 策略比例：9.30%

## 分布

```json
{
  "algorithm_family": {
    "COORDINATE_TRANSFORM": 7,
    "DOT_PRODUCT": 11,
    "FIR_CONVOLUTION": 4,
    "VECTOR_ELEMENTWISE": 4
  },
  "bottleneck_subtype": {
    "HIGH_TRANSACTION_LATENCY_WITH_II_ONE": 7,
    "OPTIMIZATION_BOTTLENECK_OTHER": 4,
    "UNKNOWN": 15
  },
  "excluded": {
    "CANDIDATE_NOT_CREATED": 8,
    "PATCH_INVALID": 7,
    "V1_NOT_RANKING_ELIGIBLE": 8
  },
  "failure_subtype": {
    "FUNCTIONAL_MISMATCH_OTHER": 7,
    "STRUCTURAL_ERROR_OTHER": 4,
    "SYNTHESIS_ERROR_OTHER": 4,
    "UNKNOWN": 11
  },
  "missing": {
    "problem.bottleneck_subtype": 15,
    "problem.failure_subtype": 11
  },
  "mode": {
    "OPTIMIZE": 11,
    "REPAIR": 7,
    "STRUCTURAL_FIX": 4,
    "SYNTH_FIX": 4
  },
  "outcome": {
    "FAILURE": 9,
    "NO_IMPROVEMENT": 2,
    "SUCCESS": 15
  },
  "provider": {
    "openai-compatible-fast-experiment": 11,
    "openai-compatible-task-aware": 15
  },
  "strategy": {
    "ARRAY_PARTITION": 8,
    "FIX_BRANCH_CONDITION": 3,
    "FIX_STREAM_ORDER": 4,
    "LOCAL_LOOP_RESTRUCTURE": 3,
    "LOOP_PIPELINE": 2,
    "LOOP_UNROLL": 6,
    "MEMORY_BANKING": 3,
    "OTHER_FUNCTIONAL_REPAIR": 4,
    "PAR_FACTOR_TUNING": 6,
    "REMOVE_DYNAMIC_ALLOCATION": 4
  },
  "toolchain": {
    "Vitis 2025.2": 26
  }
}
```

本报告只描述相关性与数据质量，不把 Oracle、golden 或 fixture 记作 Agent 成功。
