# V3-D 公开 kernel 任务

## 功能语义

先清零全部输出 bin，再对每个 0 <= b < V3D_HIST_BINS 令 bins[b] 等于输入中数值 b 的出现次数；V3D_HIST_INPUT_SIZE 固定为 16，V3D_HIST_BINS 固定为 8，合法输入元素范围为 [0, 7]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const unsigned char input[16], unsigned short bins[8])`。
- 每次调用都必须覆盖全部 8 个 bin，不能依赖输出数组的初值。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
