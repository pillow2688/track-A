# V3-D 公开 kernel 任务

## 功能语义

把长度 64 的输入按连续四元素分组；对每个 0 <= group < 16，output[group] 必须等于 input[4*group] 到 input[4*group+3] 的总和。合法输入元素范围为 [-1000000, 1000000]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const int input[64], int output[16])`。
- 必须处理全部 16 组输入并覆盖全部输出元素。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
