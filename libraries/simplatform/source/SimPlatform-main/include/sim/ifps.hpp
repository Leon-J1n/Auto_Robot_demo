#pragma once

#include "sim/collision.hpp"

#include <limits>
#include <memory>
#include <unordered_map>

namespace sim {

struct IFPSSphere {
    Vec3 center{};
    double radius{0.0};
    std::uint32_t first_child{0};
    std::uint32_t child_count{0};
};

struct IFPSSphereTree {
    std::vector<IFPSSphere> nodes;
    std::uint32_t root{0};
    std::uint32_t leaf_sphere_count{0};
};

class IFPSSphereTreeCache {
public:
    IFPSSphereTreeCache();
    ~IFPSSphereTreeCache();
    IFPSSphereTreeCache(const IFPSSphereTreeCache&) = delete;
    IFPSSphereTreeCache& operator=(const IFPSSphereTreeCache&) = delete;

    [[nodiscard]] IFPSSphereTree get(const ColliderComponent& collider,
                                     std::uint32_t leaf_sphere_count);
    [[nodiscard]] std::size_t cached_tree_count() const;
    [[nodiscard]] std::uint32_t generated_leaf_count(
        const ColliderComponent& collider) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// Builds the deterministic 8-way IFPS shell used by both collision plugins.
[[nodiscard]] IFPSSphereTree BuildIFPSSphereTree(const ColliderComponent& collider,
                                                  std::uint32_t leaf_sphere_count = 256);

class IFPSCollisionBackend : public ICollisionBackend {
public:
    explicit IFPSCollisionBackend(std::uint32_t leaf_sphere_count = 256);
    ~IFPSCollisionBackend() override;

    [[nodiscard]] BackendCapabilities capabilities() const noexcept override;
    void build(World& world) override;
    void sync_from_scene(World& world) override;
    [[nodiscard]] bool check_collision(EntityId first, EntityId second) const override;
    [[nodiscard]] bool check_scene_collision(EntityId entity) const override;
    [[nodiscard]] std::vector<std::uint8_t> check_scene_collisions(
        std::span<const EntityId> entities) const override;
    [[nodiscard]] double distance(EntityId first, EntityId second) const override;

protected:
    struct WorldSphere { Vec3 center{}; double radius{0.0}; };
    struct Object {
        IFPSSphereTree tree;
        std::vector<WorldSphere> spheres;
        Transform transform{};
    };
    struct BatchPair {
        const Object* first{nullptr};
        const Object* second{nullptr};
        std::size_t result_index{0};
        std::size_t second_result_index{std::numeric_limits<std::size_t>::max()};
    };
    [[nodiscard]] const Object& object(EntityId entity) const;
    [[nodiscard]] const std::unordered_map<EntityId, Object>& objects() const noexcept {
        return objects_;
    }
    [[nodiscard]] const IFPSSphereTreeCache& tree_cache() const noexcept {
        return tree_cache_;
    }
    [[nodiscard]] std::vector<BatchPair> batch_candidates(
        std::span<const EntityId> entities) const;
    [[nodiscard]] WorldSphere world_sphere(const Object& object,
                                           std::uint32_t node_index) const;
    [[nodiscard]] virtual bool hierarchical_collision(const Object& first,
                                                      const Object& second) const;
    [[nodiscard]] virtual bool roots_might_overlap(const WorldSphere& first,
                                                   const WorldSphere& second) const;

private:
    std::uint32_t leaf_sphere_count_;
    IFPSSphereTreeCache tree_cache_;
    std::unordered_map<EntityId, Object> objects_;
};

}  // namespace sim
