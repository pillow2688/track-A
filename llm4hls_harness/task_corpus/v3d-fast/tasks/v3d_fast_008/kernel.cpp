#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int v3d_i_6c_42d3 = 0; v3d_i_6c_42d3 < V3D_SIZE; ++v3d_i_6c_42d3) {
        output[(v3d_i_6c_42d3 + 1) % V3D_SIZE] = input[v3d_i_6c_42d3] * 3 + 7;
    }
}
