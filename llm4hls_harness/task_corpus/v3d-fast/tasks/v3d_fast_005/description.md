# V3-D 公开 kernel 任务

## 功能语义

对每个 0 <= i < V3D_PREFIX_SIZE，output[i] 必须等于 input[0] 到 input[i] 的包含当前元素的前缀和；V3D_PREFIX_SIZE 固定为 12，合法输入元素范围为 [-1000000, 1000000]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const int input[12], int output[12])`。
- 必须生成包含当前元素的全部 12 个前缀结果。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
