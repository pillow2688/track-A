#include "kernel.h"

#include <climits>
#include <iostream>

namespace {

int run_case(int case_id) {
    int a[VECTOR_SIZE] = {};
    int b[VECTOR_SIZE] = {};
    int c[VECTOR_SIZE] = {};

    for (int i = 0; i < VECTOR_SIZE; ++i) {
        if (case_id == 0) {
            a[i] = i;
            b[i] = 2 * i;
        } else if (case_id == 1) {
            a[i] = -i;
            b[i] = i / 2;
        } else if (case_id == 2) {
            a[i] = 0;
            b[i] = 0;
        } else {
            a[i] = (i % 2 == 0) ? (INT_MAX - i) : (INT_MIN + i);
            b[i] = (i % 2 == 0) ? i : -i;
        }
    }
    if (case_id == 3) {
        a[0] = INT_MAX;
        b[0] = 0;
        a[1] = INT_MIN;
        b[1] = 0;
    }

    vector_add(a, b, c);

    for (int i = 0; i < VECTOR_SIZE; ++i) {
        const int expected = a[i] + b[i];
        if (c[i] != expected) {
            std::cerr << "case " << case_id << " mismatch at index " << i
                      << ": got " << c[i] << ", expected " << expected << '\n';
            return 10 + case_id;
        }
    }
    return 0;
}

}  // namespace

int main() {
    for (int case_id = 0; case_id < 4; ++case_id) {
        const int result = run_case(case_id);
        if (result != 0) {
            return result;
        }
    }
    std::cout << "U55C V2 optimize task PASS\n";
    return 0;
}
