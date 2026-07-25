#include "kernel.h"

#include <iostream>

static int check_case(
    const ap_int<16> input[HORIZONTAL_REPAIR_SIZE],
    bool negative_branch) {
    ap_int<32> output[HORIZONTAL_REPAIR_SIZE] = {};
    kernel(input, output);
    for (int i = 0; i < HORIZONTAL_REPAIR_SIZE; ++i) {
        const ap_int<32> expected = negative_branch
            ? ap_int<32>(input[i]) * 2 + 1
            : ap_int<32>(input[i]) * 3 + 5;
        if (output[i] != expected) {
            std::cerr
                << (negative_branch ? "negative" : "nonnegative")
                << " branch mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}

int main() {
    const ap_int<16> negative[HORIZONTAL_REPAIR_SIZE] = {
        -1, -2, -3, -7, -11, -127, -1024, -32768
    };
    if (check_case(negative, true) != 0) {
        return 1;
    }
    const ap_int<16> nonnegative[HORIZONTAL_REPAIR_SIZE] = {
        0, 1, 2, 7, 11, 127, 1024, 32767
    };
    return check_case(nonnegative, false);
}

