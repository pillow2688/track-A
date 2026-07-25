#include "kernel.h"

#include <iostream>

int main() {
    const int input[HORIZONTAL_SYNTH_SIZE] = {
        -1000, -127, -11, -3, -2, -1, 0, 1,
        2, 3, 7, 11, 127, 1024, 100000, 1000000
    };
    int output[HORIZONTAL_SYNTH_SIZE] = {};
    kernel(input, output);
    for (int i = 0; i < HORIZONTAL_SYNTH_SIZE; ++i) {
        const int expected = input[i] * 2 + 1;
        if (output[i] != expected) {
            std::cerr << "public synthesis Anchor mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
