#include "kernel.h"

// V3D_MUTATION_BEGIN
void kernel(
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM]) {
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {
            int sum = 0;
            for (int inner = 0; inner < V3D_MATMUL_DIM; ++inner) {
                sum += lhs[row][inner] * rhs[inner][col];
            }
            output[row][col] = sum;
        }
    }
}
// V3D_MUTATION_END
