#include "kernel.h"
extern "C" void kernel(const int in[EXP_N], int out[EXP_N]){int s=1;for(int i=0;i<EXP_N;i++){s+=in[i];out[i]=s;}}
