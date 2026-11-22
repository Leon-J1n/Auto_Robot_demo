#include "sim/ifps.hpp"
#include "sim/plugin.hpp"

struct SimPluginHandle { const SimHostApi* host{nullptr}; };

extern "C" {
SIM_PLUGIN_API std::uint32_t SimPluginApiVersion() { return sim::kPluginApiVersion; }
SIM_PLUGIN_API SimPluginHandle* SimCreatePlugin(const SimHostApi* host) {
    if (host == nullptr || host->api_version != sim::kPluginApiVersion) return nullptr;
    if (host->log) host->log(0, "sim.collision.ifps_cpu", "IFPS CPU plugin loaded");
    return new SimPluginHandle{host};
}
SIM_PLUGIN_API void SimDestroyPlugin(SimPluginHandle* plugin) { delete plugin; }
SIM_PLUGIN_API sim::ICollisionBackend* SimCreateCollisionBackend(SimPluginHandle* plugin) {
    return plugin ? new sim::IFPSCollisionBackend{} : nullptr;
}
SIM_PLUGIN_API void SimDestroyCollisionBackend(SimPluginHandle*, sim::ICollisionBackend* backend) {
    delete backend;
}
}
