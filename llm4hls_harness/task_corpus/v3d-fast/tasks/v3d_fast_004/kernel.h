#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_POINTS = 8;

struct V3DPoint3D {
    int x;
    int y;
    int z;
};

struct V3DPoint2D {
    int x;
    int y;
};

void kernel(
    const V3DPoint3D input[V3D_POINTS],
    V3DPoint2D output[V3D_POINTS]);

#endif
