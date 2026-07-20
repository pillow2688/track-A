#include "kernel.h"

#include <iostream>

int main() {
    int input[V3D_BANK_INPUT_SIZE] = {};
    int output[V3D_BANK_OUTPUT_SIZE] = {};
    for (int i = 0; i < V3D_BANK_INPUT_SIZE; ++i) input[i] = (i * 11) % 37 - 18;
    kernel(input, output);
    for (int group = 0; group < V3D_BANK_OUTPUT_SIZE; ++group) {
        int expected = 0;
        for (int lane = 0; lane < 4; ++lane) {
            expected += input[group * 4 + lane];
        }
        if (output[group] != expected) {
            std::cerr << "hidden-like banking mismatch at " << group << "\n";
            return 1;
        }
    }
    return 0;
}
