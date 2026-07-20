#include "kernel.h"

// V3D_MUTATION_BEGIN
static void v3d_dataflow_scale(
    const int input[V3D_DATAFLOW_SIZE],
    int scaled[V3D_DATAFLOW_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_DATAFLOW_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        scaled[i] = input[i] * 5;
    }
}

static void v3d_dataflow_bias(
    const int scaled[V3D_DATAFLOW_SIZE],
    int output[V3D_DATAFLOW_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_DATAFLOW_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = scaled[i] - 3;
    }
}

void kernel(
    const int input[V3D_DATAFLOW_SIZE],
    int output[V3D_DATAFLOW_SIZE]) {
#pragma HLS DATAFLOW
    int scaled[V3D_DATAFLOW_SIZE];
    v3d_dataflow_scale(input, scaled);
    v3d_dataflow_bias(scaled, output);
}
// V3D_MUTATION_END
