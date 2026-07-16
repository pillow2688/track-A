#include "kernel.h"

void vector_add(
    const int a[VECTOR_SIZE],
    const int b[VECTOR_SIZE],
    int c[VECTOR_SIZE]) {
#pragma HLS INTERFACE ap_memory port=a
#pragma HLS INTERFACE ap_memory port=b
#pragma HLS INTERFACE ap_memory port=c
vector_add_loop:
    for (int i = 0; i < VECTOR_SIZE; ++i) {
#pragma HLS PIPELINE II=16
        c[i] = a[i] + b[i];
    }
}
