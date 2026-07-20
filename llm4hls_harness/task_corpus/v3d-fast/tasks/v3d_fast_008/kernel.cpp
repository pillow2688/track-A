#include "kernel.h"

void kernel(
    const int input[V3D_FIR_SIZE],
    int output[V3D_FIR_SIZE]) {
    for (int i = 0; i < V3D_FIR_SIZE; ++i) {
        int v3d_value_6c_42d3 = input[i];
        if (i > 1) {
            v3d_value_6c_42d3 += 2 * input[i - 1];
        }
        if (i >= 2) {
            v3d_value_6c_42d3 += input[i - 2];
        }
        output[i] = v3d_value_6c_42d3;
    }
}
