#include "kernel.h"

#include <functional>

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    std::function<int(int)> v3d_callable_cb_2e77 = [](int value) {
        return value * 3 + 7;
    };
    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = v3d_callable_cb_2e77(input[i]);
    }
}
