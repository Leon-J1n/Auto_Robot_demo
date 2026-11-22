#pragma once

#if defined(_WIN32)
#  if defined(SIM_ALGORITHMS_EXPORTS)
#    define SIM_ALGORITHMS_API __declspec(dllexport)
#  else
#    define SIM_ALGORITHMS_API __declspec(dllimport)
#  endif
#else
#  define SIM_ALGORITHMS_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef enum SimAlgorithmStatus {
    SIM_ALGORITHM_OK = 0,
    SIM_ALGORITHM_INVALID_ARGUMENT = 1,
    SIM_ALGORITHM_PYTHON_ERROR = 2
} SimAlgorithmStatus;

SIM_ALGORITHMS_API const char* sim_algorithms_last_error(void);

#ifdef __cplusplus
}
#endif
