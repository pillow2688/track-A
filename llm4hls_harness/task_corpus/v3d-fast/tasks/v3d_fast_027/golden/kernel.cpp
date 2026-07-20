#include "kernel.h"

// V3D_MUTATION_BEGIN
void kernel(
    const int input[V3D_TRANSACTION_SIZE],
    int output[V3D_TRANSACTION_SIZE]) {
#pragma HLS INTERFACE m_axi port=input offset=slave bundle=gmem0 max_read_burst_length=64 num_read_outstanding=16
#pragma HLS INTERFACE m_axi port=output offset=slave bundle=gmem1 max_write_burst_length=64 num_write_outstanding=16
v3d_transaction_loop:
    for (int i = 0; i < V3D_TRANSACTION_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = input[i] * 3 + 1;
    }
}
// V3D_MUTATION_END
