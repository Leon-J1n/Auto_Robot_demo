#include "sim/ifps.hpp"

#include "sim/assets.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <iterator>
#include <limits>
#include <map>
#include <mutex>
#include <numbers>
#include <optional>
#include <random>

namespace sim {
namespace {

constexpr std::uint32_t kBranchingFactor = 8;
constexpr std::size_t kSurfaceSampleCount = 1'000'000;
constexpr double kInverseFpsRadiusScale = 1.05;
constexpr double kInverseFpsRadiusPadding = 0.001;

double LengthSquared(const Vec3& value) {
    return value.x * value.x + value.y * value.y + value.z * value.z;
}

double Distance(const Vec3& first, const Vec3& second) {
    return std::sqrt(LengthSquared(first - second));
}

Vec3 Scale(const Vec3& value, const Vec3& scale) {
    return {value.x * scale.x, value.y * scale.y, value.z * scale.z};
}

bool SameTransform(const Transform& left, const Transform& right) {
    return left.translation.x == right.translation.x &&
        left.translation.y == right.translation.y &&
        left.translation.z == right.translation.z &&
        left.rotation.w == right.rotation.w && left.rotation.x == right.rotation.x &&
        left.rotation.y == right.rotation.y && left.rotation.z == right.rotation.z &&
        left.scale.x == right.scale.x && left.scale.y == right.scale.y &&
        left.scale.z == right.scale.z;
}

bool SameShellGeometry(const ColliderComponent& left, const ColliderComponent& right) {
    return left.shape == right.shape && left.size.x == right.size.x &&
        left.size.y == right.size.y && left.size.z == right.size.z &&
        left.mesh_resource == right.mesh_resource;
}

std::filesystem::path PathFromUtf8(std::string_view value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

struct GeometryRevision {
    bool exists{false};
    std::uintmax_t size{0};
    std::filesystem::file_time_type modified{};

    bool operator==(const GeometryRevision&) const = default;
};

GeometryRevision CurrentGeometryRevision(const ColliderComponent& collider) {
    if (collider.shape != ColliderShape::Mesh || collider.mesh_resource.empty()) return {};
    std::error_code error;
    const auto path = PathFromUtf8(collider.mesh_resource);
    const auto size = std::filesystem::file_size(path, error);
    if (error) return {};
    const auto modified = std::filesystem::last_write_time(path, error);
    if (error) return {};
    return {true, size, modified};
}

double Uniform(std::mt19937_64& generator) {
    return std::generate_canonical<double, 53>(generator);
}

std::vector<Vec3> PrimitiveSurfacePoints(const ColliderComponent& collider,
                                         std::mt19937_64& generator) {
    std::vector<Vec3> points;
    points.reserve(kSurfaceSampleCount);
    const Vec3 half = collider.size * 0.5;
    for (std::size_t index = 0; index < kSurfaceSampleCount; ++index) {
        const double u = Uniform(generator);
        const double v = Uniform(generator);
        const double selector = Uniform(generator);
        if (collider.shape == ColliderShape::Box) {
            const double xy = collider.size.x * collider.size.y;
            const double xz = collider.size.x * collider.size.z;
            const double yz = collider.size.y * collider.size.z;
            const double face = selector * 2.0 * (xy + xz + yz);
            const double x = (2.0 * u - 1.0) * half.x;
            const double y = (2.0 * v - 1.0) * half.y;
            if (face < 2.0 * xy)
                points.push_back({x, y, face < xy ? -half.z : half.z});
            else if (face < 2.0 * (xy + xz))
                points.push_back({x, face < 2.0 * xy + xz ? -half.y : half.y,
                                  (2.0 * v - 1.0) * half.z});
            else
                points.push_back({face < 2.0 * (xy + xz) + yz ? -half.x : half.x,
                                  (2.0 * u - 1.0) * half.y,
                                  (2.0 * v - 1.0) * half.z});
        } else if (collider.shape == ColliderShape::Cylinder) {
            const double radius = collider.size.x;
            const double side_area = 2.0 * std::numbers::pi * radius * collider.size.z;
            const double cap_area = std::numbers::pi * radius * radius;
            const double angle = 2.0 * std::numbers::pi * u;
            if (selector * (side_area + 2.0 * cap_area) < side_area) {
                points.push_back({radius * std::cos(angle), radius * std::sin(angle),
                                  (2.0 * v - 1.0) * half.z});
            } else {
                const double disk_radius = radius * std::sqrt(v);
                const bool upper = selector * (side_area + 2.0 * cap_area) >=
                    side_area + cap_area;
                points.push_back({disk_radius * std::cos(angle), disk_radius * std::sin(angle),
                                  upper ? half.z : -half.z});
            }
        } else if (collider.shape == ColliderShape::Capsule) {
            const double radius = collider.size.x;
            const double side_area = 2.0 * std::numbers::pi * radius * collider.size.z;
            const double sphere_area = 4.0 * std::numbers::pi * radius * radius;
            const double angle = 2.0 * std::numbers::pi * u;
            if (selector * (side_area + sphere_area) < side_area) {
                points.push_back({radius * std::cos(angle), radius * std::sin(angle),
                                  (2.0 * v - 1.0) * half.z});
            } else {
                const double z = radius * (2.0 * v - 1.0);
                const double radial = std::sqrt(std::max(0.0, radius * radius - z * z));
                points.push_back({radial * std::cos(angle), radial * std::sin(angle),
                                  z + (z >= 0.0 ? half.z : -half.z)});
            }
        }
    }
    return points;
}

std::vector<Vec3> SurfacePoints(const ColliderComponent& collider) {
    std::mt19937_64 generator(0);
    if (collider.shape != ColliderShape::Mesh || collider.mesh_resource.empty())
        return PrimitiveSurfacePoints(collider, generator);
#if defined(SIM_HAS_ASSIMP)
    try {
        std::string source = collider.mesh_resource;
        std::optional<std::size_t> selected_mesh;
        if (const auto marker = source.rfind("#mesh="); marker != std::string::npos) {
            selected_mesh = static_cast<std::size_t>(std::stoull(source.substr(marker + 6)));
            source.resize(marker);
        }
        const auto path = PathFromUtf8(source);
        auto asset = path.extension() == ".obj" ? ObjImporter{}.import(path).asset
                                                 : AssimpImporter{}.import(path).asset;
        AssetValidator::normalize(asset);
        struct Triangle { Vec3 a; Vec3 b; Vec3 c; double cumulative_area; };
        std::vector<Triangle> triangles;
        double total_area = 0.0;
        for (std::size_t mesh_index = 0; mesh_index < asset.meshes.size(); ++mesh_index) {
            if (selected_mesh && *selected_mesh != mesh_index) continue;
            const auto& mesh = asset.meshes[mesh_index];
            for (std::size_t index = 0; index + 2 < mesh.indices.size(); index += 3) {
                const Vec3& a = mesh.positions.at(mesh.indices[index]);
                const Vec3& b = mesh.positions.at(mesh.indices[index + 1]);
                const Vec3& c = mesh.positions.at(mesh.indices[index + 2]);
                const Vec3 cross{(b.y - a.y) * (c.z - a.z) - (b.z - a.z) * (c.y - a.y),
                                 (b.z - a.z) * (c.x - a.x) - (b.x - a.x) * (c.z - a.z),
                                 (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)};
                total_area += 0.5 * std::sqrt(LengthSquared(cross));
                if (total_area > (triangles.empty() ? 0.0 : triangles.back().cumulative_area))
                    triangles.push_back({a, b, c, total_area});
            }
        }
        if (!triangles.empty()) {
            std::vector<Vec3> points;
            points.reserve(kSurfaceSampleCount);
            for (std::size_t index = 0; index < kSurfaceSampleCount; ++index) {
                const double face = Uniform(generator) * total_area;
                const auto triangle = std::lower_bound(triangles.begin(), triangles.end(), face,
                    [](const Triangle& value, double area) { return value.cumulative_area < area; });
                const double u = std::sqrt(Uniform(generator));
                const double v = Uniform(generator);
                points.push_back(triangle->a * (1.0 - u) +
                                 triangle->b * (u * (1.0 - v)) + triangle->c * (u * v));
            }
            return points;
        }
    } catch (const Error&) {
        // Preserve the collider fallback used by the other backends for missing mesh assets.
    }
#endif
    ColliderComponent fallback = collider;
    fallback.shape = ColliderShape::Box;
    return PrimitiveSurfacePoints(fallback, generator);
}

IFPSSphereTree AssembleSphereTree(const std::vector<Vec3>& ordered,
                                  const std::vector<double>& inverse_radii,
                                  std::uint32_t count) {
    // Match the reference 8-way files and calCuTreeRadius() level construction.
    std::vector<std::uint32_t> level_counts{1};
    while (level_counts.back() < count) {
        const std::uint32_t next = level_counts.back() > count / kBranchingFactor
            ? count : level_counts.back() * kBranchingFactor;
        level_counts.push_back(next);
    }
    std::vector<double> level_radii;
    level_radii.reserve(level_counts.size());
    for (const std::uint32_t level_count : level_counts)
        level_radii.push_back(inverse_radii[level_count]);
    if (level_radii.size() > 2) {
        const double finest_internal = level_radii[level_radii.size() - 2];
        for (std::size_t level = 1; level + 1 < level_radii.size(); ++level) {
            double radius = 0.5 * finest_internal;
            for (std::size_t remaining = level; remaining + 1 < level_radii.size(); ++remaining)
                radius += level_radii[remaining];
            level_radii[level] = radius;
        }
    }

    IFPSSphereTree result;
    result.leaf_sphere_count = count;
    result.root = 0;
    std::vector<std::uint32_t> current_order{0};
    std::uint32_t current_start = 0;
    result.nodes.push_back({ordered.front(), level_radii.front(), 0, 0});
    for (std::size_t level = 1; level < level_counts.size(); ++level) {
        std::vector<std::vector<std::uint32_t>> groups(current_order.size());
        for (std::uint32_t child = 0; child < level_counts[level]; ++child) {
            std::size_t closest = 0;
            double closest_distance = LengthSquared(ordered[child] - ordered[current_order[0]]);
            for (std::size_t parent = 1; parent < current_order.size(); ++parent) {
                const double distance = LengthSquared(
                    ordered[child] - ordered[current_order[parent]]);
                if (distance < closest_distance) {
                    closest = parent;
                    closest_distance = distance;
                }
            }
            groups[closest].push_back(child);
        }
        const std::uint32_t next_start = static_cast<std::uint32_t>(result.nodes.size());
        std::vector<std::uint32_t> next_order;
        next_order.reserve(level_counts[level]);
        for (std::size_t parent = 0; parent < groups.size(); ++parent) {
            auto& parent_node = result.nodes[current_start + parent];
            parent_node.first_child = static_cast<std::uint32_t>(result.nodes.size());
            parent_node.child_count = static_cast<std::uint32_t>(groups[parent].size());
            for (const std::uint32_t child : groups[parent]) {
                next_order.push_back(child);
                result.nodes.push_back({ordered[child], level_radii[level], 0, 0});
            }
        }
        current_order = std::move(next_order);
        current_start = next_start;
    }
    return result;
}

}  // namespace

struct IFPSSphereTreeCache::Impl {
    struct Entry {
        ColliderComponent collider;
        GeometryRevision revision;
        std::vector<Vec3> surface_points;
        std::vector<double> nearest_distances;
        std::vector<Vec3> ordered{{0.0, 0.0, 0.0}};
        std::vector<double> inverse_radii{0.0};
        std::map<std::uint32_t, IFPSSphereTree> trees;
    };

