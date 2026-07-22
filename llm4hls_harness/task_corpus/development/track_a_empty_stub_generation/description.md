# 公开开发任务：空 Stub 矩阵变换生成

这是一个 **development-only** 的 Generation smoke，不是官方 benchmark，也不代表官方题目难度分布。

初始 `kernel.cpp` 是一个合法但未实现的 TODO stub。请生成完整、可综合的主算法，且只能修改 `kernel.cpp`。不得修改 header、testbench、task metadata 或顶层函数接口。

## 公开函数契约

对每个 `i,j`（范围均为 `0..MATRIX_DIM-1`），先计算：

```text
raw = bias[j] + sum(lhs[i][k] * rhs[k][j])，其中 k = 0..MATRIX_DIM-1
```

再将 `raw` 饱和到 `[MATRIX_CLAMP_MIN, MATRIX_CLAMP_MAX]`，写入 `output[i][j]`。

最后，`row_sums[i]` 必须等于该行所有 **已经饱和后的** `output[i][j]` 之和。

输入元素范围为 `[-64, 64]`，bias 范围为 `[-128, 128]`。实现应使用足够宽的中间累加变量，并保持 `matrix_transform` 的函数名、参数、返回类型和数组形状不变。为使饱和语义和矩阵乘法分阶段清楚，请实现两个私有 helper：一个 `clamp_value` 负责饱和，另一个 `compute_raw_cell` 负责单个 `(row, column)` 的内层乘加归约；然后由顶层函数实现输出矩阵和 `row_sums`。不要保留 TODO、空函数或固定输出。
