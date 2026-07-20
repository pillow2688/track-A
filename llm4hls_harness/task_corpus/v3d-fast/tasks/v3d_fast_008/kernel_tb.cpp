#include "kernel.h"

#include <iostream>

int main() {
    const int input[V3D_FIR_SIZE] = {3, 1, -2, 4, 0, 5, -1, 2, 7, -3, 6, 8, -4, 9, 2, -5};
    int output[V3D_FIR_SIZE] = {};
    kernel(input, output);
    for (int i = 0; i < V3D_FIR_SIZE; ++i) {
        int expected = input[i];
        if (i >= 1) expected += 2 * input[i - 1];
        if (i >= 2) expected += input[i - 2];
        if (output[i] != expected) {
            std::cerr << "public FIR mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
