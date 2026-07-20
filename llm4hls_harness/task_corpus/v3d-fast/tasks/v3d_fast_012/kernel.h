#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_MATMUL_DIM = 4;

void kernel(
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM]);

#endif
