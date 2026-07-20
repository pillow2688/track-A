#include "kernel.h"

int kernel(
    const int lhs[V3D_DOT_SIZE],
    const int rhs[V3D_DOT_SIZE]) {
    int v3d_sum_ce_046d = 0;
    for (int i = 0; i < V3D_DOT_SIZE; ++i) {
        int v3d_product_ce_046d = lhs[i] * rhs[i];
#pragma HLS BIND_OP variable=v3d_product_ce_046d op=mul impl=uram latency=1
        v3d_sum_ce_046d += v3d_product_ce_046d;
    }
    return v3d_sum_ce_046d;
}
