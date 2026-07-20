#include "kernel.h"

#include <iostream>

int main() {
    int lhs[V3D_DOT_SIZE] = {};
    int rhs[V3D_DOT_SIZE] = {};
    int expected = 0;
    for (int i = 0; i < V3D_DOT_SIZE; ++i) {
        lhs[i] = (i % 7) - 3;
        rhs[i] = ((i * 3) % 11) - 5;
        expected += lhs[i] * rhs[i];
    }
    const int actual = kernel(lhs, rhs);
    if (actual != expected) {
        std::cerr << "public dot-product mismatch: expected " << expected
                  << ", got " << actual << "\n";
        return 1;
    }
    return 0;
}
