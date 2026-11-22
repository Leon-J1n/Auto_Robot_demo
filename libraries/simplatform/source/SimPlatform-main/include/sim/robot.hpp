#pragma once

#include "sim/assets.hpp"

#include <array>
#include <span>

namespace sim {

struct JointState {
    std::vector<std::string> names;
    std::vector<double> positions;
    std::vector<double> velocities;
    std::vector<double> efforts;
};

/// What set_joint_positions does with a target that falls outside a joint's limits.
enum class JointLimitPolicy {
    Enforce,  ///< Throw. The long-standing behaviour, so it stays the default.
    Clamp,    ///< Clamp into range and carry on.
    Ignore    ///< Write the value through untouched.
};

class Robot {
public:
    Robot(World& world, EntityId root);

    [[nodiscard]] EntityId entity() const noexcept { return root_; }
    [[nodiscard]] std::vector<EntityId> joints() const;
    [[nodiscard]] JointState joint_state() const;
    /// Pose the robot directly, bypassing the controller. Iterative solvers overshoot limits
    /// on intermediate steps, so they need Clamp or Ignore rather than the default throw.
    void set_joint_positions(std::span<const double> positions,
                             JointLimitPolicy policy = JointLimitPolicy::Enforce);
    void set_joint_continuous(std::string_view joint_name, bool enabled = true);
    void command_joint_positions(std::span<const double> targets, double gain = 20.0,
                                 double velocity_limit = 10.0,
                                 std::span<const double> effort_limits = {});
    void command_joint_velocities(std::span<const double> targets);
    void command_joint_efforts(std::span<const double> targets);
    /// Cap the torque each joint's position or velocity servo may apply.
    ///
    /// A joint left at the default infinity is a kinematic servo: it tracks its target
    /// regardless of load, which suits a stiff arm. Give a joint a finite ceiling and its
    /// servo becomes a torque source that contact can stall, which is what a gripper
    /// needs -- the commanded angle then sets a direction to squeeze in, and this limit
    /// sets how hard. Pass infinity to restore kinematic tracking. Outlives the position
    /// and velocity commands sent afterwards, since it describes the actuator.
    void set_joint_effort_limits(std::span<const double> limits);
    [[nodiscard]] Transform forward_kinematics(EntityId link);
    [[nodiscard]] std::vector<std::array<double, 6>> jacobian(EntityId end_effector);

private:
    void require_target_count(std::span<const double> targets) const;
    static void require_position_in_limits(const JointComponent& joint, double position);

    World* world_;
    EntityId root_;
};

/// One configuration evaluated off to the side: the requested link poses in world frame, plus
/// the geometric Jacobian when one was asked for.
struct RobotKinematicsResult {
    std::vector<Transform> link_transforms;       ///< One per requested link, in query order.
    std::vector<std::array<double, 6>> jacobian;  ///< One column per joint; empty if not requested.
};

/// Forward kinematics, and optionally the Jacobian, at arbitrary joint configurations.
///
/// The robot is posed inside a private clone of `world`, so live state survives untouched even
/// when this is called mid-trajectory -- the equivalent of running a solver against a scratch
/// copy of the state rather than the one being simulated.
///
/// Kinematics come from the scene graph: joint origins, axes and hierarchy as held in the ECS.
/// No URDF is consulted, so this keeps working for robots whose source model is unavailable.
///
/// Cloning is amortised across the batch, which matters because iterative solvers evaluate
/// hundreds of configurations per call.
[[nodiscard]] std::vector<RobotKinematicsResult> EvaluateRobotKinematics(
    const World& world, EntityId robot, std::span<const double> joint_positions,
    std::size_t configuration_count, std::span<const EntityId> links,
    EntityId jacobian_link = kInvalidEntity,
    JointLimitPolicy policy = JointLimitPolicy::Clamp);

void InstallRobotControlSystem(World& world);

}  // namespace sim
