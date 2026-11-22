#pragma once

#include "sim/core.hpp"

#include <cstddef>
#include <filesystem>

namespace sim {

struct SimulationStateArchive {
    std::uint32_t version{1};
    WorldSnapshot world;
    std::vector<std::byte> backend_state;
};

void SaveStateFile(const std::filesystem::path& path, const SimulationStateArchive& archive);
[[nodiscard]] SimulationStateArchive LoadStateFile(const std::filesystem::path& path);

}  // namespace sim
