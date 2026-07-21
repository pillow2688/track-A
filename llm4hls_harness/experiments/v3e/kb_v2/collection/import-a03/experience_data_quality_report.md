# V3-E Experience Data Quality

Schema: `v3e.experience-data-quality.v1`

Only fixed, public, bounded features and relative artifact hashes are exported. Source, Patch, Prompt, log, credential, local-path, golden and hidden-like contents are not retained.

## Coverage

- Discovered sources: 52
- Imported terminal runs: 26
- Real LLM/Vitis runs: 26
- Fixture runs retained: 0
- Experience records: 28
- Materialized Candidates: 24
- Ranking-eligible records: 24
- UNKNOWN_OR_UNBOUNDED metrics: 0

## Missing fields

- `evidence_features.loop_ii`: 28
- `outcome.latency_after`: 4
- `outcome.latency_before`: 28

## Excluded sources

- `DETERMINISTIC_EXECUTOR_NOT_AGENT_EXPERIENCE`: 26

## Semantics

- Patch-policy rejection has `patch_valid=false`, `candidate_created=false`, and all downstream gates unattempted.
- Patch-policy rejection is retained only in the all-proposals audit; it is excluded from default Candidate statistics and strategy ranking.
- Duplicate-strategy rejection has `patch_valid=true`, `candidate_created=false`, and is excluded from ranking.
- Final validation overlays exploration `NOT_RUN` gates only for the same Candidate.
- Vitis unknown/unbounded latency sentinels become null numeric outcomes and never enter averages; a legitimate zero-cycle combinational latency is kept.
- Fixture provenance is explicit and fixtures are never ranking-eligible.
- Terminal `REAL_VITIS_ATTEMPT_FAILED` runs are retained as genuine negative Agent evidence; `FAILED` is not treated as non-terminal.
