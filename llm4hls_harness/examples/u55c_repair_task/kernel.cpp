#include "kernel.h"

void vector_add(const int a[16], const int b[16], int c[16]) {
    for (int i = 0; i < 16; ++i) {
        c[i] = a[i] - b[i];
    }
}
