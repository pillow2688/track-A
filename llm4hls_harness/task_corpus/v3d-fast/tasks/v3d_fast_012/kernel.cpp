#include "kernel.h"

#include <vector>

void kernel(
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM]) {
    std::vector<int> v3d_scratch_cc_cb37(
        V3D_MATMUL_DIM * V3D_MATMUL_DIM, 0);
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {
            for (int inner = 0; inner < V3D_MATMUL_DIM; ++inner) {
                v3d_scratch_cc_cb37[row * V3D_MATMUL_DIM + col]
                    += lhs[row][inner] * rhs[inner][col];
            }
        }
    }
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {
            output[row][col]
                = v3d_scratch_cc_cb37[row * V3D_MATMUL_DIM + col];
        }
    }
}
