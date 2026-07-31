#include "kernel.h"
#include <vector>
extern "C" void kernel(const int in[EXP_N],int out[EXP_N]){std::vector<int> v(EXP_N);for(int i=0;i<EXP_N;i++){v[i]=in[i]*2+5;out[i]=v[i];}}
