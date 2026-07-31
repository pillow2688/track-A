# expanded_structural_03

公开扩展题。目标：修复或优化 `kernel.cpp`，使公开 TB 通过。

- Mode：STRUCTURAL_FIX
- 设计根因：分段 burst 生产与锁步消费在深度为 1 的 FIFO 上产生背压停滞。
- 预期初始失败：COSIM_FAIL
- 验证：CSim、Synth、CoSim、独立最终认证和 100 MHz Gate。
