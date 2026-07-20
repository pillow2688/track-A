#include "kernel.h"

void kernel(
    const unsigned char input[V3D_HIST_INPUT_SIZE],
    unsigned short bins[V3D_HIST_BINS]) {
    // V3D_MUTATION_BEGIN
    for (int bin = 0; bin < V3D_HIST_BINS; ++bin) {
        bins[bin] = 0;
    }
    for (int i = 0; i < V3D_HIST_INPUT_SIZE; ++i) {
        if (input[i] < V3D_HIST_BINS) {
            ++bins[input[i]];
        }
    }
    // V3D_MUTATION_END
}
