#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_STENCIL_MAX_ROWS = 6;
constexpr int V3D_STENCIL_MAX_COLS = 6;

void kernel(
    const int input[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int output[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int rows,
    int cols);

#endif
