# V3-D 公开 kernel 任务

## 功能语义

对每个 0 <= i < V3D_TRANSACTION_SIZE，必须满足 output[i] = input[i] * 3 + 1；V3D_TRANSACTION_SIZE 固定为 64，合法输入元素范围为 [-1000000, 1000000]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const int input[64], int output[64])`。
- 必须处理并覆盖全部 64 个输出元素。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
