# U55C vector-add optimization task

Optimize the supplied, functionally correct `vector_add` HLS kernel for the AMD
Alveo U55C target. Preserve the public C interface and vector-add semantics for
all 256 elements. A valid Candidate must pass C simulation, synthesis, C/RTL
co-simulation, the 10 ns clock constraint, and configured resource limits.

Each optimization round may use exactly one declared optimization class. The
workflow compares only verified Candidates and retains the best Candidate under
the configured latency/II-first PPA policy and audited cost tie-breakers.
