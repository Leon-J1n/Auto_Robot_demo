#include "sim/assets.hpp"
#include "sim/plugin.hpp"

#include <btBulletDynamicsCommon.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <optional>
#include <unordered_map>

namespace {

std::filesystem::path PathFromUtf8(std::string_view value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

btVector3 ToBullet(const sim::Vec3& value) {
    return {static_cast<btScalar>(value.x), static_cast<btScalar>(value.y),
            static_cast<btScalar>(value.z)};
}

sim::Vec3 FromBullet(const btVector3& value) {
    return {static_cast<double>(value.x()), static_cast<double>(value.y()),
            static_cast<double>(value.z())};
}

btTransform ToBullet(const sim::Transform& value) {
    const auto rotation = value.rotation.normalized();
    return btTransform(btQuaternion(static_cast<btScalar>(rotation.x),
                                    static_cast<btScalar>(rotation.y),
                                    static_cast<btScalar>(rotation.z),
                                    static_cast<btScalar>(rotation.w)),
                       ToBullet(value.translation));
}

sim::Quaternion FromBullet(const btQuaternion& value) {
    return {static_cast<double>(value.w()), static_cast<double>(value.x()),
            static_cast<double>(value.y()), static_cast<double>(value.z())};
}

bool IsRobotDescendant(const sim::World& world, sim::EntityId entity) {
    auto current = entity;
    while (current != sim::kInvalidEntity) {
        if (world.registry().has<sim::RobotComponent>(current)) return true;
        current = world.registry().get<sim::HierarchyComponent>(current).parent;
    }
    return false;
}

bool HasDynamicAncestor(const sim::World& world, sim::EntityId entity) {
    auto current = world.registry().get<sim::HierarchyComponent>(entity).parent;
    while (current != sim::kInvalidEntity) {
        if (world.registry().has<sim::RigidBodyComponent>(current) &&
            world.registry().get<sim::RigidBodyComponent>(current).dynamic)
            return true;
        current = world.registry().get<sim::HierarchyComponent>(current).parent;
    }
    return false;
}

struct BodyRecord {
    std::unique_ptr<btTriangleMesh> triangle_mesh;
    std::unique_ptr<btCollisionShape> shape;
    std::unique_ptr<btDefaultMotionState> motion_state;
    std::unique_ptr<btRigidBody> body;
    bool dynamic{false};
    bool kinematic{false};
};

std::unique_ptr<btCollisionShape> MakeMeshShape(
    const sim::ColliderComponent& collider, const sim::Transform& transform,
    bool dynamic, std::unique_ptr<btTriangleMesh>& triangle_mesh) {
#if defined(SIM_HAS_ASSIMP)
    if (!collider.mesh_resource.empty()) {
        std::string source = collider.mesh_resource;
        std::optional<std::size_t> selected_mesh;
        if (const auto marker = source.rfind("#mesh="); marker != std::string::npos) {
            selected_mesh = static_cast<std::size_t>(std::stoull(source.substr(marker + 6)));
            source.resize(marker);
        }
        const auto path = PathFromUtf8(source);
        auto asset = path.extension() == ".obj" ? sim::ObjImporter{}.import(path).asset
                                                 : sim::AssimpImporter{}.import(path).asset;
        sim::AssetValidator::normalize(asset);
        const sim::Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                              std::abs(transform.scale.z)};
        if (dynamic) {
            auto shape = std::make_unique<btConvexHullShape>();
            for (std::size_t mesh_index = 0; mesh_index < asset.meshes.size(); ++mesh_index) {
                if (selected_mesh && *selected_mesh != mesh_index) continue;
                const auto& mesh = asset.meshes[mesh_index];
                for (const auto& point : mesh.positions)
                    shape->addPoint(ToBullet(sim::Vec3{point.x * scale.x,
                                                       point.y * scale.y,
                                                       point.z * scale.z}), false);
            }
            if (shape->getNumPoints() != 0) {
                shape->recalcLocalAabb();
                return shape;
            }
        } else {
            triangle_mesh = std::make_unique<btTriangleMesh>();
            for (std::size_t mesh_index = 0; mesh_index < asset.meshes.size(); ++mesh_index) {
                if (selected_mesh && *selected_mesh != mesh_index) continue;
                const auto& mesh = asset.meshes[mesh_index];
                for (std::size_t index = 0; index + 2 < mesh.indices.size(); index += 3) {
                    const auto scaled = [&](std::uint32_t vertex) {
                        const auto& point = mesh.positions.at(vertex);
                        return ToBullet(sim::Vec3{point.x * scale.x,
                                                  point.y * scale.y,
                                                  point.z * scale.z});
                    };
                    triangle_mesh->addTriangle(scaled(mesh.indices[index]),
                                               scaled(mesh.indices[index + 1]),
                                               scaled(mesh.indices[index + 2]), true);
                }
            }
            if (triangle_mesh->getNumTriangles() != 0)
                return std::make_unique<btBvhTriangleMeshShape>(triangle_mesh.get(), true);
        }
    }
#endif
    const auto half = sim::Vec3{collider.size.x * std::abs(transform.scale.x) * 0.5,
                                collider.size.y * std::abs(transform.scale.y) * 0.5,
                                collider.size.z * std::abs(transform.scale.z) * 0.5};
    return std::make_unique<btBoxShape>(ToBullet(half));
}

std::unique_ptr<btCollisionShape> MakeShape(
    const sim::ColliderComponent& collider, const sim::Transform& transform,
    bool dynamic, std::unique_ptr<btTriangleMesh>& triangle_mesh) {
    const sim::Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                          std::abs(transform.scale.z)};
    switch (collider.shape) {
        case sim::ColliderShape::Box:
            return std::make_unique<btBoxShape>(ToBullet(sim::Vec3{
                collider.size.x * scale.x * 0.5,
                collider.size.y * scale.y * 0.5,
                collider.size.z * scale.z * 0.5}));
        case sim::ColliderShape::Sphere: {
            auto shape = std::make_unique<btSphereShape>(static_cast<btScalar>(collider.size.x));
            shape->setLocalScaling(ToBullet(scale));
            return shape;
        }
        case sim::ColliderShape::Capsule:
            return std::make_unique<btCapsuleShapeZ>(
                static_cast<btScalar>(collider.size.x * std::max(scale.x, scale.y)),
                static_cast<btScalar>(collider.size.z * scale.z));
        case sim::ColliderShape::Cylinder: {
            const double radius = collider.size.x * std::max(scale.x, scale.y);
            return std::make_unique<btCylinderShapeZ>(ToBullet(sim::Vec3{
                radius, radius, collider.size.z * scale.z * 0.5}));
        }
        case sim::ColliderShape::Mesh:
            return MakeMeshShape(collider, transform, dynamic, triangle_mesh);
    }
    throw sim::Error(sim::ErrorCode::Unsupported, "unsupported Bullet collider shape");
}

