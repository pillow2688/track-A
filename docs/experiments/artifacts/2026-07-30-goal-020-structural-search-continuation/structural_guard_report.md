# Candidate Structural Guard 报告

Guard 在统一 diff 已安全应用、immutable Candidate 已产生后、CSim/Synth/CoSim
之前执行。它只在已存在 `RTL_LIVENESS_STREAM_TOPOLOGY` obligation 时启用，检查
implicated stream 的生产者、消费者、process dependency SCC、依赖环、可证明初始
令牌和父/子拓扑差。

拒绝统一使用 `STRUCTURAL_OBLIGATION_UNSATISFIED`，并至少包含以下稳定原因之一：

- `DEPTH_ONLY_CHANGE` / `NO_TOPOLOGY_PROGRESS`；
- `MULTI_PRODUCER_REMAINS`；
- `RESIDUAL_UNINITIALIZED_CYCLE`。

兼容性原因码也被保留，以便旧 evidence reader 仍能理解历史 Artifact。Guard 拒绝
不启动 Candidate CSim/Synth/CoSim，不产生相应工具 Credits；但 Candidate、Patch、
parent、guard artifact 和 action family 都会持久化。它不是 DATAFLOW 禁令：静态可见
的“同一 stream 先 write 再 read”初始令牌，或无环的单向 SPSC，均可通过。
