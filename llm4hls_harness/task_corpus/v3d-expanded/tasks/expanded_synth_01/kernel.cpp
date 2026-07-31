#include "kernel.h"
extern "C" void kernel(const int in[EXP_N],int out[EXP_N]){int *tmp=new int[EXP_N];for(int i=0;i<EXP_N;i++){tmp[i]=in[i]*2+5;out[i]=tmp[i];}delete [] tmp;}
