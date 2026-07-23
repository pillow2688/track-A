# 当前 28 题库存

英文镜像：`current-28task-inventory.md`。

本库存使用开发侧 baseline facts，通过当前确定性 Router 离线重放得到 mode。Acceptance/oracle 信息只用于离线审计，绝不会传给 Planner。

- 任务总数：**28**
- 历史 12 题覆盖：**12**
- 历史未覆盖：**16**
- Mode 分布：`OPTIMIZE`=8、`REPAIR`=8、`STRUCTURAL_FIX`=6、`SYNTH_FIX`=6
- 有效审计状态：Router 修复已提交为 `e5ba32a032432579bb9daa8015d2f705cff93498`。

| 任务 | 类型 | 需要生成 | Mode | Mode 原因 | 需要 CoSim | 历史条件/重复数 | 历史可复用 | 当前影响 | 需要 fresh run |
|---|---|---:|---|---|---:|---|---:|---|---:|
| v3d_fast_001 | generate | 是 | REPAIR | BASELINE_CSIM_FAILED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_002 | generate | 是 | REPAIR | BASELINE_CSIM_FAILED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_003 | generate | 是 | REPAIR | BASELINE_CSIM_FAILED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_004 | generate | 是 | REPAIR | BASELINE_CSIM_FAILED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_005 | generate | 是 | REPAIR | BASELINE_CSIM_FAILED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_006 | generate | 是 | REPAIR | BASELINE_CSIM_FAILED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_007 | generate | 是 | REPAIR | BASELINE_CSIM_FAILED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_008 | generate | 是 | REPAIR | BASELINE_CSIM_FAILED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_009 | generate | 是 | SYNTH_FIX | BASELINE_SYNTH_FAILED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_010 | generate | 是 | SYNTH_FIX | BASELINE_SYNTH_FAILED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_011 | generate | 是 | SYNTH_FIX | BASELINE_SYNTH_FAILED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_012 | generate | 是 | SYNTH_FIX | BASELINE_SYNTH_FAILED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_013 | generate | 是 | SYNTH_FIX | BASELINE_SYNTH_FAILED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_014 | generate | 是 | SYNTH_FIX | BASELINE_SYNTH_FAILED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_015 | generate | 是 | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | 是 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_016 | generate | 是 | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | 是 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_017 | generate | 是 | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | 是 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_018 | generate | 是 | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | 是 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_019 | generate | 是 | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | 是 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_020 | generate | 是 | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | 是 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_021 | generate | 是 | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_022 | generate | 是 | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_023 | generate | 是 | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_024 | generate | 是 | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_025 | generate | 是 | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | 否 | A:2, B:2, C:2 | 是 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_026 | generate | 是 | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_027 | generate | 是 | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | 否 | - | 否 | GLOBAL_BEHAVIORAL | 是 |
| v3d_fast_028 | generate | 是 | OPTIMIZE | BASELINE_VALIDATION_PASSED | 是 | - | 否 | GLOBAL_BEHAVIORAL | 是 |

`需要 fresh run` 表达的是提交级当前版本可比性要求。历史 12 题仍可用于描述性工程覆盖，但不能作为同版本正式矩阵。
