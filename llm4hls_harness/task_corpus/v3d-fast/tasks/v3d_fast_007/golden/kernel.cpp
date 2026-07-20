#include "kernel.h"

void kernel(
    const int lhs[V3D_VECTOR_SIZE],
    const int rhs[V3D_VECTOR_SIZE],
    int output[V3D_VECTOR_SIZE]) {
    // V3D_MUTATION_BEGIN
    for (int i = 0; i < V3D_VECTOR_SIZE; ++i) {
        output[i] = lhs[i] + rhs[i];
    }
    // V3D_MUTATION_END
}
