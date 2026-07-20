#include "kernel.h"

void kernel(
    const int input[V3D_FIR_SIZE],
    int output[V3D_FIR_SIZE]) {
    // V3D_MUTATION_BEGIN
    for (int i = 0; i < V3D_FIR_SIZE; ++i) {
        int value = input[i];
        if (i >= 1) {
            value += 2 * input[i - 1];
        }
        if (i >= 2) {
            value += input[i - 2];
        }
        output[i] = value;
    }
    // V3D_MUTATION_END
}
