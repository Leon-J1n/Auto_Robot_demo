#include "sim/assets.hpp"
#include "sim/plugin.hpp"

#include <coal/BV/OBBRSS.h>
#include <coal/BVH/BVH_model.h>
#include <coal/broadphase/broadphase_dynamic_AABB_tree.h>
#include <coal/collision.h>
#include <coal/distance.h>
#include <coal/shape/geometric_shapes.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <optional>
#include <unordered_map>

namespace {

std::filesystem::path PathFromUtf8(std::string_view value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

coal::Transform3s ToCoalTransform(const sim::Transform& source) {
    const auto rotation = source.rotation.normalized();
    coal::Transform3s result;
    result.setQuatRotation(Eigen::Quaterniond(rotation.w, rotation.x, rotation.y, rotation.z));
    result.setTranslation(coal::Vec3s(source.translation.x, source.translation.y,
                                      source.translation.z));
    return result;
}

std::shared_ptr<coal::CollisionGeometry> MakeGeometry(
    const sim::ColliderComponent& collider, const sim::Transform& transform) {
    const sim::Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                          std::abs(transform.scale.z)};
    switch (collider.shape) {
        case sim::ColliderShape::Sphere:
            return std::make_shared<coal::Ellipsoid>(collider.size.x * scale.x,
                                                     collider.size.x * scale.y,
                                                     collider.size.x * scale.z);
        case sim::ColliderShape::Capsule:
            return std::make_shared<coal::Capsule>(
                collider.size.x * std::max(scale.x, scale.y), collider.size.z * scale.z);
        case sim::ColliderShape::Cylinder:
            return std::make_shared<coal::Cylinder>(
                collider.size.x * std::max(scale.x, scale.y), collider.size.z * scale.z);
        case sim::ColliderShape::Mesh: {
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
                auto model = std::make_shared<coal::BVHModel<coal::OBBRSS>>();
                std::vector<coal::Vec3s> vertices;
                std::vector<coal::Triangle> triangles;
                std::string extension =
                    std::filesystem::path(collider.mesh_resource).extension().string();
                std::transform(extension.begin(), extension.end(), extension.begin(),
                               [](unsigned char value) {
                                   return static_cast<char>(std::tolower(value));
                               });
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
                    model->beginModel(static_cast<unsigned int>(triangles.size()),
                                      static_cast<unsigned int>(vertices.size()));
                    model->addSubModel(vertices, triangles);
                    model->endModel();
                    return model;
                }
            }
#endif
            return std::make_shared<coal::Box>(collider.size.x * scale.x,
                                               collider.size.y * scale.y,
                                               collider.size.z * scale.z);
        }
        case sim::ColliderShape::Box:
            return std::make_shared<coal::Box>(collider.size.x * scale.x,
                                               collider.size.y * scale.y,
                                               collider.size.z * scale.z);
    }
    throw sim::Error(sim::ErrorCode::Unsupported, "unsupported Coal collider shape");
}

using Object = std::unique_ptr<coal::CollisionObject>;
using ObjectMap = std::unordered_map<sim::EntityId, Object>;

coal::CollisionObject& Require(ObjectMap& objects, sim::EntityId entity) {
    const auto found = objects.find(entity);
    if (found == objects.end())
        throw sim::Error(sim::ErrorCode::NotFound, "entity has no Coal collider");
    return *found->second;
}

const coal::CollisionObject& Require(const ObjectMap& objects, sim::EntityId entity) {
    const auto found = objects.find(entity);
    if (found == objects.end())
        throw sim::Error(sim::ErrorCode::NotFound, "entity has no Coal collider");
    return *found->second;
}

bool Collides(const coal::CollisionObject& first, const coal::CollisionObject& second) {
    coal::CollisionRequest request(coal::NO_REQUEST, 1);
    coal::CollisionResult result;
    coal::collide(&first, &second, request, result);
    return result.isCollision();
}

double ObjectDistance(const coal::CollisionObject& first,
                      const coal::CollisionObject& second) {
    coal::DistanceRequest request;
    coal::DistanceResult result;
    return std::max(0.0, static_cast<double>(coal::distance(&first, &second, request, result)));
}

class BatchCollisionCallback final : public coal::CollisionCallBackBase {
public:
    BatchCollisionCallback(
        const std::unordered_map<const coal::CollisionObject*, sim::EntityId>& object_entities,
        const std::unordered_map<sim::EntityId, std::size_t>& requested_entities,
        std::vector<std::uint8_t>& result)
        : object_entities_(object_entities), requested_entities_(requested_entities),
          result_(result) {}

