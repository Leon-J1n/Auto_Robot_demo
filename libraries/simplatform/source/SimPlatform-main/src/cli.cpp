#include "sim/assets.hpp"
#include "sim/logging.hpp"
#include "sim/plugin.hpp"

#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#if defined(_WIN32)
#include <process.h>
#else
#include <unistd.h>
#endif

namespace {

void PrintUsage() {
    std::cout << "simulator 0.1.0\n"
              << "usage:\n"
              << "  simulator run\n"
              << "  simulator server\n"
              << "  simulator bake <asset>\n"
              << "  simulator validate <asset>\n"
              << "  simulator plugins list\n"
              << "  simulator backends check\n";
}

int CheckBackends() {
    const std::vector<std::pair<std::string, sim::BackendConfig>> combinations{
        {"MuJoCo + MuJoCo", {sim::BackendKind::MuJoCo, sim::BackendKind::MuJoCo,
                              sim::BackendKind::None}},
        {"MuJoCo + FCL", {sim::BackendKind::MuJoCo, sim::BackendKind::FCL,
                           sim::BackendKind::None}},
#if defined(SIM_HAS_BULLET_PLUGIN)
        {"Bullet3 + FCL", {sim::BackendKind::Bullet3, sim::BackendKind::FCL,
                            sim::BackendKind::None}},
#endif
        {"No Physics + FCL", {sim::BackendKind::None, sim::BackendKind::FCL,
                               sim::BackendKind::None}},
    };
    for (const auto& [name, config] : combinations) {
        const auto diagnostics = sim::BackendManager::validate(config);
        bool valid = true;
        for (const auto& item : diagnostics) {
            valid = valid && item.severity != sim::Severity::Fatal;
        }
        std::cout << (valid ? "[valid] " : "[invalid] ") << name << '\n';
        for (const auto& item : diagnostics) {
            std::cout << "  " << item.code << ": " << item.message << '\n';
        }
    }
    return 0;
}

int RunServer(int argc, char** argv) {
    const char* configured = std::getenv("SIM_PYTHON_EXECUTABLE");
    const std::string python = configured != nullptr && *configured != '\0' ? configured : "python";
    std::vector<std::string> values{python, "-m", "sim_platform.server"};
    for (int index = 2; index < argc; ++index) values.emplace_back(argv[index]);
    std::vector<char*> arguments;
    arguments.reserve(values.size() + 1);
    for (auto& value : values) arguments.push_back(value.data());
    arguments.push_back(nullptr);
#if defined(_WIN32)
    const intptr_t result = _spawnvp(_P_WAIT, python.c_str(), arguments.data());
    if (result == -1) {
        SIM_LOG_ERROR("CLI.Server",
                      "failed to start Python Agent RPC server; set SIM_PYTHON_EXECUTABLE");
        return 3;
    }
    return static_cast<int>(result);
#else
    execvp(python.c_str(), arguments.data());
    SIM_LOG_ERROR("CLI.Server",
                  "failed to start Python Agent RPC server; set SIM_PYTHON_EXECUTABLE");
    return 3;
#endif
}

}  // namespace

int main(int argc, char** argv) {
    SIM_LOG_VERBOSE("CLI.main dispatches command-line arguments");
    try {
        if (argc < 2) {
            PrintUsage();
            return 2;
        }
        const std::string command = argv[1];
        if (command == "run") {
            sim::Simulator simulator;
            auto& world = simulator.create_world();
            world.start();
            world.step();
            SIM_LOG_INFO("CLI.run",
                "headless world stepped to t=" + std::to_string(world.time().state().simulation_time) + " s");
            return 0;
        }
        if (command == "backends" && argc >= 3 && std::string(argv[2]) == "check") {
            return CheckBackends();
        }
        if (command == "plugins" && argc >= 3 && std::string(argv[2]) == "list") {
            sim::PluginManager plugins;
            plugins.discover(argc >= 4 ? argv[3] : "plugins");
            for (const auto& manifest : plugins.manifests()) {
                std::cout << manifest.name << ' ' << manifest.version << " (" << manifest.type
                          << ", API " << manifest.plugin_api_version << ")\n";
            }
            if (plugins.manifests().empty()) std::cout << "No plugin manifests discovered.\n";
            return 0;
        }
        if ((command == "bake" || command == "validate") && argc >= 3) {
            sim::ResourceManager resources(".sim-cache");
            const auto result = resources.load(argv[2]);
            bool fatal = false;
            for (const auto& diagnostic : result.diagnostics) {
                fatal = fatal || diagnostic.severity == sim::Severity::Fatal;
                std::cout << diagnostic.code << ": " << diagnostic.message << '\n';
            }
            std::size_t vertices = 0;
            std::size_t triangles = 0;
            for (const auto& mesh : result.asset.meshes) {
                vertices += mesh.positions.size();
                triangles += mesh.indices.size() / 3;
            }
            std::cout << "asset: nodes=" << result.asset.nodes.size()
                      << ", meshes=" << result.asset.meshes.size()
                      << ", vertices=" << vertices << ", triangles=" << triangles << '\n';
            std::cout << (command == "bake" ? (result.from_cache ? "cache hit" : "cache written")
                                              : (fatal ? "invalid" : "valid"))
                      << ": " << argv[2] << '\n';
            return fatal ? 1 : 0;
        }
        if (command == "server") {
            return RunServer(argc, argv);
        }
        PrintUsage();
        return 2;
    } catch (const sim::Error& error) {
        SIM_LOG_FATAL("CLI.main", "simulator error: " + std::string(error.what()));
        return 1;
    } catch (const std::exception& error) {
        SIM_LOG_FATAL("CLI.main", "unexpected error: " + std::string(error.what()));
        return 1;
    }
}
