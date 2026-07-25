#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_TRANSACTION_SIZE = 64;

void kernel(
    const int input[V3D_TRANSACTION_SIZE],
    int output[V3D_TRANSACTION_SIZE]);

#endif
