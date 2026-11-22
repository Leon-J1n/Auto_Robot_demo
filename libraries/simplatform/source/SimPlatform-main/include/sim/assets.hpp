#pragma once

#include "sim/core.hpp"

#include <filesystem>
#include <future>
#include <memory>
#include <mutex>
#include <span>

namespace sim {

struct MeshData {
    std::string name;
    std::vector<Vec3> positions;
    std::vector<Vec3> normals;
    std::vector<std::array<double, 2>> texcoords;
    std::vector<std::uint32_t> indices;
    std::size_t material_index{0};
};

// Generates a complete triangle mesh for LOD 0 (finest) through 7 (coarsest).
// Lower levels retain more vertex clusters; level 0 returns the source topology.
[[nodiscard]] MeshData GenerateMeshLod(const MeshData& source, std::uint8_t level);

struct MaterialData {
    std::string name;
    std::array<double, 4> base_color{1.0, 1.0, 1.0, 1.0};
    std::filesystem::path base_color_texture;
};

struct TextureData { std::filesystem::path source; std::string semantic; };

struct NodeData {
    std::string name;
    std::int64_t parent{-1};
    Transform transform{};
    std::vector<std::size_t> meshes;
};

struct LinkData { std::string name; Transform origin{}; };

struct JointData {
    std::string name;
    JointType type{JointType::Fixed};
    std::string parent_link;
    std::string child_link;
    Transform origin{};
    Vec3 axis{1.0, 0.0, 0.0};
    double lower_limit{0.0};
    double upper_limit{0.0};
};

struct InertialData {
    std::string link;
    double mass{0.0};
    Vec3 center_of_mass{};
    std::array<double, 6> inertia{};
};

struct ColliderData {
    std::string link;
    ColliderShape shape{ColliderShape::Box};
    Vec3 size{1.0, 1.0, 1.0};
    std::filesystem::path mesh;
    Transform origin{};
};

struct VisualData {
    std::string link;
    ColliderShape shape{ColliderShape::Mesh};
    Vec3 size{1.0, 1.0, 1.0};
    std::filesystem::path mesh;
    Transform origin{};
    std::array<float, 4> color{0.65F, 0.72F, 0.82F, 1.0F};
    std::filesystem::path base_color_texture;
    bool use_mesh_material{true};
};

struct SensorData { std::string name; std::string frame; std::string type; };

struct AssetMetadata {
    std::filesystem::path source;
    std::string importer;
    std::string importer_version{"1"};
    std::uint32_t asset_ir_version{1};
    std::string coordinate_system{"right-handed, x-forward, y-left, z-up"};
    double meters_per_unit{1.0};
    std::vector<std::filesystem::path> dependencies;
    std::vector<std::uint64_t> dependency_hashes;
};

struct AssetIR {
    std::vector<NodeData> nodes;
    std::vector<MeshData> meshes;
    std::vector<MaterialData> materials;
    std::vector<TextureData> textures;
    std::vector<JointData> joints;
    std::vector<LinkData> links;
    std::vector<InertialData> inertials;
    std::vector<ColliderData> colliders;
    std::vector<VisualData> visuals;
    std::vector<SensorData> sensors;
    AssetMetadata metadata;
};

struct ImportResult {
    AssetIR asset;
    std::vector<Diagnostic> diagnostics;
    bool from_cache{false};
};

class IAssetImporter {
public:
    virtual ~IAssetImporter() = default;
    [[nodiscard]] virtual std::string_view name() const noexcept = 0;
    [[nodiscard]] virtual std::span<const std::string_view> extensions() const noexcept = 0;
    [[nodiscard]] virtual ImportResult import(const std::filesystem::path& path) const = 0;
};

class UrdfImporter final : public IAssetImporter {
public:
    [[nodiscard]] std::string_view name() const noexcept override { return "urdf"; }
    [[nodiscard]] std::span<const std::string_view> extensions() const noexcept override;
    [[nodiscard]] ImportResult import(const std::filesystem::path& path) const override;
};

class ObjImporter final : public IAssetImporter {
public:
    [[nodiscard]] std::string_view name() const noexcept override { return "obj"; }
    [[nodiscard]] std::span<const std::string_view> extensions() const noexcept override;
    [[nodiscard]] ImportResult import(const std::filesystem::path& path) const override;
};

class AssimpImporter final : public IAssetImporter {
public:
    [[nodiscard]] std::string_view name() const noexcept override { return "assimp"; }
    [[nodiscard]] std::span<const std::string_view> extensions() const noexcept override;
    [[nodiscard]] ImportResult import(const std::filesystem::path& path) const override;
};

class AssetImporterRegistry {
public:
    void add(std::unique_ptr<IAssetImporter> importer);
    [[nodiscard]] const IAssetImporter& importer_for(const std::filesystem::path& path) const;
    [[nodiscard]] std::vector<std::string> formats() const;

private:
    std::vector<std::unique_ptr<IAssetImporter>> importers_;
};

class AssetValidator {
public:
    [[nodiscard]] static std::vector<Diagnostic> validate(const AssetIR& asset);
    static void normalize(AssetIR& asset);
};

class BakingCache {
public:
    explicit BakingCache(std::filesystem::path directory);
    [[nodiscard]] std::optional<AssetIR> load(const std::filesystem::path& source) const;
    void store(const AssetIR& asset) const;
    [[nodiscard]] std::filesystem::path entry_path(const std::filesystem::path& source) const;

private:
    std::filesystem::path directory_;
};

class ResourceManager {
public:
    explicit ResourceManager(std::filesystem::path cache_directory);
    [[nodiscard]] ImportResult load(const std::filesystem::path& source);
    [[nodiscard]] std::future<ImportResult> load_async(std::filesystem::path source);
    [[nodiscard]] EntityId instantiate(World& world, const AssetIR& asset,
                                       std::string root_name = {});
    [[nodiscard]] std::vector<std::string> supported_formats() const;

private:
    AssetImporterRegistry importers_;
    BakingCache cache_;
    std::mutex load_mutex_;
};

}  // namespace sim
