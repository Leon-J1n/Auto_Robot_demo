#include "sim/plugin.hpp"
#include "sim/logging.hpp"

#include <algorithm>
#include <fstream>
#include <regex>
#include <sstream>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <dlfcn.h>
#endif

namespace sim {
namespace {

void DefaultLog(int severity, const char* plugin_name, const char* message) {
    if (message == nullptr) return;
    const auto level = severity <= 0 ? logging::Level::Verbose
        : severity == 1 ? logging::Level::Info
        : severity == 2 ? logging::Level::Warning
        : severity == 3 ? logging::Level::Error : logging::Level::Fatal;
    logging::Write(level, plugin_name == nullptr ? "plugin" : plugin_name, message);
}

std::string ReadManifestText(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw Error(ErrorCode::IoError, "cannot open plugin manifest");
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

std::filesystem::path Utf8Path(const std::string& value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

std::string StringField(const std::string& json, const std::string& field, bool required = true) {
    const std::regex expression("\\\"" + field + R"(\"\s*:\s*\"([^\"]*)\")");
    std::smatch match;
    if (std::regex_search(json, match, expression)) return match[1].str();
    if (required) throw Error(ErrorCode::ValidationFailed, "plugin manifest is missing field: " + field);
    return {};
}

std::uint32_t UIntField(const std::string& json, const std::string& field) {
    const std::regex expression("\\\"" + field + R"(\"\s*:\s*([0-9]+))");
    std::smatch match;
    if (!std::regex_search(json, match, expression))
        throw Error(ErrorCode::ValidationFailed, "plugin manifest is missing field: " + field);
    return static_cast<std::uint32_t>(std::stoul(match[1].str()));
}

std::vector<std::string> StringArrayField(const std::string& json, const std::string& field) {
    const std::regex array_expression("\\\"" + field + R"(\"\s*:\s*\[([^\]]*)\])");
    std::smatch array_match;
    if (!std::regex_search(json, array_match, array_expression)) return {};
    std::vector<std::string> values;
    const std::string contents = array_match[1].str();
    const std::regex string_expression(R"(\"([^\"]*)\")");
    for (auto it = std::sregex_iterator(contents.begin(), contents.end(), string_expression);
         it != std::sregex_iterator(); ++it) values.push_back((*it)[1].str());
    return values;
}

std::unordered_map<std::string, bool> CapabilityFields(const std::string& json) {
    const std::regex object_expression(R"(\"capabilities\"\s*:\s*\{([^}]*)\})");
    std::smatch object_match;
    std::unordered_map<std::string, bool> result;
    if (!std::regex_search(json, object_match, object_expression)) return result;
    const std::string contents = object_match[1].str();
    const std::regex field_expression(R"(\"([^\"]+)\"\s*:\s*(true|false))",
                                      std::regex::icase);
    for (auto it = std::sregex_iterator(contents.begin(), contents.end(), field_expression);
         it != std::sregex_iterator(); ++it)
        result.emplace((*it)[1].str(), (*it)[2].str() == "true" || (*it)[2].str() == "TRUE");
    return result;
}

#if defined(_WIN32)
using LibraryHandle = HMODULE;
LibraryHandle OpenLibrary(const std::filesystem::path& path) {
    return LoadLibraryExW(path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
}
void* FindSymbol(LibraryHandle library, const char* name) {
    return reinterpret_cast<void*>(GetProcAddress(library, name));
}
void CloseLibrary(LibraryHandle library) { FreeLibrary(library); }
#else
using LibraryHandle = void*;
LibraryHandle OpenLibrary(const std::filesystem::path& path) { return dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL); }
void* FindSymbol(LibraryHandle library, const char* name) { return dlsym(library, name); }
void CloseLibrary(LibraryHandle library) { dlclose(library); }
#endif

}  // namespace

struct PluginManager::LoadedPlugin {
    SimHostApi host{};
    LibraryHandle library{};
    SimPluginHandle* handle{nullptr};
    SimDestroyPluginFn destroy{nullptr};
    ~LoadedPlugin() {
        if (handle != nullptr && destroy != nullptr) destroy(handle);
        if (library != nullptr) CloseLibrary(library);
    }
};

PluginManager::PluginManager(SimHostApi host) : host_(host) {
    if (host_.api_version == 0) host_.api_version = kPluginApiVersion;
    if (host_.log == nullptr) host_.log = DefaultLog;
    SIM_LOG_VERBOSE("PluginManager::PluginManager completed api_version=" +
                    std::to_string(host_.api_version));
}

PluginManager::~PluginManager() {
    SIM_LOG_VERBOSE("PluginManager::~PluginManager unloads count=" + std::to_string(loaded_.size()));
    loaded_.clear();
}

void PluginManager::discover(const std::filesystem::path& directory) {
    SIM_LOG_VERBOSE("PluginManager::discover scans directory=" + directory.string());
    SIM_PROFILE_SCOPE("Plugin.Discover");
    manifests_.clear();
    if (!std::filesystem::exists(directory)) {
        SIM_LOG_VERBOSE("PluginManager::discover early return missing directory=" + directory.string());
        return;
    }
    for (const auto& entry : std::filesystem::recursive_directory_iterator(directory)) {
        if (!entry.is_regular_file()) continue;
        const auto filename = entry.path().filename().string();
        if (filename.size() >= 15 && filename.ends_with(".simplugin.json"))
            manifests_.push_back(parse_manifest(entry.path()));
    }
    std::sort(manifests_.begin(), manifests_.end(), [](const PluginManifest& lhs, const PluginManifest& rhs) {
        return lhs.name < rhs.name;
    });
    for (std::size_t index = 1; index < manifests_.size(); ++index) {
        if (manifests_[index - 1].name == manifests_[index].name)
            throw Error(ErrorCode::AlreadyExists, "duplicate plugin manifest: " + manifests_[index].name);
    }
    SIM_LOG_VERBOSE("PluginManager::discover completed manifests=" + std::to_string(manifests_.size()));
}

PluginManifest PluginManager::parse_manifest(const std::filesystem::path& path) {
    SIM_LOG_VERBOSE("PluginManager::parse_manifest reads path=" + path.string());
    const std::string json = ReadManifestText(path);
    PluginManifest manifest;
    manifest.name = StringField(json, "name");
    manifest.version = StringField(json, "version");
    manifest.plugin_api_version = UIntField(json, "plugin_api_version");
    manifest.type = StringField(json, "type");
    manifest.library = Utf8Path(StringField(json, "library"));
    manifest.platforms = StringArrayField(json, "platforms");
    manifest.required_dependencies = StringArrayField(json, "required_dependencies");
    manifest.capabilities = CapabilityFields(json);
    manifest.manifest_path = path;
    if (manifest.name.empty() || manifest.version.empty() || manifest.library.empty())
        throw Error(ErrorCode::ValidationFailed, "plugin manifest contains an empty required field");
    SIM_LOG_VERBOSE("PluginManager::parse_manifest completed name=" + manifest.name +
                    " dependencies=" + std::to_string(manifest.required_dependencies.size()));
    return manifest;
}

std::string PluginManager::current_platform() {
#if defined(_WIN32) && defined(_WIN64)
    return "windows-x64";
#elif defined(__linux__) && defined(__x86_64__)
    return "linux-x64";
#elif defined(__linux__) && defined(__aarch64__)
    return "linux-arm64";
#else
    return "unknown";
#endif
}

const PluginManifest& PluginManager::manifest(const std::string& name) const {
    const auto it = std::find_if(manifests_.begin(), manifests_.end(), [&](const PluginManifest& item) {
        return item.name == name;
    });
    if (it == manifests_.end()) {
        SIM_LOG_VERBOSE("PluginManager::manifest early return missing name=" + name);
        throw Error(ErrorCode::NotFound, "plugin manifest not found: " + name);
    }
    SIM_LOG_VERBOSE("PluginManager::manifest returns name=" + name);
    return *it;
}

void PluginManager::load(const std::string& name) {
    SIM_LOG_VERBOSE("PluginManager::load resolves plugin=" + name);
    SIM_PROFILE_SCOPE("Plugin.Load");
    std::vector<std::string> stack;
    load_recursive(manifest(name), stack);
    SIM_LOG_VERBOSE("PluginManager::load completed plugin=" + name);
}

void PluginManager::load_recursive(const PluginManifest& item, std::vector<std::string>& stack) {
    SIM_LOG_VERBOSE("PluginManager::load_recursive validates plugin=" + item.name +
                    " dependency_depth=" + std::to_string(stack.size()));
    if (loaded(item.name)) {
        SIM_LOG_VERBOSE("PluginManager::load_recursive skips already loaded plugin=" + item.name);
        return;
    }
    if (std::find(stack.begin(), stack.end(), item.name) != stack.end())
        throw Error(ErrorCode::ValidationFailed, "plugin dependency cycle includes: " + item.name);
    if (item.plugin_api_version != kPluginApiVersion)
        throw Error(ErrorCode::ValidationFailed, "plugin API version mismatch: " + item.name);
    if (!item.platforms.empty() && std::find(item.platforms.begin(), item.platforms.end(), current_platform()) == item.platforms.end())
        throw Error(ErrorCode::Unsupported, "plugin does not support platform " + current_platform());
    stack.push_back(item.name);
    for (const auto& dependency : item.required_dependencies) load_recursive(manifest(dependency), stack);
    stack.pop_back();

    const auto library_path = item.library.is_absolute() ? item.library : item.manifest_path.parent_path() / item.library;
    const LibraryHandle library = OpenLibrary(library_path);
    if (library == nullptr) throw Error(ErrorCode::IoError, "cannot load plugin library: " + library_path.string());
    auto version = reinterpret_cast<SimPluginApiVersionFn>(FindSymbol(library, "SimPluginApiVersion"));
    auto create = reinterpret_cast<SimCreatePluginFn>(FindSymbol(library, "SimCreatePlugin"));
    auto destroy = reinterpret_cast<SimDestroyPluginFn>(FindSymbol(library, "SimDestroyPlugin"));
    if (version == nullptr || create == nullptr || destroy == nullptr || version() != kPluginApiVersion) {
        CloseLibrary(library);
        throw Error(ErrorCode::ValidationFailed, "plugin does not expose the required C ABI: " + item.name);
    }
    auto loaded_plugin = std::make_shared<LoadedPlugin>();
    loaded_plugin->host = host_;
    loaded_plugin->library = library;
    loaded_plugin->destroy = destroy;
    loaded_plugin->handle = create(&loaded_plugin->host);
    if (loaded_plugin->handle == nullptr) {
        throw Error(ErrorCode::BackendUnavailable, "plugin creation failed: " + item.name);
    }
    loaded_.emplace(item.name, std::move(loaded_plugin));
    SIM_LOG_VERBOSE("PluginManager::load_recursive completed plugin=" + item.name);
}

bool PluginManager::unload(const std::string& name) {
    SIM_LOG_VERBOSE("PluginManager::unload releases plugin=" + name);
    for (const auto& manifest_item : manifests_) {
        if (loaded(manifest_item.name) && std::find(manifest_item.required_dependencies.begin(),
                                                    manifest_item.required_dependencies.end(), name) !=
                                                   manifest_item.required_dependencies.end())
            throw Error(ErrorCode::InvalidState, "plugin is still required by: " + manifest_item.name);
    }
    const auto found = loaded_.find(name);
    if (found == loaded_.end()) {
        SIM_LOG_VERBOSE("PluginManager::unload early return not loaded plugin=" + name);
        return false;
    }
    if (found->second.use_count() != 1)
        throw Error(ErrorCode::InvalidState, "plugin service is still in use: " + name);
    loaded_.erase(found);
    SIM_LOG_VERBOSE("PluginManager::unload completed plugin=" + name);
    return true;
}

bool PluginManager::loaded(const std::string& name) const {
    const bool result = loaded_.contains(name);
    SIM_LOG_VERBOSE("PluginManager::loaded name=" + name + " result=" + (result ? "true" : "false"));
    return result;
}

std::shared_ptr<IPhysicsBackend> PluginManager::create_physics_backend(
    const std::string& name) {
    const auto& item = manifest(name);
    if (item.type != "physics_backend")
        throw Error(ErrorCode::InvalidArgument, "plugin is not a physics backend: " + name);
    load(name);
    const auto owner = loaded_.at(name);
    auto create = reinterpret_cast<SimCreatePhysicsBackendFn>(
        FindSymbol(owner->library, "SimCreatePhysicsBackend"));
    auto destroy = reinterpret_cast<SimDestroyPhysicsBackendFn>(
        FindSymbol(owner->library, "SimDestroyPhysicsBackend"));
    if (create == nullptr || destroy == nullptr)
        throw Error(ErrorCode::ValidationFailed,
                    "physics plugin does not expose the required backend factory: " + name);
    IPhysicsBackend* backend = create(owner->handle);
    if (backend == nullptr)
        throw Error(ErrorCode::BackendUnavailable,
                    "physics backend creation failed: " + name);
    return std::shared_ptr<IPhysicsBackend>(backend,
        [owner, destroy](IPhysicsBackend* value) { destroy(owner->handle, value); });
}

std::shared_ptr<ICollisionBackend> PluginManager::create_collision_backend(
    const std::string& name) {
    const auto& item = manifest(name);
    if (item.type != "collision_backend")
        throw Error(ErrorCode::InvalidArgument, "plugin is not a collision backend: " + name);
    load(name);
    const auto owner = loaded_.at(name);
    auto create = reinterpret_cast<SimCreateCollisionBackendFn>(
        FindSymbol(owner->library, "SimCreateCollisionBackend"));
    auto destroy = reinterpret_cast<SimDestroyCollisionBackendFn>(
        FindSymbol(owner->library, "SimDestroyCollisionBackend"));
    if (create == nullptr || destroy == nullptr)
        throw Error(ErrorCode::ValidationFailed,
                    "collision plugin does not expose the required backend factory: " + name);
    ICollisionBackend* backend = create(owner->handle);
    if (backend == nullptr)
        throw Error(ErrorCode::BackendUnavailable,
                    "collision backend creation failed: " + name);
    return std::shared_ptr<ICollisionBackend>(backend,
        [owner, destroy](ICollisionBackend* value) { destroy(owner->handle, value); });
}

}  // namespace sim
