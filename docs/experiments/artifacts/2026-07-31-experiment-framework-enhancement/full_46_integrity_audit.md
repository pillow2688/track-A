# RC2 46-task FULL_REF integrity audit

- Campaign: `/home/ying/CompetitionTrackA/track-A-rc1/llm4hls_harness/runs/rc2-full-ref-46-20260731T063617Z`
- Result count / unique task IDs: 46 / 46
- Batch terminal marker: `FULL46_EXIT_CODE=0`
- Frozen HEAD: `6f1e31a86159dd26fa8f7c161b77e3aaaa25a0b4`; current HEAD: `6f1e31a86159dd26fa8f7c161b77e3aaaa25a0b4`.
- Frozen aggregate runtime fingerprint: `39e1e925a2b0a15809a8b342a2cde177dd779d2bba62bdcebbaaf3a51f4e2435`.
- Per-run executor fingerprint(s): `ffb0f18bd71d5339359d762ad60da22dd5ff6f51a48be92c309f1a8fe498a080`.
- The freeze aggregate fingerprint and the per-run executor fingerprint are different layers of evidence and are therefore recorded separately; this audit does not falsely compare them as identical strings.
- Package + certification receipt + B2 Ledger-unchanged evidence: 46/46.
- `git diff --check`: PASS.
- Audit status: **PASS**.

## Findings

- No integrity failure was found in the completed campaign artifacts.
- Existing dirty-worktree entries were present at freeze and are documented by the freeze receipt; this report generator does not modify product runtime logic or historical run artifacts.
