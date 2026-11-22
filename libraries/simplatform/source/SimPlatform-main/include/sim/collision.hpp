#pragma once

#include "sim/sensors.hpp"

#include <memory>
#include <array>
#include <span>

namespace sim {

class ICollisionBackend {
public:
    virtual ~ICollisionBackend() = default;
    [[nodiscard]] virtual BackendCapabilities capabilities() const noexcept = 0;
    virtual void build(World& world) = 0;
    virtual void sync_from_scene(World& world) = 0;
    [[nodiscard]] virtual bool check_collision(EntityId first, EntityId second) const = 0;
    [[nodiscard]] virtual bool check_scene_collision(EntityId entity) const = 0;
    [[nodiscard]] virtual std::vector<std::uint8_t> check_scene_collisions(
        std::span<const EntityId> entities) const {
        std::vector<std::uint8_t> result;
        result.reserve(entities.size());
        for (const EntityId entity : entities)
            result.push_back(check_scene_collision(entity) ? 1U : 0U);
        return result;
    }
    [[nodiscard]] virtual double distance(EntityId first, EntityId second) const = 0;
};

/// One robot collider measured against one other collider.
///
/// `distance` is the separation between the two surfaces, so it is 0.0 whenever `colliding` is
/// true. Penetration depth is deliberately not reported: FCL's depth for the mesh-versus-primitive
/// pairs that dominate a loaded robot depends on the narrow-phase solver, and an unverified number
/// is worse than none.
struct CollisionPair {
    EntityId robot_collider{kInvalidEntity};
    EntityId other{kInvalidEntity};
    double distance{0.0};
    bool colliding{false};
};

#if defined(SIM_HAS_FCL)
class FCLQueryContext {
public:
    ~FCLQueryContext();
    FCLQueryContext(FCLQueryContext&&) noexcept;
    FCLQueryContext& operator=(FCLQueryContext&&) noexcept;
    FCLQueryContext(const FCLQueryContext&) = delete;
    FCLQueryContext& operator=(const FCLQueryContext&) = delete;

    void set_transform(EntityId entity, const Transform& transform);
    [[nodiscard]] bool check_collision(EntityId first, EntityId second) const;
    [[nodiscard]] bool check_scene_collision(EntityId entity) const;
    [[nodiscard]] double distance(EntityId first, EntityId second) const;

private:
    friend class FCLBackend;
    struct Impl;
    explicit FCLQueryContext(std::unique_ptr<Impl> impl);
    std::unique_ptr<Impl> impl_;
};

class FCLBackend final : public ICollisionBackend, public ISensorDataProvider {
public:
    FCLBackend();
    ~FCLBackend() override;
    FCLBackend(const FCLBackend&) = delete;
    FCLBackend& operator=(const FCLBackend&) = delete;

    [[nodiscard]] BackendCapabilities capabilities() const noexcept override;
    void build(World& world) override;
    void sync_from_scene(World& world) override;
    [[nodiscard]] bool check_collision(EntityId first, EntityId second) const override;
    [[nodiscard]] bool check_scene_collision(EntityId entity) const override;
    [[nodiscard]] std::vector<std::uint8_t> check_scene_collisions(
        std::span<const EntityId> entities) const override;
    [[nodiscard]] double distance(EntityId first, EntityId second) const override;
    [[nodiscard]] FCLQueryContext create_query_context() const;
    [[nodiscard]] bool check_robot_configuration(const World& world, EntityId robot,
                                                  std::span<const double> joint_positions) const;
    [[nodiscard]] std::vector<bool> check_robot_configurations(
        const World& world, EntityId robot, std::span<const double> joint_positions,
        std::size_t configuration_count) const;
    /// Every robot-versus-other and robot-versus-robot pair at one configuration, reported
    /// individually instead of collapsed into a single bool.
    ///
    /// check_robot_configuration answers "does anything touch", which for an articulated arm is
    /// always yes: neighbouring links share a joint and therefore always overlap. Deciding what
    /// counts as a real collision needs the pairs themselves, so the caller can discount the
    /// adjacent-link and baseline contacts and whatever it is deliberately holding.
    ///
    /// Pairs closer than `distance_threshold` are reported even when not touching, which is what
    /// a safety margin needs. Pass 0.0 for touching pairs only. The robot is posed in a private
    /// clone, so live state is untouched.
    ///
    /// Two colliders on the same link are never reported against each other: rigidly attached
    /// bodies cannot collide as a function of configuration, and coincident faces make the
    /// narrow phase answer inconsistently, which would corrupt a baseline comparison.
    [[nodiscard]] std::vector<CollisionPair> robot_configuration_contacts(
        const World& world, EntityId robot, std::span<const double> joint_positions,
        double distance_threshold = 0.0,
        JointLimitPolicy policy = JointLimitPolicy::Clamp,
        std::span<const std::array<EntityId, 2>> excluded_pairs = {}) const;

    void render_camera(const Transform&, const CameraComponent&, std::vector<std::uint8_t>&,
                       std::vector<float>&) override;
    [[nodiscard]] std::vector<float> raycast(const Vec3& world_origin,
                                             std::span<const Vec3> world_directions,
                                             double min_distance,
                                             double max_distance) override;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
#endif

}  // namespace sim
