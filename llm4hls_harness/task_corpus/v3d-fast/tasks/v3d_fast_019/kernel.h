#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_SIZE = 16;

#ifdef __SYNTHESIS__
#include <hls_stream.h>
#else
#include <queue>
namespace hls {
template <typename T>
class stream {
  public:
    explicit stream(const char* = "") {}
    void write(const T& value) { values_.push(value); }
    T read() {
        T value = values_.front();
        values_.pop();
        return value;
    }

  private:
    std::queue<T> values_;
};
}  // namespace hls
#endif

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]);

#endif
