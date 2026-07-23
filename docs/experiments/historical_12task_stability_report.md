# Historical 12-task repeated-pair stability audit

中文版本：`historical_12task_stability_report.zh-CN.md`。

## Scope and evidence boundary

- Source: the content-confirmed 2026-07-22 Token Policy A/B/C batch.
- Design: 12 tasks × 3 conditions × 2 repeats = 72 intended slots.
- This is repeated-pair consistency and pairwise variability only. It is not a significance, convergence, or population-reliability claim.
- Relative deltas use `|r1-r2| / mean(|r1|, |r2|)`.

## Summary

| Metric | Value | Denominator |
|---|---:|---|
| Intended/discovered/eligible slots | 72 / 72 / 72 | 72 slots |
| Missing/duplicate slots | 0 / 0 | 72 slots |
| Valid comparable pairs | 36 | 36 intended pairs |
| Final terminal-status agreement | 0.9722 | 35 / 36 comparable pairs |
| Final CSim agreement | 0.9722 | 35 / 36 comparable pairs |
| Final Synth agreement | 0.9722 | 35 / 36 comparable pairs |
| Final CoSim agreement | 0.9722 | 35 / 36 comparable pairs |
| Stop-reason agreement | 0.9167 | 33 / 36 comparable pairs |
| Failure-class agreement | 0.9444 | 34 / 36 comparable pairs |
| Planner-call agreement | 1.0000 | 36 / 36 pairs |
| Median token absolute/relative delta | 12.5000 / 0.0063 | 36 pairs |
| Maximum token relative delta | 0.0721 | 36 pairs |
| Median Planner-call delta | 0.0000 | 36 pairs |
| Median Tool Credit delta | 0.0000 | 36 pairs |
| Median acceleration absolute/relative delta | 0.0000 / 0.0000 | only pairs with two recorded accelerations |

A pair with missing final-stage evidence is excluded from that field's denominator rather than forced to agree or disagree.

## Pair detail

| Task | Cond. | Terminal | CSim | Synth | CoSim | Stop | Failure | ΔToken | rel ΔToken | ΔPlanner | ΔCredit | ΔAccel | rel ΔAccel |
|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| v3d_fast_001 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 12 | 0.0077 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_001 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 3 | 0.0020 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_001 | C | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 0 | 0.0000 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_004 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 7 | 0.0039 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_004 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 11 | 0.0062 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_004 | C | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 12 | 0.0064 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_005 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 1 | 0.0007 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_005 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 3 | 0.0020 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_005 | C | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 11 | 0.0068 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_009 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 32 | 0.0185 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_009 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 5 | 0.0029 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_009 | C | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 0 | 0.0000 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_012 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 128 | 0.0528 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_012 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 177 | 0.0721 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_012 | C | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 11 | 0.0042 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_013 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 59 | 0.0289 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_013 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 23 | 0.0113 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_013 | C | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 72 | 0.0330 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_015 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 31 | 0.0141 | 0 | 0 | 0.0000 | 0.0000 |
| v3d_fast_015 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 105 | 0.0482 | 0 | 0 | 1.0526 | 0.6897 |
| v3d_fast_015 | C | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 25 | 0.0108 | 0 | 0 | 0.0000 | 0.0000 |
| v3d_fast_016 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 6 | 0.0028 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_016 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 2 | 0.0005 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_016 | C | DIFFER | AGREE | AGREE | AGREE | DIFFER | DIFFER | 26 | 0.0057 | 0 | 21 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_017 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 26 | 0.0114 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_017 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 2 | 0.0009 | 0 | 0 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_017 | C | AGREE | DIFFER | DIFFER | DIFFER | AGREE | DIFFER | 16 | 0.0066 | 0 | 25 | NOT_COMPARABLE | NOT_COMPARABLE |
| v3d_fast_021 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 3 | 0.0015 | 0 | 0 | 0.0000 | 0.0000 |
| v3d_fast_021 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 137 | 0.0330 | 0 | 0 | 0.0000 | 0.0000 |
| v3d_fast_021 | C | AGREE | AGREE | AGREE | AGREE | DIFFER | AGREE | 119 | 0.0256 | 0 | 0 | 1.2500 | 0.7692 |
| v3d_fast_023 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 9 | 0.0037 | 0 | 0 | 0.0000 | 0.0000 |
| v3d_fast_023 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 31 | 0.0060 | 0 | 0 | 28.3462 | 1.4667 |
| v3d_fast_023 | C | AGREE | AGREE | AGREE | AGREE | DIFFER | AGREE | 13 | 0.0051 | 0 | 0 | 0.8108 | 0.5769 |
| v3d_fast_025 | A | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 32 | 0.0131 | 0 | 0 | 0.0000 | 0.0000 |
| v3d_fast_025 | B | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 42 | 0.0172 | 0 | 0 | 0.0000 | 0.0000 |
| v3d_fast_025 | C | AGREE | AGREE | AGREE | AGREE | AGREE | AGREE | 12 | 0.0048 | 0 | 0 | 0.0000 | 0.0000 |

## Continuation comparison with old V3-F R02

| Metric | Old V3-F R02 | This 72-run offline audit |
|---|---:|---:|
| scanned runs | 83 | 72 |
| runs with follow-up | UNKNOWN | 10 |
| follow-up decision points | at least 11 bindable | 10 |
| completely bound samples | 11 | 9 |
| beneficial/essential | 5 | 3 |
| harmful/waste | 6 | 6 |
| excluded | 6 known old exclusions | 1 |
| beneficial retention | 60% | NOT_EVALUATED |
| waste block rate | 16.7% | NOT_EVALUATED |
| admission | NOT_READY | INSUFFICIENT_EVIDENCE |

The old report is preserved unchanged. More rows do not enable `enforce`; current authority remains `SHADOW`.
