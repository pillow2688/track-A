# V3-D Batch Benchmark Report

- Benchmark fingerprint: `fe0fe53beda4418b41a5727d950887a03322cfa8bb6861c90a4cddc105359367`
- Selected tasks / planned runs: `2 / 2`
- Latest slots / all attempts / new / resumed: `2 / 2 / 2 / 0`
- Stop reason: `COMPLETED`

> Evidence rule: DEMO and DETERMINISTIC rows are fixture evidence only. They are never included in the real-evidence headline below.

## Real Vitis evidence headline (all current-plan attempts)

> Retries remain in this population, so a later success cannot hide an earlier failed attempt. Latest-slot rows are used only for completion.

| Runs | E2E success | Fresh final | Router accuracy |
|---:|---:|---:|---:|
| 2 | 100.0% | 100.0% | 100.0% |

## Latest-slot completion population

| Slots | E2E | Fresh final | Real slots | Real E2E |
|---:|---:|---:|---:|---:|
| 2 | 100.0% | 100.0% | 2 | 100.0% |

## Evidence populations

| Evidence | Runs | E2E | Fresh final | Credits | Tokens | Calls C/S/Co/L | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| REAL | 2 | 100.0% | 100.0% | 26.0 | 3185.0 | 6/5/0/2 | 89.4674532902427 |
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

- `horizontal_repair_multistage--deepseek-v4-pro--r001--347f9939c539`
- `horizontal_synth_multistage--deepseek-v4-pro--r001--301b0ace0213`
