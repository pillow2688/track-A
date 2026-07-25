#ifndef HORIZONTAL_SYNTH_MULTISTAGE_H
#define HORIZONTAL_SYNTH_MULTISTAGE_H

constexpr int HORIZONTAL_SYNTH_SIZE = 16;

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

void kernel(
    const int input[HORIZONTAL_SYNTH_SIZE],
    int output[HORIZONTAL_SYNTH_SIZE]);

#endif
