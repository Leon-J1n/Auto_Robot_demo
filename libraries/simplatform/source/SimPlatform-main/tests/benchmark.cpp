#include "sim/robot.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>

int main() {
    using Clock = std::chrono::steady_clock;
    sim::World world(1);
    constexpr std::size_t entity_count = 100'000;
    const auto create_start = Clock::now();
    for (std::size_t index = 0; index < entity_count; ++index) {
        const auto entity = world.create_entity("entity");
        (void)entity;
    }
    const auto create_end = Clock::now();
    const auto snapshot = world.snapshot();
    const auto snapshot_end = Clock::now();
    const double create_ms = std::chrono::duration<double, std::milli>(create_end - create_start).count();
    const double snapshot_ms = std::chrono::duration<double, std::milli>(snapshot_end - create_end).count();
    std::cout << "entities=" << entity_count << '\n'
              << "create_ms=" << create_ms << '\n'
              << "create_entities_per_second=" << entity_count / (create_ms / 1000.0) << '\n'
              << "snapshot_ms=" << snapshot_ms << '\n'
              << "snapshot_entities=" << snapshot.registry.size() << '\n';

    const auto benchmark_root = std::filesystem::temp_directory_path() / "sim-platform-cache-benchmark";
    std::filesystem::remove_all(benchmark_root);
    std::filesystem::create_directories(benchmark_root);
    const auto asset_path = benchmark_root / "large.obj";
    {
        std::ofstream asset(asset_path, std::ios::trunc);
        constexpr std::size_t vertex_count = 60'000;
        for (std::size_t index = 0; index < vertex_count; ++index)
            asset << "v " << index * 0.001 << " 0 0\n";
        for (std::size_t index = 1; index + 2 <= vertex_count; index += 3)
            asset << "f " << index << ' ' << index + 1 << ' ' << index + 2 << '\n';
    }
    sim::ResourceManager resources(benchmark_root / "cache");
    const auto import_start = Clock::now();
    const auto imported = resources.load(asset_path);
    const auto import_end = Clock::now();
    const auto cached = resources.load(asset_path);
    const auto cache_end = Clock::now();
    const double import_ms = std::chrono::duration<double, std::milli>(import_end - import_start).count();
    const double cache_ms = std::chrono::duration<double, std::milli>(cache_end - import_end).count();
    const double speedup = cache_ms > 0.0 ? import_ms / cache_ms : 0.0;
    std::cout << "asset_import_ms=" << import_ms << '\n'
              << "asset_cache_ms=" << cache_ms << '\n'
              << "asset_cache_speedup=" << speedup << '\n';
    std::filesystem::remove_all(benchmark_root);
    const bool cache_significant = !imported.from_cache && cached.from_cache && cache_ms < import_ms * 0.8;
    return snapshot.registry.size() == entity_count && cache_significant ? 0 : 1;
}
