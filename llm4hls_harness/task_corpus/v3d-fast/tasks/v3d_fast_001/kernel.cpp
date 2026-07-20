#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int v3d_i_65_63b6 = 0; v3d_i_65_63b6 < V3D_SIZE - 1; ++v3d_i_65_63b6) {
        output[v3d_i_65_63b6] = input[v3d_i_65_63b6] * 3 + 7;
    }
}
