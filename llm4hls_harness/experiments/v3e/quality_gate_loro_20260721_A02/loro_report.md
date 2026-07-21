# V3-E Guidance Leave-One-Run-Out 评估

- Experience records: 18
- Eligible real train Candidates: 18
- Guided decision: `SKIP_GUIDED`

| 指标 | 结果 |
|---|---:|
| `coverage` | 0.222222 |
| `success_strategy_hit_rate` | 0.25 |
| `harmful_recommendation_rate` | 0.0 |
| `abstain_rate` | 0.777778 |
| `duplicate_failure_suppression_rate` | 1.0 |
| `average_guidance_tokens` | 108.166667 |

## 按 mode

| Mode | Evaluated | Coverage | Hit | Harmful | Abstain |
|---|---:|---:|---:|---:|---:|
| REPAIR | 4 | 0.0 | 0.0 | None | 1.0 |
| SYNTH_FIX | 4 | 0.0 | 0.0 | None | 1.0 |
| STRUCTURAL_FIX | 4 | 1.0 | 1.0 | None | 0.0 |
| OPTIMIZE | 6 | 0.0 | 0.0 | 0.0 | 1.0 |

## Guided gate

- INSUFFICIENT_HIGH_CONFIDENCE_INJECTIONS
