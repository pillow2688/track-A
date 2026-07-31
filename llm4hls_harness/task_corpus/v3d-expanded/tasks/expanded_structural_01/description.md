# expanded_structural_01

公开扩展题。目标：修复或优化 `kernel.cpp`，使公开 TB 通过。

- Mode：STRUCTURAL_FIX
- 设计根因：生产端先填满有界 main FIFO，再发出配对 side token，导致 RTL 数据流停滞。
- 预期初始失败：COSIM_FAIL
- 验证：CSim、Synth、CoSim、独立最终认证和 100 MHz Gate。
