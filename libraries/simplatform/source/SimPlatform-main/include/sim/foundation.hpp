#pragma once

#include "sim/types.hpp"

#include <chrono>
#include <filesystem>
#include <functional>
#include <mutex>
#include <unordered_map>

namespace sim {

enum class LogLevel { Verbose, Debug, Info, Warning, Error, Fatal, Trace = Verbose };

struct LogRecord {
    std::chrono::system_clock::time_point timestamp;
    LogLevel level{LogLevel::Info};
    std::string category;
    std::string message;
};

class Logger {
public:
    using SinkId = std::uint64_t;
    using Sink = std::function<void(const LogRecord&)>;

    [[nodiscard]] SinkId add_sink(Sink sink);
    bool remove_sink(SinkId id);
    void set_level(LogLevel level) noexcept { level_ = level; }
    void log(LogLevel level, std::string category, std::string message) const;

private:
    mutable std::mutex mutex_;
    std::unordered_map<SinkId, Sink> sinks_;
    SinkId next_id_{1};
    LogLevel level_{LogLevel::Info};
};

class Config {
public:
    static Config load(const std::filesystem::path& path);
    void set(std::string key, std::string value);
    [[nodiscard]] bool contains(std::string_view key) const;
    [[nodiscard]] std::string string(std::string_view key) const;
    [[nodiscard]] double number(std::string_view key) const;
    [[nodiscard]] bool boolean(std::string_view key) const;

private:
    std::unordered_map<std::string, std::string> values_;
};

struct ProfileSample { std::uint64_t calls{0}; double total_milliseconds{0.0}; };

class Profiler {
public:
    void record(std::string name, std::chrono::steady_clock::duration duration);
    [[nodiscard]] std::unordered_map<std::string, ProfileSample> snapshot() const;
    void clear();

private:
    mutable std::mutex mutex_;
    std::unordered_map<std::string, ProfileSample> samples_;
};

class ScopedProfile {
public:
    ScopedProfile(Profiler& profiler, std::string name);
    ~ScopedProfile();

private:
    Profiler* profiler_;
    std::string name_;
    std::chrono::steady_clock::time_point start_;
};

}  // namespace sim