    bool collide(coal::CollisionObject* first, coal::CollisionObject* second) override {
        if (!Collides(*first, *second)) return false;
        mark(first);
        mark(second);
        return false;
    }

private:
    void mark(const coal::CollisionObject* object) {
        const auto entity = object_entities_.at(object);
        const auto requested = requested_entities_.find(entity);
        if (requested != requested_entities_.end()) result_[requested->second] = 1U;
    }

    const std::unordered_map<const coal::CollisionObject*, sim::EntityId>& object_entities_;
    const std::unordered_map<sim::EntityId, std::size_t>& requested_entities_;
    std::vector<std::uint8_t>& result_;
};

class CoalCollisionBackend final : public sim::ICollisionBackend {
public:
    sim::BackendCapabilities capabilities() const noexcept override {
        return {.collision_query = true, .distance_query = true};
    }

    void build(sim::World& world) override {
        objects_.clear();
        object_entities_.clear();
        broad_phase_ = std::make_unique<coal::DynamicAABBTreeCollisionManager>();
        world.update_world_transforms();
        for (const auto entity :
             world.registry().view<sim::ColliderComponent, sim::TransformComponent>()) {
            const auto& collider = world.registry().get<sim::ColliderComponent>(entity);
            const auto& transform =
                world.registry().get<sim::TransformComponent>(entity).world;
            auto object = std::make_unique<coal::CollisionObject>(
                MakeGeometry(collider, transform), ToCoalTransform(transform));
            object->computeAABB();
            const auto* pointer = object.get();
            objects_.emplace(entity, std::move(object));
            object_entities_.emplace(pointer, entity);
        }
        std::vector<coal::CollisionObject*> objects;
        objects.reserve(objects_.size());
        for (const auto& [entity, object] : objects_) {
            (void)entity;
            objects.push_back(object.get());
        }
        broad_phase_->registerObjects(objects);
        broad_phase_->setup();
    }

    void sync_from_scene(sim::World& world) override {
        world.update_world_transforms();
        for (auto& [entity, object] : objects_) {
            if (!world.registry().valid(entity)) continue;
            object->setTransform(ToCoalTransform(
                world.registry().get<sim::TransformComponent>(entity).world));
            object->computeAABB();
        }
        if (broad_phase_) broad_phase_->update();
    }

    bool check_collision(sim::EntityId first, sim::EntityId second) const override {
        return Collides(Require(objects_, first), Require(objects_, second));
    }

    bool check_scene_collision(sim::EntityId entity) const override {
        const std::array entities{entity};
        return check_scene_collisions(entities).front() != 0U;
    }

    std::vector<std::uint8_t> check_scene_collisions(
        std::span<const sim::EntityId> entities) const override {
        std::vector<std::uint8_t> result(entities.size(), 0U);
        std::unordered_map<sim::EntityId, std::size_t> requested;
        requested.reserve(entities.size());
        for (std::size_t index = 0; index < entities.size(); ++index) {
            (void)Require(objects_, entities[index]);
            requested.emplace(entities[index], index);
        }
        if (broad_phase_) {
            BatchCollisionCallback callback(object_entities_, requested, result);
            broad_phase_->collide(&callback);
        }
        return result;
    }

    double distance(sim::EntityId first, sim::EntityId second) const override {
        return ObjectDistance(Require(objects_, first), Require(objects_, second));
    }

private:
    ObjectMap objects_;
    std::unordered_map<const coal::CollisionObject*, sim::EntityId> object_entities_;
    std::unique_ptr<coal::DynamicAABBTreeCollisionManager> broad_phase_;
};

}  // namespace

struct SimPluginHandle {
    const SimHostApi* host{nullptr};
};

extern "C" {

SIM_PLUGIN_API std::uint32_t SimPluginApiVersion() { return sim::kPluginApiVersion; }

SIM_PLUGIN_API SimPluginHandle* SimCreatePlugin(const SimHostApi* host) {
    if (host == nullptr || host->api_version != sim::kPluginApiVersion) return nullptr;
    if (host->log != nullptr) host->log(0, "sim.collision.coal", "Coal collision plugin loaded");
    return new SimPluginHandle{host};
}

SIM_PLUGIN_API void SimDestroyPlugin(SimPluginHandle* plugin) { delete plugin; }

SIM_PLUGIN_API sim::ICollisionBackend* SimCreateCollisionBackend(SimPluginHandle* plugin) {
    return plugin != nullptr ? new CoalCollisionBackend{} : nullptr;
}

SIM_PLUGIN_API void SimDestroyCollisionBackend(SimPluginHandle*,
                                                sim::ICollisionBackend* backend) {
    delete backend;
}

}
