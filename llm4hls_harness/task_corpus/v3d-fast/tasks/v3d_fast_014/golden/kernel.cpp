#include "kernel.h"

// V3D_MUTATION_BEGIN
int kernel(
    const int lhs[V3D_DOT_SIZE],
    const int rhs[V3D_DOT_SIZE]) {
    int sum = 0;
    for (int i = 0; i < V3D_DOT_SIZE; ++i) {
        sum += lhs[i] * rhs[i];
    }
    return sum;
}
// V3D_MUTATION_END
