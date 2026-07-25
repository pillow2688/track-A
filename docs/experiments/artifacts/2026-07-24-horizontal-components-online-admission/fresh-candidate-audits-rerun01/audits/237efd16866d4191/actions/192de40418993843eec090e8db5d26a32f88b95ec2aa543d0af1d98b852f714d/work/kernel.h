#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_DATAFLOW_SIZE = 32;

void kernel(
    const int input[V3D_DATAFLOW_SIZE],
    int output[V3D_DATAFLOW_SIZE]);

#endif
