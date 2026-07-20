#include "kernel.h"

#include <iostream>

int main() {
    const unsigned char input[V3D_HIST_INPUT_SIZE] = {7, 6, 5, 4, 3, 2, 1, 0, 7, 7, 4, 4, 2, 6, 1, 7};
    unsigned short bins[V3D_HIST_BINS] = {};
    unsigned short expected[V3D_HIST_BINS] = {};
    for (int i = 0; i < V3D_HIST_INPUT_SIZE; ++i) {
        ++expected[input[i]];
    }
    kernel(input, bins);
    for (int bin = 0; bin < V3D_HIST_BINS; ++bin) {
        if (bins[bin] != expected[bin]) {
            std::cerr << "hidden-like histogram mismatch at " << bin << "\n";
            return 1;
        }
    }
    return 0;
}
