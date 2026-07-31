# expanded_synth_03

公开扩展题。目标：修复或优化 `kernel.cpp`，使公开 TB 通过。

- Mode：SYNTH_FIX
- 设计根因：dynamic STL container in kernel
- 预期初始失败：SYNTH_FAIL
- 验证：CSim、Synth、独立最终认证和 100 MHz Gate。
