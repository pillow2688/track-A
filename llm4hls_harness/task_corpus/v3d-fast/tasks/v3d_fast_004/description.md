# V3-D 公开 kernel 任务

## 功能语义

对每个 0 <= i < V3D_POINTS，输出二维点必须满足 output[i].x = input[i].x 且 output[i].y = input[i].y；V3D_POINTS 固定为 8，z 坐标不参与结果，合法输入坐标范围为 [-1000000, 1000000]。

## 接口与约束

- 顶层函数必须保持为 `kernel(const V3DPoint3D input[8], V3DPoint2D output[8])`。
- 必须处理全部 8 个点；不得改变点的顺序或读写数组边界之外的数据。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
