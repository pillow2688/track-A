#include "kernel.h"

// V3D_MUTATION_BEGIN
static void v3d_cycle_forward(
    const int input[V3D_SIZE],
    hls::stream<int>& forward_stream) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        forward_stream.write(input[i]);
    }
}

static void v3d_cycle_consume(
    hls::stream<int>& forward_stream,
    int output[V3D_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = forward_stream.read() * 2 + 1;
    }
}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
#pragma HLS DATAFLOW
    hls::stream<int> forward_stream("forward_stream");
#pragma HLS STREAM variable=forward_stream depth=1
    v3d_cycle_forward(input, forward_stream);
    v3d_cycle_consume(forward_stream, output);
}
// V3D_MUTATION_END
