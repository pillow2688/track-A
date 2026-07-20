#include "kernel.h"

#include <iostream>

int main() {
    const int lhs[V3D_VECTOR_SIZE] = {400, -90, 17, 999, -301, 88, 240, 1, 73, -44, 512, 6, 27, 360, -8, 19};
    const int rhs[V3D_VECTOR_SIZE] = {30, 12, -8, 2, 44, 190, 31, -5, 184, 90, -12, 300, -40, 7, 260, -22};
    int output[V3D_VECTOR_SIZE] = {};
    kernel(lhs, rhs, output);
    for (int i = 0; i < V3D_VECTOR_SIZE; ++i) {
        const int expected = lhs[i] + rhs[i];
        if (output[i] != expected) {
            std::cerr << "hidden-like vector-add mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
