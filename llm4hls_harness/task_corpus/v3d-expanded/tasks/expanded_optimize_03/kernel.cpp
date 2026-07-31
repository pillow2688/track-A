#include "kernel.h"
extern "C" void kernel(const int in[EXP_N],int out[EXP_N]){for(int i=0;i<EXP_N;i++){int x=in[i];if(i)x+=in[i-1];if(i+1<EXP_N)x+=in[i+1];out[i]=x;}}
