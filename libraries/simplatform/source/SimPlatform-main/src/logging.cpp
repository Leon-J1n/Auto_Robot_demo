#include "sim/logging.hpp"

#include <bq_common/bq_common.h>
#include <bq_log/bq_log.h>

#include <mutex>

namespace sim::logging {
namespace {

bq::log& Log() {
    static bq::log instance = bq::log::create_log(
        "sim_core",
        "appenders_config.binary.type=compressed_file\n"
        "appenders_config.binary.levels=[all]\n"
        "appenders_config.binary.file_name=logs/sim_core\n"
        "appenders_config.binary.always_create_new_file=true\n"
        "log.buffer_size=65535\n"
        "log.recovery=true\n"
        "log.thread_mode=async\n");
    return instance;
}

std::mutex& LogMutex() {
    static std::mutex mutex;
    return mutex;
}

}  // namespace

void Write(Level level, std::string_view category, std::string_view message) {
    if (!CompiledIn(level)) return;
    std::scoped_lock lock(LogMutex());
    auto& log = Log();
    const std::string category_text(category);
    const std::string message_text(message);
    switch (level) {
        case Level::Verbose: log.verbose("{}: {}", category_text, message_text); break;
        case Level::Debug: log.debug("{}: {}", category_text, message_text); break;
        case Level::Info: log.info("{}: {}", category_text, message_text); break;
        case Level::Warning: log.warning("{}: {}", category_text, message_text); break;
        case Level::Error: log.error("{}: {}", category_text, message_text); break;
        case Level::Fatal: log.fatal("{}: {}", category_text, message_text); break;
    }
}

void WriteVerbose(const char* file, const char* function, int line,
                  std::string_view behavior) {
#if SIM_LOG_ENABLE_VERBOSE
    const std::string event(behavior);
    std::scoped_lock lock(LogMutex());
    Log().verbose("{}:{}:{} {}", file, function, line, event);
#else
    (void)file; (void)function; (void)line; (void)behavior;
#endif
}

void Flush() {
    std::scoped_lock lock(LogMutex());
    Log().force_flush();
}

ScopedProfilerTimer::ScopedProfilerTimer(std::string name) noexcept
    : name_(std::move(name)), started_(std::chrono::steady_clock::now()) {}

ScopedProfilerTimer::~ScopedProfilerTimer() {
#if SIM_LOG_ENABLE_PERF && SIM_LOG_ENABLE_INFO
    const double milliseconds = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - started_).count();
    SIM_LOG_PERF("Perf", "[Perf] Name=" + name_ +
                 " DurationMs=" + std::to_string(milliseconds));
#endif
}

}  // namespace sim::logging
