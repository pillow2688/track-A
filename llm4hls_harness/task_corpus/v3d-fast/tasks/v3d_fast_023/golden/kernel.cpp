#include "kernel.h"

// V3D_MUTATION_BEGIN
int kernel(
    const int lhs[V3D_REDUCTION_SIZE],
    const int rhs[V3D_REDUCTION_SIZE]) {
#pragma HLS ARRAY_PARTITION variable=lhs cyclic factor=8 dim=1
#pragma HLS ARRAY_PARTITION variable=rhs cyclic factor=8 dim=1
    int partial[8] = {};
#pragma HLS ARRAY_PARTITION variable=partial complete dim=1
    for (int lane = 0; lane < 8; ++lane) {
#pragma HLS UNROLL
        for (int i = lane; i < V3D_REDUCTION_SIZE; i += 8) {
#pragma HLS PIPELINE II=1
            partial[lane] += lhs[i] * rhs[i];
        }
    }
    int sum = 0;
    for (int lane = 0; lane < 8; ++lane) {
#pragma HLS UNROLL
        sum += partial[lane];
    }
    return sum;
}
// V3D_MUTATION_END
