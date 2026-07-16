# U55C repair task

This public task is an end-to-end V1 repair fixture. The baseline kernel
intentionally subtracts `b[i]`; the public testbench expects vector addition.
The repair provider must submit a minimal unified diff that changes only
`kernel.cpp`.

目标 part 是 `xcu55c-fsvh2892-2L-e`，目标周期为 10 ns（100 MHz）。该任务仅用于
验证 V1 的诊断、补丁校验、候选隔离和回滚/晋级逻辑。
