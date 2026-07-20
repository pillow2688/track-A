#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int v3d_i_6b_7e32 = 0; v3d_i_6b_7e32 < V3D_SIZE; ++v3d_i_6b_7e32) {
        if (input[v3d_i_6b_7e32] >= 0) {
            output[v3d_i_6b_7e32] = input[v3d_i_6b_7e32] * 3 - 7;
        } else {
            output[v3d_i_6b_7e32] = input[v3d_i_6b_7e32] * 3 + 7;
        }
    }
}
