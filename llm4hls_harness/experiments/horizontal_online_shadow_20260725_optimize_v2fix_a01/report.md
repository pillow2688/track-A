# V3-D Batch Benchmark Report

- Benchmark fingerprint: `38b1f6f38456b35b69418bcd0a2c18320a28e1c3ab80f9236634fb115337bf82`
- Selected tasks / planned runs: `1 / 1`
- Latest slots / all attempts / new / resumed: `1 / 1 / 1 / 0`
- Stop reason: `COMPLETED`

> Evidence rule: DEMO and DETERMINISTIC rows are fixture evidence only. They are never included in the real-evidence headline below.

## Real Vitis evidence headline (all current-plan attempts)

> Retries remain in this population, so a later success cannot hide an earlier failed attempt. Latest-slot rows are used only for completion.

| Runs | E2E success | Fresh final | Router accuracy |
|---:|---:|---:|---:|
| 1 | 100.0% | 100.0% | 100.0% |

## Latest-slot completion population

| Slots | E2E | Fresh final | Real slots | Real E2E |
|---:|---:|---:|---:|---:|
| 1 | 100.0% | 100.0% | 1 | 100.0% |

## Evidence populations

| Evidence | Runs | E2E | Fresh final | Credits | Tokens | Calls C/S/Co/L | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| REAL | 1 | 100.0% | 100.0% | 40.0 | 5121.0 | 4/4/1/2 | 132.9901170351077 |
| DETERMINISTIC | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |
| DEMO | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |

## Real expected-mode results

| Expected mode | Runs | Success | Success rate | Fresh final |
|---|---:|---:|---:|---:|
| OPTIMIZE | 1 | 1 | 100.0% | 100.0% |
| REPAIR | 0 | 0 | - | - |
| STRUCTURAL_FIX | 0 | 0 | - | - |
| SYNTH_FIX | 0 | 0 | - | - |

## Expected-mode diagnostics (all evidence populations)

> This table can mix fixture populations. It is diagnostic only; the REAL table above is the formal result.

| Mode | Runs | Success | Success rate | Patch rejections | Failure stages |
|---|---:|---:|---:|---:|---|
| OPTIMIZE | 1 | 1 | 100.0% | 0 | `{}` |
| REPAIR | 0 | 0 | - | 0 | `{}` |
| STRUCTURAL_FIX | 0 | 0 | - | 0 | `{}` |
| SYNTH_FIX | 0 | 0 | - | 0 | `{}` |

## Cross-run diagnostics (all evidence, kept separate above)

- Acceleration mean / median / count: `27.026315789473685 / 27.026315789473685 / 1`
- Patch rejection rate: `0.0%`
- Patch rejection reasons: `{}`
- Failure stages: `{}`

## Run IDs

- `dotProduct_optimize--deepseek-v4-pro--r001--a09ed30eb98c`
