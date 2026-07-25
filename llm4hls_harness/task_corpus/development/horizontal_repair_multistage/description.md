# Public multi-stage repair Anchor

For every element:

- if `input[i] < 0`, `output[i]` must equal `input[i] * 2 + 1`;
- otherwise, `output[i]` must equal `input[i] * 3 + 5`.

The top interface and all public files except `kernel.cpp` are read-only.
The implementation must be synthesizable and correct for every signed 16-bit
input, not only the examples in the public testbench.

这是一个完全公开的多阶段功能修复 Anchor。负数分支必须计算
`2*x+1`，非负数分支必须计算 `3*x+5`。只能修改 `kernel.cpp`，并保持
顶层接口不变。

