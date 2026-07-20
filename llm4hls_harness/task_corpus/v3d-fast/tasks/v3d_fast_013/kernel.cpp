#include "kernel.h"

void kernel(
    const int input[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int output[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int rows,
    int cols) {
    for (int row = 0; row < V3D_STENCIL_MAX_ROWS; ++row) {
        for (int col = 0; col < V3D_STENCIL_MAX_COLS; ++col) {
            output[row][col] = 0;
        }
    }

    int v3d_row_order_cd_de6b[rows];
    int v3d_initialized_cd_de6b = 0;
    while (v3d_initialized_cd_de6b < rows) {
        v3d_row_order_cd_de6b[v3d_initialized_cd_de6b] = v3d_initialized_cd_de6b;
        ++v3d_initialized_cd_de6b;
    }

    int v3d_position_cd_de6b = 1;
    while (v3d_position_cd_de6b < rows - 1) {
        const int row = v3d_row_order_cd_de6b[v3d_position_cd_de6b];
        int col = 1;
        while (col < cols - 1) {
            output[row][col] = input[row][col]
                + input[row - 1][col]
                + input[row + 1][col]
                + input[row][col - 1]
                + input[row][col + 1];
            ++col;
        }
        ++v3d_position_cd_de6b;
    }
}
