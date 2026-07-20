#include "kernel.h"

void kernel(
    const V3DPoint3D input[V3D_POINTS],
    V3DPoint2D output[V3D_POINTS]) {
    for (int v3d_point_68_0a00 = 0; v3d_point_68_0a00 < V3D_POINTS; ++v3d_point_68_0a00) {
        const int source_index = (v3d_point_68_0a00 + 1) % V3D_POINTS;
        output[v3d_point_68_0a00].x = input[source_index].x;
        output[v3d_point_68_0a00].y = input[source_index].y;
    }
}
