#include "sim/foundation.hpp"
#include "sim/logging.hpp"

#include <algorithm>
#include <cctype>
#include <fstream>

namespace sim {
namespace {

std::string Trim(std::string value) {
    const auto first = std::find_if_not(value.begin(), value.end(), [](unsigned char item) { return std::isspace(item); });
    const auto last = std::find_if_not(value.rbegin(), value.rend(), [](unsigned char item) { return std::isspace(item); }).base();
    return first < last ? std::string(first, last) : std::string{};
}

}  // namespace

Logger::SinkId Logger::add_sink(Sink sink) {
    if (!sink) throw Error(ErrorCode::InvalidArgument, "log sink cannot be empty");
    std::scoped_lock lock(mutex_);
    const SinkId id = next_id_++;
    sinks_.emplace(id, std::move(sink));
    return id;
}

bool Logger::remove_sink(SinkId id) {
    std::scoped_lock lock(mutex_);
    return sinks_.erase(id) != 0;
}

void Logger::log(LogLevel level, std::string category, std::string message) const {
    if (level < level_ || !logging::CompiledIn(static_cast<logging::Level>(level))) return;
    const LogRecord record{std::chrono::system_clock::now(), level, std::move(category), std::move(message)};
    logging::Write(static_cast<logging::Level>(level), record.category, record.message);
    std::vector<Sink> sinks;
    {
        std::scoped_lock lock(mutex_);
        for (const auto& [_, sink] : sinks_) sinks.push_back(sink);
    }
    for (const auto& sink : sinks) sink(record);
}

Config Config::load(const std::filesystem::path& path) {
    std::ifstream input(path);
    if (!input) throw Error(ErrorCode::IoError, "cannot open configuration file");
    Config config;
    std::string section;
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        line = Trim(line);
        if (line.empty() || line.starts_with('#')) continue;
        if (line.front() == '[' && line.back() == ']') {
            section = Trim(line.substr(1, line.size() - 2));
            continue;
        }
        const auto equals = line.find('=');
        if (equals == std::string::npos)
            throw Error(ErrorCode::ValidationFailed, "invalid configuration line " + std::to_string(line_number));
        std::string key = Trim(line.substr(0, equals));
        std::string value = Trim(line.substr(equals + 1));
        if (value.size() >= 2 && value.front() == '"' && value.back() == '"')
            value = value.substr(1, value.size() - 2);
        config.set(section.empty() ? key : section + "." + key, value);
    }
    return config;
}

void Config::set(std::string key, std::string value) {
    if (key.empty()) throw Error(ErrorCode::InvalidArgument, "configuration key cannot be empty");
    values_.insert_or_assign(std::move(key), std::move(value));
}

bool Config::contains(std::string_view key) const { return values_.contains(std::string(key)); }

std::string Config::string(std::string_view key) const {
    const auto it = values_.find(std::string(key));
    if (it == values_.end()) throw Error(ErrorCode::NotFound, "configuration key not found: " + std::string(key));
    return it->second;
}

double Config::number(std::string_view key) const {
    const std::string value = string(key);
    std::size_t parsed = 0;
    const double result = std::stod(value, &parsed);
    if (parsed != value.size() || !std::isfinite(result))
        throw Error(ErrorCode::ValidationFailed, "configuration value is not a finite number: " + std::string(key));
    return result;
}

bool Config::boolean(std::string_view key) const {
    std::string value = string(key);
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char item) { return static_cast<char>(std::tolower(item)); });
    if (value == "true" || value == "1") return true;
    if (value == "false" || value == "0") return false;
    throw Error(ErrorCode::ValidationFailed, "configuration value is not boolean: " + std::string(key));
}

void Profiler::record(std::string name, std::chrono::steady_clock::duration duration) {
    const double milliseconds = std::chrono::duration<double, std::milli>(duration).count();
    std::scoped_lock lock(mutex_);
    auto& sample = samples_[std::move(name)];
    ++sample.calls;
    sample.total_milliseconds += milliseconds;
}

std::unordered_map<std::string, ProfileSample> Profiler::snapshot() const {
    std::scoped_lock lock(mutex_);
    return samples_;
}

void Profiler::clear() {
    std::scoped_lock lock(mutex_);
    samples_.clear();
}

ScopedProfile::ScopedProfile(Profiler& profiler, std::string name)
    : profiler_(&profiler), name_(std::move(name)), start_(std::chrono::steady_clock::now()) {}

ScopedProfile::~ScopedProfile() {
    const auto duration = std::chrono::steady_clock::now() - start_;
    profiler_->record(name_, duration);
    SIM_LOG_PERF("Perf", "[Perf] Name=" + name_ +
        " DurationMs=" + std::to_string(
            std::chrono::duration<double, std::milli>(duration).count()));
}

}  // namespace sim