template <typename T>
void Append(std::vector<std::byte>& output, const T& value) {
    const auto offset = output.size();
    output.resize(offset + sizeof(T));
    std::memcpy(output.data() + offset, &value, sizeof(T));
}

template <typename T>
T Read(std::span<const std::byte> state, std::size_t& offset) {
    if (offset + sizeof(T) > state.size())
        throw sim::Error(sim::ErrorCode::ValidationFailed, "Bullet state is truncated");
    T value{};
    std::memcpy(&value, state.data() + offset, sizeof(T));
    offset += sizeof(T);
    return value;
}

class BulletPhysicsBackend final : public sim::IPhysicsBackend {
public:
    BulletPhysicsBackend()
        : collision_configuration_(std::make_unique<btDefaultCollisionConfiguration>()),
          dispatcher_(std::make_unique<btCollisionDispatcher>(collision_configuration_.get())),
          broadphase_(std::make_unique<btDbvtBroadphase>()),
          solver_(std::make_unique<btSequentialImpulseConstraintSolver>()),
          world_(std::make_unique<btDiscreteDynamicsWorld>(
              dispatcher_.get(), broadphase_.get(), solver_.get(),
              collision_configuration_.get())) {
        world_->setGravity({0, 0, static_cast<btScalar>(-9.81)});
    }

    ~BulletPhysicsBackend() override { clear(); }

    sim::BackendCapabilities capabilities() const noexcept override {
        return {.rigid_body_dynamics = true, .state_serialization = true};
    }

