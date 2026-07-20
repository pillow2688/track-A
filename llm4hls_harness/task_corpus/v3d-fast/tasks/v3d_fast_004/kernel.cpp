#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int v3d_i_68_0a00 = 0; v3d_i_68_0a00 < V3D_SIZE; ++v3d_i_68_0a00) {
        output[v3d_i_68_0a00] = input[(v3d_i_68_0a00 + 1) % V3D_SIZE] * 3 + 7;
    }
}
