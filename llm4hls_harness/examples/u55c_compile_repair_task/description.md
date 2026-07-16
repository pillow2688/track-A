# U55C compile repair task / U55C 编译修复任务

The baseline intentionally references the undeclared identifier `rhs` inside
the kernel. Repair only `kernel.cpp`, preserve the `vector_add` interface and
implement element-wise vector addition.

baseline 在 kernel 中故意引用未声明的 `rhs`。修复时只能修改 `kernel.cpp`，必须
保留 `vector_add` 接口和逐元素向量加法语义。该任务用于验证真实
`COMPILE_ERROR -> DeepSeek Patch -> csim/synth/cosim PASS` 闭环。
