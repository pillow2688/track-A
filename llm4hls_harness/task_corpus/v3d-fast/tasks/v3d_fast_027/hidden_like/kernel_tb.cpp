#include "kernel.h"

#include <iostream>

int main() {
    int input[V3D_TRANSACTION_SIZE] = {};
    int output[V3D_TRANSACTION_SIZE] = {};
    for (int i = 0; i < V3D_TRANSACTION_SIZE; ++i) input[i] = i * 13 - 101;
    kernel(input, output);
    for (int i = 0; i < V3D_TRANSACTION_SIZE; ++i) {
        const int expected = input[i] * 3 + 1;
        if (output[i] != expected) {
            std::cerr << "hidden-like transaction mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
