#pragma once

#include "sim/sensors.hpp"

#include <array>
#include <memory>

namespace sim {

#if defined(SIM_HAS_VULKAN)
struct RenderInstance {
    EntityId entity{kInvalidEntity};
    Transform world_transform{};
    Vec3 size{1.0, 1.0, 1.0};
    std::string mesh_resource;
    std::string texture_resource;
    std::array<float, 4> color{0.65F, 0.72F, 0.82F, 1.0F};
    Vec3 bounds_min{};
    Vec3 bounds_max{};
    std::uint8_t min_lod{0};
    std::uint8_t max_lod{7};
    bool use_mesh_material{false};
};

struct RenderLight {
    EntityId entity{kInvalidEntity};
    LightType type{LightType::Point};
    Transform world_transform{};
    std::array<float, 3> color{1.0F, 1.0F, 1.0F};
    float intensity{1.0F};
    float range{10.0F};
    float decay{2.0F};
    float inner_cone_radians{0.35F};
    float outer_cone_radians{0.7F};
};

struct RenderSnapshot {
    std::uint64_t revision{0};
    std::vector<RenderInstance> instances;
    std::vector<RenderLight> lights;
    bool has_bounds{false};
    Vec3 bounds_min{};
    Vec3 bounds_max{};
};

class VulkanBackend final : public ISensorDataProvider {
public:
    VulkanBackend();
    ~VulkanBackend() override;
    VulkanBackend(const VulkanBackend&) = delete;
    VulkanBackend& operator=(const VulkanBackend&) = delete;

    [[nodiscard]] BackendCapabilities capabilities() const noexcept;
    [[nodiscard]] std::string device_name() const;
    void sync_from_scene(World& world);
    [[nodiscard]] const RenderSnapshot& snapshot() const noexcept;
    [[nodiscard]] std::vector<std::string> consume_diagnostics();
    void render_camera(const Transform& world_from_camera, const CameraComponent& camera,
                       std::vector<std::uint8_t>& rgb,
                       std::vector<float>& linear_depth_meters) override;
    void render_camera_rgba(const Transform& world_from_camera, const CameraComponent& camera,
                            std::vector<std::uint8_t>& rgba);
    [[nodiscard]] std::vector<float> raycast(const Vec3&, std::span<const Vec3>,
                                             double, double) override;

private:
    void render_camera_internal(const Transform& world_from_camera, const CameraComponent& camera,
                                std::vector<std::uint8_t>& color,
                                std::vector<float>& linear_depth_meters,
                                bool rgba_output, bool read_depth);
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
#endif

}  // namespace sim
