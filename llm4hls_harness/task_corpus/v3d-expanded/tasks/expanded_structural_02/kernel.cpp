#include "kernel.h"
#include <hls_stream.h>

static void produce_side_then_main(const int in[EXP_N], hls::stream<int>& main, hls::stream<int>& side) {
#pragma HLS INLINE off
    for (int i = 0; i < EXP_N; ++i) side.write(in[i] + 1);
    for (int i = 0; i < EXP_N; ++i) main.write(in[i]);
}

static void consume_in_lockstep(hls::stream<int>& main, hls::stream<int>& side, int out[EXP_N]) {
#pragma HLS INLINE off
    for (int i = 0; i < EXP_N; ++i) {
#pragma HLS PIPELINE II=1
        out[i] = main.read() + side.read();
    }
}

extern "C" void kernel(const int in[EXP_N], int out[EXP_N]) {
#pragma HLS DATAFLOW
    hls::stream<int> main("main");
    hls::stream<int> side("side");
#pragma HLS STREAM variable=main depth=1
#pragma HLS STREAM variable=side depth=1
    produce_side_then_main(in, main, side);
    consume_in_lockstep(main, side, out);
}
