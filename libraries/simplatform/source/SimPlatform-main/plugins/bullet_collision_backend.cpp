#include "sim/assets.hpp"
#include "sim/plugin.hpp"

#include <btBulletCollisionCommon.h>
#include <BulletCollision/Gimpact/btGImpactCollisionAlgorithm.h>
#include <BulletCollision/Gimpact/btGImpactShape.h>

#include <algorithm>
#include <cmath>
#include <optional>
#include <unordered_map>
#include <unordered_set>

namespace {

std::filesystem::path PathFromUtf8(std::string_view value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

btVector3 ToBullet(const sim::Vec3& value) {
    return {static_cast<btScalar>(value.x), static_cast<btScalar>(value.y),
            static_cast<btScalar>(value.z)};
}

btTransform ToBullet(const sim::Transform& value) {
    const auto rotation = value.rotation.normalized();
    return btTransform(btQuaternion(static_cast<btScalar>(rotation.x),
                                    static_cast<btScalar>(rotation.y),
                                    static_cast<btScalar>(rotation.z),
                                    static_cast<btScalar>(rotation.w)),
                       ToBullet(value.translation));
}

struct ObjectRecord {
    std::unique_ptr<btTriangleMesh> triangle_mesh;
    std::unique_ptr<btCollisionShape> shape;
    std::unique_ptr<btCollisionObject> object;
};

std::unique_ptr<btCollisionShape> MakeMeshShape(
    const sim::ColliderComponent& collider, const sim::Transform& transform,
    std::unique_ptr<btTriangleMesh>& triangle_mesh) {
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
        triangle_mesh = std::make_unique<btTriangleMesh>();
        for (std::size_t mesh_index = 0; mesh_index < asset.meshes.size(); ++mesh_index) {
            if (selected_mesh && *selected_mesh != mesh_index) continue;
            const auto& mesh = asset.meshes[mesh_index];
            for (std::size_t index = 0; index + 2 < mesh.indices.size(); index += 3) {
                const auto scaled = [&](std::uint32_t vertex) {
                    const auto& point = mesh.positions.at(vertex);
                    return ToBullet(sim::Vec3{point.x * scale.x, point.y * scale.y,
                                              point.z * scale.z});
                };
                triangle_mesh->addTriangle(scaled(mesh.indices[index]),
                                           scaled(mesh.indices[index + 1]),
                                           scaled(mesh.indices[index + 2]), true);
            }
        }
        if (triangle_mesh->getNumTriangles() != 0) {
            auto shape = std::make_unique<btGImpactMeshShape>(triangle_mesh.get());
            shape->updateBound();
            return shape;
        }
    }
#endif
    const sim::Vec3 half{collider.size.x * std::abs(transform.scale.x) * 0.5,
                         collider.size.y * std::abs(transform.scale.y) * 0.5,
                         collider.size.z * std::abs(transform.scale.z) * 0.5};
    return std::make_unique<btBoxShape>(ToBullet(half));
}

std::unique_ptr<btCollisionShape> MakeShape(
    const sim::ColliderComponent& collider, const sim::Transform& transform,
    std::unique_ptr<btTriangleMesh>& triangle_mesh) {
    const sim::Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                          std::abs(transform.scale.z)};
    switch (collider.shape) {
        case sim::ColliderShape::Box:
            return std::make_unique<btBoxShape>(ToBullet(sim::Vec3{
                collider.size.x * scale.x * 0.5, collider.size.y * scale.y * 0.5,
                collider.size.z * scale.z * 0.5}));
        case sim::ColliderShape::Sphere: {
            auto shape = std::make_unique<btSphereShape>(
                static_cast<btScalar>(collider.size.x));
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
            return MakeMeshShape(collider, transform, triangle_mesh);
    }
    throw sim::Error(sim::ErrorCode::Unsupported,
                     "unsupported Bullet3 collision shape");
}

class ClosestContactCallback final : public btCollisionWorld::ContactResultCallback {
public:
    explicit ClosestContactCallback(btScalar threshold) {
        m_closestDistanceThreshold = threshold;
    }

    btScalar addSingleResult(btManifoldPoint& point,
                             const btCollisionObjectWrapper*, int, int,
                             const btCollisionObjectWrapper*, int, int) override {
        minimum_distance = std::min(minimum_distance, point.getDistance());
        return btScalar{0};
    }

    btScalar minimum_distance{BT_LARGE_FLOAT};
};

double AabbDistance(const btCollisionObject& first, const btCollisionObject& second) {
    btVector3 first_min, first_max, second_min, second_max;
    first.getCollisionShape()->getAabb(first.getWorldTransform(), first_min, first_max);
    second.getCollisionShape()->getAabb(second.getWorldTransform(), second_min, second_max);
    double squared = 0.0;
    for (int axis = 0; axis < 3; ++axis) {
        const double gap = std::max({0.0,
            static_cast<double>(second_min[axis] - first_max[axis]),
            static_cast<double>(first_min[axis] - second_max[axis])});
        squared += gap * gap;
    }
    return std::sqrt(squared);
}

class BulletCollisionBackend final : public sim::ICollisionBackend {
public:
    BulletCollisionBackend()
        : collision_configuration_(std::make_unique<btDefaultCollisionConfiguration>()),
          dispatcher_(std::make_unique<btCollisionDispatcher>(collision_configuration_.get())),
          broadphase_(std::make_unique<btDbvtBroadphase>()),
          world_(std::make_unique<btCollisionWorld>(dispatcher_.get(), broadphase_.get(),
                                                   collision_configuration_.get())) {
        btGImpactCollisionAlgorithm::registerAlgorithm(dispatcher_.get());
    }

