# V3-D 公开 kernel 任务

## 功能语义

返回两路长度为 V3D_DOT_SIZE 的整数向量点积，即 lhs[i] * rhs[i] 在全部 i 上的总和；V3D_DOT_SIZE 固定为 32，合法输入元素范围为 [-10000, 10000]。

## 接口与约束

- 顶层函数必须保持为 `int kernel(const int lhs[32], const int rhs[32])`。
- 必须归约全部 32 对输入元素并返回一个整数结果。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
