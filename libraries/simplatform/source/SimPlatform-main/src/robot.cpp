#include "sim/robot.hpp"
#include "sim/logging.hpp"

#include <algorithm>
#include <unordered_set>

namespace sim {
namespace {

Quaternion AxisAngle(Vec3 axis, double angle) {
    const double length = std::sqrt(axis.x * axis.x + axis.y * axis.y + axis.z * axis.z);
    if (NearlyEqual(length, 0.0)) {
        throw Error(ErrorCode::ValidationFailed, "joint axis must be non-zero");
    }
    axis = axis * (1.0 / length);
    const double sine = std::sin(angle * 0.5);
    return {std::cos(angle * 0.5), axis.x * sine, axis.y * sine, axis.z * sine};
}

Vec3 Cross(const Vec3& lhs, const Vec3& rhs) {
    return {lhs.y * rhs.z - lhs.z * rhs.y, lhs.z * rhs.x - lhs.x * rhs.z,
            lhs.x * rhs.y - lhs.y * rhs.x};
}

void ApplyJointTransform(World& world, EntityId entity) {
    auto& joint = world.registry().get<JointComponent>(entity);
    Transform motion;
    if (joint.type == JointType::Revolute || joint.type == JointType::Continuous) {
        motion.rotation = AxisAngle(joint.axis, joint.position);
    } else if (joint.type == JointType::Prismatic) {
        motion.translation = joint.axis * joint.position;
    }
    world.registry().get<TransformComponent>(entity).local = joint.joint_origin * motion;
}

}  // namespace

Robot::Robot(World& world, EntityId root) : world_(&world), root_(root) {
    SIM_LOG_VERBOSE("Robot::Robot validates root entity=" + std::to_string(root));
    if (!world.registry().valid(root) || !world.registry().has<RobotComponent>(root)) {
        throw Error(ErrorCode::NotFound, "invalid robot handle");
    }
}

std::vector<EntityId> Robot::joints() const {
    SIM_LOG_VERBOSE("Robot::joints enumerates ordered joint entities root=" + std::to_string(root_));
    std::vector<EntityId> result;
    const auto& robot = world_->registry().get<RobotComponent>(root_);
    for (const EntityId link : robot.links) {
        if (world_->registry().has<JointComponent>(link)) result.push_back(link);
    }
    std::sort(result.begin(), result.end(), [this](EntityId lhs, EntityId rhs) {
        return world_->registry().get<JointComponent>(lhs).name <
               world_->registry().get<JointComponent>(rhs).name;
    });
    SIM_LOG_VERBOSE("Robot::joints completed count=" + std::to_string(result.size()) +
                    " root=" + std::to_string(root_));
    return result;
}

JointState Robot::joint_state() const {
    SIM_LOG_VERBOSE("Robot::joint_state reads position velocity and effort root=" +
                    std::to_string(root_));
    JointState state;
    for (const EntityId entity : joints()) {
        const auto& joint = world_->registry().get<JointComponent>(entity);
        state.names.push_back(joint.name);
        state.positions.push_back(joint.position);
        state.velocities.push_back(joint.velocity);
        state.efforts.push_back(joint.effort);
    }
    SIM_LOG_VERBOSE("Robot::joint_state completed joint_count=" +
                    std::to_string(state.names.size()) + " root=" + std::to_string(root_));
    return state;
}

void Robot::set_joint_continuous(std::string_view joint_name, bool enabled) {
    SIM_LOG_VERBOSE("Robot::set_joint_continuous name=" + std::string(joint_name) +
                    " enabled=" + (enabled ? "true" : "false"));
    for (const EntityId entity : joints()) {
        auto& joint = world_->registry().get<JointComponent>(entity);
        if (joint.name != joint_name) continue;
        joint.type = enabled ? JointType::Continuous : JointType::Revolute;
        SIM_LOG_VERBOSE("Robot::set_joint_continuous completed name=" + std::string(joint_name));
        return;
    }
    throw Error(ErrorCode::NotFound, "joint was not found: " + std::string(joint_name));
}

void Robot::require_target_count(std::span<const double> targets) const {
    SIM_LOG_VERBOSE("Robot::require_target_count validates count=" + std::to_string(targets.size()));
    if (targets.size() != joints().size()) {
        throw Error(ErrorCode::InvalidArgument, "joint target count does not match the robot");
    }
    if (std::any_of(targets.begin(), targets.end(), [](double value) { return !std::isfinite(value); })) {
        throw Error(ErrorCode::InvalidArgument, "joint targets must be finite");
    }
    SIM_LOG_VERBOSE("Robot::require_target_count completed");
}

void Robot::require_position_in_limits(const JointComponent& joint, double position) {
    SIM_LOG_VERBOSE("Robot::require_position_in_limits joint=" + joint.name +
                    " position=" + std::to_string(position));
    if (joint.type != JointType::Continuous &&
        (position < joint.lower_limit || position > joint.upper_limit)) {
        throw Error(ErrorCode::ValidationFailed, "joint target violates limits: " + joint.name);
    }
}

void Robot::set_joint_positions(std::span<const double> positions, JointLimitPolicy policy) {
    SIM_LOG_VERBOSE("Robot::set_joint_positions root=" + std::to_string(root_) +
                    " joint_count=" + std::to_string(positions.size()));
    require_target_count(positions);
    const auto entities = joints();
    for (std::size_t index = 0; index < entities.size(); ++index) {
        auto& joint = world_->registry().get<JointComponent>(entities[index]);
        double target = positions[index];
        if (policy == JointLimitPolicy::Enforce) {
            require_position_in_limits(joint, target);
        } else if (policy == JointLimitPolicy::Clamp && joint.type != JointType::Continuous &&
                   joint.lower_limit <= joint.upper_limit) {
            // Guarded against inverted limits, which would make std::clamp undefined.
            target = std::clamp(target, joint.lower_limit, joint.upper_limit);
        }
        joint.position = target;
        joint.velocity = 0.0;
        ApplyJointTransform(*world_, entities[index]);
    }
    world_->update_world_transforms();
    SIM_LOG_VERBOSE("Robot::set_joint_positions completed root=" + std::to_string(root_));
}

void Robot::command_joint_positions(std::span<const double> targets, double gain,
                                    double velocity_limit, std::span<const double> effort_limits) {
    SIM_LOG_VERBOSE("Robot::command_joint_positions root=" + std::to_string(root_) +
                    " joint_count=" + std::to_string(targets.size()) +
                    " gain=" + std::to_string(gain));
    require_target_count(targets);
    if (!std::isfinite(gain) || gain <= 0.0 || !std::isfinite(velocity_limit) ||
        velocity_limit <= 0.0) {
        throw Error(ErrorCode::InvalidArgument, "position gain and velocity limit must be positive");
    }
    const auto entities = joints();
    // Validate the entire command before changing any servo or effort ceiling.
    for (std::size_t index = 0; index < entities.size(); ++index) {
        require_position_in_limits(world_->registry().get<JointComponent>(entities[index]),
                                   targets[index]);
    }
    if (!effort_limits.empty()) set_joint_effort_limits(effort_limits);
    for (std::size_t index = 0; index < entities.size(); ++index) {
        if (!world_->registry().has<JointControlComponent>(entities[index])) {
            world_->registry().emplace<JointControlComponent>(entities[index]);
        }
        auto& control = world_->registry().get<JointControlComponent>(entities[index]);
        // Assigned field by field rather than as a whole: max_effort describes the
        // actuator, not this command, so a configured effort ceiling has to outlive
        // every position command sent to the joint.
        control.mode = JointControlMode::Position;
        control.target = targets[index];
        control.position_gain = gain;
        control.velocity_limit = velocity_limit;
    }
    SIM_LOG_VERBOSE("Robot::command_joint_positions completed root=" + std::to_string(root_));
}

void Robot::command_joint_velocities(std::span<const double> targets) {
    SIM_LOG_VERBOSE("Robot::command_joint_velocities root=" + std::to_string(root_) +
                    " joint_count=" + std::to_string(targets.size()));
    require_target_count(targets);
    const auto entities = joints();
    for (std::size_t index = 0; index < entities.size(); ++index) {
        if (!world_->registry().has<JointControlComponent>(entities[index])) {
            world_->registry().emplace<JointControlComponent>(entities[index]);
        }
        auto& control = world_->registry().get<JointControlComponent>(entities[index]);
        control.mode = JointControlMode::Velocity;
        control.target = targets[index];
    }
    SIM_LOG_VERBOSE("Robot::command_joint_velocities completed root=" + std::to_string(root_));
}

void Robot::command_joint_efforts(std::span<const double> targets) {
    SIM_LOG_VERBOSE("Robot::command_joint_efforts root=" + std::to_string(root_) +
                    " joint_count=" + std::to_string(targets.size()));
    require_target_count(targets);
    const auto entities = joints();
    for (std::size_t index = 0; index < entities.size(); ++index) {
        if (!world_->registry().has<JointControlComponent>(entities[index])) {
            world_->registry().emplace<JointControlComponent>(entities[index]);
        }
        auto& control = world_->registry().get<JointControlComponent>(entities[index]);
        control.mode = JointControlMode::Effort;
        control.target = targets[index];
    }
    SIM_LOG_VERBOSE("Robot::command_joint_efforts completed root=" + std::to_string(root_));
}

void Robot::set_joint_effort_limits(std::span<const double> limits) {
    SIM_LOG_VERBOSE("Robot::set_joint_effort_limits root=" + std::to_string(root_) +
                    " joint_count=" + std::to_string(limits.size()));
    // Only the count is borrowed from require_target_count: it also rejects non-finite
    // values, which is right for a target but wrong here, where infinity is the default
    // and names the kinematic servo. The loop below validates the values instead.
    if (limits.size() != joints().size()) {
        throw Error(ErrorCode::InvalidArgument, "joint effort limit count does not match the robot");
    }
    for (const double limit : limits) {
        // Positive infinity restores the legacy kinematic servo; zero disables torque.
        if (std::isnan(limit) || limit < 0.0) {
            throw Error(ErrorCode::InvalidArgument,
                        "joint effort limits must be nonnegative, or infinite for a kinematic servo");
        }
    }
    const auto entities = joints();
    for (std::size_t index = 0; index < entities.size(); ++index) {
        if (!world_->registry().has<JointControlComponent>(entities[index])) {
            world_->registry().emplace<JointControlComponent>(entities[index]);
        }
        world_->registry().get<JointControlComponent>(entities[index]).max_effort = limits[index];
    }
    SIM_LOG_VERBOSE("Robot::set_joint_effort_limits completed root=" + std::to_string(root_));
}

Transform Robot::forward_kinematics(EntityId link) {
    SIM_LOG_VERBOSE("Robot::forward_kinematics refreshes root=" + std::to_string(root_) +
                    " link=" + std::to_string(link));
    const auto& links = world_->registry().get<RobotComponent>(root_).links;
    if (std::find(links.begin(), links.end(), link) == links.end()) {
        throw Error(ErrorCode::InvalidArgument, "entity is not a link of this robot");
    }
    world_->update_world_transforms();
    const Transform result = world_->registry().get<TransformComponent>(link).world;
    SIM_LOG_VERBOSE("Robot::forward_kinematics completed link=" + std::to_string(link));
    return result;
}

std::vector<std::array<double, 6>> Robot::jacobian(EntityId end_effector) {
    SIM_LOG_VERBOSE("Robot::jacobian computes root=" + std::to_string(root_) +
                    " end_effector=" + std::to_string(end_effector));
    SIM_PROFILE_SCOPE("Robot.Jacobian");
    const Transform end = forward_kinematics(end_effector);
    const auto entities = joints();
    std::unordered_set<EntityId> ancestors;
    EntityId current = end_effector;
    while (current != kInvalidEntity) {
        ancestors.insert(current);
        current = world_->registry().get<HierarchyComponent>(current).parent;
    }
    std::vector<std::array<double, 6>> result;
    result.reserve(entities.size());
    for (const EntityId entity : entities) {
        std::array<double, 6> column{};
        if (ancestors.contains(entity)) {
            const auto& joint = world_->registry().get<JointComponent>(entity);
            const Transform joint_world = world_->registry().get<TransformComponent>(entity).world;
            const Vec3 axis_world = joint_world.rotation.rotate(joint.axis);
            Vec3 linear{};
            Vec3 angular{};
            if (joint.type == JointType::Revolute || joint.type == JointType::Continuous) {
                linear = Cross(axis_world, end.translation - joint_world.translation);
                angular = axis_world;
            } else if (joint.type == JointType::Prismatic) {
                linear = axis_world;
            }
            column = {linear.x, linear.y, linear.z, angular.x, angular.y, angular.z};
        }
        result.push_back(column);
    }
    SIM_LOG_VERBOSE("Robot::jacobian completed columns=" + std::to_string(result.size()));
    return result;
}

std::vector<RobotKinematicsResult> EvaluateRobotKinematics(
    const World& world, EntityId robot_entity, std::span<const double> joint_positions,
    std::size_t configuration_count, std::span<const EntityId> links, EntityId jacobian_link,
    JointLimitPolicy policy) {
    SIM_LOG_VERBOSE("EvaluateRobotKinematics clones world and evaluates configurations=" +
                    std::to_string(configuration_count) + " links=" +
                    std::to_string(links.size()));
    SIM_PROFILE_SCOPE("Robot.EvaluateKinematicsBatch");
    // Pose a private clone so a solver sweeping hundreds of configurations never disturbs the
    // world being simulated. Cloning once here amortises it over the whole batch.
    World configured(world.id(), world.time().state().fixed_time_step);
    configured.restore(world.snapshot());
    Robot robot(configured, robot_entity);
    const std::size_t joint_count = robot.joints().size();
    if (joint_positions.size() != joint_count * configuration_count) {
        throw Error(ErrorCode::InvalidArgument,
                    "kinematics batch buffer size does not match robot joints");
    }
    const auto& members = configured.registry().get<RobotComponent>(robot_entity).links;
    const auto require_link = [&](EntityId link) {
        if (std::find(members.begin(), members.end(), link) == members.end())
            throw Error(ErrorCode::InvalidArgument, "entity is not a link of this robot");
    };
    for (const EntityId link : links) require_link(link);
    if (jacobian_link != kInvalidEntity) require_link(jacobian_link);

    std::vector<RobotKinematicsResult> results;
    results.reserve(configuration_count);
    SIM_LOG_VERBOSE("EvaluateRobotKinematics begins batch loop joint_count=" +
                    std::to_string(joint_count));
    for (std::size_t index = 0; index < configuration_count; ++index) {
        robot.set_joint_positions(joint_positions.subspan(index * joint_count, joint_count),
                                  policy);
        RobotKinematicsResult result;
        result.link_transforms.reserve(links.size());
        // set_joint_positions already refreshed the tree, so read the poses straight out.
        for (const EntityId link : links)
            result.link_transforms.push_back(
                configured.registry().get<TransformComponent>(link).world);
        if (jacobian_link != kInvalidEntity) result.jacobian = robot.jacobian(jacobian_link);
        results.push_back(std::move(result));
    }
    SIM_LOG_VERBOSE("EvaluateRobotKinematics completed results=" + std::to_string(results.size()));
    return results;
}

void InstallRobotControlSystem(World& world) {
    // JointControlComponent::max_effort is deliberately ignored here. This controller
    // integrates joints kinematically with no notion of contact, so there is nothing for
    // a torque ceiling to stall against and honouring it would only mean pretending. A
    // dynamics backend installs its own controller instead and does respect the limit;
    // this one runs when none is present, and then a joint always reaches its target.
    world.scheduler().add_system(SimulationPhase::ControllerUpdate, "robot.basic_controller",
        [&world](double time_step) {
            SIM_LOG_VERBOSE("InstallRobotControlSystem::controller begins time_step=" +
                            std::to_string(time_step));
            SIM_PROFILE_SCOPE("Robot.BasicController");
            std::size_t updated = 0;
            for (const EntityId entity : world.registry().view<JointComponent, JointControlComponent>()) {
                auto& joint = world.registry().get<JointComponent>(entity);
                const auto& control = world.registry().get<JointControlComponent>(entity);
                switch (control.mode) {
                    case JointControlMode::Disabled:
                        joint.velocity = 0.0;
                        break;
                    case JointControlMode::Position: {
                        const double requested = control.position_gain * (control.target - joint.position);
                        joint.velocity = std::clamp(requested, -control.velocity_limit, control.velocity_limit);
                        const double next = joint.position + joint.velocity * time_step;
                        if ((joint.velocity > 0.0 && next > control.target) ||
                            (joint.velocity < 0.0 && next < control.target)) {
                            joint.position = control.target;
                            joint.velocity = 0.0;
                        } else {
                            joint.position = next;
                        }
                        break;
                    }
                    case JointControlMode::Velocity:
                        joint.velocity = control.target;
                        joint.position += joint.velocity * time_step;
                        if (joint.type != JointType::Continuous)
                            joint.position = std::clamp(joint.position, joint.lower_limit, joint.upper_limit);
                        break;
                    case JointControlMode::Effort:
                        joint.effort = control.target;
                        break;
                }
                ApplyJointTransform(world, entity);
                ++updated;
            }
            SIM_LOG_VERBOSE("InstallRobotControlSystem::controller completed joints=" +
                            std::to_string(updated));
        });
}

}  // namespace sim