    ~BulletCollisionBackend() override { clear(); }

    sim::BackendCapabilities capabilities() const noexcept override {
        return {.collision_query = true, .distance_query = true};
    }

    void build(sim::World& scene) override {
        clear();
        scene.update_world_transforms();
        for (const auto entity : scene.registry().view<sim::ColliderComponent,
                                                       sim::TransformComponent>()) {
            const auto& collider = scene.registry().get<sim::ColliderComponent>(entity);
            const auto& transform = scene.registry().get<sim::TransformComponent>(entity).world;
            ObjectRecord record;
            record.shape = MakeShape(collider, transform, record.triangle_mesh);
            record.object = std::make_unique<btCollisionObject>();
            record.object->setCollisionShape(record.shape.get());
            record.object->setWorldTransform(ToBullet(transform));
            record.object->setUserIndex2(static_cast<int>(entity));
            world_->addCollisionObject(record.object.get());
            entity_by_object_.emplace(record.object.get(), entity);
            objects_.emplace(entity, std::move(record));
        }
        built_ = true;
        world_->performDiscreteCollisionDetection();
    }

    void sync_from_scene(sim::World& scene) override {
        require_built();
        scene.update_world_transforms();
        for (auto& [entity, record] : objects_) {
            record.object->setWorldTransform(ToBullet(
                scene.registry().get<sim::TransformComponent>(entity).world));
            world_->updateSingleAabb(record.object.get());
        }
    }

    bool check_collision(sim::EntityId first, sim::EntityId second) const override {
        auto& first_object = object(first);
        auto& second_object = object(second);
        ClosestContactCallback callback{btScalar{0}};
        world_->contactPairTest(&first_object, &second_object, callback);
        return callback.minimum_distance <= btScalar{0};
    }

    bool check_scene_collision(sim::EntityId entity) const override {
        const std::array entities{entity};
        return check_scene_collisions(entities).front() != 0U;
    }

    std::vector<std::uint8_t> check_scene_collisions(
        std::span<const sim::EntityId> entities) const override {
        require_built();
        for (const auto entity : entities) (void)object(entity);
        std::unordered_set<sim::EntityId> colliding_entities;
        world_->performDiscreteCollisionDetection();
        for (int index = 0; index < dispatcher_->getNumManifolds(); ++index) {
            const auto* manifold = dispatcher_->getManifoldByIndexInternal(index);
            bool colliding = false;
            for (int contact = 0; contact < manifold->getNumContacts(); ++contact) {
                if (manifold->getContactPoint(contact).getDistance() <= btScalar{0}) {
                    colliding = true;
                    break;
                }
            }
            if (!colliding) continue;
            mark_colliding(manifold->getBody0(), colliding_entities);
            mark_colliding(manifold->getBody1(), colliding_entities);
        }
        std::vector<std::uint8_t> result;
        result.reserve(entities.size());
        for (const auto entity : entities)
            result.push_back(colliding_entities.contains(entity) ? 1U : 0U);
        return result;
    }

    double distance(sim::EntityId first, sim::EntityId second) const override {
        auto& first_object = object(first);
        auto& second_object = object(second);
        ClosestContactCallback callback{BT_LARGE_FLOAT};
        world_->contactPairTest(&first_object, &second_object, callback);
        if (callback.minimum_distance < BT_LARGE_FLOAT)
            return std::max(0.0, static_cast<double>(callback.minimum_distance));
        return AabbDistance(first_object, second_object);
    }

private:
    btCollisionObject& object(sim::EntityId entity) const {
        require_built();
        const auto found = objects_.find(entity);
        if (found == objects_.end())
            throw sim::Error(sim::ErrorCode::NotFound,
                             "entity has no Bullet3 collision object");
        return *found->second.object;
    }

    void require_built() const {
        if (!built_)
            throw sim::Error(sim::ErrorCode::InvalidState,
                             "Bullet3 collision backend has not been built");
    }

    void mark_colliding(const btCollisionObject* object,
                        std::unordered_set<sim::EntityId>& colliding) const {
        const auto entity = entity_by_object_.find(object);
        if (entity != entity_by_object_.end()) colliding.insert(entity->second);
    }

    void clear() {
        for (auto& [entity, record] : objects_) {
            (void)entity;
            if (world_ && record.object) world_->removeCollisionObject(record.object.get());
        }
        objects_.clear();
        entity_by_object_.clear();
        built_ = false;
    }

    std::unique_ptr<btDefaultCollisionConfiguration> collision_configuration_;
    std::unique_ptr<btCollisionDispatcher> dispatcher_;
    std::unique_ptr<btBroadphaseInterface> broadphase_;
    std::unique_ptr<btCollisionWorld> world_;
    std::unordered_map<sim::EntityId, ObjectRecord> objects_;
    std::unordered_map<const btCollisionObject*, sim::EntityId> entity_by_object_;
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
    if (host->log != nullptr)
        host->log(0, "sim.collision.bullet3", "Bullet3 collision plugin loaded");
    return new SimPluginHandle{host};
}

SIM_PLUGIN_API void SimDestroyPlugin(SimPluginHandle* plugin) { delete plugin; }

SIM_PLUGIN_API sim::ICollisionBackend* SimCreateCollisionBackend(SimPluginHandle* plugin) {
    return plugin != nullptr ? new BulletCollisionBackend{} : nullptr;
}

SIM_PLUGIN_API void SimDestroyCollisionBackend(SimPluginHandle*,
                                                sim::ICollisionBackend* backend) {
    delete backend;
}

}
