# V3-D 公开 kernel 任务

## 功能语义

计算因果三抽头一维卷积，系数依次为 [1, 2, 1]，数组左侧按零填充；即 output[i] = input[i] + 2*input[i-1] + input[i-2]，负下标项取 0。V3D_FIR_SIZE 固定为 16，合法输入元素范围为 [-1000000, 1000000]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const int input[16], int output[16])`。
- 必须处理全部 16 个输出，并按公开零填充规则处理左边界。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
