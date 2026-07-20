#include "kernel.h"

#include <vector>

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    std::vector<int> v3d_buffer_cc_cb37(V3D_SIZE);
    for (int i = 0; i < V3D_SIZE; ++i) {
        v3d_buffer_cc_cb37[i] = input[i] * 3 + 7;
        output[i] = v3d_buffer_cc_cb37[i];
    }
}
