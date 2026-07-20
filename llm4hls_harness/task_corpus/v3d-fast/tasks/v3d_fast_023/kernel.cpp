#include "kernel.h"

int kernel(
    const int lhs[V3D_REDUCTION_SIZE],
    const int rhs[V3D_REDUCTION_SIZE]) {
    int v3d_serial_sum_193_7fd1 = 0;
    for (int i = 0; i < V3D_REDUCTION_SIZE; ++i) {
        v3d_serial_sum_193_7fd1 += lhs[i] * rhs[i];
    }
    return v3d_serial_sum_193_7fd1;
}
