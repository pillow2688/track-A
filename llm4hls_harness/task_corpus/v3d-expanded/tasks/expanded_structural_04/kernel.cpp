#include "kernel.h"
#include <hls_stream.h>

static void produce_main_pair_before_side_pair(const int in[EXP_N], hls::stream<int>& main, hls::stream<int>& side) {
#pragma HLS INLINE off
    for (int base = 0; base < EXP_N; base += 2) {
        for (int i = base; i < base + 2 && i < EXP_N; ++i) main.write(in[i]);
        for (int i = base; i < base + 2 && i < EXP_N; ++i) side.write(in[i] + 1);
    }
}

static void consume_side_before_main(hls::stream<int>& main, hls::stream<int>& side, int out[EXP_N]) {
#pragma HLS INLINE off
    for (int i = 0; i < EXP_N; ++i) {
#pragma HLS PIPELINE II=1
        const int side_value = side.read();
        out[i] = main.read() + side_value;
    }
}

extern "C" void kernel(const int in[EXP_N], int out[EXP_N]) {
#pragma HLS DATAFLOW
    hls::stream<int> main("main");
    hls::stream<int> side("side");
#pragma HLS STREAM variable=main depth=1
#pragma HLS STREAM variable=side depth=1
    produce_main_pair_before_side_pair(in, main, side);
    consume_side_before_main(main, side, out);
}
