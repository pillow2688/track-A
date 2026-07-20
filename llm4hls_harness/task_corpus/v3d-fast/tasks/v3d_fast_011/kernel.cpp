#include "kernel.h"

static int v3d_transform_cb_2e77(int value) {
    return value * 3 + 7;
}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    int (*v3d_function_cb_2e77)(int) = &v3d_transform_cb_2e77;
    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = v3d_function_cb_2e77(input[i]);
    }
}
