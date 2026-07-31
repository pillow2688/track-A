# RC2 46-task FULL_REF benchmark report

## Result

- Fresh completed tasks: **46/46**; E2E success: **46/46 (100.00%)**.
- Every reported success has a frozen final Candidate, independent B2 certification, and a 100 MHz clock-gate result.
- This report uses public Artifact evidence only. `official_score` is `NOT_EXECUTED_NO_HIDDEN_RECEIPT` for every task; no hidden test was run or inferred.
- `public_validation_proxy_score` implements the official `scoring.py` formula using public B2 evidence and public synth latency. It is not an official hidden-test score.

## Benchmark overview

- Mode distribution: OPTIMIZE 11; REPAIR 14; STRUCTURAL_FIX 11; SYNTH_FIX 10.
- Difficulty comes from the frozen public task manifests; the benchmark contains original `v3d-fast` and expanded `v3d-expanded` tasks.

## Mode analysis

| Scope | Tasks | Success | Success rate | Planner | Tokens | Credits | Candidates | Wall seconds | Public proxy | Mean accel |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ALL | 46 | 46 | 1.000 | 67 | 211922 | 1516 | 67 | 6308.859 | 94.647 | 3.507 |
| OPTIMIZE | 11 | 11 | 1.000 | 30 | 74099 | 366 | 30 | 1498.640 | 23.474 | 4.921 |
| REPAIR | 14 | 14 | 1.000 | 14 | 43646 | 194 | 14 | 1088.295 | 18.900 | N/A |
| STRUCTURAL_FIX | 11 | 11 | 1.000 | 13 | 59506 | 806 | 13 | 2345.764 | 32.674 | 1.561 |
| SYNTH_FIX | 10 | 10 | 1.000 | 10 | 34671 | 150 | 10 | 1376.160 | 19.600 | N/A |

## Agent efficiency and tools

- Planner calls: 67; Tokens: 211922; Agent Credits: 1516; Candidates: 67; total wall time: 6308.9s.
- Tool calls: CSim 152; Synth 136; CoSim 41; LLM 67.
- Measured accelerations: mean 3.507x; tasks at/above 8x: 3.

## Routing and notable evidence

- `v3d_fast_018` was expected `STRUCTURAL_FIX` but routed `OPTIMIZE`; it remained a certified success using its legal baseline. The difference is preserved, not normalized away.
- `v3d_fast_020` baseline CoSim timed out with the public deadlock evidence; its single Planner candidate passed CSim/Synth/CoSim and B2, then finalized safely.

## Terminal failures and diagnostic evidence

- Terminal E2E failures: 0. Terminal-stage counts: {'NONE': 46}.
- Pre-finalization diagnostic evidence (not terminal failures): {'UNKNOWN': 9, 'COMPILE_ERROR': 2, 'RUNTIME_FAIL': 14, 'DEADLOCK': 8, 'TIMEOUT': 2, 'SYNTH_ERROR': 10, 'CONSTRAINT_VIOLATION': 1}. The CSV inventory labels each row as `PRE_FINALIZATION_DIAGNOSTIC` or `TERMINAL_FAILURE`.
- Last action-family distribution: {'UNIFIED_DIFF_COORDINATE_RECOVERY': 2, 'LOCAL_SEMANTIC_CHANGE': 9, 'ARITHMETIC_FIX': 3, 'LOOP_BOUND_FIX': 2, 'FIR_COEFFICIENT_FIX': 1, 'INITIALIZE_ACCUMULATOR': 1, 'INDEX_FIX': 1, 'FIFO_CAPACITY_OR_PROTOCOL': 7, 'STREAM_BALANCE_FIX': 1, 'DATAFLOW_TOPOLOGY_OR_ORDER': 2, 'REPLACE_DYNAMIC_ALLOCATION_WITH_STATIC_ARRAY': 3, 'REWRITE_RECURSIVE_FUNCTION': 1, 'INLINE_LAMBDA': 2, 'FUNCTIONAL_REPAIR': 1, 'FIX_OFF_BY_ONE_IN_PREFIX_SUM': 1, 'FIX_OFF_BY_ONE_CONDITION': 1, 'REMOVE_BITMASK': 1, 'FIX_BOUNDARY_CONDITION': 1, 'REPLACE_DYNAMIC_ALLOCATION_WITH_STATIC': 1, 'REPLACE_RECURSION_WITH_LOOP': 1, 'REPLACE_VLA_WITH_FIXED_ARRAY': 1, 'REMOVE_INVALID_PRAGMA': 1, 'STREAM_BALANCE_REPAIR': 1, 'SYNTHESIS_LEGALITY_LOCAL_REWRITE': 1}.

## Per-task scorecards

