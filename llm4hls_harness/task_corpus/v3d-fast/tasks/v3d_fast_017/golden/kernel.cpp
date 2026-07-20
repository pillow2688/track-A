#include "kernel.h"

// V3D_MUTATION_BEGIN
static void v3d_produce(
    const int input[V3D_SIZE],
    hls::stream<int>& main_stream,
    hls::stream<int>& side_stream) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        main_stream.write(input[i]);
        side_stream.write(input[i] + 1);
    }
}
// V3D_MUTATION_END

static void v3d_consume(
    hls::stream<int>& main_stream,
    hls::stream<int>& side_stream,
    int output[V3D_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = main_stream.read() + side_stream.read();
    }
}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
#pragma HLS DATAFLOW
    hls::stream<int> main_stream("main_stream");
    hls::stream<int> side_stream("side_stream");
#pragma HLS STREAM variable=main_stream depth=1
#pragma HLS STREAM variable=side_stream depth=1
    v3d_produce(input, main_stream, side_stream);
    v3d_consume(main_stream, side_stream, output);
}
