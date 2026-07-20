# V3-D 公开 kernel 任务

## 功能语义

对每个 0 <= i < V3D_SIZE，必须满足 output[i] = input[i] * 3 + 7；V3D_SIZE 固定为 16，合法输入元素范围为 [-1000000, 1000000]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const int input[16], int output[16])`。
- 必须处理全部 16 个元素，不得越界访问输入或输出数组。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
