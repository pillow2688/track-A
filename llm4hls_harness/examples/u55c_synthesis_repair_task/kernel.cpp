#include "kernel.h"

void vector_add_buffered(const int a[16], const int b[16], int c[16]) {
    int *buffer = new int[16];
    for (int i = 0; i < 16; ++i) {
        buffer[i] = a[i] + b[i];
    }
    for (int i = 0; i < 16; ++i) {
        c[i] = buffer[i];
    }
    delete[] buffer;
}
