#include "kernel.h"

#include <iostream>

int main() {
    int lhs[V3D_REDUCTION_SIZE] = {};
    int rhs[V3D_REDUCTION_SIZE] = {};
    int expected = 0;
    for (int i = 0; i < V3D_REDUCTION_SIZE; ++i) {
        lhs[i] = (i % 9) - 4;
        rhs[i] = ((i * 5) % 13) - 6;
        expected += lhs[i] * rhs[i];
    }
    const int actual = kernel(lhs, rhs);
    if (actual != expected) {
        std::cerr << "public reduction mismatch: expected " << expected
                  << ", got " << actual << "\n";
        return 1;
    }
    return 0;
}
