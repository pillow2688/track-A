#include "kernel.h"

#include <iostream>

int main() {
    const int input[V3D_SIZE] = {-31, 17, 0, 8, -9, 42, 3, -5, 11, -2, 29, 6, -15, 1, 23, -7};
    int output[V3D_SIZE] = {};
    kernel(input, output);
    for (int i = 0; i < V3D_SIZE; ++i) {
        const int expected = input[i] * 2 + 1;
        if (output[i] != expected) {
            std::cerr << "hidden-like mismatch at " << i
                      << ": expected " << expected
                      << ", got " << output[i] << "\n";
            return 1;
        }
    }
    return 0;
}
