#pragma once

#include "sim_algorithms/test_algorithms.h"

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef int (*SimAlgorithmNoArgsFn)(void);
typedef int (*SimAlgorithmIntFn)(int value);
typedef int (*SimAlgorithmStringFn)(const char* value_utf8);

typedef enum SimAlgorithmKind {
    SIM_ALGORITHM_NO_ARGS = 0,
    SIM_ALGORITHM_INT = 1,
    SIM_ALGORITHM_STRING = 2
} SimAlgorithmKind;

typedef union SimAlgorithmFunction {
    SimAlgorithmNoArgsFn no_args;
    SimAlgorithmIntFn integer;
    SimAlgorithmStringFn string;
} SimAlgorithmFunction;

typedef struct SimAlgorithmEntry {
    const char* name;
    SimAlgorithmKind kind;
    SimAlgorithmFunction function;
} SimAlgorithmEntry;

SIM_ALGORITHMS_API size_t sim_algorithms_count(void);
SIM_ALGORITHMS_API const SimAlgorithmEntry* sim_algorithms_at(size_t index);
SIM_ALGORITHMS_API const SimAlgorithmEntry* sim_algorithms_find(const char* name);

#ifdef __cplusplus
}
#endif
