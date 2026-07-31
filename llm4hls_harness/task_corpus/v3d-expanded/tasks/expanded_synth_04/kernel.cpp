#include "kernel.h"
#include <functional>

extern "C" void kernel(const int in[EXP_N], int out[EXP_N]) {
    std::function<int(int)> scale_and_bias = [](int value) { return value * 2 + 5; };
    for (int i = 0; i < EXP_N; ++i) out[i] = scale_and_bias(in[i]);
}
