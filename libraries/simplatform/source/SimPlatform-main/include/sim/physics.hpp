#pragma once

#include "sim/sensors.hpp"

#include <cstddef>
#include <memory>

namespace sim {

struct PhysicsContact {
    EntityId first;
    EntityId second;
    double distance;
    double normal_force;
};

class IPhysicsBackend {
public:
    virtual ~IPhysicsBackend() = default;
    [[nodiscard]] virtual BackendCapabilities capabilities() const noexcept = 0;
    virtual void build(World& world) = 0;
    virtual void step(World& world) = 0;
    virtual void sync_to_scene(World& world) = 0;
    /// Adopt the scene's current configuration as the physics state.
    ///
    /// The counterpart of sync_to_scene, for when something outside physics has posed
    /// the scene: a joint written directly, an object dragged in the editor, an IK
    /// solution applied. Without it the backend keeps integrating from the pose it was
    /// built with, and a robot moved that way collides with nothing. Velocities are
    /// cleared, since a teleported body has no meaningful history.
    ///
    /// Backends that cannot be re-seeded may ignore this; a scene-driven pose change
    /// is then simply not reflected in their state.
    virtual void sync_from_scene(World&) {}
    /// Active solver contacts at the current state; signed distance and force in newtons.
    [[nodiscard]] virtual std::vector<PhysicsContact> contacts() const {
        throw Error(ErrorCode::Unsupported, "physics backend does not support contact queries");
    }
    [[nodiscard]] virtual std::vector<std::byte> save_state() const = 0;
    virtual void restore_state(std::span<const std::byte> state) = 0;
    virtual void apply_body_wrench(EntityId, const Vec3&, const Vec3& = {}) {
        throw Error(ErrorCode::Unsupported, "physics backend does not support body wrenches");
    }
};

#if defined(SIM_HAS_MUJOCO)
class MuJoCoBackend final : public IPhysicsBackend, public ISensorDataProvider {
public:
    MuJoCoBackend();
    ~MuJoCoBackend() override;
    MuJoCoBackend(const MuJoCoBackend&) = delete;
    MuJoCoBackend& operator=(const MuJoCoBackend&) = delete;

    [[nodiscard]] BackendCapabilities capabilities() const noexcept override;
    void build(World& world) override;
    void step(World& world) override;
    void sync_to_scene(World& world) override;
    void sync_from_scene(World& world) override;
    [[nodiscard]] std::vector<PhysicsContact> contacts() const override;
    [[nodiscard]] std::vector<std::byte> save_state() const override;
    void restore_state(std::span<const std::byte> state) override;

    void render_camera(const Transform&, const CameraComponent&, std::vector<std::uint8_t>&,
                       std::vector<float>&) override;
    [[nodiscard]] std::vector<float> raycast(const Vec3& world_origin,
                                             std::span<const Vec3> world_directions,
                                             double min_distance,
                                             double max_distance) override;
    [[nodiscard]] double distance(EntityId first, EntityId second, double maximum_distance);
    void apply_body_wrench(EntityId entity, const Vec3& force,
                           const Vec3& torque = {}) override;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
#endif

}  // namespace sim
