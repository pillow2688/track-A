#include "kernel.h"

#include <iostream>

int main() {
    const int input[V3D_SIZE] = {-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10};
    int output[V3D_SIZE] = {};
    kernel(input, output);
    for (int i = 0; i < V3D_SIZE; ++i) {
        const int expected = input[i] * 2 + 1;
        if (output[i] != expected) {
            std::cerr << "public mismatch at " << i
                      << ": expected " << expected
                      << ", got " << output[i] << "\n";
            return 1;
        }
    }
    return 0;
}
