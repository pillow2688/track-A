#include "kernel.h"

#include <iostream>

int main() {
    const int input[V3D_PREFIX_SIZE] = {3, -1, 4, 2, -2, 5, 0, 7, -3, 1, 6, -4};
    int output[V3D_PREFIX_SIZE] = {};
    kernel(input, output);
    int expected = 0;
    for (int i = 0; i < V3D_PREFIX_SIZE; ++i) {
        expected += input[i];
        if (output[i] != expected) {
            std::cerr << "public prefix mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
