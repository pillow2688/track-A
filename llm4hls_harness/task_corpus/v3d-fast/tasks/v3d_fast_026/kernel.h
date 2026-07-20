#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_BANK_INPUT_SIZE = 64;
constexpr int V3D_BANK_OUTPUT_SIZE = 16;

void kernel(
    const int input[V3D_BANK_INPUT_SIZE],
    int output[V3D_BANK_OUTPUT_SIZE]);

#endif
