#include "sim/logging.hpp"

#include <string>

int main() {
    int evaluations = 0;
    const auto message = [&]() {
        ++evaluations;
        return std::string("compile-time logging check");
    };

    SIM_LOG_VERBOSE(message());
    SIM_LOG_DEBUG("CompileTest", message());
    SIM_LOG_INFO("CompileTest", message());
    SIM_LOG_WARNING("CompileTest", message());
    SIM_LOG_ERROR("CompileTest", message());
    SIM_LOG_FATAL("CompileTest", message());
    SIM_LOG_PERF("CompileTest", message());
    {
        SIM_PROFILE_SCOPE(message());
    }

    constexpr int expected = SIM_LOG_ENABLE_VERBOSE + SIM_LOG_ENABLE_DEBUG +
        SIM_LOG_ENABLE_INFO + SIM_LOG_ENABLE_WARNING + SIM_LOG_ENABLE_ERROR +
        SIM_LOG_ENABLE_FATAL + (SIM_LOG_ENABLE_PERF && SIM_LOG_ENABLE_INFO ? 2 : 0);
    return evaluations == expected ? 0 : 1;
}
