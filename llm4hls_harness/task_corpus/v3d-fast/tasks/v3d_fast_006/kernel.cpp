#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int v3d_i_6a_f55e = 0; v3d_i_6a_f55e < V3D_SIZE; ++v3d_i_6a_f55e) {
        int v3d_value_6a = input[v3d_i_6a_f55e] * 3 + 7;
        output[v3d_i_6a_f55e] = v3d_value_6a < 0 ? 0 : v3d_value_6a;
    }
}
