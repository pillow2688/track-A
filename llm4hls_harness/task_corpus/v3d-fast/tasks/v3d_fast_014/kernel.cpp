#include "kernel.h"

struct v3d_transform_base_ce_046d {
    virtual int apply(int value) const = 0;
};

struct v3d_transform_impl_ce_046d : v3d_transform_base_ce_046d {
    int apply(int value) const override { return value * 3 + 7; }
};

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    v3d_transform_impl_ce_046d implementation;
    const v3d_transform_base_ce_046d* transform = &implementation;
    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = transform->apply(input[i]);
    }
}
