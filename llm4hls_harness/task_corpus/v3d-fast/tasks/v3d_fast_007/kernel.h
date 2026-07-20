#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_VECTOR_SIZE = 16;

void kernel(
    const int lhs[V3D_VECTOR_SIZE],
    const int rhs[V3D_VECTOR_SIZE],
    int output[V3D_VECTOR_SIZE]);

#endif
