# Aborted audit batch

This batch is excluded from Experience and Ranker evaluation.

The audit runner supplied a relative `ToolServer.run_root`. Vitis generated the
CSim executable successfully, but the backend then resolved that relative path
again from the action working directory. All six executions therefore returned
shell status 127 (`csim.exe: No such file or directory`). These are runner-path
failures, not Candidate validation labels.

The corrected run uses an absolute run root and a separate
`fresh-candidate-audits-rerun01` directory so the failed action cache cannot be
reused.
