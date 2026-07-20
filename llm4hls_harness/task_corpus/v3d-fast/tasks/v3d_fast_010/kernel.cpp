#include "kernel.h"

static int v3d_recursive_value_ca_c565(const int input[V3D_SIZE], int index) {
    if (index == 0) return input[0] * 3 + 7;
    return v3d_recursive_value_ca_c565(input, index - 1)
        + (input[index] - input[index - 1]) * 3;
}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = v3d_recursive_value_ca_c565(input, i);
    }
}
