#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
v3d_map_197_350e:
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS UNROLL factor=4
        output[i] = input[i] * 3 + 7;
    }
}
