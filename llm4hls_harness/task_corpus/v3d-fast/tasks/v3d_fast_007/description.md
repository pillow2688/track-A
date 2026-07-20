# V3-D 公开 kernel 任务

## 功能语义

对每个 0 <= i < V3D_VECTOR_SIZE，必须满足 output[i] = lhs[i] + rhs[i]；V3D_VECTOR_SIZE 固定为 16，两路合法输入元素范围均为 [-1000000, 1000000]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const int lhs[16], const int rhs[16], int output[16])`。
- 必须以整数精度处理全部 16 个元素，不得缩窄中间结果。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
