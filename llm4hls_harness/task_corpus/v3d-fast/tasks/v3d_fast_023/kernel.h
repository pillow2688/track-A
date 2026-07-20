#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_REDUCTION_SIZE = 64;

int kernel(
    const int lhs[V3D_REDUCTION_SIZE],
    const int rhs[V3D_REDUCTION_SIZE]);

#endif
