#include "kernel.h"

void kernel(
    const int input[V3D_PREFIX_SIZE],
    int output[V3D_PREFIX_SIZE]) {
    int v3d_running_69_b2bf = 0;
    for (int i = 0; i < V3D_PREFIX_SIZE; ++i) {
        output[i] = v3d_running_69_b2bf;
        v3d_running_69_b2bf += input[i];
    }
}
