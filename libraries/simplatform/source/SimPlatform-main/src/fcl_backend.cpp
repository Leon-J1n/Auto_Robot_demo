#include "sim/collision.hpp"

#include <fcl/fcl.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <limits>
#include <optional>
#include <unordered_map>
#include <unordered_set>

namespace sim {
namespace {

std::filesystem::path PathFromUtf8(std::string_view value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

using Object = std::unique_ptr<fcl::CollisionObjectd>;
using ObjectMap = std::unordered_map<EntityId, Object>;
using BroadPhaseManager = fcl::DynamicAABBTreeCollisionManagerd;

fcl::Transform3d ToFclTransform(const Transform& source) {
    fcl::Transform3d result = fcl::Transform3d::Identity();
    result.translation() = Eigen::Vector3d(source.translation.x, source.translation.y,
                                           source.translation.z);
    result.linear() = Eigen::Quaterniond(source.rotation.w, source.rotation.x,
                                         source.rotation.y, source.rotation.z)
                          .normalized().toRotationMatrix();
    return result;
}

std::shared_ptr<fcl::CollisionGeometryd> MakeGeometry(const ColliderComponent& collider,
                                                       const Transform& transform) {
    const Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                     std::abs(transform.scale.z)};
    switch (collider.shape) {
        case ColliderShape::Sphere:
            return std::make_shared<fcl::Ellipsoidd>(collider.size.x * scale.x,
                                                     collider.size.x * scale.y,
                                                     collider.size.x * scale.z);
        case ColliderShape::Capsule:
            return std::make_shared<fcl::Capsuled>(collider.size.x * std::max(scale.x, scale.y),
                                                   collider.size.z * scale.z);
        case ColliderShape::Cylinder:
            return std::make_shared<fcl::Cylinderd>(collider.size.x * std::max(scale.x, scale.y),
                                                    collider.size.z * scale.z);
        case ColliderShape::Mesh: {
#if defined(SIM_HAS_ASSIMP)
            if (!collider.mesh_resource.empty()) {
                std::string source = collider.mesh_resource;
                std::optional<std::size_t> selected_mesh;
                if (const auto marker = source.rfind("#mesh="); marker != std::string::npos) {
                    try {
                        selected_mesh = static_cast<std::size_t>(
                            std::stoull(source.substr(marker + 6)));
                        source.resize(marker);
                    } catch (const std::exception&) {
                        selected_mesh.reset();
                    }
                }
                auto source_path = PathFromUtf8(source);
                auto asset = (source_path.extension() == ".obj" ||
                              source_path.extension() == ".OBJ")
                    ? ObjImporter{}.import(source_path).asset
                    : AssimpImporter{}.import(source_path).asset;
                AssetValidator::normalize(asset);
                auto model = std::make_shared<fcl::BVHModel<fcl::OBBRSSd>>();
                std::vector<fcl::Vector3d> vertices;
                std::vector<fcl::Triangle> triangles;
                std::string extension = std::filesystem::path(source).extension().string();
                std::transform(extension.begin(), extension.end(), extension.begin(),
                               [](unsigned char value) { return static_cast<char>(std::tolower(value)); });
                for (std::size_t mesh_index = 0; mesh_index < asset.meshes.size(); ++mesh_index) {
                    if (selected_mesh && *selected_mesh != mesh_index) continue;
                    const auto& mesh = asset.meshes[mesh_index];
                    const auto offset = static_cast<std::uint32_t>(vertices.size());
                    for (const auto& point : mesh.positions) {
                        if (extension == ".fbx")
                            vertices.emplace_back(-point.z * scale.x, -point.x * scale.y,
                                                  point.y * scale.z);
                        else
                            vertices.emplace_back(point.x * scale.x, point.y * scale.y,
                                                  point.z * scale.z);
                    }
                    for (std::size_t index = 0; index + 2 < mesh.indices.size(); index += 3)
                        triangles.emplace_back(offset + mesh.indices[index],
                                               offset + mesh.indices[index + 1],
                                               offset + mesh.indices[index + 2]);
                }
                if (!vertices.empty() && !triangles.empty()) {
                    model->beginModel();
                    model->addSubModel(vertices, triangles);
                    model->endModel();
                    return model;
                }
            }
#endif
            return std::make_shared<fcl::Boxd>(collider.size.x * scale.x,
                                               collider.size.y * scale.y,
                                               collider.size.z * scale.z);
        }
        case ColliderShape::Box:
            return std::make_shared<fcl::Boxd>(collider.size.x * scale.x,
                                               collider.size.y * scale.y,
                                               collider.size.z * scale.z);
    }
    throw Error(ErrorCode::Unsupported, "unsupported FCL collider shape");
}

fcl::CollisionObjectd& Require(ObjectMap& objects, EntityId entity) {
    const auto found = objects.find(entity);
    if (found == objects.end()) throw Error(ErrorCode::NotFound, "entity has no FCL collider");
    return *found->second;
}

const fcl::CollisionObjectd& Require(const ObjectMap& objects, EntityId entity) {
    const auto found = objects.find(entity);
    if (found == objects.end()) throw Error(ErrorCode::NotFound, "entity has no FCL collider");
    return *found->second;
}

bool Collides(const fcl::CollisionObjectd& first, const fcl::CollisionObjectd& second) {
    fcl::CollisionRequestd request;
    request.num_max_contacts = 1;
    fcl::CollisionResultd result;
    fcl::collide(&first, &second, request, result);
    return result.isCollision();
}

double ObjectDistance(const fcl::CollisionObjectd& first, const fcl::CollisionObjectd& second) {
    fcl::DistanceRequestd request;
    request.enable_nearest_points = true;
    fcl::DistanceResultd result;
    return std::max(0.0, fcl::distance(&first, &second, request, result));
}

bool SceneCollision(const ObjectMap& objects, EntityId entity) {
    const auto& target = Require(objects, entity);
    for (const auto& [other, object] : objects)
        if (other != entity && Collides(target, *object)) return true;
    return false;
}

struct BatchCollisionData {
    const std::unordered_map<const fcl::CollisionObjectd*, EntityId>* object_entities;
    const std::unordered_map<EntityId, std::size_t>* requested_entities;
    std::vector<std::uint8_t>* result;
};

bool CollectBatchCollision(fcl::CollisionObjectd* first, fcl::CollisionObjectd* second,
                           void* opaque) {
    if (!Collides(*first, *second)) return false;
    auto& data = *static_cast<BatchCollisionData*>(opaque);
    const auto mark = [&](const fcl::CollisionObjectd* object) {
        const auto entity = data.object_entities->at(object);
        const auto requested = data.requested_entities->find(entity);
        if (requested != data.requested_entities->end())
            (*data.result)[requested->second] = 1U;
    };
    mark(first);
    mark(second);
    return false;
}

}  // namespace

struct FCLQueryContext::Impl { ObjectMap objects; };
struct FCLBackend::Impl {
    ObjectMap objects;
    std::unordered_map<const fcl::CollisionObjectd*, EntityId> object_entities;
    std::unique_ptr<BroadPhaseManager> broad_phase;
};

FCLQueryContext::FCLQueryContext(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
FCLQueryContext::~FCLQueryContext() = default;
FCLQueryContext::FCLQueryContext(FCLQueryContext&&) noexcept = default;
FCLQueryContext& FCLQueryContext::operator=(FCLQueryContext&&) noexcept = default;

void FCLQueryContext::set_transform(EntityId entity, const Transform& transform) {
    auto& object = Require(impl_->objects, entity);
    object.setTransform(ToFclTransform(transform));
    object.computeAABB();
}
bool FCLQueryContext::check_collision(EntityId first, EntityId second) const {
    return Collides(Require(impl_->objects, first), Require(impl_->objects, second));
}
bool FCLQueryContext::check_scene_collision(EntityId entity) const {
    return SceneCollision(impl_->objects, entity);
}
double FCLQueryContext::distance(EntityId first, EntityId second) const {
    return ObjectDistance(Require(impl_->objects, first), Require(impl_->objects, second));
}

FCLBackend::FCLBackend() : impl_(std::make_unique<Impl>()) {}
FCLBackend::~FCLBackend() = default;
BackendCapabilities FCLBackend::capabilities() const noexcept {
    return {.collision_query = true, .distance_query = true, .raycast = true};
}

void FCLBackend::build(World& world) {
    impl_->objects.clear();
    impl_->object_entities.clear();
    impl_->broad_phase = std::make_unique<BroadPhaseManager>();
    world.update_world_transforms();
    for (const EntityId entity : world.registry().view<ColliderComponent, TransformComponent>()) {
        const auto& collider = world.registry().get<ColliderComponent>(entity);
        const auto& transform = world.registry().get<TransformComponent>(entity).world;
        auto object = std::make_unique<fcl::CollisionObjectd>(MakeGeometry(collider, transform),
                                                              ToFclTransform(transform));
        object->computeAABB();
        const auto* pointer = object.get();
        impl_->objects.emplace(entity, std::move(object));
        impl_->object_entities.emplace(pointer, entity);
    }
    std::vector<fcl::CollisionObjectd*> objects;
    objects.reserve(impl_->objects.size());
    for (const auto& [entity, object] : impl_->objects) {
        (void)entity;
        objects.push_back(object.get());
    }
    impl_->broad_phase->registerObjects(objects);
    impl_->broad_phase->setup();
}

void FCLBackend::sync_from_scene(World& world) {
    world.update_world_transforms();
    for (auto& [entity, object] : impl_->objects) {
        if (!world.registry().valid(entity)) continue;
        object->setTransform(ToFclTransform(world.registry().get<TransformComponent>(entity).world));
        object->computeAABB();
    }
    if (impl_->broad_phase) impl_->broad_phase->update();
}
bool FCLBackend::check_collision(EntityId first, EntityId second) const {
    return Collides(Require(impl_->objects, first), Require(impl_->objects, second));
}
bool FCLBackend::check_scene_collision(EntityId entity) const {
    return SceneCollision(impl_->objects, entity);
}
std::vector<std::uint8_t> FCLBackend::check_scene_collisions(
    std::span<const EntityId> entities) const {
    std::vector<std::uint8_t> result(entities.size(), 0U);
    std::unordered_map<EntityId, std::size_t> requested;
    requested.reserve(entities.size());
    for (std::size_t index = 0; index < entities.size(); ++index) {
        (void)Require(impl_->objects, entities[index]);
        requested.emplace(entities[index], index);
    }
    BatchCollisionData data{&impl_->object_entities, &requested, &result};
    if (impl_->broad_phase)
        impl_->broad_phase->collide(&data, CollectBatchCollision);
    return result;
}
double FCLBackend::distance(EntityId first, EntityId second) const {
    return ObjectDistance(Require(impl_->objects, first), Require(impl_->objects, second));
}

FCLQueryContext FCLBackend::create_query_context() const {
    auto context = std::make_unique<FCLQueryContext::Impl>();
    for (const auto& [entity, source] : impl_->objects) {
        auto geometry = std::const_pointer_cast<fcl::CollisionGeometryd>(source->collisionGeometry());
        auto object = std::make_unique<fcl::CollisionObjectd>(std::move(geometry),
                                                              source->getTransform());
        object->computeAABB();
        context->objects.emplace(entity, std::move(object));
    }
    return FCLQueryContext(std::move(context));
}

bool FCLBackend::check_robot_configuration(const World& world, EntityId robot_entity,
                                           std::span<const double> joint_positions) const {
    World configured(world.id(), world.time().state().fixed_time_step);
    configured.restore(world.snapshot());
    Robot robot(configured, robot_entity);
    robot.set_joint_positions(joint_positions);
    auto context = create_query_context();
    const auto& links = configured.registry().get<RobotComponent>(robot_entity).links;
    const auto belongs_to_robot = [&](EntityId entity) {
        while (entity != kInvalidEntity && configured.registry().valid(entity)) {
            if (std::find(links.begin(), links.end(), entity) != links.end()) return true;
            entity = configured.registry().get<HierarchyComponent>(entity).parent;
        }
        return false;
    };
    std::vector<EntityId> robot_colliders;
    for (const auto& [entity, object] : impl_->objects) {
        (void)object;
        if (!belongs_to_robot(entity)) continue;
        context.set_transform(entity,
            configured.registry().get<TransformComponent>(entity).world);
        robot_colliders.push_back(entity);
    }
    for (const EntityId collider : robot_colliders)
        if (context.check_scene_collision(collider)) return true;
    return false;
}

std::vector<CollisionPair> FCLBackend::robot_configuration_contacts(
    const World& world, EntityId robot_entity, std::span<const double> joint_positions,
    double distance_threshold, JointLimitPolicy policy,
    std::span<const std::array<EntityId, 2>> excluded_pairs) const {
    if (!std::isfinite(distance_threshold) || distance_threshold < 0.0)
        throw Error(ErrorCode::InvalidArgument,
                    "contact distance threshold must be finite and non-negative");
    World configured(world.id(), world.time().state().fixed_time_step);
    configured.restore(world.snapshot());
    Robot robot(configured, robot_entity);
    robot.set_joint_positions(joint_positions, policy);
    auto context = create_query_context();
    const auto& links = configured.registry().get<RobotComponent>(robot_entity).links;
    // Which link a collider hangs off, or kInvalidEntity when it is not part of the robot at all.
    // Anything parented under a link counts, so a grasped object travels with the gripper and
    // shows up against the scene as an ordinary robot-side collider.
    const auto owning_link = [&](EntityId entity) {
        while (entity != kInvalidEntity && configured.registry().valid(entity)) {
            if (std::find(links.begin(), links.end(), entity) != links.end()) return entity;
            entity = configured.registry().get<HierarchyComponent>(entity).parent;
        }
        return kInvalidEntity;
    };
    std::vector<EntityId> robot_colliders;
    std::vector<EntityId> others;
    std::unordered_map<EntityId, EntityId> link_of;
    for (const auto& [entity, object] : impl_->objects) {
        (void)object;
        if (const EntityId link = owning_link(entity); link != kInvalidEntity) {
            context.set_transform(entity,
                configured.registry().get<TransformComponent>(entity).world);
            robot_colliders.push_back(entity);
            link_of.emplace(entity, link);
        } else {
            others.push_back(entity);
        }
    }
    // Hash-map order is unspecified; sort so a repeated query reports pairs in the same order.
    std::sort(robot_colliders.begin(), robot_colliders.end());
    std::sort(others.begin(), others.end());

    const auto& objects = context.impl_->objects;
    std::vector<CollisionPair> pairs;
    const double margin = distance_threshold + 1e-9;
    std::unordered_map<EntityId, std::unordered_set<EntityId>> excluded;
    for (const auto& pair : excluded_pairs) {
        excluded[pair[0]].insert(pair[1]);
        excluded[pair[1]].insert(pair[0]);
    }
    const auto is_excluded = [&](EntityId first, EntityId second) {
        const auto found = excluded.find(first);
        return found != excluded.end() && found->second.contains(second);
    };
    const auto consider = [&](EntityId first, EntityId second) {
        if (is_excluded(first, second)) return;
        const auto& left = Require(objects, first);
        const auto& right = Require(objects, second);
        // Grow one box by the margin and test overlap. An AABB gap never exceeds the surface
        // gap, so this can only discard pairs the narrow phase would have rejected anyway.
        fcl::AABBd expanded = left.getAABB();
        expanded.expand(Eigen::Vector3d(margin, margin, margin));
        if (!expanded.overlap(right.getAABB())) return;
        CollisionPair pair{first, second, 0.0, false};
        if (Collides(left, right)) {
            pair.colliding = true;
        } else {
            if (distance_threshold <= 0.0) return;
            pair.distance = ObjectDistance(left, right);
            if (pair.distance > distance_threshold) return;
        }
        pairs.push_back(pair);
    };
    for (const EntityId collider : robot_colliders)
        for (const EntityId other : others) consider(collider, other);
    // Self pairs, each unordered combination once. Two colliders on the same link are rigidly
    // attached, so whether they touch is a fixed property of the model rather than of the
    // configuration -- and when they share a face exactly, the narrow phase answers
    // inconsistently across configurations. Skipping them keeps the reported set stable, which
    // is what callers subtracting a rest-pose baseline depend on.
    for (std::size_t first = 0; first < robot_colliders.size(); ++first)
        for (std::size_t second = first + 1; second < robot_colliders.size(); ++second) {
            if (link_of.at(robot_colliders[first]) == link_of.at(robot_colliders[second]))
                continue;
            consider(robot_colliders[first], robot_colliders[second]);
        }
    return pairs;
}

std::vector<bool> FCLBackend::check_robot_configurations(
    const World& world, EntityId robot, std::span<const double> joint_positions,
    std::size_t configuration_count) const {
    const std::size_t joint_count = Robot(const_cast<World&>(world), robot).joints().size();
    if (joint_positions.size() != joint_count * configuration_count)
        throw Error(ErrorCode::InvalidArgument,
                    "FCL batch configuration buffer size does not match robot joints");
    std::vector<bool> result;
    result.reserve(configuration_count);
    for (std::size_t index = 0; index < configuration_count; ++index)
        result.push_back(check_robot_configuration(
            world, robot, joint_positions.subspan(index * joint_count, joint_count)));
    return result;
}

void FCLBackend::render_camera(const Transform&, const CameraComponent&,
                               std::vector<std::uint8_t>&, std::vector<float>&) {
    throw Error(ErrorCode::Unsupported, "FCL collision backend does not render cameras");
}

std::vector<float> FCLBackend::raycast(const Vec3& world_origin,
                                       std::span<const Vec3> world_directions,
                                       double min_distance, double max_distance) {
    if (!(min_distance >= 0.0) || !(max_distance > min_distance))
        throw Error(ErrorCode::InvalidArgument, "invalid FCL raycast range");
    std::vector<float> distances;
    distances.reserve(world_directions.size());
    constexpr double radius = 1e-5;
    for (const Vec3& source : world_directions) {
        Eigen::Vector3d direction(source.x, source.y, source.z);
        if (direction.norm() <= std::numeric_limits<double>::epsilon()) {
            distances.push_back(static_cast<float>(max_distance));
            continue;
        }
        direction.normalize();
        const auto intersects = [&](double length) {
            const double segment = std::max(length - min_distance, radius);
            auto geometry = std::make_shared<fcl::Capsuled>(radius, segment);
            fcl::Transform3d pose = fcl::Transform3d::Identity();
            pose.linear() = Eigen::Quaterniond::FromTwoVectors(Eigen::Vector3d::UnitZ(), direction)
                                .toRotationMatrix();
            pose.translation() = Eigen::Vector3d(world_origin.x, world_origin.y, world_origin.z) +
                                 direction * (min_distance + segment * 0.5);
            fcl::CollisionObjectd ray(geometry, pose);
            for (const auto& [entity, object] : impl_->objects) {
                (void)entity;
                if (Collides(ray, *object)) return true;
            }
            return false;
        };
        if (!intersects(max_distance)) {
            distances.push_back(static_cast<float>(max_distance));
            continue;
        }
        double lower = min_distance;
        double upper = max_distance;
        for (int iteration = 0; iteration < 24; ++iteration) {
            const double middle = (lower + upper) * 0.5;
            if (intersects(middle)) upper = middle;
            else lower = middle;
        }
        distances.push_back(static_cast<float>(upper));
    }
    return distances;
}

}  // namespace sim
