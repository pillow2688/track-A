#include "kernel.h"
extern "C" void kernel(const int in[EXP_N], int out[EXP_N]){for(int i=0;i<EXP_N-1;i++) out[i]=in[i]*2+1;}
