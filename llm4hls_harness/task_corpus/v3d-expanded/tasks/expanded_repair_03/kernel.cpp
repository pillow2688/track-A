#include "kernel.h"
extern "C" void kernel(const int in[EXP_N], int out[EXP_N]){for(int i=0;i<EXP_N;i++) out[i]=in[i]+(i?in[i-1]:0);}
