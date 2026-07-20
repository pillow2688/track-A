#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_DOT_SIZE = 32;

int kernel(
    const int lhs[V3D_DOT_SIZE],
    const int rhs[V3D_DOT_SIZE]);

#endif
