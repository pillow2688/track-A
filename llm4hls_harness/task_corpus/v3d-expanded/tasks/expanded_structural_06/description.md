# expanded_structural_06

公开扩展题。目标：修复或优化 `kernel.cpp`，使公开 TB 通过。

- Mode：STRUCTURAL_FIX
- 设计根因：反馈 seed 在反馈环能够消费前写满深度为 1 的 FIFO。
- 预期初始失败：COSIM_FAIL
- 验证：CSim、Synth、CoSim、独立最终认证和 100 MHz Gate。
