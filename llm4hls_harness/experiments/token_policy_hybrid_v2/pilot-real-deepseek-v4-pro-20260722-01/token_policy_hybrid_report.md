# Token Policy Hybrid V2 A/D Real Evaluation

Hybrid Token Policy did not pass engineering admission; the 28-task stage must not run.

- Runs: 48/48
- Engineering admission: NOT_PASSED
- Supports statistical conclusion: False

| Condition | Success | Avg tokens | Tokens/success | Input | Output | Planner calls | Acceleration median/geomean |
|---|---:|---:|---:|---:|---:|---:|---:|
| A_FIXED | 0.9166666666666666 | 2044.0416666666667 | 2229.8636363636365 | 1565.25 | 478.7916666666667 | 1.0 | 1.0144927536231885/1.288374859431098 |
| D_HYBRID | 0.875 | 2714.8333333333335 | 3102.6666666666665 | 2056.7083333333335 | 658.125 | 1.25 | 1.0/1.332317307443563 |

## Admission checks

- final_success_drop_within_5pp: `True`
- average_tokens_not_increased: `False`
- average_token_reduction_at_least_10pct: `False`
- tokens_per_success_not_increased: `False`
- planner_calls_not_increased: `False`
- patch_invalid_zero: `False`
- truncation_zero: `True`
- budget_compliance_100pct: `True`
- optimize_acceleration_median_preserved: `True`
- optimize_acceleration_geomean_preserved: `True`
