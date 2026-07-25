#ifndef HORIZONTAL_REPAIR_MULTISTAGE_H
#define HORIZONTAL_REPAIR_MULTISTAGE_H

#include <ap_int.h>

constexpr int HORIZONTAL_REPAIR_SIZE = 8;

void kernel(
    const ap_int<16> input[HORIZONTAL_REPAIR_SIZE],
    ap_int<32> output[HORIZONTAL_REPAIR_SIZE]);

#endif

