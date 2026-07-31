#include "kernel.h"
#include <hls_stream.h>

static void seed_feedback(hls::stream<int>& feedback) {
#pragma HLS INLINE off
    for (int i = 0; i < EXP_N; ++i) feedback.write(0);
}

static void forward_with_feedback(const int in[EXP_N], hls::stream<int>& feedback, hls::stream<int>& forward) {
#pragma HLS INLINE off
    for (int i = 0; i < EXP_N; ++i) {
#pragma HLS PIPELINE II=1
        forward.write(in[i] + feedback.read());
    }
}

static void consume_and_return(hls::stream<int>& forward, hls::stream<int>& feedback, int out[EXP_N]) {
#pragma HLS INLINE off
    for (int i = 0; i < EXP_N; ++i) {
#pragma HLS PIPELINE II=1
        out[i] = forward.read() * 2 + 1;
        feedback.write(0);
    }
}

extern "C" void kernel(const int in[EXP_N], int out[EXP_N]) {
#pragma HLS DATAFLOW
    hls::stream<int> feedback("feedback");
    hls::stream<int> forward("forward");
#pragma HLS STREAM variable=feedback depth=1
#pragma HLS STREAM variable=forward depth=1
    seed_feedback(feedback);
    forward_with_feedback(in, feedback, forward);
    consume_and_return(forward, feedback, out);
}
