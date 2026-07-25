# V3-D Batch Benchmark Report

- Benchmark fingerprint: `73114218050a06503120ed3f0d5005bb3cbf6139a0c3dd077a7feac327440010`
- Selected tasks / planned runs: `1 / 1`
- Latest slots / all attempts / new / resumed: `1 / 1 / 1 / 0`
- Stop reason: `COMPLETED`

> Evidence rule: DEMO and DETERMINISTIC rows are fixture evidence only. They are never included in the real-evidence headline below.

## Real Vitis evidence headline (all current-plan attempts)

> Retries remain in this population, so a later success cannot hide an earlier failed attempt. Latest-slot rows are used only for completion.

| Runs | E2E success | Fresh final | Router accuracy |
|---:|---:|---:|---:|
| 1 | 0.0% | 0.0% | - |

## Latest-slot completion population

| Slots | E2E | Fresh final | Real slots | Real E2E |
|---:|---:|---:|---:|---:|
| 1 | 0.0% | 0.0% | 1 | 0.0% |

## Evidence populations

| Evidence | Runs | E2E | Fresh final | Credits | Tokens | Calls C/S/Co/L | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| REAL | 1 | 0.0% | 0.0% | 35.0 | 2117.0 | 3/3/1/1 | 72.88111386704259 |
| DETERMINISTIC | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |
| DEMO | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |

## Real expected-mode results

| Expected mode | Runs | Success | Success rate | Fresh final |
|---|---:|---:|---:|---:|
| OPTIMIZE | 0 | 0 | - | - |
| REPAIR | 0 | 0 | - | - |
| STRUCTURAL_FIX | 0 | 0 | - | - |
| SYNTH_FIX | 0 | 0 | - | - |

## Expected-mode diagnostics (all evidence populations)

> This table can mix fixture populations. It is diagnostic only; the REAL table above is the formal result.

| Mode | Runs | Success | Success rate | Patch rejections | Failure stages |
|---|---:|---:|---:|---:|---|
| OPTIMIZE | 0 | 0 | - | 0 | `{}` |
| REPAIR | 0 | 0 | - | 0 | `{}` |
| STRUCTURAL_FIX | 0 | 0 | - | 0 | `{}` |
| SYNTH_FIX | 0 | 0 | - | 0 | `{}` |

## Cross-run diagnostics (all evidence, kept separate above)

- Acceleration mean / median / count: `None / None / 0`
- Patch rejection rate: `0.0%`
- Patch rejection reasons: `{}`
- Failure stages: `{"FINAL_COSIM":1}`

## Run IDs

- `horizontal_synth_multistage--deepseek-v4-pro--r001--724f5431691e`
