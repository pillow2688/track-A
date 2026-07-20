#include "kernel.h"

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    // V3D_MUTATION_BEGIN
    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = input[i] * 3 + 7;
    }
    // V3D_MUTATION_END
}
