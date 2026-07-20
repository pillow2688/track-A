#include "kernel.h"

// V3D_MUTATION_BEGIN
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
    for (int row = 1; row < V3D_STENCIL_MAX_ROWS - 1; ++row) {
        for (int col = 1; col < V3D_STENCIL_MAX_COLS - 1; ++col) {
            if (row < rows - 1 && col < cols - 1) {
                output[row][col] = input[row][col]
                    + input[row - 1][col]
                    + input[row + 1][col]
                    + input[row][col - 1]
                    + input[row][col + 1];
            }
        }
    }
}
// V3D_MUTATION_END
