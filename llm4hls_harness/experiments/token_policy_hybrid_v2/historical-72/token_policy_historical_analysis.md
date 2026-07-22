# Token Policy Historical Analysis (72 Real Runs)

This is an offline analysis of reconciled DeepSeek + Vitis receipts; it made no external model or Vitis calls.

| Mode | Runs | Success | Successful call P95 | Stable cap | Planner calls mean | Second-call rate | Second-call improvement | Acceleration median/geomean |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| REPAIR | 18 | 1.0 | 459.15 | 544 | 1.0 | 0.0 | None | None/None |
| SYNTH_FIX | 18 | 1.0 | 829.2499999999999 | 960 | 1.0 | 0.0 | None | None/None |
| STRUCTURAL_FIX | 18 | 0.6111111111111112 | 462.0 | 544 | 1.2222222222222223 | 0.2222222222222222 | 0.0 | 1.5263157894736843/1.4327007988227578 |
| OPTIMIZE | 18 | 1.0 | 796.65 | 928 | 1.3333333333333333 | 0.3333333333333333 | 0.5 | 1.8108108108108107/1.8587942179668822 |

## Cap rule

Each mode cap is the successful real Planner-call output P95 plus a 15% safety margin, rounded upward to 16 tokens. It is a configurable starting point, not a claim of optimality.

## Second-call interpretation

A second call has new evidence only when its structured round input contains a new failure record or a Candidate history entry bound to metrics/synthesis evidence. Improvement requires a round-2-or-later Candidate to be promoted or final-verified.
