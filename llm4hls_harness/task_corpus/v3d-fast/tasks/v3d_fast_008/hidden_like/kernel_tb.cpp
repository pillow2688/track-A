#include "kernel.h"

#include <iostream>

int main() {
    const int input[V3D_FIR_SIZE] = {-4, 9, 1, -7, 5, 3, 12, -2, 8, 6, -5, 10, 2, -1, 7, 4};
    int output[V3D_FIR_SIZE] = {};
    kernel(input, output);
    for (int i = 0; i < V3D_FIR_SIZE; ++i) {
        int expected = input[i];
        if (i >= 1) expected += 2 * input[i - 1];
        if (i >= 2) expected += input[i - 2];
        if (output[i] != expected) {
            std::cerr << "hidden-like FIR mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
