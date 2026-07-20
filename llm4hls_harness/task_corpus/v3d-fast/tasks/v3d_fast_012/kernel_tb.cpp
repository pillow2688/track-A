#include "kernel.h"

#include <iostream>

int main() {
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {{1, 2, 3, 4}, {-2, 0, 5, 1}, {3, -1, 2, 6}, {4, 2, 0, -3}};
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {{2, -1, 0, 3}, {4, 5, -2, 1}, {1, 0, 6, -4}, {-3, 2, 1, 5}};
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {};
    kernel(lhs, rhs, output);
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {
            int expected = 0;
            for (int inner = 0; inner < V3D_MATMUL_DIM; ++inner) {
                expected += lhs[row][inner] * rhs[inner][col];
            }
            if (output[row][col] != expected) {
                std::cerr << "public matmul mismatch at "
                          << row << "," << col << "\n";
                return 1;
            }
        }
    }
    return 0;
}
