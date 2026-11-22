#include "sim_algorithms/test_algorithms.h"

#include "internal.hpp"

#if defined(_WIN32)
#  include <process.h>
#else
#  include <sys/wait.h>
#  include <unistd.h>
#endif

#include <string>
#include <string_view>

namespace {

std::string Base64Encode(std::string_view input) {
    static constexpr char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string output;
    output.reserve(((input.size() + 2U) / 3U) * 4U);
    for (std::size_t offset = 0; offset < input.size(); offset += 3U) {
        const auto first = static_cast<unsigned char>(input[offset]);
        const auto second = offset + 1U < input.size()
            ? static_cast<unsigned char>(input[offset + 1U]) : 0U;
        const auto third = offset + 2U < input.size()
            ? static_cast<unsigned char>(input[offset + 2U]) : 0U;
        const unsigned value = (static_cast<unsigned>(first) << 16U) |
                               (static_cast<unsigned>(second) << 8U) |
                               static_cast<unsigned>(third);
        output.push_back(alphabet[(value >> 18U) & 0x3FU]);
        output.push_back(alphabet[(value >> 12U) & 0x3FU]);
        output.push_back(offset + 1U < input.size() ? alphabet[(value >> 6U) & 0x3FU] : '=');
        output.push_back(offset + 2U < input.size() ? alphabet[value & 0x3FU] : '=');
    }
    return output;
}

int InvokePython(std::string_view function, const char* value_utf8 = nullptr) {
    const std::string function_name(function);
    const std::string encoded = value_utf8 == nullptr
        ? std::string{} : "b64:" + Base64Encode(value_utf8);
    std::fflush(stdout);
    int status = -1;
#if defined(_WIN32)
    const char* arguments[] = {
        SIM_ALGORITHMS_PYTHON_EXECUTABLE,
        SIM_ALGORITHMS_BRIDGE_SCRIPT,
        function_name.c_str(),
        value_utf8 == nullptr ? nullptr : encoded.c_str(),
        nullptr,
    };
    status = static_cast<int>(_spawnv(
        _P_WAIT, SIM_ALGORITHMS_PYTHON_EXECUTABLE, arguments));
#else
    const pid_t child = fork();
    if (child == 0) {
        if (value_utf8 == nullptr) {
            execl(SIM_ALGORITHMS_PYTHON_EXECUTABLE, SIM_ALGORITHMS_PYTHON_EXECUTABLE,
                  SIM_ALGORITHMS_BRIDGE_SCRIPT, function_name.c_str(),
                  static_cast<char*>(nullptr));
        } else {
            execl(SIM_ALGORITHMS_PYTHON_EXECUTABLE, SIM_ALGORITHMS_PYTHON_EXECUTABLE,
                  SIM_ALGORITHMS_BRIDGE_SCRIPT, function_name.c_str(), encoded.c_str(),
                  static_cast<char*>(nullptr));
        }
        _exit(127);
    }
    if (child > 0) {
        int wait_status = 0;
        status = waitpid(child, &wait_status, 0) == child && WIFEXITED(wait_status)
            ? WEXITSTATUS(wait_status) : -1;
    }
#endif
    if (status != 0) {
        sim_algorithms::SetError("Python algorithm bridge failed with status " +
                                 std::to_string(status));
        return SIM_ALGORITHM_PYTHON_ERROR;
    }
    sim_algorithms::ClearError();
    return SIM_ALGORITHM_OK;
}

}  // namespace

extern "C" {

int testPrintPy(const char* value_utf8) {
    if (value_utf8 == nullptr) {
        sim_algorithms::SetError("testPrintPy value must not be null");
        return SIM_ALGORITHM_INVALID_ARGUMENT;
    }
    return InvokePython("testPrintPy", value_utf8);
}

int testPyReg(void) { return InvokePython("testPyReg"); }

}  // extern "C"
