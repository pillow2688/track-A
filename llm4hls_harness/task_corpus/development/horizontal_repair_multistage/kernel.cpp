#include "kernel.h"

static ap_int<32> add_unit_steps(ap_int<32> value, int steps) {
    if (steps == 0) {
        return value;
    }
    return add_unit_steps(value + 1, steps - 1);
}

void kernel(
    const ap_int<16> input[HORIZONTAL_REPAIR_SIZE],
    ap_int<32> output[HORIZONTAL_REPAIR_SIZE]) {
    for (int i = 0; i < HORIZONTAL_REPAIR_SIZE; ++i) {
        if (input[i] < 0) {
            output[i] = add_unit_steps(input[i] * 2, 2);
        } else {
            output[i] = add_unit_steps(input[i] * 3, 4);
        }
    }
}
