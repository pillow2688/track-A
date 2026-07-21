# V3-E Experience Data Quality

Schema: `v3e.experience-data-quality.v1`

Only fixed, public, bounded features and relative artifact hashes are exported. Source, Patch, Prompt, log, credential, local-path, golden and hidden-like contents are not retained.

## Coverage

- Discovered sources: 335
- Imported terminal runs: 20
- Real LLM/Vitis runs: 20
- Fixture runs retained: 0
- Experience records: 26
- Materialized Candidates: 18
- Ranking-eligible records: 18
- UNKNOWN_OR_UNBOUNDED metrics: 2

## Missing fields

- `evidence_features.failure_type`: 11
- `evidence_features.loop_ii`: 11
- `outcome.latency_after`: 9
- `outcome.latency_before`: 11

## Excluded sources

- `DETERMINISTIC_EXECUTOR_NOT_AGENT_EXPERIENCE`: 153
- `FIXTURE_EXCLUDED_BY_POLICY`: 11
- `ORACLE_GOLDEN_NOT_AGENT_EXPERIENCE`: 151

## Semantics

- Patch-policy rejection has `patch_valid=false`, `candidate_created=false`, and all downstream gates unattempted.
- Patch-policy rejection is retained only in the all-proposals audit; it is excluded from default Candidate statistics and strategy ranking.
- Duplicate-strategy rejection has `patch_valid=true`, `candidate_created=false`, and is excluded from ranking.
- Final validation overlays exploration `NOT_RUN` gates only for the same Candidate.
- Vitis unknown/unbounded latency sentinels become null numeric outcomes and never enter averages; a legitimate zero-cycle combinational latency is kept.
- Fixture provenance is explicit and fixtures are never ranking-eligible.
- Terminal `REAL_VITIS_ATTEMPT_FAILED` runs are retained as genuine negative Agent evidence; `FAILED` is not treated as non-terminal.
