#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    #pragma HLS UNROLL factor=16
    for (int i = 0; i < V3D_SIZE; ++i) {
        #pragma HLS PIPELINE II=1
        int tmp = input[i] * 3;
        output[i] = tmp + 7;
    }
}