    void build(sim::World& world) override {
        clear();
        world.update_world_transforms();
        for (const auto entity : world.registry().view<sim::ColliderComponent,
                                                       sim::TransformComponent>()) {
            const auto& collider = world.registry().get<sim::ColliderComponent>(entity);
            const auto& transform = world.registry().get<sim::TransformComponent>(entity).world;
            const auto* component = world.registry().has<sim::RigidBodyComponent>(entity)
                ? &world.registry().get<sim::RigidBodyComponent>(entity) : nullptr;
            BodyRecord record;
            const bool own_dynamic = component != nullptr && component->dynamic;
            record.kinematic = IsRobotDescendant(world, entity) ||
                               (!own_dynamic && HasDynamicAncestor(world, entity));
            record.dynamic = own_dynamic && !record.kinematic;
            record.shape = MakeShape(collider, transform, record.dynamic, record.triangle_mesh);
            const btScalar mass = record.dynamic
                ? static_cast<btScalar>(component->mass) : btScalar{0};
            btVector3 inertia{0, 0, 0};
            if (mass > 0) record.shape->calculateLocalInertia(mass, inertia);
            record.motion_state = std::make_unique<btDefaultMotionState>(ToBullet(transform));
            btRigidBody::btRigidBodyConstructionInfo info(
                mass, record.motion_state.get(), record.shape.get(), inertia);
            record.body = std::make_unique<btRigidBody>(info);
            record.body->setUserIndex2(static_cast<int>(entity));
            record.body->setFriction(static_cast<btScalar>(collider.friction.x));
            record.body->setRollingFriction(static_cast<btScalar>(collider.friction.y));
            record.body->setSpinningFriction(static_cast<btScalar>(collider.friction.z));
            record.body->setRestitution(static_cast<btScalar>(collider.restitution));
            if (collider.is_trigger)
                record.body->setCollisionFlags(record.body->getCollisionFlags() |
                                                btCollisionObject::CF_NO_CONTACT_RESPONSE);
            if (record.kinematic) {
                record.body->setCollisionFlags(record.body->getCollisionFlags() |
                                                btCollisionObject::CF_KINEMATIC_OBJECT);
                record.body->setActivationState(DISABLE_DEACTIVATION);
            }
            if (record.dynamic) {
                record.body->setLinearVelocity(ToBullet(component->linear_velocity));
                record.body->setAngularVelocity(ToBullet(component->angular_velocity));
            }
            world_->addRigidBody(record.body.get());
            if (record.dynamic && !component->gravity_enabled)
                record.body->setGravity({0, 0, 0});
            bodies_.emplace(entity, std::move(record));
        }
        built_ = true;
        sync_to_scene(world);
    }

    void step(sim::World& world) override {
        require_built();
        world.update_world_transforms();
        for (auto& [entity, record] : bodies_) {
            if (!record.kinematic) continue;
            const auto pose = ToBullet(
                world.registry().get<sim::TransformComponent>(entity).world);
            record.body->setWorldTransform(pose);
            record.motion_state->setWorldTransform(pose);
        }
        world_->stepSimulation(static_cast<btScalar>(world.time().state().fixed_time_step), 0);
        sync_to_scene(world);
    }

    void sync_to_scene(sim::World& world) override {
        require_built();
        std::vector<sim::EntityId> dynamic_entities;
        for (auto& [entity, record] : bodies_) {
            if (!record.dynamic) continue;
            const auto& pose = record.body->getWorldTransform();
            auto& transform = world.registry().get<sim::TransformComponent>(entity);
            transform.world.translation = FromBullet(pose.getOrigin());
            transform.world.rotation = FromBullet(pose.getRotation());
            auto& component = world.registry().get<sim::RigidBodyComponent>(entity);
            component.linear_velocity = FromBullet(record.body->getLinearVelocity());
            component.angular_velocity = FromBullet(record.body->getAngularVelocity());
            dynamic_entities.push_back(entity);
        }
        for (const auto entity : dynamic_entities) {
            auto& transform = world.registry().get<sim::TransformComponent>(entity);
            const auto parent = world.registry().get<sim::HierarchyComponent>(entity).parent;
            transform.local = parent == sim::kInvalidEntity
                ? transform.world
                : world.registry().get<sim::TransformComponent>(parent).world.inverse() *
                      transform.world;
        }
        world.update_world_transforms();
    }

    std::vector<std::byte> save_state() const override {
        require_built();
        std::vector<std::byte> output;
        Append(output, std::uint32_t{0x42554C54});
        Append(output, std::uint32_t{1});
        const auto count = static_cast<std::uint64_t>(std::count_if(
            bodies_.begin(), bodies_.end(), [](const auto& item) { return item.second.dynamic; }));
        Append(output, count);
        for (const auto& [entity, record] : bodies_) {
            if (!record.dynamic) continue;
            Append(output, entity);
            const auto& pose = record.body->getWorldTransform();
            const double values[13]{
                pose.getOrigin().x(), pose.getOrigin().y(), pose.getOrigin().z(),
                pose.getRotation().w(), pose.getRotation().x(), pose.getRotation().y(),
                pose.getRotation().z(), record.body->getLinearVelocity().x(),
                record.body->getLinearVelocity().y(), record.body->getLinearVelocity().z(),
                record.body->getAngularVelocity().x(), record.body->getAngularVelocity().y(),
                record.body->getAngularVelocity().z()};
            for (const auto value : values) Append(output, value);
        }
        return output;
    }

