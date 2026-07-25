#include "kernel.h"

int kernel(
    const int lhs[V3D_REDUCTION_SIZE],
    const int rhs[V3D_REDUCTION_SIZE]) {
    int partial_sums[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    #pragma HLS ARRAY_PARTITION variable=partial_sums complete
    for (int i = 0; i < V3D_REDUCTION_SIZE; i += 8) {
        #pragma HLS UNROLL factor=8
        for (int j = 0; j < 8; ++j) {
            partial_sums[j] += lhs[i + j] * rhs[i + j];
        }
    }
    int sum = 0;
    for (int j = 0; j < 8; ++j) {
        #pragma HLS UNROLL
        sum += partial_sums[j];
    }
    return sum;
}
