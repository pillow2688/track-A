#include "kernel.h"

void kernel(
    const unsigned char input[V3D_HIST_INPUT_SIZE],
    unsigned short bins[V3D_HIST_BINS]) {
    for (int bin = 0; bin < V3D_HIST_BINS; ++bin) {
        bins[bin] = 0;
    }
    for (int v3d_sample_6a_f55e = 0;
         v3d_sample_6a_f55e < V3D_HIST_INPUT_SIZE;
         ++v3d_sample_6a_f55e) {
        if (input[v3d_sample_6a_f55e] < V3D_HIST_BINS - 1) {
            ++bins[input[v3d_sample_6a_f55e]];
        }
    }
}
