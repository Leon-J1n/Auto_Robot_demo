#include "sim_algorithms/registry.h"

#include <cstring>

namespace {

const SimAlgorithmEntry entries[] = {
    {"testHelloworld", SIM_ALGORITHM_NO_ARGS, {.no_args = testHelloworld}},
    {"testPrintInt", SIM_ALGORITHM_INT, {.integer = testPrintInt}},
    {"testPrintPy", SIM_ALGORITHM_STRING, {.string = testPrintPy}},
    {"testCReg", SIM_ALGORITHM_NO_ARGS, {.no_args = testCReg}},
    {"testPyReg", SIM_ALGORITHM_NO_ARGS, {.no_args = testPyReg}},
};

}  // namespace

extern "C" {

size_t sim_algorithms_count(void) { return sizeof(entries) / sizeof(entries[0]); }

const SimAlgorithmEntry* sim_algorithms_at(size_t index) {
    return index < sim_algorithms_count() ? &entries[index] : nullptr;
}

const SimAlgorithmEntry* sim_algorithms_find(const char* name) {
    if (name == nullptr) return nullptr;
    for (const auto& entry : entries) {
        if (std::strcmp(entry.name, name) == 0) return &entry;
    }
    return nullptr;
}

}  // extern "C"
