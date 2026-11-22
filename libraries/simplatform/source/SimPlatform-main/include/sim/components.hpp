#pragma once

#include "sim/types.hpp"

#include <array>
#include <limits>
#include <string>
#include <vector>

namespace sim {

struct IdentityComponent { EntityId id{kInvalidEntity}; };
struct ObjectIdentityComponent { std::string value; };
struct NameComponent { std::string name; };
struct TransformComponent { Transform local{}; Transform world{}; };
struct HierarchyComponent { EntityId parent{kInvalidEntity}; std::vector<EntityId> children; };

enum class ColliderShape { Box, Sphere, Capsule, Cylinder, Mesh };

struct VisualComponent {
    std::string mesh_resource;
    std::string material_resource;
    bool visible{true};
    ColliderShape shape{ColliderShape::Mesh};
    Vec3 size{1.0, 1.0, 1.0};
    std::array<float, 4> color{0.65F, 0.72F, 0.82F, 1.0F};
    std::uint8_t min_lod{0};
    std::uint8_t max_lod{7};
    bool use_mesh_material{false};
};

// Keep existing numeric values stable for serialized scenes.
enum class LightType : std::uint8_t { Point, Spot, Directional, Ambient };

struct LightComponent {
    LightType type{LightType::Point};
    std::array<float, 3> color{1.0F, 1.0F, 1.0F};
    float intensity{1.0F};
    float range{10.0F};
    float decay{2.0F};
    float inner_cone_radians{0.35F};
    float outer_cone_radians{0.7F};
    float angular_diameter_radians{0.0F};
    bool enabled{true};
};

struct ColliderComponent {
    ColliderShape shape{ColliderShape::Box};
    Vec3 size{1.0, 1.0, 1.0};
    std::string mesh_resource;
    bool is_trigger{false};
    Vec3 friction{1.0, 0.005, 0.0001};
    double restitution{0.0};
    std::uint32_t ifps_sphere_count{0};
};

struct RigidBodyComponent {
    double mass{1.0};
    Vec3 linear_velocity{};
    Vec3 angular_velocity{};
    bool dynamic{true};
    bool gravity_enabled{true};
};

struct RobotComponent { std::string model_resource; std::vector<EntityId> links; };

enum class JointType { Fixed, Revolute, Continuous, Prismatic, Floating, Planar };
struct JointComponent {
    std::string name;
    JointType type{JointType::Fixed};
    EntityId parent_link{kInvalidEntity};
    EntityId child_link{kInvalidEntity};
    Vec3 axis{1.0, 0.0, 0.0};
    Transform joint_origin{};
    double lower_limit{-std::numeric_limits<double>::infinity()};
    double upper_limit{std::numeric_limits<double>::infinity()};
    double position{0.0};
    double velocity{0.0};
    double effort{0.0};
};

enum class JointControlMode { Disabled, Position, Velocity, Effort };
struct JointControlComponent {
    JointControlMode mode{JointControlMode::Disabled};
    double target{0.0};
    double position_gain{20.0};
    double velocity_limit{10.0};
    /// Ceiling on the torque (or force, for a prismatic joint) the servo may apply.
    ///
    /// Infinity removes the actuator torque ceiling. MuJoCo still integrates the
    /// powered joint physically so contact reactions are preserved. A finite limit
    /// permits contact to stall the servo short of its position target, as needed
    /// when fingers squeeze a payload. Non-dynamics controllers ignore this limit.
    double max_effort{std::numeric_limits<double>::infinity()};
};

struct SensorComponent {
    std::string frame_id;
    double update_rate_hz{0.0};
    double next_update_time{0.0};
};

enum class CameraOutput : std::uint8_t { RGB, RGBD };

struct CameraComponent {
    std::uint32_t width{640};
    std::uint32_t height{480};
    double vertical_fov_radians{1.0471975511965976};
    double near_plane{0.01};
    double far_plane{1000.0};
    CameraOutput output{CameraOutput::RGBD};
};

struct LiDARComponent {
    double horizontal_fov_radians{6.283185307179586};
    double vertical_fov_radians{0.5235987755982988};
    std::uint32_t horizontal_samples{1024};
    std::uint32_t vertical_channels{16};
    double min_distance{0.05};
    double max_distance{100.0};
};

struct JointStateSensorComponent { EntityId robot{kInvalidEntity}; };

}  // namespace sim
