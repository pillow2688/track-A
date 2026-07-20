#include "kernel.h"

void kernel(
    const int input[V3D_PREFIX_SIZE],
    int output[V3D_PREFIX_SIZE]) {
    // V3D_MUTATION_BEGIN
    int running_sum = 0;
    for (int i = 0; i < V3D_PREFIX_SIZE; ++i) {
        running_sum += input[i];
        output[i] = running_sum;
    }
    // V3D_MUTATION_END
}
