#include "kernel.h"

#include <iostream>

int main() {
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {{2, -1, 4, 3}, {0, 5, -2, 1}, {7, 2, 1, -3}, {4, 0, 6, 2}};
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {{1, 3, 0, -2}, {4, -1, 2, 5}, {-3, 6, 1, 0}, {2, 4, -5, 3}};
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {};
    kernel(lhs, rhs, output);
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {
            int expected = 0;
            for (int inner = 0; inner < V3D_MATMUL_DIM; ++inner) {
                expected += lhs[row][inner] * rhs[inner][col];
            }
            if (output[row][col] != expected) {
                std::cerr << "hidden-like matmul mismatch at "
                          << row << "," << col << "\n";
                return 1;
            }
        }
    }
    return 0;
}
