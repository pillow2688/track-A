#include "kernel.h"

#include <iostream>

int main() {
    int input[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS] = {};
    int output[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS] = {};
    for (int row = 0; row < V3D_STENCIL_MAX_ROWS; ++row) {
        for (int col = 0; col < V3D_STENCIL_MAX_COLS; ++col) {
            input[row][col] = row * 7 + col * -3 + 2;
        }
    }
    constexpr int rows = 6;
    constexpr int cols = 5;
    kernel(input, output, rows, cols);
    for (int row = 0; row < V3D_STENCIL_MAX_ROWS; ++row) {
        for (int col = 0; col < V3D_STENCIL_MAX_COLS; ++col) {
            int expected = 0;
            if (row > 0 && row < rows - 1 && col > 0 && col < cols - 1) {
                expected = input[row][col]
                    + input[row - 1][col]
                    + input[row + 1][col]
                    + input[row][col - 1]
                    + input[row][col + 1];
            }
            if (output[row][col] != expected) {
                std::cerr << "public stencil mismatch at "
                          << row << "," << col << "\n";
                return 1;
            }
        }
    }
    return 0;
}
