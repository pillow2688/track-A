# Public multi-stage synthesis/RTL Anchor

For every `0 <= i < HORIZONTAL_SYNTH_SIZE`, the kernel must compute
`output[i] = input[i] * 2 + 1`.

The baseline is C-simulation correct. Make the implementation synthesizable
and preserve the stated behavior through RTL co-simulation. Only `kernel.cpp`
may change and the top interface must remain unchanged.

这是一个完全公开的分阶段综合与 RTL Anchor。baseline 的 CSim 行为正确；
修改后必须能够综合，并通过 RTL CoSim，同时保持 `2*x+1` 的公开语义。
