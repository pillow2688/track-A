#include "matrix_transform.h"

#include <cstdio>

static int clamp_reference(int value) {
    if (value < MATRIX_CLAMP_MIN) {
        return MATRIX_CLAMP_MIN;
    }
    if (value > MATRIX_CLAMP_MAX) {
        return MATRIX_CLAMP_MAX;
    }
    return value;
}

static void reference_transform(
    const int lhs[MATRIX_DIM][MATRIX_DIM],
    const int rhs[MATRIX_DIM][MATRIX_DIM],
    const int bias[MATRIX_DIM],
    int output[MATRIX_DIM][MATRIX_DIM],
    int row_sums[MATRIX_DIM]) {
    for (int i = 0; i < MATRIX_DIM; ++i) {
        int row_sum = 0;
        for (int j = 0; j < MATRIX_DIM; ++j) {
            int accumulator = bias[j];
            for (int k = 0; k < MATRIX_DIM; ++k) {
                accumulator += lhs[i][k] * rhs[k][j];
            }
            output[i][j] = clamp_reference(accumulator);
            row_sum += output[i][j];
        }
        row_sums[i] = row_sum;
    }
}

static void build_case(
    int case_index,
    int lhs[MATRIX_DIM][MATRIX_DIM],
    int rhs[MATRIX_DIM][MATRIX_DIM],
    int bias[MATRIX_DIM]) {
    for (int i = 0; i < MATRIX_DIM; ++i) {
        bias[i] = ((case_index + 3) * (i + 5) * 19) % 257 - 128;
        for (int j = 0; j < MATRIX_DIM; ++j) {
            lhs[i][j] = ((case_index + 5) * (i + 2) * (j + 7) * 11) % 129 - 64;
            rhs[i][j] = ((case_index + 9) * (i + 4) * (j + 3) * 13) % 129 - 64;
        }
    }
    if (case_index == 0) {
        for (int i = 0; i < MATRIX_DIM; ++i) {
            for (int j = 0; j < MATRIX_DIM; ++j) {
                lhs[i][j] = 0;
                rhs[i][j] = 0;
            }
            bias[i] = 0;
        }
    }
}

int main() {
    for (int case_index = 0; case_index < 5; ++case_index) {
        int lhs[MATRIX_DIM][MATRIX_DIM];
        int rhs[MATRIX_DIM][MATRIX_DIM];
        int bias[MATRIX_DIM];
        int expected[MATRIX_DIM][MATRIX_DIM];
        int expected_rows[MATRIX_DIM];
        int actual[MATRIX_DIM][MATRIX_DIM];
        int actual_rows[MATRIX_DIM];

        build_case(case_index, lhs, rhs, bias);
        reference_transform(lhs, rhs, bias, expected, expected_rows);
        for (int i = 0; i < MATRIX_DIM; ++i) {
            actual_rows[i] = 777777;
            for (int j = 0; j < MATRIX_DIM; ++j) {
                actual[i][j] = 777777;
            }
        }

        matrix_transform(lhs, rhs, bias, actual, actual_rows);
        for (int i = 0; i < MATRIX_DIM; ++i) {
            if (actual_rows[i] != expected_rows[i]) {
                std::fprintf(stderr, "row mismatch case=%d row=%d got=%d expected=%d\n", case_index, i, actual_rows[i], expected_rows[i]);
                return 1;
            }
            for (int j = 0; j < MATRIX_DIM; ++j) {
                if (actual[i][j] != expected[i][j]) {
                    std::fprintf(stderr, "matrix mismatch case=%d row=%d col=%d got=%d expected=%d\n", case_index, i, j, actual[i][j], expected[i][j]);
                    return 1;
                }
            }
        }
    }
    return 0;
}
