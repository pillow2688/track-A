# V3-D Batch Benchmark Report

- Benchmark fingerprint: `edc0fe002ae41f172953fb9e06e9f6917a6b8b2651e23efe306158d525dd2142`
- Selected tasks / planned runs: `28 / 28`
- Latest slots / all attempts / new / resumed: `28 / 28 / 28 / 0`
- Stop reason: `COMPLETED`

> Evidence rule: DEMO and DETERMINISTIC rows are fixture evidence only. They are never included in the real-evidence headline below.

## Real Vitis evidence headline (all current-plan attempts)

> Retries remain in this population, so a later success cannot hide an earlier failed attempt. Latest-slot rows are used only for completion.

| Runs | E2E success | Fresh final | Router accuracy |
|---:|---:|---:|---:|
| 24 | 95.8% | 95.8% | 95.8% |

## Latest-slot completion population

| Slots | E2E | Fresh final | Real slots | Real E2E |
|---:|---:|---:|---:|---:|
| 28 | 82.1% | 82.1% | 24 | 95.8% |

## Evidence populations

| Evidence | Runs | E2E | Fresh final | Credits | Tokens | Calls C/S/Co/L | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| REAL | 28 | 82.1% | 82.1% | 653.0 | 66774.0 | 73/60/17/31 | 3269.712673016591 |
| DETERMINISTIC | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |
| DEMO | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |

## Real expected-mode results

| Expected mode | Runs | Success | Success rate | Fresh final |
|---|---:|---:|---:|---:|
| OPTIMIZE | 8 | 6 | 75.0% | 75.0% |
| REPAIR | 8 | 7 | 87.5% | 87.5% |
| STRUCTURAL_FIX | 6 | 4 | 66.7% | 66.7% |
| SYNTH_FIX | 6 | 6 | 100.0% | 100.0% |

## Expected-mode diagnostics (all evidence populations)

> This table can mix fixture populations. It is diagnostic only; the REAL table above is the formal result.

| Mode | Runs | Success | Success rate | Patch rejections | Failure stages |
|---|---:|---:|---:|---:|---|
| OPTIMIZE | 8 | 6 | 75.0% | 6 | `{"EXECUTOR":2}` |
| REPAIR | 8 | 7 | 87.5% | 2 | `{"BUDGET":1}` |
| STRUCTURAL_FIX | 6 | 4 | 66.7% | 2 | `{"EXECUTOR":2}` |
| SYNTH_FIX | 6 | 6 | 100.0% | 0 | `{}` |

## Cross-run diagnostics (all evidence, kept separate above)

- Acceleration mean / median / count: `4.489434523809524 / 3.614583333333333 / 4`
- Patch rejection rate: `32.3%`
- Patch rejection reasons: `{"CANDIDATE_CSIM_FAILED":2,"DUPLICATE_STRATEGY_BUNDLE":2,"LATENCY_NOT_STRICTLY_IMPROVED":3,"PATCH_POLICY_REJECTED":3}`
- Failure stages: `{"BUDGET":1,"EXECUTOR":4}`

## Run IDs

- `v3d_fast_001--deepseek-v4-pro--r001--ca18eaf1c00a`
- `v3d_fast_002--deepseek-v4-pro--r001--0ad86551e253`
- `v3d_fast_003--deepseek-v4-pro--r001--7c70797bd7a5`
- `v3d_fast_004--deepseek-v4-pro--r001--ff3141afd3f3`
- `v3d_fast_005--deepseek-v4-pro--r001--6adf55d74a00`
- `v3d_fast_006--deepseek-v4-pro--r001--459ae66da79d`
- `v3d_fast_007--deepseek-v4-pro--r001--18dc26fb52fa`
- `v3d_fast_008--deepseek-v4-pro--r001--0e29dbad4aac`
- `v3d_fast_009--deepseek-v4-pro--r001--0ae06b0e0fc6`
- `v3d_fast_010--deepseek-v4-pro--r001--be6f2f352824`
- `v3d_fast_011--deepseek-v4-pro--r001--a96bdcb51dbc`
- `v3d_fast_012--deepseek-v4-pro--r001--ec8067f015ff`
- `v3d_fast_013--deepseek-v4-pro--r001--15c3a6f1351d`
- `v3d_fast_014--deepseek-v4-pro--r001--4b6c8d51f2fc`
- `v3d_fast_015--deepseek-v4-pro--r001--faf1753952ee`
- `v3d_fast_016--deepseek-v4-pro--r001--4161e1478c6f`
- `v3d_fast_017--deepseek-v4-pro--r001--d81263109247`
- `v3d_fast_018--deepseek-v4-pro--r001--b33a3f7a74c9`
- `v3d_fast_019--deepseek-v4-pro--r001--99777ac3cfa8`
- `v3d_fast_020--deepseek-v4-pro--r001--6b209d29fd4f`
- `v3d_fast_021--deepseek-v4-pro--r001--a5ae903077f2`
- `v3d_fast_022--deepseek-v4-pro--r001--cc0b8e8f21b8`
- `v3d_fast_023--deepseek-v4-pro--r001--a8eab2d286c7`
- `v3d_fast_024--deepseek-v4-pro--r001--b462ade44dde`
- `v3d_fast_025--deepseek-v4-pro--r001--0047ed7af0bb`
- `v3d_fast_026--deepseek-v4-pro--r001--71a72bb086a6`
- `v3d_fast_027--deepseek-v4-pro--r001--fe9fc415631b`
- `v3d_fast_028--deepseek-v4-pro--r001--c7776ed94df9`
