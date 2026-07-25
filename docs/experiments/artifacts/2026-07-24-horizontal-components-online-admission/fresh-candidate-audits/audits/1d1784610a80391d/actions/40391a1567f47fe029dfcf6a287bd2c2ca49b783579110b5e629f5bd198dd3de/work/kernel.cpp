#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
#pragma HLS ARRAY_PARTITION variable=input cyclic factor=4 dim=1
#pragma HLS ARRAY_PARTITION variable=output cyclic factor=4 dim=1
v3d_map_194_2cc5:
    for (int i = 0; i < V3D_SIZE; ++i) {
        #pragma HLS PIPELINE II=1
        #pragma HLS UNROLL factor=4
        for (int j = 0; j < 4; ++j) {
            output[i*4 + j] = input[i*4 + j] * 3 + 7;
        }
    }
}
