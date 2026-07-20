#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int v3d_i_66_4bdd = 0; v3d_i_66_4bdd < V3D_SIZE; ++v3d_i_66_4bdd) {
        output[v3d_i_66_4bdd] = input[v3d_i_66_4bdd] * 2 + 7;
    }
}
