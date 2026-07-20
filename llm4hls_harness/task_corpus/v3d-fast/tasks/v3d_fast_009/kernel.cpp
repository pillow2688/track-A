#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    int* v3d_scratch_c9_225b = new int[V3D_SIZE];
    for (int i = 0; i < V3D_SIZE; ++i) {
        v3d_scratch_c9_225b[i] = input[i] * 3 + 7;
        output[i] = v3d_scratch_c9_225b[i];
    }
    delete[] v3d_scratch_c9_225b;
}
