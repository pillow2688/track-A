# expanded_structural_04

公开扩展题。目标：修复或优化 `kernel.cpp`，使公开 TB 通过。

- Mode：STRUCTURAL_FIX
- 设计根因：生产端先写入一对 `main` token，消费者却先等待 `side` token；深度为 1 的 FIFO 在 RTL DATAFLOW 调度中形成交叉等待。
- 预期初始失败：COSIM_FAIL
- 验证：CSim、Synth、CoSim、独立最终认证和 100 MHz Gate。
