# V3-D Batch Benchmark Report

- Benchmark fingerprint: `6b152dc9e3373639b23d665077cc4f05bd59f0438b826b5df2365dc0640d33d0`
- Selected tasks / planned runs: `4 / 4`
- Latest slots / all attempts / new / resumed: `4 / 4 / 4 / 0`
- Stop reason: `COMPLETED`

> Evidence rule: DEMO and DETERMINISTIC rows are fixture evidence only. They are never included in the real-evidence headline below.

## Real Vitis evidence headline (all current-plan attempts)

> Retries remain in this population, so a later success cannot hide an earlier failed attempt. Latest-slot rows are used only for completion.

| Runs | E2E success | Fresh final | Router accuracy |
|---:|---:|---:|---:|
| 4 | 75.0% | 75.0% | 66.7% |

## Latest-slot completion population

| Slots | E2E | Fresh final | Real slots | Real E2E |
|---:|---:|---:|---:|---:|
| 4 | 75.0% | 75.0% | 4 | 75.0% |

## Evidence populations

| Evidence | Runs | E2E | Fresh final | Credits | Tokens | Calls C/S/Co/L | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| REAL | 4 | 75.0% | 75.0% | 146.0 | 8698.0 | 10/9/5/4 | 318.93908202601597 |
| DETERMINISTIC | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |
| DEMO | 0 | - | - | 0.0 | 0.0 | 0/0/0/0 | 0.0 |

## Real expected-mode results

| Expected mode | Runs | Success | Success rate | Fresh final |
|---|---:|---:|---:|---:|
| OPTIMIZE | 1 | 1 | 100.0% | 100.0% |
| REPAIR | 1 | 0 | 0.0% | 0.0% |
| STRUCTURAL_FIX | 1 | 1 | 100.0% | 100.0% |
| SYNTH_FIX | 0 | 0 | - | - |

## Expected-mode diagnostics (all evidence populations)

> This table can mix fixture populations. It is diagnostic only; the REAL table above is the formal result.

| Mode | Runs | Success | Success rate | Patch rejections | Failure stages |
|---|---:|---:|---:|---:|---|
| OPTIMIZE | 1 | 1 | 100.0% | 1 | `{}` |
| REPAIR | 1 | 0 | 0.0% | 0 | `{"UNKNOWN":1}` |
| STRUCTURAL_FIX | 1 | 1 | 100.0% | 0 | `{}` |
| SYNTH_FIX | 0 | 0 | - | 0 | `{}` |

## Cross-run diagnostics (all evidence, kept separate above)

- Acceleration mean / median / count: `1.9826254826254825 / 1.9826254826254825 / 1`
- Patch rejection rate: `25.0%`
- Patch rejection reasons: `{"LATENCY_NOT_STRICTLY_IMPROVED":1}`
- Failure stages: `{"UNKNOWN":1}`

## Run IDs

- `dotProduct_optimize--deepseek-v4-pro--r001--b56d8231b80c`
- `projection_bugfix--deepseek-v4-pro--r001--8ce61f731baa`
- `residual_stream_deadlock--deepseek-v4-pro--r001--a9bdbeb8608e`
- `u55c_synthesis_repair--deepseek-v4-pro--r001--7ed9094f406d`
