#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
v3d_map_191_fed0:
    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = input[i] * 3 + 7;
    }
}
