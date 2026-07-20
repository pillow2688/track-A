#include "kernel.h"

#include <iostream>

int main() {
    const V3DPoint3D input[V3D_POINTS] = {{2, -3, 9}, {-5, 7, 4}, {11, 13, -8}, {0, 4, 6}, {-9, -2, 5}, {8, 1, 3}, {6, -7, 2}, {15, 10, -1}};
    V3DPoint2D output[V3D_POINTS] = {};
    kernel(input, output);
    for (int i = 0; i < V3D_POINTS; ++i) {
        if (output[i].x != input[i].x || output[i].y != input[i].y) {
            std::cerr << "public projection mismatch at " << i << "\n";
            return 1;
        }
    }
    return 0;
}
