#include "dotProduct.h"

// Baseline: functionally correct but unoptimized (no HLS pragmas).
FeatureType
dotProduct(FeatureType param[NUM_FEATURES], DataType feature[NUM_FEATURES]) {
    FeatureType result = 0;
    for (int i = 0; i < NUM_FEATURES; i++) {
        result += param[i] * feature[i];
    }
    return result;
}
