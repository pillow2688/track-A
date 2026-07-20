#include "kernel.h"

void kernel(
    const int input[V3D_TRANSACTION_SIZE],
    int output[V3D_TRANSACTION_SIZE]) {
#pragma HLS INTERFACE m_axi port=input offset=slave bundle=gmem0 max_read_burst_length=1 num_read_outstanding=1
#pragma HLS INTERFACE m_axi port=output offset=slave bundle=gmem1 max_write_burst_length=1 num_write_outstanding=1
v3d_transaction_loop_197_350e:
    for (int i = 0; i < V3D_TRANSACTION_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = input[i] * 3 + 1;
    }
}
