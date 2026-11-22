#pragma once

#include "sim/types.hpp"

#include <chrono>
#include <string>
#include <string_view>

namespace sim::logging {

enum class Level : int { Verbose, Debug, Info, Warning, Error, Fatal };

#ifndef SIM_LOG_ENABLE_VERBOSE
#define SIM_LOG_ENABLE_VERBOSE 1
#endif
#ifndef SIM_LOG_ENABLE_DEBUG
#define SIM_LOG_ENABLE_DEBUG 1
#endif
#ifndef SIM_LOG_ENABLE_INFO
#define SIM_LOG_ENABLE_INFO 1
#endif
#ifndef SIM_LOG_ENABLE_WARNING
#define SIM_LOG_ENABLE_WARNING 1
#endif
#ifndef SIM_LOG_ENABLE_ERROR
#define SIM_LOG_ENABLE_ERROR 1
#endif
#ifndef SIM_LOG_ENABLE_FATAL
#define SIM_LOG_ENABLE_FATAL 1
#endif
#ifndef SIM_LOG_ENABLE_PERF
#define SIM_LOG_ENABLE_PERF 1
#endif

[[nodiscard]] constexpr bool CompiledIn(Level level) noexcept {
    switch (level) {
        case Level::Verbose: return SIM_LOG_ENABLE_VERBOSE;
        case Level::Debug: return SIM_LOG_ENABLE_DEBUG;
        case Level::Info: return SIM_LOG_ENABLE_INFO;
        case Level::Warning: return SIM_LOG_ENABLE_WARNING;
        case Level::Error: return SIM_LOG_ENABLE_ERROR;
        case Level::Fatal: return SIM_LOG_ENABLE_FATAL;
    }
    return false;
}

void Write(Level level, std::string_view category, std::string_view message);
void WriteVerbose(const char* file, const char* function, int line,
                  std::string_view behavior);
void Flush();

class ScopedProfilerTimer {
public:
    explicit ScopedProfilerTimer(std::string name) noexcept;
    ~ScopedProfilerTimer();
    ScopedProfilerTimer(const ScopedProfilerTimer&) = delete;
    ScopedProfilerTimer& operator=(const ScopedProfilerTimer&) = delete;

private:
    std::string name_;
    std::chrono::steady_clock::time_point started_;
};

}  // namespace sim::logging

#if SIM_LOG_ENABLE_VERBOSE
#define SIM_LOG_VERBOSE(behavior) \
    ::sim::logging::WriteVerbose(__FILE__, __func__, __LINE__, (behavior))
#else
#define SIM_LOG_VERBOSE(behavior) ((void)0)
#endif

#if SIM_LOG_ENABLE_DEBUG
#define SIM_LOG_DEBUG(category, message) \
    ::sim::logging::Write(::sim::logging::Level::Debug, (category), (message))
#else
#define SIM_LOG_DEBUG(category, message) ((void)0)
#endif

#if SIM_LOG_ENABLE_INFO
#define SIM_LOG_INFO(category, message) \
    ::sim::logging::Write(::sim::logging::Level::Info, (category), (message))
#else
#define SIM_LOG_INFO(category, message) ((void)0)
#endif

#if SIM_LOG_ENABLE_WARNING
#define SIM_LOG_WARNING(category, message) \
    ::sim::logging::Write(::sim::logging::Level::Warning, (category), (message))
#else
#define SIM_LOG_WARNING(category, message) ((void)0)
#endif

#if SIM_LOG_ENABLE_ERROR
#define SIM_LOG_ERROR(category, message) \
    ::sim::logging::Write(::sim::logging::Level::Error, (category), (message))
#else
#define SIM_LOG_ERROR(category, message) ((void)0)
#endif

#if SIM_LOG_ENABLE_FATAL
#define SIM_LOG_FATAL(category, message) \
    ::sim::logging::Write(::sim::logging::Level::Fatal, (category), (message))
#else
#define SIM_LOG_FATAL(category, message) ((void)0)
#endif

#if SIM_LOG_ENABLE_PERF && SIM_LOG_ENABLE_INFO
#define SIM_LOG_PERF(category, message) SIM_LOG_INFO((category), (message))
#define SIM_PROFILE_SCOPE(name) \
    ::sim::logging::ScopedProfilerTimer sim_profile_timer_##__LINE__(name)
#else
#define SIM_LOG_PERF(category, message) ((void)0)
#define SIM_PROFILE_SCOPE(name) ((void)0)
#endif