| Task | Expected | Routed | B2 | 100 MHz | Base lat | Cand lat | Accel | Public proxy | Planner | Tokens | Credits |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| expanded_optimize_01 | OPTIMIZE | OPTIMIZE | PASS | PASS | 18 | 1 | 18.000 | 3.000 | 4 | 9244 | 45 |
| expanded_optimize_02 | OPTIMIZE | OPTIMIZE | PASS | PASS | 39 | 15 | 2.600 | 2.393 | 4 | 9647 | 70 |
| expanded_optimize_03 | OPTIMIZE | OPTIMIZE | PASS | PASS | 34 | 34 | 1.000 | 2.212 | 4 | 9909 | 16 |
| expanded_repair_01 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 1.400 | 1 | 2851 | 11 |
| expanded_repair_02 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 1.400 | 1 | 2827 | 11 |
| expanded_repair_03 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 1.400 | 1 | 2900 | 11 |
| expanded_repair_04 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 1.400 | 1 | 2954 | 11 |
| expanded_repair_05 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 1.400 | 1 | 2878 | 11 |
| expanded_structural_01 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | 39 | 39 | 1.000 | 2.950 | 1 | 3983 | 75 |
| expanded_structural_02 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | 39 | 39 | 1.000 | 2.950 | 2 | 8450 | 75 |
| expanded_structural_03 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | N/A | N/A | N/A | 2.800 | 1 | 4106 | 75 |
| expanded_structural_04 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | N/A | N/A | N/A | 2.800 | 1 | 4113 | 75 |
| expanded_structural_05 | REPAIR | REPAIR | PASS | PASS | N/A | 19 | N/A | 2.800 | 1 | 3499 | 51 |
| expanded_structural_06 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | 60 | 19 | 3.158 | 3.274 | 1 | 6582 | 75 |
| expanded_synth_01 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 18 | N/A | 2.100 | 1 | 3166 | 15 |
| expanded_synth_02 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 18 | N/A | 2.100 | 1 | 3062 | 15 |
| expanded_synth_03 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 18 | N/A | 2.100 | 1 | 3259 | 15 |
| expanded_synth_04 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 18 | N/A | 2.100 | 1 | 3400 | 15 |
| v3d_fast_001 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 0.700 | 1 | 3241 | 11 |
| v3d_fast_002 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 0.700 | 1 | 3131 | 11 |
| v3d_fast_003 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 0.700 | 1 | 3089 | 11 |
| v3d_fast_004 | REPAIR | REPAIR | PASS | PASS | N/A | 10 | N/A | 1.400 | 1 | 3330 | 11 |
| v3d_fast_005 | REPAIR | REPAIR | PASS | PASS | N/A | 14 | N/A | 1.400 | 1 | 3034 | 11 |
| v3d_fast_006 | REPAIR | REPAIR | PASS | PASS | N/A | 47 | N/A | 1.400 | 1 | 3322 | 11 |
| v3d_fast_007 | REPAIR | REPAIR | PASS | PASS | N/A | 18 | N/A | 1.400 | 1 | 3248 | 11 |
| v3d_fast_008 | REPAIR | REPAIR | PASS | PASS | N/A | 34 | N/A | 1.400 | 1 | 3342 | 11 |
| v3d_fast_009 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 18 | N/A | 1.400 | 1 | 3425 | 15 |
| v3d_fast_010 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 18 | N/A | 2.100 | 1 | 3468 | 15 |
| v3d_fast_011 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 18 | N/A | 2.100 | 1 | 3500 | 15 |
| v3d_fast_012 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 76 | N/A | 1.400 | 1 | 4223 | 15 |
| v3d_fast_013 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | N/A | N/A | 2.100 | 1 | 3775 | 15 |
| v3d_fast_014 | SYNTH_FIX | SYNTH_FIX | PASS | PASS | N/A | 35 | N/A | 2.100 | 1 | 3393 | 15 |
| v3d_fast_015 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | 39 | 39 | 1.000 | 2.950 | 1 | 4355 | 75 |
| v3d_fast_016 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | 39 | 39 | 1.000 | 2.950 | 1 | 4522 | 75 |
| v3d_fast_017 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | N/A | N/A | N/A | 2.800 | 1 | 4447 | 75 |
| v3d_fast_018 | STRUCTURAL_FIX | OPTIMIZE | PASS | PASS | 21 | 21 | 1.000 | 2.950 | 2 | 7077 | 56 |
| v3d_fast_019 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | 49 | 49 | 1.000 | 2.950 | 1 | 4422 | 75 |
| v3d_fast_020 | STRUCTURAL_FIX | STRUCTURAL_FIX | PASS | PASS | 60 | 18 | 3.333 | 3.300 | 1 | 7449 | 75 |
| v3d_fast_021 | OPTIMIZE | OPTIMIZE | PASS | PASS | 18 | 18 | 1.000 | 1.475 | 2 | 4605 | 15 |
| v3d_fast_022 | OPTIMIZE | OPTIMIZE | PASS | PASS | 9 | 2 | 4.500 | 1.738 | 3 | 6871 | 45 |
| v3d_fast_023 | OPTIMIZE | OPTIMIZE | PASS | PASS | 67 | 6 | 11.167 | 2.000 | 1 | 2542 | 15 |
| v3d_fast_024 | OPTIMIZE | OPTIMIZE | PASS | PASS | 18 | 6 | 3.000 | 1.625 | 3 | 7920 | 20 |
| v3d_fast_025 | OPTIMIZE | OPTIMIZE | PASS | PASS | 71 | 71 | 1.000 | 1.475 | 2 | 5543 | 20 |
| v3d_fast_026 | OPTIMIZE | OPTIMIZE | PASS | PASS | 87 | 10 | 8.700 | 3.000 | 2 | 5569 | 20 |
| v3d_fast_027 | OPTIMIZE | OPTIMIZE | PASS | PASS | 81 | 81 | 1.000 | 2.212 | 2 | 5039 | 15 |
| v3d_fast_028 | OPTIMIZE | OPTIMIZE | PASS | PASS | 39 | 18 | 2.167 | 2.344 | 3 | 7210 | 85 |
