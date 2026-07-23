# Current 28-task inventory

中文版本：`current-28task-inventory.zh-CN.md`。

Mode is replayed from development-side baseline facts through the current deterministic router. Acceptance/oracle facts are used only for offline audit and are never Planner-visible.

- Tasks: **28**
- Historical 12 coverage: **12**
- Missing historical coverage: **16**
- Mode counts: `OPTIMIZE`=8, `REPAIR`=8, `STRUCTURAL_FIX`=6, `SYNTH_FIX`=6
- Effective audit state: Router fix committed as `e5ba32a032432579bb9daa8015d2f705cff93498`.

| Task | Type | Generation | Mode | Mode reason | CoSim required | Hist. conditions/repeats | Reusable | Impact | Fresh |
|---|---|---:|---|---|---:|---|---:|---|---:|
| v3d_fast_001 | generate | True | REPAIR | BASELINE_CSIM_FAILED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_002 | generate | True | REPAIR | BASELINE_CSIM_FAILED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_003 | generate | True | REPAIR | BASELINE_CSIM_FAILED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_004 | generate | True | REPAIR | BASELINE_CSIM_FAILED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_005 | generate | True | REPAIR | BASELINE_CSIM_FAILED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_006 | generate | True | REPAIR | BASELINE_CSIM_FAILED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_007 | generate | True | REPAIR | BASELINE_CSIM_FAILED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_008 | generate | True | REPAIR | BASELINE_CSIM_FAILED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_009 | generate | True | SYNTH_FIX | BASELINE_SYNTH_FAILED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_010 | generate | True | SYNTH_FIX | BASELINE_SYNTH_FAILED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_011 | generate | True | SYNTH_FIX | BASELINE_SYNTH_FAILED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_012 | generate | True | SYNTH_FIX | BASELINE_SYNTH_FAILED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_013 | generate | True | SYNTH_FIX | BASELINE_SYNTH_FAILED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_014 | generate | True | SYNTH_FIX | BASELINE_SYNTH_FAILED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_015 | generate | True | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | True | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_016 | generate | True | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | True | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_017 | generate | True | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | True | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_018 | generate | True | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | True | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_019 | generate | True | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | True | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_020 | generate | True | STRUCTURAL_FIX | REQUIRED_BASELINE_COSIM_FAILED | True | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_021 | generate | True | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_022 | generate | True | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_023 | generate | True | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_024 | generate | True | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_025 | generate | True | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | False | A:2, B:2, C:2 | True | GLOBAL_BEHAVIORAL | True |
| v3d_fast_026 | generate | True | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_027 | generate | True | OPTIMIZE | BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED | False | - | False | GLOBAL_BEHAVIORAL | True |
| v3d_fast_028 | generate | True | OPTIMIZE | BASELINE_VALIDATION_PASSED | True | - | False | GLOBAL_BEHAVIORAL | True |

Fresh-run flags express submission-grade current-version comparability. The historical 12 remain usable for descriptive engineering coverage, but not as a same-version formal matrix.
