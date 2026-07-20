#include "kernel.h"

void kernel(
    const V3DPoint3D input[V3D_POINTS],
    V3DPoint2D output[V3D_POINTS]) {
    // V3D_MUTATION_BEGIN
    for (int i = 0; i < V3D_POINTS; ++i) {
        output[i].x = input[i].x;
        output[i].y = input[i].y;
    }
    // V3D_MUTATION_END
}
