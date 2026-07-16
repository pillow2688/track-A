# U55C synthesis repair task / U55C 综合修复任务

The software behavior is correct, but the baseline uses dynamic allocation in
the synthesizable kernel. Replace it with an equivalent fixed-size local
storage implementation. Modify only `kernel.cpp` and preserve the interface.

baseline 的软件执行结果正确，但在可综合 kernel 中使用了动态内存分配。请改为
等价的固定大小局部存储，只修改 `kernel.cpp` 并保持接口不变。该 fixture 只有在
真实 Vitis 2025.2 preflight 证明“baseline csim PASS、synth `synth_error`”后，
才能计入 `SYNTHESIS_ERROR` 验收。
