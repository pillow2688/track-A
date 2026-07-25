#include "kernel.h"

#include <iostream>

int main() {
    int input[V3D_DATAFLOW_SIZE] = {};
    int output[V3D_DATAFLOW_SIZE] = {};
    for (int i = 0; i < V3D_DATAFLOW_SIZE; ++i) input[i] = i * 3 - 11;
    kernel(input, output);
    for (int i = 0; i < V3D_DATAFLOW_SIZE; ++i) {
        const int expected = input[i] * 5 - 3;
        if (output[i] != expected) {
            std::cerr << "public dataflow mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
