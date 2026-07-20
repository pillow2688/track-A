#include "kernel.h"

#include <iostream>

int main() {
    const int lhs[V3D_VECTOR_SIZE] = {250, -20, 100, 1000, -300, 7, 128, 42, 15, -80, 511, 3, 64, 201, -5, 9};
    const int rhs[V3D_VECTOR_SIZE] = {10, 5, 200, -3, 45, 255, 130, -8, 260, 100, 2, 300, -70, 80, 270, -12};
    int output[V3D_VECTOR_SIZE] = {};
    kernel(lhs, rhs, output);
    for (int i = 0; i < V3D_VECTOR_SIZE; ++i) {
        const int expected = lhs[i] + rhs[i];
        if (output[i] != expected) {
            std::cerr << "public vector-add mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
