#pragma once

constexpr int MATRIX_DIM = 4;
constexpr int MATRIX_CLAMP_MIN = -1024;
constexpr int MATRIX_CLAMP_MAX = 1023;

void matrix_transform(
    const int lhs[MATRIX_DIM][MATRIX_DIM],
    const int rhs[MATRIX_DIM][MATRIX_DIM],
    const int bias[MATRIX_DIM],
    int output[MATRIX_DIM][MATRIX_DIM],
    int row_sums[MATRIX_DIM]);
