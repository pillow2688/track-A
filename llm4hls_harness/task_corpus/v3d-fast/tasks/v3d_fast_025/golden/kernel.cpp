#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    // V3D_MUTATION_BEGIN
#pragma HLS ARRAY_PARTITION variable=input cyclic factor=4 dim=1
#pragma HLS ARRAY_PARTITION variable=output cyclic factor=4 dim=1
v3d_map:
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS UNROLL factor=4
        output[i] = input[i] * 3 + 7;
    }
    // V3D_MUTATION_END
}
