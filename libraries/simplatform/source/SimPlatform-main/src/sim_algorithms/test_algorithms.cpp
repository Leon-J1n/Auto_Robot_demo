#include "sim_algorithms/test_algorithms.h"

#include "internal.hpp"

#include <cstdio>
#include <string>

namespace {

thread_local std::string last_error;

}  // namespace

namespace sim_algorithms {

void ClearError() { last_error.clear(); }
void SetError(std::string message) { last_error = std::move(message); }

}  // namespace sim_algorithms

extern "C" {

const char* sim_algorithms_last_error(void) { return last_error.c_str(); }

int testHelloworld(void) {
    sim_algorithms::ClearError();
    std::puts("Hello World!");
    std::fflush(stdout);
    return SIM_ALGORITHM_OK;
}

int testPrintInt(int num) {
    sim_algorithms::ClearError();
    std::printf("%d\n", num);
    std::fflush(stdout);
    return SIM_ALGORITHM_OK;
}

int testCReg(void) {
    sim_algorithms::ClearError();
    std::puts("CREG");
    std::fflush(stdout);
    return SIM_ALGORITHM_OK;
}

}  // extern "C"
