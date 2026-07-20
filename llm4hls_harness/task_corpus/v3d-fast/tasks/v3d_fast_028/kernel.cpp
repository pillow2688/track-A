#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    int v3d_temporary_198_e17e[V3D_SIZE];
v3d_scale_198_e17e:
    for (int i = 0; i < V3D_SIZE; ++i) {
        v3d_temporary_198_e17e[i] = input[i] * 3;
    }
v3d_bias_198_e17e:
    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = v3d_temporary_198_e17e[i] + 7;
    }
}
