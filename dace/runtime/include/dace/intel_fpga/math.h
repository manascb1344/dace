/**
    This file contains a set of preprocessor macros useful
    for simple arithmetics
*/

#define int_ceil(N,D) ((int)((N+D-1)/D))
#define int_floor(N,D) (1+(int)((N-1)/D))
