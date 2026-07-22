# Token Policy Dynamic A/B/C Real Evaluation

Controlled real-provider A/B/C Pilot runs completed, but engineering admission was not met. No claim is made about token-efficiency or competition-score improvement.

- Planned runs: 72
- Actual runs: 72
- Missing runs: 0
- Engineering admission: NOT_PASSED
- Statistical conclusion supported: False

| Condition | Runs | Final success | Avg tokens | Tokens/success | Truncation | Patch valid | Budget compliant |
|---|---:|---:|---:|---:|---:|---:|---:|
| A_FIXED | 24 | 0.9166666666666666 | 2047.75 | 2233.909090909091 | 0.0 | 1.0 | 1.0 |
| B_DYNAMIC_HARD | 24 | 0.9166666666666666 | 2634.0416666666665 | 2873.5 | 0.0 | 1.0 | 1.0 |
| C_DYNAMIC_VISIBLE | 24 | 0.875 | 2571.375 | 2938.714285714286 | 0.0 | 0.9791666666666666 | 1.0 |

Full paired statistics and per-mode results are in `token_policy_abc_summary.json`.
