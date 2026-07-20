#include "kernel.h"

struct v3d_exception_cd_de6b {};

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int i = 0; i < V3D_SIZE; ++i) {
        try {
            if (input[i] == 2147483647) throw v3d_exception_cd_de6b{};
            output[i] = input[i] * 3 + 7;
        } catch (const v3d_exception_cd_de6b&) {
            output[i] = 0;
        }
    }
}
