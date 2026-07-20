#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int v3d_i_67_e99f = 0; v3d_i_67_e99f < V3D_SIZE; ++v3d_i_67_e99f) {
        output[v3d_i_67_e99f] = input[v3d_i_67_e99f] * 3 - 7;
    }
}
