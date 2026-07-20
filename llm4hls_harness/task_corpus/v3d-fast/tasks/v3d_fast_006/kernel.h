#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_HIST_INPUT_SIZE = 16;
constexpr int V3D_HIST_BINS = 8;

void kernel(
    const unsigned char input[V3D_HIST_INPUT_SIZE],
    unsigned short bins[V3D_HIST_BINS]);

#endif
