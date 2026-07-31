#include "kernel.h"
#include <cstdio>
int main(){int in[EXP_N];int out[EXP_N]={0};for(int i=0;i<EXP_N;i++)in[i]=i-8;kernel(in,out);for(int i=0;i<EXP_N;i++){int want=in[EXP_N-1-i];if(out[i]!=want){std::fprintf(stderr,"mismatch %d: %d != %d\n",i,out[i],want);return 1;}}return 0;}