    void restore_state(std::span<const std::byte> state) override {
        require_built();
        std::size_t offset = 0;
        if (Read<std::uint32_t>(state, offset) != 0x42554C54 ||
            Read<std::uint32_t>(state, offset) != 1)
            throw sim::Error(sim::ErrorCode::ValidationFailed,
                             "Bullet state signature is invalid");
        const auto count = Read<std::uint64_t>(state, offset);
        for (std::uint64_t index = 0; index < count; ++index) {
            const auto entity = Read<sim::EntityId>(state, offset);
            const auto found = bodies_.find(entity);
            if (found == bodies_.end() || !found->second.dynamic)
                throw sim::Error(sim::ErrorCode::ValidationFailed,
                                 "Bullet state references an unknown body");
            double values[13]{};
            for (auto& value : values) value = Read<double>(state, offset);
            btTransform pose = btTransform::getIdentity();
            pose.setOrigin({static_cast<btScalar>(values[0]), static_cast<btScalar>(values[1]),
                            static_cast<btScalar>(values[2])});
            pose.setRotation({static_cast<btScalar>(values[4]), static_cast<btScalar>(values[5]),
                              static_cast<btScalar>(values[6]), static_cast<btScalar>(values[3])});
            auto& body = *found->second.body;
            body.setWorldTransform(pose);
            found->second.motion_state->setWorldTransform(pose);
            body.setLinearVelocity({static_cast<btScalar>(values[7]),
                                    static_cast<btScalar>(values[8]),
                                    static_cast<btScalar>(values[9])});
            body.setAngularVelocity({static_cast<btScalar>(values[10]),
                                     static_cast<btScalar>(values[11]),
                                     static_cast<btScalar>(values[12])});
            body.activate(true);
        }
        if (offset != state.size())
            throw sim::Error(sim::ErrorCode::ValidationFailed,
                             "Bullet state contains trailing data");
    }

    void apply_body_wrench(sim::EntityId entity, const sim::Vec3& force,
                           const sim::Vec3& torque) override {
        require_built();
        const auto found = bodies_.find(entity);
        if (found == bodies_.end() || !found->second.dynamic)
            throw sim::Error(sim::ErrorCode::NotFound,
                             "entity has no dynamic Bullet body");
        found->second.body->activate(true);
        found->second.body->applyCentralForce(ToBullet(force));
        found->second.body->applyTorque(ToBullet(torque));
    }

private:
    void require_built() const {
        if (!built_)
            throw sim::Error(sim::ErrorCode::InvalidState,
                             "Bullet backend has not been built");
    }

    void clear() {
        for (auto& [entity, record] : bodies_) {
            (void)entity;
            if (world_ && record.body) world_->removeRigidBody(record.body.get());
        }
        bodies_.clear();
        built_ = false;
    }

    std::unique_ptr<btDefaultCollisionConfiguration> collision_configuration_;
    std::unique_ptr<btCollisionDispatcher> dispatcher_;
    std::unique_ptr<btBroadphaseInterface> broadphase_;
    std::unique_ptr<btSequentialImpulseConstraintSolver> solver_;
    std::unique_ptr<btDiscreteDynamicsWorld> world_;
    std::unordered_map<sim::EntityId, BodyRecord> bodies_;
    bool built_{false};
};

}  // namespace

struct SimPluginHandle {
    const SimHostApi* host{nullptr};
};

extern "C" {

SIM_PLUGIN_API std::uint32_t SimPluginApiVersion() { return sim::kPluginApiVersion; }

SIM_PLUGIN_API SimPluginHandle* SimCreatePlugin(const SimHostApi* host) {
    if (host == nullptr || host->api_version != sim::kPluginApiVersion) return nullptr;
    if (host->log != nullptr) host->log(0, "sim.physics.bullet3", "Bullet3 plugin loaded");
    return new SimPluginHandle{host};
}

SIM_PLUGIN_API void SimDestroyPlugin(SimPluginHandle* plugin) { delete plugin; }

SIM_PLUGIN_API sim::IPhysicsBackend* SimCreatePhysicsBackend(SimPluginHandle* plugin) {
    return plugin != nullptr ? new BulletPhysicsBackend{} : nullptr;
}

SIM_PLUGIN_API void SimDestroyPhysicsBackend(SimPluginHandle*,
                                              sim::IPhysicsBackend* backend) {
    delete backend;
}

}
