# expanded_structural_05

公开扩展题。目标：修复或优化 `kernel.cpp`，使公开 TB 通过。

- Mode：REPAIR
- 设计根因：side channel 每轮写入额外 token，消费者只读取一个 token，导致从第二个元素起与公开规格的计算结果不一致。
- 预期初始失败：CSIM_FAIL
- 验证：CSim、Synth、CoSim、独立最终认证和 100 MHz Gate。
