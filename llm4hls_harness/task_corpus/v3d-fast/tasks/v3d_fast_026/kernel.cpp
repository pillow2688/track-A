#include "kernel.h"

void kernel(
    const int input[V3D_BANK_INPUT_SIZE],
    int output[V3D_BANK_OUTPUT_SIZE]) {
    int v3d_single_bank_196_d78f[V3D_BANK_INPUT_SIZE];
#pragma HLS BIND_STORAGE variable=v3d_single_bank_196_d78f type=ram_1p impl=bram
    for (int i = 0; i < V3D_BANK_INPUT_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        v3d_single_bank_196_d78f[i] = input[i];
    }
    for (int group = 0; group < V3D_BANK_OUTPUT_SIZE; ++group) {
#pragma HLS PIPELINE II=1
        int sum = 0;
        for (int lane = 0; lane < 4; ++lane) {
#pragma HLS UNROLL
            sum += v3d_single_bank_196_d78f[group * 4 + lane];
        }
        output[group] = sum;
    }
}
