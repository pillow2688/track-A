# V3-D Batch Benchmark Report

- Benchmark fingerprint: `45b66ae4569b67a67679641ecfac7eb9d2b7040ef7c08efc8cb4a101c6004688`
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
| REAL | 1 | 100.0% | 100.0% | 11.0 | 2040.0 | 3/2/0/1 | 45.672649193089455 |
| DETERMINISTIC | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |
| DEMO | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |

## Real expected-mode results

| Expected mode | Runs | Success | Success rate | Fresh final |
|---|---:|---:|---:|---:|
| OPTIMIZE | 0 | 0 | - | - |
| REPAIR | 1 | 1 | 100.0% | 100.0% |
| STRUCTURAL_FIX | 0 | 0 | - | - |
| SYNTH_FIX | 0 | 0 | - | - |

## Expected-mode diagnostics (all evidence populations)

> This table can mix fixture populations. It is diagnostic only; the REAL table above is the formal result.

| Mode | Runs | Success | Success rate | Patch rejections | Failure stages |
|---|---:|---:|---:|---:|---|
| OPTIMIZE | 0 | 0 | - | 0 | `{}` |
| REPAIR | 1 | 1 | 100.0% | 0 | `{}` |
| STRUCTURAL_FIX | 0 | 0 | - | 0 | `{}` |
| SYNTH_FIX | 0 | 0 | - | 0 | `{}` |

## Cross-run diagnostics (all evidence, kept separate above)

- Acceleration mean / median / count: `None / None / 0`
- Patch rejection rate: `0.0%`
- Patch rejection reasons: `{}`
- Failure stages: `{}`

## Run IDs

- `projection_bugfix--deepseek-v4-pro--r001--ffca7bc73ef7`
