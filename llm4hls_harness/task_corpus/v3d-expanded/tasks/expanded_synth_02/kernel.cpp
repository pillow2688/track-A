#include "kernel.h"
int f(int x){return x>0?f(x-1)+2:5;} extern "C" void kernel(const int in[EXP_N],int out[EXP_N]){for(int i=0;i<EXP_N;i++)out[i]=f(in[i]+8)-16;}
