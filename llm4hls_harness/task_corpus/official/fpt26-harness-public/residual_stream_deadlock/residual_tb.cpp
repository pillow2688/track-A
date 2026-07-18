#include "residual.h"
#include <iostream>

int main() {
    data_t in[N], out[N];
    for (int i = 0; i < N; i++) in[i] = i;

    residual(in, out);

    int test_failures = 0;
    for (int i = 0; i < N; i++) {
        data_t expected = 2 * in[i] + in[i];
        if (out[i] != expected) {
            std::cout << "Mismatch at " << i << ": expected " << expected
                      << ", got " << out[i] << std::endl;
            test_failures++;
        }
    }

    if (test_failures == 0) {
        std::cout << "All test cases passed!" << std::endl;
        return 0;
    } else {
        std::cout << test_failures << " test case(s) failed!" << std::endl;
        return 1;
    }
}
