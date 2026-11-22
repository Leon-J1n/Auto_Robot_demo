#include "sim/plugin.hpp"

struct SimPluginHandle { int marker{42}; };

namespace {

class SamplePhysicsBackend final : public sim::IPhysicsBackend {
public:
    sim::BackendCapabilities capabilities() const noexcept override {
        return {.rigid_body_dynamics = true, .state_serialization = true};
    }
    void build(sim::World&) override {}
    void step(sim::World&) override {}
    void sync_to_scene(sim::World&) override {}
    std::vector<std::byte> save_state() const override { return {std::byte{42}}; }
    void restore_state(std::span<const std::byte>) override {}
};

}  // namespace

extern "C" {

SIM_PLUGIN_API std::uint32_t SimPluginApiVersion() { return sim::kPluginApiVersion; }

SIM_PLUGIN_API SimPluginHandle* SimCreatePlugin(const SimHostApi* host) {
    if (host == nullptr || host->api_version != sim::kPluginApiVersion) return nullptr;
    if (host->log != nullptr) host->log(0, "sim.test.plugin", "created");
    return new SimPluginHandle{};
}

SIM_PLUGIN_API void SimDestroyPlugin(SimPluginHandle* plugin) { delete plugin; }

SIM_PLUGIN_API sim::IPhysicsBackend* SimCreatePhysicsBackend(SimPluginHandle* plugin) {
    return plugin != nullptr ? new SamplePhysicsBackend{} : nullptr;
}

SIM_PLUGIN_API void SimDestroyPhysicsBackend(SimPluginHandle*,
                                              sim::IPhysicsBackend* backend) {
    delete backend;
}

}
