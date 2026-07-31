#include "kernel.h"
#include <hls_stream.h>

static void produce_with_rate_mismatch(const int in[EXP_N], hls::stream<int>& main, hls::stream<int>& tag) {
#pragma HLS INLINE off
    for (int i = 0; i < EXP_N; ++i) {
        tag.write(in[i] + 1);
        tag.write(0);
        main.write(in[i]);
    }
}

static void consume_one_tag(hls::stream<int>& main, hls::stream<int>& tag, int out[EXP_N]) {
#pragma HLS INLINE off
    for (int i = 0; i < EXP_N; ++i) {
#pragma HLS PIPELINE II=1
        out[i] = main.read() + tag.read();
    }
}

extern "C" void kernel(const int in[EXP_N], int out[EXP_N]) {
#pragma HLS DATAFLOW
    hls::stream<int> main("main");
    hls::stream<int> tag("tag");
#pragma HLS STREAM variable=main depth=1
#pragma HLS STREAM variable=tag depth=1
    produce_with_rate_mismatch(in, main, tag);
    consume_one_tag(main, tag, out);
}
