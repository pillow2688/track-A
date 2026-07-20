#include "kernel.h"

#include <iostream>

int main() {
    const int input[V3D_PREFIX_SIZE] = {-7, 4, 12, -3, 9, 1, -8, 6, 5, -2, 11, -4};
    int output[V3D_PREFIX_SIZE] = {};
    kernel(input, output);
    int expected = 0;
    for (int i = 0; i < V3D_PREFIX_SIZE; ++i) {
        expected += input[i];
        if (output[i] != expected) {
            std::cerr << "hidden-like prefix mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
