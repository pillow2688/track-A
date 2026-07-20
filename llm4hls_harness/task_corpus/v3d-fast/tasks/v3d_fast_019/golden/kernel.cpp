#include "kernel.h"

// V3D_MUTATION_BEGIN
static void v3d_rate_produce(
    const int input[V3D_SIZE],
    hls::stream<int>& data_stream,
    hls::stream<int>& pace_stream) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        data_stream.write(input[i]);
        pace_stream.write(i);
    }
}
// V3D_MUTATION_END

static void v3d_rate_consume(
    hls::stream<int>& data_stream,
    hls::stream<int>& pace_stream,
    int output[V3D_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        const int value = data_stream.read();
        (void)pace_stream.read();
        output[i] = value * 2 + 1;
    }
}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
#pragma HLS DATAFLOW
    hls::stream<int> data_stream("data_stream");
    hls::stream<int> pace_stream("pace_stream");
#pragma HLS STREAM variable=data_stream depth=2
#pragma HLS STREAM variable=pace_stream depth=2
    v3d_rate_produce(input, data_stream, pace_stream);
    v3d_rate_consume(data_stream, pace_stream, output);
}
