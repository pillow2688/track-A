#include "kernel.h"

static void v3d_cycle_seed(
    hls::stream<int>& feedback_stream) {
#pragma HLS INLINE off
    constexpr int v3d_cycle_token_132_dc13 = 0;
    for (int i = 0; i < V3D_SIZE; ++i) {
        feedback_stream.write(v3d_cycle_token_132_dc13);
    }
}

static void v3d_cycle_forward(
    const int input[V3D_SIZE],
    hls::stream<int>& feedback_stream,
    hls::stream<int>& forward_stream) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        const int dependency = feedback_stream.read();
        forward_stream.write(input[i] + dependency);
    }
}

static void v3d_cycle_consume(
    hls::stream<int>& forward_stream,
    hls::stream<int>& feedback_stream,
    int output[V3D_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = forward_stream.read() * 2 + 1;
        feedback_stream.write(0);
    }
}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
#pragma HLS DATAFLOW
    hls::stream<int> feedback_stream("feedback_stream");
    hls::stream<int> forward_stream("forward_stream");
#pragma HLS STREAM variable=feedback_stream depth=1
#pragma HLS STREAM variable=forward_stream depth=1
    v3d_cycle_seed(feedback_stream);
    v3d_cycle_forward(input, feedback_stream, forward_stream);
    v3d_cycle_consume(forward_stream, feedback_stream, output);
}
