#include "kernel.h"
#include <hls_stream.h>

static void produce_in_bursts(const int in[EXP_N], hls::stream<int>& main, hls::stream<int>& side) {
#pragma HLS INLINE off
    constexpr int burst = 3;
    for (int base = 0; base < EXP_N; base += burst) {
        for (int i = base; i < base + burst && i < EXP_N; ++i) main.write(in[i]);
        for (int i = base; i < base + burst && i < EXP_N; ++i) side.write(in[i] + 1);
    }
}

static void merge_bursts(hls::stream<int>& main, hls::stream<int>& side, int out[EXP_N]) {
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
    produce_in_bursts(in, main, side);
    merge_bursts(main, side, out);
}
