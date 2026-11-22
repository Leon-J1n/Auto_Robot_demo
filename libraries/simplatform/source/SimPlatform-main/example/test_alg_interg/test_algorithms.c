#include "sim_algorithms/registry.h"
#include "sim_algorithms/test_algorithms.h"

#include <stdio.h>

static int call_no_args_from_registry(const char* name) {
    const SimAlgorithmEntry* entry = sim_algorithms_find(name);
    if (entry == NULL || entry->kind != SIM_ALGORITHM_NO_ARGS) {
        fprintf(stderr, "registry entry is missing or has the wrong kind: %s\n", name);
        return SIM_ALGORITHM_INVALID_ARGUMENT;
    }
    return entry->function.no_args();
}

int main(void) {
    static const char* expected_names[] = {
        "testHelloworld", "testPrintInt", "testPrintPy", "testCReg", "testPyReg",
    };
    if (sim_algorithms_count() != sizeof(expected_names) / sizeof(expected_names[0])) {
        fprintf(stderr, "unexpected C registry size\n");
        return SIM_ALGORITHM_INVALID_ARGUMENT;
    }
    for (size_t index = 0; index < sim_algorithms_count(); ++index) {
        const SimAlgorithmEntry* entry = sim_algorithms_at(index);
        if (entry == NULL || sim_algorithms_find(expected_names[index]) != entry) {
            fprintf(stderr, "C registry mismatch at index %zu\n", index);
            return SIM_ALGORITHM_INVALID_ARGUMENT;
        }
    }

    int status = testHelloworld();
    if (status == SIM_ALGORITHM_OK) status = testPrintInt(42);
    if (status == SIM_ALGORITHM_OK) status = testPrintPy("Python called from C");
    if (status == SIM_ALGORITHM_OK) status = call_no_args_from_registry("testCReg");
    if (status == SIM_ALGORITHM_OK) status = call_no_args_from_registry("testPyReg");
    if (status != SIM_ALGORITHM_OK) {
        fprintf(stderr, "%s\n", sim_algorithms_last_error());
    }
    return status;
}
