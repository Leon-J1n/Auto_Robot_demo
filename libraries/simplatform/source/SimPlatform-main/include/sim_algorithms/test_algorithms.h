#pragma once

#include "sim_algorithms/common.h"

#ifdef __cplusplus
extern "C" {
#endif

SIM_ALGORITHMS_API int testHelloworld(void);
SIM_ALGORITHMS_API int testPrintInt(int num);
SIM_ALGORITHMS_API int testPrintPy(const char* value_utf8);
SIM_ALGORITHMS_API int testCReg(void);
SIM_ALGORITHMS_API int testPyReg(void);

#ifdef __cplusplus
}
#endif
