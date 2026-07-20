# V3-D 公开 kernel 任务

## 功能语义

计算两个 4x4 整数矩阵的乘积；对每个 row、col，output[row][col] 必须等于 lhs[row][k] * rhs[k][col] 在 k=0..3 上的总和。合法输入元素范围为 [-10000, 10000]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const int lhs[4][4], const int rhs[4][4], int output[4][4])`。
- 必须写出全部 16 个矩阵元素，矩阵维度固定为 4。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
