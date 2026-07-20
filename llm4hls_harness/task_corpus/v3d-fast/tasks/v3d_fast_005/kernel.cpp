#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    int v3d_acc_69 = 0;
    for (int v3d_i_69_b2bf = 0; v3d_i_69_b2bf < V3D_SIZE; ++v3d_i_69_b2bf) {
        v3d_acc_69 += input[v3d_i_69_b2bf] * 3 + 7;
        output[v3d_i_69_b2bf] = v3d_acc_69;
    }
}
