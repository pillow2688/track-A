#include "kernel.h"

// V3D_MUTATION_BEGIN
void kernel(
    const int input[V3D_BANK_INPUT_SIZE],
    int output[V3D_BANK_OUTPUT_SIZE]) {
    int banks[4][V3D_BANK_OUTPUT_SIZE];
#pragma HLS ARRAY_PARTITION variable=banks complete dim=1
    for (int group = 0; group < V3D_BANK_OUTPUT_SIZE; ++group) {
#pragma HLS PIPELINE II=1
        for (int lane = 0; lane < 4; ++lane) {
#pragma HLS UNROLL
            banks[lane][group] = input[group * 4 + lane];
        }
    }
    for (int group = 0; group < V3D_BANK_OUTPUT_SIZE; ++group) {
#pragma HLS PIPELINE II=1
        output[group] = banks[0][group] + banks[1][group]
            + banks[2][group] + banks[3][group];
    }
}
// V3D_MUTATION_END
