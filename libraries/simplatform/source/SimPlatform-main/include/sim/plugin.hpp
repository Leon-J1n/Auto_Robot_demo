#pragma once

#include "sim/collision.hpp"
#include "sim/physics.hpp"

#include <filesystem>
#include <memory>

#if defined(_WIN32)
#  if defined(SIM_PLUGIN_EXPORTS)
#    define SIM_PLUGIN_API __declspec(dllexport)
#  else
#    define SIM_PLUGIN_API __declspec(dllimport)
#  endif
#else
#  define SIM_PLUGIN_API __attribute__((visibility("default")))
#endif

extern "C" {

struct SimPluginHandle;

struct SimHostApi {
    std::uint32_t api_version;
    void (*log)(int severity, const char* plugin_name, const char* message);
};

using SimPluginApiVersionFn = std::uint32_t (*)();
using SimCreatePluginFn = SimPluginHandle* (*)(const SimHostApi* host);
using SimDestroyPluginFn = void (*)(SimPluginHandle* plugin);

}

namespace sim {

using SimCreatePhysicsBackendFn = IPhysicsBackend* (*)(SimPluginHandle* plugin);
using SimDestroyPhysicsBackendFn = void (*)(SimPluginHandle* plugin,
                                             IPhysicsBackend* backend);
using SimCreateCollisionBackendFn = ICollisionBackend* (*)(SimPluginHandle* plugin);
using SimDestroyCollisionBackendFn = void (*)(SimPluginHandle* plugin,
                                               ICollisionBackend* backend);

struct PluginManifest {
    std::string name;
    std::string version;
    std::uint32_t plugin_api_version{0};
    std::string type;
    std::filesystem::path library;
    std::vector<std::string> platforms;
    std::vector<std::string> required_dependencies;
    std::unordered_map<std::string, bool> capabilities;
    std::filesystem::path manifest_path;
};

class PluginManager {
public:
    explicit PluginManager(SimHostApi host = {});
    ~PluginManager();
    PluginManager(const PluginManager&) = delete;
    PluginManager& operator=(const PluginManager&) = delete;

    void discover(const std::filesystem::path& directory);
    [[nodiscard]] const std::vector<PluginManifest>& manifests() const noexcept { return manifests_; }
    void load(const std::string& name);
    bool unload(const std::string& name);
    [[nodiscard]] bool loaded(const std::string& name) const;
    [[nodiscard]] std::shared_ptr<IPhysicsBackend> create_physics_backend(
        const std::string& name);
    [[nodiscard]] std::shared_ptr<ICollisionBackend> create_collision_backend(
        const std::string& name);

    [[nodiscard]] static PluginManifest parse_manifest(const std::filesystem::path& path);
    [[nodiscard]] static std::string current_platform();

private:
    struct LoadedPlugin;
    [[nodiscard]] const PluginManifest& manifest(const std::string& name) const;
    void load_recursive(const PluginManifest& manifest, std::vector<std::string>& stack);

    SimHostApi host_;
    std::vector<PluginManifest> manifests_;
    std::unordered_map<std::string, std::shared_ptr<LoadedPlugin>> loaded_;
};

}  // namespace sim
