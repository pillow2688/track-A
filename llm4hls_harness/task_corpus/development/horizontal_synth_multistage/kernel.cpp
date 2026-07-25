#include "kernel.h"

#include <functional>

static int recursive_increment(int value, int remaining) {
    if (remaining == 0) {
        return value;
    }
    return recursive_increment(value + 1, remaining - 1);
}

static int dynamic_identity(int value) {
    std::function<int(int)> identity = [](int item) {
        return item;
    };
    return identity(value);
}

static void produce(
    const int input[HORIZONTAL_SYNTH_SIZE],
    hls::stream<int>& main_stream,
    hls::stream<int>& side_stream) {
#pragma HLS INLINE off
    for (int i = 0; i < HORIZONTAL_SYNTH_SIZE; ++i) {
        side_stream.write(recursive_increment(input[i], 1));
        main_stream.write(dynamic_identity(input[i]));
    }
}

static void consume(
    hls::stream<int>& main_stream,
    hls::stream<int>& side_stream,
    int output[HORIZONTAL_SYNTH_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < HORIZONTAL_SYNTH_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = main_stream.read() + side_stream.read();
    }
}

void kernel(
    const int input[HORIZONTAL_SYNTH_SIZE],
    int output[HORIZONTAL_SYNTH_SIZE]) {
#pragma HLS DATAFLOW
    hls::stream<int> main_stream("main_stream");
    hls::stream<int> side_stream("side_stream");
#pragma HLS STREAM variable=main_stream depth=1
#pragma HLS STREAM variable=side_stream depth=1
    produce(input, main_stream, side_stream);
    consume(main_stream, side_stream, output);
}
