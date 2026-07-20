#include "kernel.h"

void kernel(
    const int lhs[V3D_VECTOR_SIZE],
    const int rhs[V3D_VECTOR_SIZE],
    int output[V3D_VECTOR_SIZE]) {
    for (int v3d_lane_6b_7e32 = 0; v3d_lane_6b_7e32 < V3D_VECTOR_SIZE; ++v3d_lane_6b_7e32) {
        output[v3d_lane_6b_7e32] = (lhs[v3d_lane_6b_7e32] + rhs[v3d_lane_6b_7e32]) & 0xff;
    }
}
