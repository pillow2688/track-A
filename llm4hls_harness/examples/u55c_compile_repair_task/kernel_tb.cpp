#include "kernel.h"

#include <iostream>

int main() {
    int a[16];
    int b[16];
    int c[16] = {};
    for (int i = 0; i < 16; ++i) {
        a[i] = i - 8;
        b[i] = 3 * i + 1;
    }

    vector_add(a, b, c);

    for (int i = 0; i < 16; ++i) {
        const int expected = a[i] + b[i];
        if (c[i] != expected) {
            std::cerr << "mismatch at index " << i << ": got " << c[i]
                      << ", expected " << expected << '\n';
            return 1;
        }
    }
    std::cout << "U55C compile repair task PASS\n";
    return 0;
}
