#include "kernel.h"

#include <iostream>

int main() {
    const V3DPoint3D input[V3D_POINTS] = {{17, -9, 4}, {-6, 31, -2}, {8, 5, 99}, {0, -12, 7}, {43, 2, -8}, {-21, -17, 3}, {9, 28, 6}, {3, -4, 11}};
    V3DPoint2D output[V3D_POINTS] = {};
    kernel(input, output);
    for (int i = 0; i < V3D_POINTS; ++i) {
        if (output[i].x != input[i].x || output[i].y != input[i].y) {
            std::cerr << "hidden-like projection mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
