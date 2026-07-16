# U55C vector-add smoke task

This public task validates the local Vitis 2025.2 HLS flow using the Alveo U55C
part `xcu55c-fsvh2892-2L-e` at a 10 ns target clock (100 MHz minimum).

The kernel must compute `c[i] = a[i] + b[i]` for 16 signed 32-bit elements.
The public testbench checks every output element and returns a non-zero exit
code on mismatch.

这是环境验证任务，不代表比赛任务或最终性能结果。