    mutable std::mutex mutex;
    std::vector<std::unique_ptr<Entry>> entries;
};

IFPSSphereTreeCache::IFPSSphereTreeCache() : impl_(std::make_unique<Impl>()) {}
IFPSSphereTreeCache::~IFPSSphereTreeCache() = default;

IFPSSphereTree IFPSSphereTreeCache::get(const ColliderComponent& collider,
                                        std::uint32_t leaf_sphere_count) {
    std::scoped_lock lock(impl_->mutex);
    const std::uint32_t count = collider.shape == ColliderShape::Sphere
        ? 1U : std::max(1U, leaf_sphere_count);
    if (count > kSurfaceSampleCount)
        throw Error(ErrorCode::ValidationFailed,
                    "IFPS sphere count exceeds the one-million-point surface sample");
    const GeometryRevision revision = CurrentGeometryRevision(collider);
    auto found = std::find_if(impl_->entries.begin(), impl_->entries.end(),
        [&](const auto& entry) {
            return SameShellGeometry(entry->collider, collider) && entry->revision == revision;
        });
    if (found == impl_->entries.end()) {
        auto entry = std::make_unique<Impl::Entry>();
        entry->collider = collider;
        entry->revision = revision;
        if (collider.shape != ColliderShape::Sphere) {
            entry->surface_points = SurfacePoints(collider);
            if (entry->surface_points.empty())
                throw Error(ErrorCode::ValidationFailed, "IFPS collider has no surface points");
            entry->nearest_distances.assign(entry->surface_points.size(),
                                             std::numeric_limits<double>::infinity());
        }
        impl_->entries.push_back(std::move(entry));
        found = std::prev(impl_->entries.end());
    }

    auto& entry = **found;
    if (const auto cached = entry.trees.find(count); cached != entry.trees.end())
        return cached->second;
    if (collider.shape == ColliderShape::Sphere) {
        const auto [inserted, unused] = entry.trees.emplace(
            1U, IFPSSphereTree{{{{0.0, 0.0, 0.0}, collider.size.x, 0, 0}}, 0, 1});
        return inserted->second;
    }

    entry.ordered.reserve(static_cast<std::size_t>(count) + 1U);
    entry.inverse_radii.reserve(static_cast<std::size_t>(count) + 1U);
    while (entry.ordered.size() <= count) {
        const Vec3 latest = entry.ordered.back();
        std::size_t farthest = 0;
        for (std::size_t index = 0; index < entry.surface_points.size(); ++index) {
            entry.nearest_distances[index] = std::min(entry.nearest_distances[index],
                LengthSquared(entry.surface_points[index] - latest));
            if (entry.nearest_distances[index] > entry.nearest_distances[farthest])
                farthest = index;
        }
        entry.ordered.push_back(entry.surface_points[farthest]);
        entry.inverse_radii.push_back(
            std::sqrt(entry.nearest_distances[farthest] * kInverseFpsRadiusScale) +
            kInverseFpsRadiusPadding);
    }
    const auto [inserted, unused] = entry.trees.emplace(
        count, AssembleSphereTree(entry.ordered, entry.inverse_radii, count));
    return inserted->second;
}

std::size_t IFPSSphereTreeCache::cached_tree_count() const {
    std::scoped_lock lock(impl_->mutex);
    std::size_t count = 0;
    for (const auto& entry : impl_->entries) count += entry->trees.size();
    return count;
}

std::uint32_t IFPSSphereTreeCache::generated_leaf_count(
    const ColliderComponent& collider) const {
    std::scoped_lock lock(impl_->mutex);
    const GeometryRevision revision = CurrentGeometryRevision(collider);
    const auto found = std::find_if(impl_->entries.begin(), impl_->entries.end(),
        [&](const auto& entry) {
            return SameShellGeometry(entry->collider, collider) && entry->revision == revision;
        });
    if (found == impl_->entries.end()) return 0;
    if (collider.shape == ColliderShape::Sphere) return 1;
    return static_cast<std::uint32_t>((*found)->ordered.size() - 1U);
}

IFPSSphereTree BuildIFPSSphereTree(const ColliderComponent& collider,
                                   std::uint32_t leaf_sphere_count) {
    static IFPSSphereTreeCache cache;
    return cache.get(collider, leaf_sphere_count);
}

IFPSCollisionBackend::IFPSCollisionBackend(std::uint32_t leaf_sphere_count)
    : leaf_sphere_count_(std::max(1U, leaf_sphere_count)) {}
IFPSCollisionBackend::~IFPSCollisionBackend() = default;

BackendCapabilities IFPSCollisionBackend::capabilities() const noexcept {
    return {.collision_query = true, .distance_query = true};
}

void IFPSCollisionBackend::build(World& world) {
    objects_.clear();
    world.update_world_transforms();
    for (const EntityId entity : world.registry().view<ColliderComponent, TransformComponent>()) {
        const auto& collider = world.registry().get<ColliderComponent>(entity);
        const std::uint32_t requested_count = collider.ifps_sphere_count == 0
            ? leaf_sphere_count_ : collider.ifps_sphere_count;
        objects_.emplace(entity, Object{tree_cache_.get(collider, requested_count), {}});
    }
    sync_from_scene(world);
}

void IFPSCollisionBackend::sync_from_scene(World& world) {
    world.update_world_transforms();
    for (auto& [entity, object] : objects_) {
        if (!world.registry().valid(entity)) continue;
        const Transform transform = world.registry().get<TransformComponent>(entity).world;
        if (object.spheres.size() == object.tree.nodes.size() &&
            SameTransform(object.transform, transform))
            continue;
        object.transform = transform;
        const Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                         std::abs(transform.scale.z)};
        const double radius_scale = std::max({scale.x, scale.y, scale.z});
        object.spheres.resize(object.tree.nodes.size());
        for (std::size_t index = 0; index < object.tree.nodes.size(); ++index) {
            const IFPSSphere& sphere = object.tree.nodes[index];
            object.spheres[index] = {
                transform.translation + transform.rotation.rotate(Scale(sphere.center, scale)),
                sphere.radius * radius_scale};
        }
    }
}

const IFPSCollisionBackend::Object& IFPSCollisionBackend::object(EntityId entity) const {
    const auto found = objects_.find(entity);
    if (found == objects_.end()) throw Error(ErrorCode::NotFound, "entity has no IFPS collider");
    return found->second;
}

bool IFPSCollisionBackend::roots_might_overlap(const WorldSphere& first,
                                               const WorldSphere& second) const {
    return Distance(first.center, second.center) <= first.radius + second.radius;
}

IFPSCollisionBackend::WorldSphere IFPSCollisionBackend::world_sphere(
    const Object& object, std::uint32_t node_index) const {
    return object.spheres[node_index];
}

bool IFPSCollisionBackend::hierarchical_collision(const Object& first, const Object& second) const {
    if (!roots_might_overlap(world_sphere(first, first.tree.root),
                             world_sphere(second, second.tree.root)))
        return false;
    std::vector<std::pair<std::uint32_t, std::uint32_t>> pending{{first.tree.root, second.tree.root}};
    while (!pending.empty()) {
        const auto [left, right] = pending.back(); pending.pop_back();
        const WorldSphere left_sphere = world_sphere(first, left);
        const WorldSphere right_sphere = world_sphere(second, right);
        if (!roots_might_overlap(left_sphere, right_sphere)) continue;
        const auto& left_node = first.tree.nodes[left];
        const auto& right_node = second.tree.nodes[right];
        if (left_node.child_count == 0 && right_node.child_count == 0) return true;
        const bool expand_left = left_node.child_count != 0 &&
            (right_node.child_count == 0 || left_sphere.radius >= right_sphere.radius);
        if (expand_left)
            for (std::uint32_t child = 0; child < left_node.child_count; ++child)
                pending.emplace_back(left_node.first_child + child, right);
        else
            for (std::uint32_t child = 0; child < right_node.child_count; ++child)
                pending.emplace_back(left, right_node.first_child + child);
    }
    return false;
}

bool IFPSCollisionBackend::check_collision(EntityId first, EntityId second) const {
    return hierarchical_collision(object(first), object(second));
}

bool IFPSCollisionBackend::check_scene_collision(EntityId entity) const {
    return check_scene_collisions(std::span<const EntityId>(&entity, 1)).front() != 0U;
}

std::vector<IFPSCollisionBackend::BatchPair> IFPSCollisionBackend::batch_candidates(
    std::span<const EntityId> entities) const {
    std::vector<BatchPair> pairs;
    pairs.reserve(entities.size() * objects_.size());
    std::unordered_map<EntityId, std::size_t> requested;
    requested.reserve(entities.size());
    for (std::size_t index = 0; index < entities.size(); ++index)
        requested.emplace(entities[index], index);
    for (std::size_t result_index = 0; result_index < entities.size(); ++result_index) {
        const EntityId entity = entities[result_index];
        const Object& target = object(entity);
        for (const auto& [other, candidate] : objects_) {
            if (other == entity) continue;
            const auto requested_other = requested.find(other);
            if (requested_other != requested.end() && requested_other->second < result_index)
                continue;
            if (roots_might_overlap(world_sphere(target, target.tree.root),
                                    world_sphere(candidate, candidate.tree.root))) {
                const std::size_t second_result = requested_other == requested.end()
                    ? std::numeric_limits<std::size_t>::max() : requested_other->second;
                pairs.push_back({&target, &candidate, result_index, second_result});
            }
        }
    }
    return pairs;
}

std::vector<std::uint8_t> IFPSCollisionBackend::check_scene_collisions(
    std::span<const EntityId> entities) const {
    std::vector<std::uint8_t> result(entities.size(), 0U);
    for (const BatchPair& pair : batch_candidates(entities)) {
        if (result[pair.result_index] != 0U &&
            (pair.second_result_index >= result.size() ||
             result[pair.second_result_index] != 0U))
            continue;
        if (hierarchical_collision(*pair.first, *pair.second)) {
            result[pair.result_index] = 1U;
            if (pair.second_result_index < result.size())
                result[pair.second_result_index] = 1U;
        }
    }
    return result;
}

double IFPSCollisionBackend::distance(EntityId first, EntityId second) const {
    const Object& left = object(first);
    const Object& right = object(second);
    if (hierarchical_collision(left, right)) return 0.0;
    double result = std::numeric_limits<double>::infinity();
    for (std::size_t left_index = 0; left_index < left.tree.nodes.size(); ++left_index) {
        if (left.tree.nodes[left_index].child_count != 0) continue;
        const WorldSphere a = world_sphere(left, static_cast<std::uint32_t>(left_index));
        for (std::size_t right_index = 0; right_index < right.tree.nodes.size(); ++right_index) {
            if (right.tree.nodes[right_index].child_count != 0) continue;
            const WorldSphere b = world_sphere(right, static_cast<std::uint32_t>(right_index));
            result = std::min(result,
                std::max(0.0, Distance(a.center, b.center) - a.radius - b.radius));
        }
    }
    return result;
}

}  // namespace sim
