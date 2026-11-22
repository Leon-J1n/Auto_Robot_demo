#include "sim/c_api.h"
#include "sim/collision.hpp"
#include "sim/physics.hpp"
#include "sim/plugin.hpp"
#include "sim/render.hpp"
#include "sim/state_io.hpp"
#include "sim/logging.hpp"

#include <algorithm>
#include <filesystem>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <string_view>

namespace {

thread_local std::string g_last_error;

}  // namespace

namespace {

sim::BackendKind CollisionKind(SimCollisionBackend backend) {
    if (backend == SIM_COLLISION_IFPS_CPU) return sim::BackendKind::IFPSCPU;
    if (backend == SIM_COLLISION_IFPS_GPU) return sim::BackendKind::IFPSGPU;
    if (backend == SIM_COLLISION_BULLET3) return sim::BackendKind::Bullet3Collision;
    if (backend == SIM_COLLISION_COAL) return sim::BackendKind::Coal;
    return sim::BackendKind::FCL;
}

sim::JointLimitPolicy LimitPolicy(SimJointLimitPolicy policy) {
    if (policy == SIM_JOINT_LIMIT_CLAMP) return sim::JointLimitPolicy::Clamp;
    if (policy == SIM_JOINT_LIMIT_IGNORE) return sim::JointLimitPolicy::Ignore;
    if (policy != SIM_JOINT_LIMIT_ENFORCE)
        throw sim::Error(sim::ErrorCode::InvalidArgument, "unknown joint limit policy");
    return sim::JointLimitPolicy::Enforce;
}

std::filesystem::path Utf8Path(const char* value) {
    if (value == nullptr) return {};
    const std::string text(value);
    const auto* begin = reinterpret_cast<const char8_t*>(text.data());
    return std::filesystem::path(std::u8string(begin, begin + text.size()));
}

struct SavedState {
    sim::WorldSnapshot world;
    std::vector<std::byte> physics;
};

class CompositeSensorProvider final : public sim::ISensorDataProvider {
public:
    void set_render(std::shared_ptr<sim::ISensorDataProvider> provider) {
        render_ = std::move(provider);
    }
    void set_collision(std::shared_ptr<sim::ISensorDataProvider> provider) {
        collision_ = std::move(provider);
    }
    void render_camera(const sim::Transform& pose, const sim::CameraComponent& camera,
                       std::vector<std::uint8_t>& rgb, std::vector<float>& depth) override {
        if (!render_)
            throw sim::Error(sim::ErrorCode::BackendUnavailable,
                             "no render provider is available for this world");
        render_->render_camera(pose, camera, rgb, depth);
    }
    std::vector<float> raycast(const sim::Vec3& origin,
                               std::span<const sim::Vec3> directions,
                               double min_distance, double max_distance) override {
        if (!collision_)
            throw sim::Error(sim::ErrorCode::BackendUnavailable,
                             "no collision provider is available for this world");
        return collision_->raycast(origin, directions, min_distance, max_distance);
    }

private:
    std::shared_ptr<sim::ISensorDataProvider> render_;
    std::shared_ptr<sim::ISensorDataProvider> collision_;
};

struct PhysicsInstance {
    std::shared_ptr<sim::IPhysicsBackend> dynamics;
#if defined(SIM_HAS_MUJOCO)
    std::shared_ptr<sim::MuJoCoBackend> mujoco_sensor;
#endif
#if defined(SIM_HAS_FCL)
    std::shared_ptr<sim::FCLBackend> fcl;
#endif
    std::shared_ptr<sim::ICollisionBackend> collision;
#if defined(SIM_HAS_VULKAN)
    std::shared_ptr<sim::VulkanBackend> vulkan;
#endif
    std::shared_ptr<CompositeSensorProvider> sensors;
    bool dirty{true};
};

}  // namespace

struct SimHandle {
    SimHandle(const SimConfig& config, const std::filesystem::path& plugin_directory)
        : simulator(config.physics_backend == SIM_PHYSICS_MUJOCO
                        ? sim::BackendConfig{sim::BackendKind::MuJoCo,
                                             CollisionKind(config.collision_backend),
                                             sim::BackendKind::None}
                        : config.physics_backend == SIM_PHYSICS_BULLET3
                            ? sim::BackendConfig{sim::BackendKind::Bullet3,
                                                 CollisionKind(config.collision_backend),
                                                 sim::BackendKind::None}
                            : sim::BackendConfig{sim::BackendKind::None,
                                                 CollisionKind(config.collision_backend),
                                                 sim::BackendKind::None}),
          resources(config.cache_directory_utf8 != nullptr
                        ? Utf8Path(config.cache_directory_utf8)
                        : std::filesystem::temp_directory_path() / "sim-platform-cache"),
          backend(config.physics_backend), collision_backend(config.collision_backend) {
        if (backend == SIM_PHYSICS_BULLET3 || collision_backend != SIM_COLLISION_FCL) {
            if (plugin_directory.empty())
                throw sim::Error(sim::ErrorCode::BackendUnavailable,
                                 "plugin directory is not configured");
            plugins.discover(plugin_directory);
        }
    }

    sim::Simulator simulator;
    sim::ResourceManager resources;
    SimPhysicsBackend backend{SIM_PHYSICS_NONE};
    SimCollisionBackend collision_backend{SIM_COLLISION_FCL};
    sim::PluginManager plugins;
    std::unordered_map<sim::WorldId, PhysicsInstance> physics;
    std::unordered_map<sim::WorldId, std::unique_ptr<sim::SensorSystem>> sensors;
    std::unordered_map<sim::WorldId, std::deque<sim::Event>> events;
    std::unordered_map<sim::WorldId, std::uint64_t> scene_revisions;
    std::unordered_map<sim::WorldId, std::uint64_t> scene_generations;
    std::unordered_map<sim::StateId, SavedState> states;
    sim::StateId next_state{1};
    std::mutex mutex;
};

namespace {

template <typename Function>
int Guard(Function&& function) {
    try {
        g_last_error.clear();
        function();
        return 0;
    } catch (const std::exception& error) {
        g_last_error = error.what();
        SIM_LOG_ERROR("CAPI", g_last_error);
        return -1;
    } catch (...) {
        g_last_error = "unknown simulation error";
        SIM_LOG_FATAL("CAPI", g_last_error);
        return -1;
    }
}

SimHandle& Require(SimHandle* handle) {
    if (handle == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "simulator handle is null");
    return *handle;
}

void EnsurePhysics(SimHandle& handle, sim::WorldId world_id) {
    auto& instance = handle.physics.at(world_id);
    if (!instance.dirty) return;
    SIM_LOG_VERBOSE("EnsurePhysics rebuilds world=" + std::to_string(world_id));
    SIM_PROFILE_SCOPE("CAPI.EnsurePhysics");
    if (instance.dynamics) instance.dynamics->build(handle.simulator.world(world_id));
    if (instance.collision) instance.collision->build(handle.simulator.world(world_id));
    instance.dirty = false;
}

std::filesystem::path PluginDirectory(const char* configured) {
    if (configured != nullptr && *configured != '\0') return Utf8Path(configured);
    if (const char* environment = std::getenv("SIM_PLUGIN_DIR");
        environment != nullptr && *environment != '\0')
        return Utf8Path(environment);
#if defined(SIM_DEFAULT_PLUGIN_DIR)
    return Utf8Path(SIM_DEFAULT_PLUGIN_DIR);
#else
    return {};
#endif
}

void ValidateConfiguration(const SimConfig& config) {
#if !defined(SIM_HAS_MUJOCO)
    if (config.physics_backend == SIM_PHYSICS_MUJOCO)
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "this build does not include MuJoCo");
#endif
#if !defined(SIM_HAS_BULLET_PLUGIN)
    if (config.physics_backend == SIM_PHYSICS_BULLET3)
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "this build does not include the Bullet3 plugin");
#endif
#if !defined(SIM_HAS_BULLET_COLLISION_PLUGIN)
    if (config.collision_backend == SIM_COLLISION_BULLET3)
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "this build does not include the Bullet3 collision plugin");
#endif
#if !defined(SIM_HAS_COAL_COLLISION_PLUGIN)
    if (config.collision_backend == SIM_COLLISION_COAL)
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "this build does not include the Coal collision plugin");
#endif
    if (config.physics_backend != SIM_PHYSICS_NONE &&
        config.physics_backend != SIM_PHYSICS_MUJOCO &&
        config.physics_backend != SIM_PHYSICS_BULLET3)
        throw sim::Error(sim::ErrorCode::InvalidArgument, "unknown physics backend");
    if (config.collision_backend != SIM_COLLISION_FCL &&
        config.collision_backend != SIM_COLLISION_IFPS_CPU &&
        config.collision_backend != SIM_COLLISION_IFPS_GPU &&
        config.collision_backend != SIM_COLLISION_BULLET3 &&
        config.collision_backend != SIM_COLLISION_COAL)
        throw sim::Error(sim::ErrorCode::InvalidArgument, "unknown collision backend");
}

void CopyUtf8(std::string_view source, char* destination, std::size_t capacity) {
    if (destination == nullptr || capacity == 0) return;
    const auto count = std::min(source.size(), capacity - 1);
    std::memcpy(destination, source.data(), count);
    destination[count] = '\0';
}

void CopyTransform(const sim::Transform& source, SimTransform& destination) {
    destination.translation[0] = source.translation.x;
    destination.translation[1] = source.translation.y;
    destination.translation[2] = source.translation.z;
    destination.rotation_wxyz[0] = source.rotation.w;
    destination.rotation_wxyz[1] = source.rotation.x;
    destination.rotation_wxyz[2] = source.rotation.y;
    destination.rotation_wxyz[3] = source.rotation.z;
    destination.scale[0] = source.scale.x;
    destination.scale[1] = source.scale.y;
    destination.scale[2] = source.scale.z;
}

void AppendJsonString(std::ostringstream& output, std::string_view value) {
    output << '"';
    for (const unsigned char character : value) {
        switch (character) {
            case '"': output << "\\\""; break;
            case '\\': output << "\\\\"; break;
            case '\b': output << "\\b"; break;
            case '\f': output << "\\f"; break;
            case '\n': output << "\\n"; break;
            case '\r': output << "\\r"; break;
            case '\t': output << "\\t"; break;
            default:
                if (character < 0x20) {
                    output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                           << static_cast<int>(character) << std::dec << std::setfill(' ');
                } else {
                    output << static_cast<char>(character);
                }
        }
    }
    output << '"';
}

void AppendVec3(std::ostringstream& output, const sim::Vec3& value) {
    output << '[' << value.x << ',' << value.y << ',' << value.z << ']';
}

void AppendTransform(std::ostringstream& output, const sim::Transform& value) {
    output << "{\"translation\":";
    AppendVec3(output, value.translation);
    output << ",\"rotationWxyz\":[" << value.rotation.w << ','
           << value.rotation.x << ',' << value.rotation.y << ','
           << value.rotation.z << "],\"scale\":";
    AppendVec3(output, value.scale);
    output << '}';
}

const char* ShapeName(sim::ColliderShape shape) {
    switch (shape) {
        case sim::ColliderShape::Box: return "box";
        case sim::ColliderShape::Sphere: return "sphere";
        case sim::ColliderShape::Capsule: return "capsule";
        case sim::ColliderShape::Cylinder: return "cylinder";
        case sim::ColliderShape::Mesh: return "mesh";
    }
    return "mesh";
}

std::string BuildSceneSnapshotJson(SimHandle& handle, sim::WorldId world_id,
                                   bool transforms_only = false) {
    SIM_LOG_VERBOSE("BuildSceneSnapshotJson");
    SIM_PROFILE_SCOPE("CAPI.BuildSceneSnapshotJson");
    auto& world = handle.simulator.world(world_id);
    world.update_world_transforms();
    const auto& registry = world.registry();
    auto entities = registry.entities();
    std::sort(entities.begin(), entities.end());
    std::ostringstream output;
    output << std::setprecision(17)
           << "{\"schemaVersion\":\"1.0\",\"sceneId\":\"main\",\"worldId\":"
           << world_id << ",\"sceneGeneration\":" << handle.scene_generations[world_id]
           << ",\"revision\":" << handle.scene_revisions[world_id]
           << ",\"simulationTime\":" << world.time().state().simulation_time
           << ",\"status\":";
    const char* status = world.status() == sim::SimulationStatus::Running ? "running" :
                         world.status() == sim::SimulationStatus::Paused ? "paused" : "stopped";
    AppendJsonString(output, status);
    output << ",\"entities\":[";
    bool first = true;
    for (const auto entity : entities) {
        if (!first) output << ',';
        first = false;
        const auto& hierarchy = registry.get<sim::HierarchyComponent>(entity);
        const auto& transform = registry.get<sim::TransformComponent>(entity);
        output << "{\"id\":" << entity << ",\"objectId\":";
        AppendJsonString(output, world.object_id(entity));
        if (!transforms_only) {
            output << ",\"name\":";
            AppendJsonString(output, registry.get<sim::NameComponent>(entity).name);
        }
        output << ",\"parent\":";
        if (hierarchy.parent == sim::kInvalidEntity) output << "null";
        else output << hierarchy.parent;
        output << ",\"local\":";
        AppendTransform(output, transform.local);
        output << ",\"world\":";
        AppendTransform(output, transform.world);
        if (transforms_only) {
            output << '}';
            continue;
        }
        if (registry.has<sim::VisualComponent>(entity)) {
            const auto& visual = registry.get<sim::VisualComponent>(entity);
            output << ",\"visual\":{\"visible\":" << (visual.visible ? "true" : "false")
                   << ",\"minLod\":" << static_cast<unsigned>(visual.min_lod)
                   << ",\"maxLod\":" << static_cast<unsigned>(visual.max_lod)
                   << ",\"shape\":";
            AppendJsonString(output, ShapeName(visual.shape));
            output << ",\"size\":";
            AppendVec3(output, visual.size);
            output << ",\"color\":[" << visual.color[0] << ',' << visual.color[1] << ','
                   << visual.color[2] << ',' << visual.color[3] << "],\"resource\":";
            AppendJsonString(output, visual.mesh_resource);
            output << '}';
        }
        if (registry.has<sim::ColliderComponent>(entity)) {
            const auto& collider = registry.get<sim::ColliderComponent>(entity);
            output << ",\"collider\":{\"shape\":";
            AppendJsonString(output, ShapeName(collider.shape));
            output << ",\"size\":";
            AppendVec3(output, collider.size);
            output << ",\"resource\":";
            AppendJsonString(output, collider.mesh_resource);
            output << ",\"trigger\":" << (collider.is_trigger ? "true" : "false")
                   << ",\"friction\":";
            AppendVec3(output, collider.friction);
            output << ",\"restitution\":" << collider.restitution
                   << ",\"ifpsSphereCount\":" << collider.ifps_sphere_count << '}';
        }
        if (registry.has<sim::RigidBodyComponent>(entity)) {
            const auto& body = registry.get<sim::RigidBodyComponent>(entity);
            output << ",\"rigidBody\":{\"enabled\":true,\"dynamic\":"
                   << (body.dynamic ? "true" : "false") << ",\"gravity\":"
                   << (body.gravity_enabled ? "true" : "false")
                   << ",\"mass\":" << body.mass << '}';
        }
        if (registry.has<sim::SensorComponent>(entity)) {
            const auto& sensor = registry.get<sim::SensorComponent>(entity);
            output << ",\"sensor\":{\"frameId\":";
            AppendJsonString(output, sensor.frame_id);
            output << ",\"updateRateHz\":" << sensor.update_rate_hz << '}';
        }
        if (registry.has<sim::CameraComponent>(entity)) {
            const auto& camera = registry.get<sim::CameraComponent>(entity);
            output << ",\"camera\":{\"output\":\""
                   << (camera.output == sim::CameraOutput::RGBD ? "rgbd" : "rgb")
                   << "\",\"width\":" << camera.width << ",\"height\":" << camera.height
                   << ",\"verticalFovRadians\":" << camera.vertical_fov_radians
                   << ",\"nearPlane\":" << camera.near_plane
                   << ",\"farPlane\":" << camera.far_plane << '}';
        }
        if (registry.has<sim::LightComponent>(entity)) {
            const auto& light = registry.get<sim::LightComponent>(entity);
            output << ",\"light\":{\"type\":\""
                   << (light.type == sim::LightType::Point ? "point" : light.type == sim::LightType::Spot ? "spot" :
                       light.type == sim::LightType::Directional ? "directional" : "ambient")
                   << "\",\"color\":[" << light.color[0] << ',' << light.color[1] << ',' << light.color[2]
                   << "],\"intensity\":" << light.intensity << ",\"range\":" << light.range
                   << ",\"decay\":" << light.decay << ",\"innerConeRadians\":" << light.inner_cone_radians
                   << ",\"outerConeRadians\":" << light.outer_cone_radians
                   << ",\"angularDiameterRadians\":" << light.angular_diameter_radians
                   << ",\"enabled\":" << (light.enabled ? "true" : "false") << '}';
        }
        if (registry.has<sim::RobotComponent>(entity)) {
            const auto state = sim::Robot(world, entity).joint_state();
            output << ",\"robot\":{\"jointNames\":[";
            for (std::size_t index = 0; index < state.names.size(); ++index) {
                if (index != 0) output << ',';
                AppendJsonString(output, state.names[index]);
            }
            const auto append_values = [&output](std::string_view name,
                                                  const std::vector<double>& values) {
                output << "],\"" << name << "\":[";
                for (std::size_t index = 0; index < values.size(); ++index) {
                    if (index != 0) output << ',';
                    output << values[index];
                }
            };
            append_values("positions", state.positions);
            append_values("velocities", state.velocities);
            append_values("efforts", state.efforts);
            output << "]}";
        }
        output << '}';
    }
    output << "]}";
    return output.str();
}

}  // namespace

extern "C" {

uint32_t sim_api_version(void) { return 9; }
const char* sim_last_error(void) { return g_last_error.c_str(); }

int sim_create(const SimConfig* config, SimHandle** output) {
    return sim_create_with_plugins(config, nullptr, output);
}

int sim_create_with_plugins(const SimConfig* config, const char* plugin_directory_utf8,
                            SimHandle** output) {
    return Guard([&] {
        if (config == nullptr || output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "config or output is null");
        ValidateConfiguration(*config);
        *output = new SimHandle(*config, PluginDirectory(plugin_directory_utf8));
    });
}

void sim_destroy(SimHandle* simulator) { delete simulator; }

int sim_create_world(SimHandle* simulator, double fixed_time_step, uint64_t seed,
                     SimWorldHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        auto& world = handle.simulator.create_world(fixed_time_step, seed);
        PhysicsInstance instance;
#if defined(SIM_HAS_MUJOCO)
        if (handle.backend == SIM_PHYSICS_MUJOCO) {
            instance.mujoco_sensor = std::make_shared<sim::MuJoCoBackend>();
            instance.dynamics = instance.mujoco_sensor;
        }
#endif
#if defined(SIM_HAS_BULLET_PLUGIN)
        if (handle.backend == SIM_PHYSICS_BULLET3)
            instance.dynamics = handle.plugins.create_physics_backend("sim.physics.bullet3");
#endif
        if (instance.dynamics) {
            auto backend = instance.dynamics;
            world.scheduler().add_system(sim::SimulationPhase::PhysicsStep, "backend.physics",
                [&world, backend](double) { backend->step(world); });
        }
        if (handle.collision_backend == SIM_COLLISION_FCL) {
#if defined(SIM_HAS_FCL)
            instance.fcl = std::make_shared<sim::FCLBackend>();
            instance.collision = instance.fcl;
#else
            throw sim::Error(sim::ErrorCode::BackendUnavailable, "this build does not include FCL");
#endif
        } else {
            const char* name = handle.collision_backend == SIM_COLLISION_IFPS_CPU
                ? "sim.collision.ifps-cpu"
                : handle.collision_backend == SIM_COLLISION_IFPS_GPU
                    ? "sim.collision.ifps-gpu"
                    : handle.collision_backend == SIM_COLLISION_COAL
                        ? "sim.collision.coal" : "sim.collision.bullet3";
            instance.collision = handle.plugins.create_collision_backend(name);
        }
        auto collision = instance.collision;
        world.scheduler().add_system(sim::SimulationPhase::CollisionSync, "backend.collision.sync",
            [&world, collision](double) { collision->sync_from_scene(world); });
        instance.sensors = std::make_shared<CompositeSensorProvider>();
#if defined(SIM_HAS_FCL)
        if (instance.fcl) instance.sensors->set_collision(instance.fcl);
#elif defined(SIM_HAS_MUJOCO)
        if (instance.mujoco_sensor) instance.sensors->set_collision(instance.mujoco_sensor);
#endif
        if (handle.backend != SIM_PHYSICS_MUJOCO) sim::InstallRobotControlSystem(world);
        handle.physics.emplace(world.id(), std::move(instance));
        auto provider = handle.physics.at(world.id()).sensors;
        handle.sensors.emplace(world.id(), std::make_unique<sim::SensorSystem>(world, provider));
        handle.events.try_emplace(world.id());
        handle.scene_revisions.try_emplace(world.id(), 0);
        handle.scene_generations.try_emplace(world.id(), 0);
        auto* event_queue = &handle.events.at(world.id());
        const auto revision_world = world.id();
        auto* revision_map = &handle.scene_revisions;
        (void)world.events().subscribe("", [event_queue, revision_map, revision_world](
                                                const sim::Event& event) {
            event_queue->push_back(event);
            while (event_queue->size() > 256) event_queue->pop_front();
            ++revision_map->at(revision_world);
        });
        *output = world.id();
    });
}

int sim_editor_entity_count(SimHandle* simulator, SimWorldHandle world_id, size_t* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        *output = handle.simulator.world(world_id).registry().entities().size();
    });
}

int sim_editor_entity_info(SimHandle* simulator, SimWorldHandle world_id, size_t index,
                           SimEditorEntityInfo* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        auto& world = handle.simulator.world(world_id);
        world.update_world_transforms();
        const auto entities = world.registry().entities();
        if (index >= entities.size())
            throw sim::Error(sim::ErrorCode::InvalidArgument, "entity index is out of range");
        const auto entity = entities[index];
        const auto& registry = world.registry();
        *output = {};
        output->entity = entity;
        output->parent = registry.get<sim::HierarchyComponent>(entity).parent;
        CopyUtf8(registry.get<sim::NameComponent>(entity).name,
                 output->name_utf8, sizeof(output->name_utf8));
        const auto& transform = registry.get<sim::TransformComponent>(entity);
        CopyTransform(transform.local, output->local_transform);
        CopyTransform(transform.world, output->world_transform);
        if (registry.has<sim::VisualComponent>(entity)) {
            output->component_flags |= SIM_EDITOR_COMPONENT_VISUAL;
            const auto& visual = registry.get<sim::VisualComponent>(entity);
            output->visual_visible = visual.visible ? 1 : 0;
            output->min_lod = visual.min_lod;
            output->max_lod = visual.max_lod;
            output->visual_shape = static_cast<int>(visual.shape);
            output->visual_size[0] = visual.size.x;
            output->visual_size[1] = visual.size.y;
            output->visual_size[2] = visual.size.z;
            std::copy(visual.color.begin(), visual.color.end(), output->visual_color);
            CopyUtf8(visual.mesh_resource, output->visual_resource_utf8,
                     sizeof(output->visual_resource_utf8));
        }
        if (registry.has<sim::ColliderComponent>(entity)) {
            output->component_flags |= SIM_EDITOR_COMPONENT_COLLIDER;
            const auto& collider = registry.get<sim::ColliderComponent>(entity);
            output->collider_shape = static_cast<int>(collider.shape);
            output->collider_size[0] = collider.size.x;
            output->collider_size[1] = collider.size.y;
            output->collider_size[2] = collider.size.z;
            CopyUtf8(collider.mesh_resource, output->collider_resource_utf8,
                     sizeof(output->collider_resource_utf8));
            output->collider_trigger = collider.is_trigger ? 1 : 0;
            output->friction[0] = collider.friction.x;
            output->friction[1] = collider.friction.y;
            output->friction[2] = collider.friction.z;
            output->restitution = collider.restitution;
            output->ifps_sphere_count = collider.ifps_sphere_count;
        }
        if (registry.has<sim::RigidBodyComponent>(entity)) {
            output->component_flags |= SIM_EDITOR_COMPONENT_RIGID_BODY;
            const auto& body = registry.get<sim::RigidBodyComponent>(entity);
            output->body_dynamic = body.dynamic ? 1 : 0;
            output->gravity_enabled = body.gravity_enabled ? 1 : 0;
            output->mass = body.mass;
        }
        if (registry.has<sim::RobotComponent>(entity))
            output->component_flags |= SIM_EDITOR_COMPONENT_ROBOT;
        if (registry.has<sim::JointComponent>(entity))
            output->component_flags |= SIM_EDITOR_COMPONENT_JOINT;
        if (registry.has<sim::SensorComponent>(entity)) {
            output->component_flags |= SIM_EDITOR_COMPONENT_SENSOR;
            const auto& sensor = registry.get<sim::SensorComponent>(entity);
            CopyUtf8(sensor.frame_id, output->sensor_frame_utf8,
                     sizeof(output->sensor_frame_utf8));
            output->sensor_rate_hz = sensor.update_rate_hz;
        }
        if (registry.has<sim::CameraComponent>(entity)) {
            output->component_flags |= SIM_EDITOR_COMPONENT_CAMERA;
            const auto& camera = registry.get<sim::CameraComponent>(entity);
            output->camera_width = camera.width;
            output->camera_height = camera.height;
            output->camera_vertical_fov_radians = camera.vertical_fov_radians;
            output->camera_near_plane = camera.near_plane;
            output->camera_far_plane = camera.far_plane;
            output->camera_rgbd_output = camera.output == sim::CameraOutput::RGBD ? 1 : 0;
        }
        if (registry.has<sim::LightComponent>(entity)) {
            output->component_flags |= SIM_EDITOR_COMPONENT_LIGHT;
            const auto& light = registry.get<sim::LightComponent>(entity);
            output->light.type = static_cast<SimLightType>(light.type);
            std::copy(light.color.begin(), light.color.end(), output->light.color_rgb);
            output->light.intensity = light.intensity; output->light.range = light.range;
            output->light.decay = light.decay; output->light.inner_cone_radians = light.inner_cone_radians;
            output->light.outer_cone_radians = light.outer_cone_radians;
            output->light.angular_diameter_radians = light.angular_diameter_radians;
            output->light.enabled = light.enabled ? 1 : 0;
        }
    });
}

int sim_scene_snapshot_json_size(SimHandle* simulator, SimWorldHandle world_id,
                                 size_t* output_size) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output_size == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "output size is null");
        *output_size = BuildSceneSnapshotJson(handle, world_id).size() + 1;
    });
}

int sim_scene_snapshot_json(SimHandle* simulator, SimWorldHandle world_id,
                            char* output_utf8, size_t output_capacity, size_t* output_size) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output_size == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "output size is null");
        const auto snapshot = BuildSceneSnapshotJson(handle, world_id);
        *output_size = snapshot.size() + 1;
        if (output_utf8 == nullptr || output_capacity < *output_size)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "scene snapshot buffer is too small");
        std::memcpy(output_utf8, snapshot.c_str(), *output_size);
    });
}

int sim_scene_transforms_json_size(SimHandle* simulator, SimWorldHandle world_id,
                                   size_t* output_size) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output_size == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "output size is null");
        *output_size = BuildSceneSnapshotJson(handle, world_id, true).size() + 1;
    });
}

int sim_scene_transforms_json(SimHandle* simulator, SimWorldHandle world_id,
                              char* output_utf8, size_t output_capacity, size_t* output_size) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output_size == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "output size is null");
        const auto snapshot = BuildSceneSnapshotJson(handle, world_id, true);
        *output_size = snapshot.size() + 1;
        if (output_utf8 == nullptr || output_capacity < *output_size)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "scene transforms buffer is too small");
        std::memcpy(output_utf8, snapshot.c_str(), *output_size);
    });
}

int sim_scene_metadata(SimHandle* simulator, SimWorldHandle world_id,
                       uint64_t* generation, uint64_t* revision) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (generation == nullptr || revision == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "scene metadata output is null");
        (void)handle.simulator.world(world_id);
        *generation = handle.scene_generations.at(world_id);
        *revision = handle.scene_revisions.at(world_id);
    });
}

int sim_get_object_id(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle entity,
                      char* object_id_utf8, size_t capacity, size_t* required) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (required == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "required is null");
        const auto& value = handle.simulator.world(world_id).object_id(entity);
        *required = value.size() + 1;
        if (object_id_utf8 == nullptr && capacity == 0) return;
        if (object_id_utf8 == nullptr || capacity < *required)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "object id buffer is too small");
        std::memcpy(object_id_utf8, value.c_str(), *required);
    });
}

int sim_find_entity_by_object_id(SimHandle* simulator, SimWorldHandle world_id,
                                 const char* object_id_utf8, SimEntityHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (object_id_utf8 == nullptr || output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "object id or output is null");
        const auto entity =
            handle.simulator.world(world_id).find_entity_by_object_id(object_id_utf8);
        if (entity == sim::kInvalidEntity)
            throw sim::Error(sim::ErrorCode::NotFound, "object id was not found");
        *output = entity;
    });
}

int sim_load_asset(SimHandle* simulator, SimWorldHandle world_id, const char* path_utf8,
                   SimEntityHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (path_utf8 == nullptr || output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "asset path or output is null");
        auto result = handle.resources.load(Utf8Path(path_utf8));
        *output = handle.resources.instantiate(handle.simulator.world(world_id), result.asset);
        handle.physics.at(world_id).dirty = true;
        handle.simulator.world(world_id).capture_initial_state();
    });
}

int sim_create_entity(SimHandle* simulator, SimWorldHandle world_id, const char* name_utf8,
                      SimEntityHandle parent, SimEntityHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (name_utf8 == nullptr || output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "entity name or output is null");
        *output = handle.simulator.world(world_id).create_entity(name_utf8, parent);
        handle.physics.at(world_id).dirty = true;
    });
}

int sim_destroy_entity(SimHandle* simulator, SimWorldHandle world_id,
                       SimEntityHandle entity) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        auto& world = handle.simulator.world(world_id);
        if (!world.destroy_entity(entity))
            throw sim::Error(sim::ErrorCode::InvalidArgument, "entity does not exist");
        handle.physics.at(world_id).dirty = true;
        world.capture_initial_state();
    });
}

int sim_set_parent(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle entity,
                   SimEntityHandle parent, int preserve_world_transform) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        auto& world = handle.simulator.world(world_id);
        if (preserve_world_transform != 0)
            world.set_parent_preserving_world(entity, parent);
        else
            world.set_parent(entity, parent);
        handle.physics.at(world_id).dirty = true;
        world.capture_initial_state();
    });
}

int sim_set_entity_name(SimHandle* simulator, SimWorldHandle world_id,
                        SimEntityHandle entity, const char* name_utf8) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (name_utf8 == nullptr || *name_utf8 == '\0')
            throw sim::Error(sim::ErrorCode::InvalidArgument, "entity name is empty");
        handle.simulator.world(world_id).registry().get<sim::NameComponent>(entity).name = name_utf8;
    });
}

int sim_create_primitive(SimHandle* simulator, SimWorldHandle world_id, const char* name_utf8,
                         SimPrimitiveShape shape, const double size_xyz[3],
                         SimEntityHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (name_utf8 == nullptr || size_xyz == nullptr || output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "primitive name, size, or output is null");
        if (size_xyz[0] <= 0.0 || size_xyz[1] <= 0.0 || size_xyz[2] <= 0.0)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "primitive dimensions must be positive");
        sim::ColliderShape native_shape;
        std::string mesh;
        switch (shape) {
            case SIM_PRIMITIVE_BOX: native_shape = sim::ColliderShape::Box; break;
            case SIM_PRIMITIVE_SPHERE:
                native_shape = sim::ColliderShape::Sphere; mesh = "builtin://sphere"; break;
            case SIM_PRIMITIVE_CAPSULE:
                native_shape = sim::ColliderShape::Capsule; mesh = "builtin://capsule"; break;
            case SIM_PRIMITIVE_CYLINDER:
                native_shape = sim::ColliderShape::Cylinder; mesh = "builtin://cylinder"; break;
            default:
                throw sim::Error(sim::ErrorCode::InvalidArgument, "unknown primitive shape");
        }
        sim::Vec3 canonical_size;
        sim::Vec3 scale;
        switch (native_shape) {
            case sim::ColliderShape::Box:
                canonical_size = {1.0, 1.0, 1.0};
                scale = {size_xyz[0], size_xyz[1], size_xyz[2]};
                break;
            case sim::ColliderShape::Sphere:
                canonical_size = {0.5, 0.5, 0.5};
                scale = {size_xyz[0] / 0.5, size_xyz[1] / 0.5, size_xyz[2] / 0.5};
                break;
            case sim::ColliderShape::Capsule:
                canonical_size = {0.25, 0.25, 0.5};
                scale = {size_xyz[0] / 0.25, size_xyz[1] / 0.25, size_xyz[2] / 0.5};
                break;
            case sim::ColliderShape::Cylinder:
                canonical_size = {0.5, 0.5, 1.0};
                scale = {size_xyz[0] / 0.5, size_xyz[1] / 0.5, size_xyz[2]};
                break;
            case sim::ColliderShape::Mesh:
                throw sim::Error(sim::ErrorCode::Unsupported,
                                 "mesh is not a C API primitive shape");
        }
        auto& world = handle.simulator.world(world_id);
        const auto entity = world.create_entity(name_utf8);
        world.registry().get<sim::TransformComponent>(entity).local.scale = scale;
        world.registry().emplace<sim::VisualComponent>(entity, sim::VisualComponent{
            mesh, {}, true, native_shape, {1.0, 1.0, 1.0},
            {0.62F, 0.68F, 0.76F, 1.0F}});
        world.registry().emplace<sim::ColliderComponent>(entity, sim::ColliderComponent{
            native_shape, canonical_size, {}, false});
        world.update_world_transforms();
        handle.physics.at(world_id).dirty = true;
        *output = entity;
    });
}

int sim_set_visual_properties(SimHandle* simulator, SimWorldHandle world_id,
                              SimEntityHandle entity, int visible,
                              uint8_t min_lod, uint8_t max_lod) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (min_lod > 7U || max_lod > 7U || min_lod > max_lod)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "LOD range is invalid");
        auto& visual = handle.simulator.world(world_id).registry()
                           .get<sim::VisualComponent>(entity);
        visual.visible = visible != 0;
        visual.min_lod = min_lod;
        visual.max_lod = max_lod;
    });
}

int sim_create_light(SimHandle* simulator, SimWorldHandle world_id, const char* name_utf8,
                     SimLightType type, SimEntityHandle parent,
                     const SimLightProperties* properties, SimEntityHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (!name_utf8 || !output || type < SIM_LIGHT_POINT || type > SIM_LIGHT_AMBIENT)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "light arguments are invalid");
        auto& world = handle.simulator.world(world_id);
        const auto entity = world.create_entity(name_utf8, parent);
        sim::LightComponent value;
        value.type = static_cast<sim::LightType>(type);
        if (properties) {
            if (properties->type < SIM_LIGHT_POINT || properties->type > SIM_LIGHT_AMBIENT)
                throw sim::Error(sim::ErrorCode::InvalidArgument, "light type is invalid");
            value.type = static_cast<sim::LightType>(properties->type);
            std::copy(std::begin(properties->color_rgb), std::end(properties->color_rgb), value.color.begin());
            value.intensity = properties->intensity; value.range = properties->range; value.decay = properties->decay;
            value.inner_cone_radians = properties->inner_cone_radians; value.outer_cone_radians = properties->outer_cone_radians;
            value.angular_diameter_radians = properties->angular_diameter_radians; value.enabled = properties->enabled != 0;
        }
        world.registry().emplace<sim::LightComponent>(entity, value);
        *output = entity;
    });
}

int sim_set_light_properties(SimHandle* simulator, SimWorldHandle world_id,
                             SimEntityHandle entity, const SimLightProperties* properties) {
    return Guard([&] {
        if (!properties) throw sim::Error(sim::ErrorCode::InvalidArgument, "light properties are null");
        if (properties->type < SIM_LIGHT_POINT || properties->type > SIM_LIGHT_AMBIENT)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "light type is invalid");
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        auto& value = handle.simulator.world(world_id).registry().get<sim::LightComponent>(entity);
        value.type = static_cast<sim::LightType>(properties->type);
        std::copy(std::begin(properties->color_rgb), std::end(properties->color_rgb), value.color.begin());
        value.intensity = properties->intensity; value.range = properties->range; value.decay = properties->decay;
        value.inner_cone_radians = properties->inner_cone_radians; value.outer_cone_radians = properties->outer_cone_radians;
        value.angular_diameter_radians = properties->angular_diameter_radians; value.enabled = properties->enabled != 0;
    });
}

int sim_get_light_properties(SimHandle* simulator, SimWorldHandle world_id,
                             SimEntityHandle entity, SimLightProperties* output) {
    return Guard([&] {
        if (!output) throw sim::Error(sim::ErrorCode::InvalidArgument, "light output is null");
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        const auto& value = handle.simulator.world(world_id).registry().get<sim::LightComponent>(entity);
        output->type = static_cast<SimLightType>(value.type);
        std::copy(value.color.begin(), value.color.end(), output->color_rgb);
        output->intensity = value.intensity; output->range = value.range; output->decay = value.decay;
        output->inner_cone_radians = value.inner_cone_radians; output->outer_cone_radians = value.outer_cone_radians;
        output->angular_diameter_radians = value.angular_diameter_radians; output->enabled = value.enabled ? 1 : 0;
    });
}

int sim_set_rigid_body(SimHandle* simulator, SimWorldHandle world_id,
                       SimEntityHandle entity, int enabled, int dynamic,
                       int gravity_enabled, double mass, const double friction[3],
                       double restitution) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (friction == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "friction is null");
        if (mass <= 0.0 || restitution < 0.0 || restitution > 1.0 ||
            friction[0] < 0.0 || friction[1] < 0.0 || friction[2] < 0.0)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "invalid rigid-body material or mass");
        auto& registry = handle.simulator.world(world_id).registry();
        const auto material = sim::Vec3{friction[0], friction[1], friction[2]};
        // Imported mesh assets keep their collider on a visual descendant while the
        // dynamic/free-joint body belongs to the imported root. Apply material values
        // to every collider in that subtree instead of requiring a proxy collider on
        // the root entity.
        std::vector<sim::EntityId> pending{entity};
        while (!pending.empty()) {
            const auto current = pending.back();
            pending.pop_back();
            if (registry.has<sim::ColliderComponent>(current)) {
                auto& collider = registry.get<sim::ColliderComponent>(current);
                collider.friction = material;
                collider.restitution = restitution;
            }
            if (!registry.has<sim::HierarchyComponent>(current)) continue;
            for (const auto child : registry.get<sim::HierarchyComponent>(current).children)
                pending.push_back(child);
        }
        if (enabled) {
            if (registry.has<sim::RigidBodyComponent>(entity)) {
                auto& body = registry.get<sim::RigidBodyComponent>(entity);
                body.mass = mass;
                body.dynamic = dynamic != 0;
                body.gravity_enabled = gravity_enabled != 0;
            } else {
                registry.emplace<sim::RigidBodyComponent>(entity, sim::RigidBodyComponent{
                    mass, {}, {}, dynamic != 0, gravity_enabled != 0});
            }
        } else if (registry.has<sim::RigidBodyComponent>(entity)) {
            registry.remove<sim::RigidBodyComponent>(entity);
        }
        handle.physics.at(world_id).dirty = true;
    });
}

int sim_set_ifps_sphere_count(SimHandle* simulator, SimWorldHandle world_id,
                              SimEntityHandle entity, uint32_t sphere_count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (sphere_count > 1'000'000U)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "IFPS sphere count must not exceed 1000000");
        auto& collider = handle.simulator.world(world_id).registry()
            .get<sim::ColliderComponent>(entity);
        collider.ifps_sphere_count = sphere_count;
        handle.physics.at(world_id).dirty = true;
    });
}

int sim_get_ifps_sphere_count(SimHandle* simulator, SimWorldHandle world_id,
                              SimEntityHandle entity, uint32_t* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        *output = handle.simulator.world(world_id).registry()
            .get<sim::ColliderComponent>(entity).ifps_sphere_count;
    });
}

int sim_set_transform(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle entity,
                      const SimTransform* transform) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (transform == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "transform is null");
        auto& value = handle.simulator.world(world_id).registry().get<sim::TransformComponent>(entity).local;
        value.translation = {transform->translation[0], transform->translation[1], transform->translation[2]};
        value.rotation = {transform->rotation_wxyz[0], transform->rotation_wxyz[1],
                          transform->rotation_wxyz[2], transform->rotation_wxyz[3]};
        value.scale = {transform->scale[0], transform->scale[1], transform->scale[2]};
        handle.simulator.world(world_id).update_world_transforms();
        handle.physics.at(world_id).dirty = true;
    });
}

int sim_get_transform(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle entity,
                      SimTransform* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        const auto& value = handle.simulator.world(world_id).registry().get<sim::TransformComponent>(entity).world;
        output->translation[0] = value.translation.x; output->translation[1] = value.translation.y;
        output->translation[2] = value.translation.z; output->rotation_wxyz[0] = value.rotation.w;
        output->rotation_wxyz[1] = value.rotation.x; output->rotation_wxyz[2] = value.rotation.y;
        output->rotation_wxyz[3] = value.rotation.z; output->scale[0] = value.scale.x;
        output->scale[1] = value.scale.y; output->scale[2] = value.scale.z;
    });
}

int sim_set_transforms(SimHandle* simulator, SimWorldHandle world_id,
                       const SimEntityHandle* entities, const SimTransform* transforms,
                       size_t count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if ((entities == nullptr || transforms == nullptr) && count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "batch transform buffer is null");
        auto& world = handle.simulator.world(world_id);
        for (std::size_t index = 0; index < count; ++index)
            (void)world.registry().get<sim::TransformComponent>(entities[index]);
        for (std::size_t index = 0; index < count; ++index) {
            auto& value = world.registry().get<sim::TransformComponent>(entities[index]).local;
            const auto& input = transforms[index];
            value.translation = {input.translation[0], input.translation[1], input.translation[2]};
            value.rotation = {input.rotation_wxyz[0], input.rotation_wxyz[1],
                              input.rotation_wxyz[2], input.rotation_wxyz[3]};
            value.scale = {input.scale[0], input.scale[1], input.scale[2]};
        }
        world.update_world_transforms();
        handle.physics.at(world_id).dirty = true;
    });
}

int sim_robot_joint_count(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
                          size_t* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        *output = sim::Robot(handle.simulator.world(world_id), robot).joints().size();
    });
}

int sim_robot_joint_name(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
                         size_t joint_index, char* name_utf8, size_t capacity,
                         size_t* required) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (required == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "required is null");
        const auto state = sim::Robot(handle.simulator.world(world_id), robot).joint_state();
        if (joint_index >= state.names.size())
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint index is out of range");
        const auto& name = state.names[joint_index];
        *required = name.size() + 1;
        if (name_utf8 == nullptr && capacity == 0) return;
        if (name_utf8 == nullptr || capacity < *required)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint name buffer is too small");
        std::memcpy(name_utf8, name.c_str(), *required);
    });
}

int sim_robot_link_count(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
                         size_t* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        const auto& registry = handle.simulator.world(world_id).registry();
        *output = registry.get<sim::RobotComponent>(robot).links.size();
    });
}

int sim_robot_link_info(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
                        size_t link_index, SimEntityHandle* link, char* name_utf8,
                        size_t capacity, size_t* required) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (link == nullptr || required == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "link or required is null");
        const auto& registry = handle.simulator.world(world_id).registry();
        const auto& links = registry.get<sim::RobotComponent>(robot).links;
        if (link_index >= links.size())
            throw sim::Error(sim::ErrorCode::InvalidArgument, "link index is out of range");
        *link = links[link_index];
        const auto& name = registry.get<sim::NameComponent>(*link).name;
        *required = name.size() + 1;
        if (name_utf8 == nullptr && capacity == 0) return;
        if (name_utf8 == nullptr || capacity < *required)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "link name buffer is too small");
        std::memcpy(name_utf8, name.c_str(), *required);
    });
}

int sim_robot_get_joint_state(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
                              double* positions, double* velocities, double* efforts, size_t count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (positions == nullptr || velocities == nullptr || efforts == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint output buffer is null");
        const auto state = sim::Robot(handle.simulator.world(world_id), robot).joint_state();
        if (count != state.positions.size())
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint output count mismatch");
        std::copy(state.positions.begin(), state.positions.end(), positions);
        std::copy(state.velocities.begin(), state.velocities.end(), velocities);
        std::copy(state.efforts.begin(), state.efforts.end(), efforts);
    });
}

int sim_robot_command_joint_positions(SimHandle* simulator, SimWorldHandle world_id,
                                      SimEntityHandle robot, const double* targets, size_t count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (targets == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "joint target is null");
        sim::Robot(handle.simulator.world(world_id), robot).command_joint_positions({targets, count});
    });
}

int sim_robot_set_joint_continuous(SimHandle* simulator, SimWorldHandle world_id,
                                   SimEntityHandle robot, const char* joint_name_utf8,
                                   int enabled) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (joint_name_utf8 == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint name is null");
        sim::Robot(handle.simulator.world(world_id), robot).set_joint_continuous(
            joint_name_utf8, enabled != 0);
    });
}

int sim_robot_command_joint_velocities(SimHandle* simulator, SimWorldHandle world_id,
                                       SimEntityHandle robot, const double* targets, size_t count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (targets == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint target is null");
        sim::Robot(handle.simulator.world(world_id), robot).command_joint_velocities({targets, count});
    });
}

int sim_robot_command_joint_positions_batch(SimHandle* simulator, SimWorldHandle world_id,
                                            const SimEntityHandle* robots, size_t robot_count,
                                            const double* targets, size_t targets_per_robot) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if ((robots == nullptr || targets == nullptr) && robot_count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "batch joint target buffer is null");
        auto& world = handle.simulator.world(world_id);
        std::vector<sim::Robot> robot_handles;
        robot_handles.reserve(robot_count);
        for (std::size_t index = 0; index < robot_count; ++index) {
            robot_handles.emplace_back(world, robots[index]);
            if (robot_handles.back().joints().size() != targets_per_robot)
                throw sim::Error(sim::ErrorCode::InvalidArgument,
                                 "batch joint target width does not match a robot");
        }
        for (std::size_t index = 0; index < robot_count; ++index)
            robot_handles[index].command_joint_positions(
                {targets + index * targets_per_robot, targets_per_robot});
    });
}

int sim_robot_set_joint_positions(SimHandle* simulator, SimWorldHandle world_id,
                                  SimEntityHandle robot, const double* positions, size_t count,
                                  SimJointLimitPolicy policy) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (positions == nullptr && count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint position buffer is null");
        sim::Robot(handle.simulator.world(world_id), robot).set_joint_positions(
            {positions, count}, LimitPolicy(policy));
        // Posing the robot moves its colliders, so the backends have to see the new transforms.
        handle.physics.at(world_id).dirty = true;
    });
}

int sim_robot_joint_info(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
                         size_t joint_index, SimJointInfo* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint info output is null");
        auto& world = handle.simulator.world(world_id);
        const auto joints = sim::Robot(world, robot).joints();
        if (joint_index >= joints.size())
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint index is out of range");
        const auto& joint = world.registry().get<sim::JointComponent>(joints[joint_index]);
        output->type = static_cast<SimJointType>(joint.type);
        output->axis[0] = joint.axis.x;
        output->axis[1] = joint.axis.y;
        output->axis[2] = joint.axis.z;
        output->lower_limit = joint.lower_limit;
        output->upper_limit = joint.upper_limit;
        output->entity = joints[joint_index];
        output->parent_link = joint.parent_link;
        output->child_link = joint.child_link;
    });
}

int sim_robot_command_joint_positions_tuned(SimHandle* simulator, SimWorldHandle world_id,
                                            SimEntityHandle robot, const double* targets,
                                            size_t count, double position_gain,
                                            double velocity_limit) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (targets == nullptr && count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint target is null");
        sim::Robot(handle.simulator.world(world_id), robot).command_joint_positions(
            {targets, count}, position_gain, velocity_limit);
    });
}

int sim_robot_command_joint_positions_with_effort_limits(
    SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
    const double* targets, size_t count, double position_gain, double velocity_limit,
    const double* effort_limits) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (count != 0 && (targets == nullptr || effort_limits == nullptr))
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint targets or effort limits are null");
        sim::Robot(handle.simulator.world(world_id), robot).command_joint_positions(
            {targets, count}, position_gain, velocity_limit, {effort_limits, count});
    });
}

int sim_robot_command_joint_efforts(SimHandle* simulator, SimWorldHandle world_id,
                                    SimEntityHandle robot, const double* targets, size_t count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (targets == nullptr && count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint target is null");
        sim::Robot(handle.simulator.world(world_id), robot).command_joint_efforts({targets, count});
    });
}

int sim_robot_set_joint_effort_limits(SimHandle* simulator, SimWorldHandle world_id,
                                     SimEntityHandle robot, const double* limits, size_t count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (limits == nullptr && count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "joint effort limit is null");
        sim::Robot(handle.simulator.world(world_id), robot).set_joint_effort_limits({limits, count});
    });
}

int sim_robot_kinematics_batch(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
                               const double* joint_positions, size_t configuration_count,
                               size_t joints_per_configuration, const SimEntityHandle* links,
                               size_t link_count, SimEntityHandle jacobian_link,
                               SimJointLimitPolicy policy, SimTransform* transforms,
                               double* jacobians) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        const std::size_t value_count = configuration_count * joints_per_configuration;
        if (joint_positions == nullptr && value_count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "kinematics joint buffer is null");
        if (links == nullptr && link_count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "kinematics link buffer is null");
        if (transforms == nullptr && configuration_count * link_count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "kinematics transform output is null");
        if (jacobians == nullptr && jacobian_link != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "a Jacobian link was requested without an output buffer");
        const auto results = sim::EvaluateRobotKinematics(
            handle.simulator.world(world_id), robot, {joint_positions, value_count},
            configuration_count, {links, link_count}, jacobian_link, LimitPolicy(policy));
        for (std::size_t configuration = 0; configuration < results.size(); ++configuration) {
            const auto& result = results[configuration];
            for (std::size_t link = 0; link < result.link_transforms.size(); ++link)
                CopyTransform(result.link_transforms[link],
                              transforms[configuration * link_count + link]);
            if (jacobians == nullptr) continue;
            for (std::size_t joint = 0; joint < result.jacobian.size(); ++joint) {
                const auto& column = result.jacobian[joint];
                std::copy(column.begin(), column.end(),
                          jacobians + (configuration * result.jacobian.size() + joint) * 6);
            }
        }
    });
}

int sim_robot_configuration_contacts(SimHandle* simulator, SimWorldHandle world_id,
                                     SimEntityHandle robot, const double* joint_positions,
                                     size_t joint_count, double distance_threshold,
                                     SimJointLimitPolicy policy, SimContactPair* pairs,
                                     size_t capacity, size_t* required) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (joint_positions == nullptr && joint_count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "contact joint buffer is null");
        if (required == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "contact pair count output is null");
        if (pairs == nullptr && capacity != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "contact pair buffer is null");
        EnsurePhysics(handle, world_id);
#if defined(SIM_HAS_FCL)
        const auto& instance = handle.physics.at(world_id);
        if (!instance.fcl)
            throw sim::Error(sim::ErrorCode::BackendUnavailable,
                             "contact pair queries require the FCL collision backend");
        const auto found = instance.fcl->robot_configuration_contacts(
            handle.simulator.world(world_id), robot, {joint_positions, joint_count},
            distance_threshold, LimitPolicy(policy));
        *required = found.size();
        for (std::size_t index = 0; index < found.size() && index < capacity; ++index) {
            pairs[index].robot_collider = found[index].robot_collider;
            pairs[index].other = found[index].other;
            pairs[index].distance = found[index].distance;
            pairs[index].colliding = found[index].colliding ? 1 : 0;
        }
#else
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "contact pair queries require the FCL backend");
#endif
    });
}

int sim_robot_configuration_contacts_filtered(
    SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle robot,
    const double* joint_positions, size_t joint_count, double distance_threshold,
    SimJointLimitPolicy policy, const SimEntityHandle* excluded_pairs,
    size_t excluded_pair_count, SimContactPair* pairs, size_t capacity, size_t* required) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (joint_positions == nullptr && joint_count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "contact joint buffer is null");
        if (excluded_pairs == nullptr && excluded_pair_count != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "excluded pair buffer is null");
        if (required == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "contact pair count output is null");
        if (pairs == nullptr && capacity != 0)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "contact pair buffer is null");
        EnsurePhysics(handle, world_id);
#if defined(SIM_HAS_FCL)
        const auto& instance = handle.physics.at(world_id);
        if (!instance.fcl)
            throw sim::Error(sim::ErrorCode::BackendUnavailable,
                             "contact pair queries require the FCL collision backend");
        const auto excluded = std::span<const std::array<sim::EntityId, 2>>(
            reinterpret_cast<const std::array<sim::EntityId, 2>*>(excluded_pairs),
            excluded_pair_count);
        const auto found = instance.fcl->robot_configuration_contacts(
            handle.simulator.world(world_id), robot, {joint_positions, joint_count},
            distance_threshold, LimitPolicy(policy), excluded);
        *required = found.size();
        for (std::size_t index = 0; index < found.size() && index < capacity; ++index) {
            pairs[index].robot_collider = found[index].robot_collider;
            pairs[index].other = found[index].other;
            pairs[index].distance = found[index].distance;
            pairs[index].colliding = found[index].colliding ? 1 : 0;
        }
#else
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "contact pair queries require the FCL backend");
#endif
    });
}

int sim_collision_sync(SimHandle* simulator, SimWorldHandle world_id) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        EnsurePhysics(handle, world_id);
        auto& instance = handle.physics.at(world_id);
        if (instance.collision)
            instance.collision->sync_from_scene(handle.simulator.world(world_id));
    });
}

int sim_physics_contacts(SimHandle* simulator, SimWorldHandle world_id,
                         SimPhysicsContact* contacts, size_t capacity, size_t* required) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (required == nullptr || (contacts == nullptr && capacity != 0))
            throw sim::Error(sim::ErrorCode::InvalidArgument, "invalid physics contact output buffer");
        EnsurePhysics(handle, world_id);
        const auto& dynamics = handle.physics.at(world_id).dynamics;
        if (!dynamics)
            throw sim::Error(sim::ErrorCode::BackendUnavailable, "physics contact queries require a dynamics backend");
        const auto found = dynamics->contacts();
        *required = found.size();
        for (std::size_t index = 0; index < found.size() && index < capacity; ++index)
            contacts[index] = {found[index].first, found[index].second,
                               found[index].distance, found[index].normal_force};
    });
}

int sim_physics_sync(SimHandle* simulator, SimWorldHandle world_id) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        EnsurePhysics(handle, world_id);
        auto& instance = handle.physics.at(world_id);
        if (instance.dynamics)
            instance.dynamics->sync_from_scene(handle.simulator.world(world_id));
    });
}

int sim_create_camera(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle parent,
                      const char* frame_id_utf8, double update_rate_hz,
                      uint32_t width, uint32_t height, double vertical_fov_radians,
                      double near_plane, double far_plane, SimEntityHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (frame_id_utf8 == nullptr || output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "camera frame or output is null");
#if defined(SIM_HAS_VULKAN)
        auto& instance = handle.physics.at(world_id);
        if (!instance.vulkan) {
            instance.vulkan = std::make_shared<sim::VulkanBackend>();
            instance.sensors->set_render(instance.vulkan);
            auto render = instance.vulkan;
            auto& world = handle.simulator.world(world_id);
            world.scheduler().add_system(sim::SimulationPhase::CollisionSync,
                "backend.vulkan.sync", [&world, render](double) { render->sync_from_scene(world); }, 20);
            render->sync_from_scene(world);
        }
        sim::CameraComponent camera;
        camera.width = width; camera.height = height;
        camera.vertical_fov_radians = vertical_fov_radians;
        camera.near_plane = near_plane; camera.far_plane = far_plane;
        *output = handle.sensors.at(world_id)->create_camera(
            "camera", parent, frame_id_utf8, update_rate_hz, camera);
#else
        (void)world_id; (void)parent; (void)update_rate_hz; (void)width; (void)height;
        (void)vertical_fov_radians; (void)near_plane; (void)far_plane;
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "this build does not include Vulkan camera rendering");
#endif
    });
}

int sim_set_camera_properties(SimHandle* simulator, SimWorldHandle world_id,
                              SimEntityHandle camera, const char* frame_id_utf8,
                              double update_rate_hz, uint32_t width, uint32_t height,
                              double vertical_fov_radians, double near_plane,
                              double far_plane, int rgbd_output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (frame_id_utf8 == nullptr || *frame_id_utf8 == '\0' || update_rate_hz <= 0.0 ||
            width == 0U || height == 0U || vertical_fov_radians <= 0.0 ||
            vertical_fov_radians >= 3.14159265358979323846 || near_plane <= 0.0 ||
            far_plane <= near_plane)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "camera properties are invalid");
        auto& registry = handle.simulator.world(world_id).registry();
        auto& sensor = registry.get<sim::SensorComponent>(camera);
        auto& component = registry.get<sim::CameraComponent>(camera);
        sensor.frame_id = frame_id_utf8;
        sensor.update_rate_hz = update_rate_hz;
        sensor.next_update_time = handle.simulator.world(world_id).time().state().simulation_time;
        component.width = width;
        component.height = height;
        component.vertical_fov_radians = vertical_fov_radians;
        component.near_plane = near_plane;
        component.far_plane = far_plane;
        component.output = rgbd_output != 0 ? sim::CameraOutput::RGBD : sim::CameraOutput::RGB;
    });
}

int sim_camera_get_dimensions(SimHandle* simulator, SimWorldHandle world_id,
                              SimEntityHandle camera, uint32_t* width, uint32_t* height) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (width == nullptr || height == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "camera dimension output is null");
        const auto& component = handle.simulator.world(world_id).registry()
                                    .get<sim::CameraComponent>(camera);
        *width = component.width; *height = component.height;
    });
}

int sim_camera_get_intrinsics(SimHandle* simulator, SimWorldHandle world_id,
                              SimEntityHandle camera, double* fx, double* fy,
                              double* cx, double* cy) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (fx == nullptr || fy == nullptr || cx == nullptr || cy == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "camera intrinsics output is null");
        const auto& component = handle.simulator.world(world_id).registry()
                                    .get<sim::CameraComponent>(camera);
        *fy = 0.5 * static_cast<double>(component.height) /
              std::tan(component.vertical_fov_radians * 0.5);
        *fx = *fy;
        *cx = (static_cast<double>(component.width) - 1.0) * 0.5;
        *cy = (static_cast<double>(component.height) - 1.0) * 0.5;
    });
}

int sim_camera_get_rgb(SimHandle* simulator, SimWorldHandle world_id,
                       SimEntityHandle camera, uint8_t* rgb, size_t byte_count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (rgb == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "camera RGB output is null");
        const auto& frame = handle.sensors.at(world_id)->buffer(camera).latest<sim::RGBFrame>();
        if (byte_count != frame.rgb.size())
            throw sim::Error(sim::ErrorCode::InvalidArgument, "camera RGB output size mismatch");
        std::copy(frame.rgb.begin(), frame.rgb.end(), rgb);
    });
}

int sim_camera_get_depth(SimHandle* simulator, SimWorldHandle world_id,
                         SimEntityHandle camera, float* depth, size_t pixel_count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (depth == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "camera depth output is null");
        const auto& frame = handle.sensors.at(world_id)->buffer(camera).latest<sim::DepthFrame>();
        if (pixel_count != frame.linear_depth_meters.size())
            throw sim::Error(sim::ErrorCode::InvalidArgument, "camera depth output size mismatch");
        std::copy(frame.linear_depth_meters.begin(), frame.linear_depth_meters.end(), depth);
    });
}

int sim_create_lidar(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle parent,
                     const char* frame_id_utf8, double update_rate_hz,
                     uint32_t horizontal_samples, uint32_t vertical_channels,
                     double min_distance, double max_distance, SimEntityHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (frame_id_utf8 == nullptr || output == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "LiDAR frame or output is null");
#if !defined(SIM_HAS_FCL) && !defined(SIM_HAS_MUJOCO)
            throw sim::Error(sim::ErrorCode::BackendUnavailable,
                             "LiDAR requires the MuJoCo or FCL collision provider");
#endif
        sim::LiDARComponent lidar;
        lidar.horizontal_samples = horizontal_samples; lidar.vertical_channels = vertical_channels;
        lidar.min_distance = min_distance; lidar.max_distance = max_distance;
        *output = handle.sensors.at(world_id)->create_lidar(
            "lidar", parent, frame_id_utf8, update_rate_hz, lidar);
        handle.physics.at(world_id).dirty = true;
    });
}

int sim_lidar_point_count(SimHandle* simulator, SimWorldHandle world_id,
                          SimEntityHandle lidar, size_t* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        const auto& frame = handle.sensors.at(world_id)->buffer(lidar).latest();
        if (!std::holds_alternative<sim::LiDARFrame>(frame))
            throw sim::Error(sim::ErrorCode::InvalidState, "latest sensor frame is not LiDAR data");
        *output = std::get<sim::LiDARFrame>(frame).points.size();
    });
}

int sim_lidar_get_points(SimHandle* simulator, SimWorldHandle world_id, SimEntityHandle lidar,
                         float* xyz, float* distances, size_t point_count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (xyz == nullptr || distances == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "LiDAR output buffer is null");
        const auto& sensor_frame = handle.sensors.at(world_id)->buffer(lidar).latest();
        if (!std::holds_alternative<sim::LiDARFrame>(sensor_frame))
            throw sim::Error(sim::ErrorCode::InvalidState, "latest sensor frame is not LiDAR data");
        const auto& frame = std::get<sim::LiDARFrame>(sensor_frame);
        if (point_count != frame.points.size())
            throw sim::Error(sim::ErrorCode::InvalidArgument, "LiDAR output count mismatch");
        for (std::size_t index = 0; index < point_count; ++index) {
            xyz[3 * index] = static_cast<float>(frame.points[index].x);
            xyz[3 * index + 1] = static_cast<float>(frame.points[index].y);
            xyz[3 * index + 2] = static_cast<float>(frame.points[index].z);
            distances[index] = frame.distances[index];
        }
    });
}

int sim_collision_check(SimHandle* simulator, SimWorldHandle world_id,
                        SimEntityHandle first, SimEntityHandle second, int* colliding) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (colliding == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "collision output is null");
        EnsurePhysics(handle, world_id);
        const auto& collision = handle.physics.at(world_id).collision;
        if (!collision) throw sim::Error(sim::ErrorCode::BackendUnavailable,
                                         "no collision backend is available");
        *colliding = collision->check_collision(first, second) ? 1 : 0;
    });
}

int sim_collision_distance(SimHandle* simulator, SimWorldHandle world_id,
                           SimEntityHandle first, SimEntityHandle second, double* distance) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (distance == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "distance output is null");
        EnsurePhysics(handle, world_id);
        const auto& collision = handle.physics.at(world_id).collision;
        if (!collision) throw sim::Error(sim::ErrorCode::BackendUnavailable,
                                         "no collision backend is available");
        *distance = collision->distance(first, second);
    });
}

int sim_scene_collision_batch(SimHandle* simulator, SimWorldHandle world_id,
                              const SimEntityHandle* entities, size_t count,
                              uint8_t* colliding, double* elapsed_ms) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (((entities == nullptr || colliding == nullptr) && count != 0) ||
            elapsed_ms == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "scene collision input or output is null");
        EnsurePhysics(handle, world_id);
        const auto begin = std::chrono::steady_clock::now();
        const auto& collision_backend = handle.physics.at(world_id).collision;
        if (!collision_backend) throw sim::Error(sim::ErrorCode::BackendUnavailable,
                                                 "no collision backend is available");
        const auto result = collision_backend->check_scene_collisions({entities, count});
        std::copy(result.begin(), result.end(), colliding);
        const auto end = std::chrono::steady_clock::now();
        *elapsed_ms = std::chrono::duration<double, std::milli>(end - begin).count();
    });
}

int sim_robot_configuration_collision(SimHandle* simulator, SimWorldHandle world_id,
                                      SimEntityHandle robot, const double* joint_positions,
                                      size_t joint_count, int* colliding) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if ((joint_positions == nullptr && joint_count != 0) || colliding == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "robot configuration input or output is null");
        EnsurePhysics(handle, world_id);
#if defined(SIM_HAS_FCL)
        *colliding = handle.physics.at(world_id).fcl->check_robot_configuration(
            handle.simulator.world(world_id), robot, {joint_positions, joint_count}) ? 1 : 0;
#else
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "robot configuration checks require the FCL backend");
#endif
    });
}

int sim_robot_configurations_collision(SimHandle* simulator, SimWorldHandle world_id,
                                       SimEntityHandle robot, const double* joint_positions,
                                       size_t configuration_count,
                                       size_t joints_per_configuration, uint8_t* colliding) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        const std::size_t value_count = configuration_count * joints_per_configuration;
        if ((joint_positions == nullptr && value_count != 0) ||
            (colliding == nullptr && configuration_count != 0))
            throw sim::Error(sim::ErrorCode::InvalidArgument,
                             "batch robot configuration input or output is null");
        EnsurePhysics(handle, world_id);
#if defined(SIM_HAS_FCL)
        const auto result = handle.physics.at(world_id).fcl->check_robot_configurations(
            handle.simulator.world(world_id), robot, {joint_positions, value_count},
            configuration_count);
        for (std::size_t index = 0; index < result.size(); ++index)
            colliding[index] = result[index] ? 1U : 0U;
#else
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "batch robot configuration checks require the FCL backend");
#endif
    });
}

int sim_step(SimHandle* simulator, SimWorldHandle world_id, uint64_t count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        EnsurePhysics(handle, world_id);
        handle.simulator.world(world_id).step(count);
    });
}

int sim_step_unpaced(SimHandle* simulator, SimWorldHandle world_id, uint64_t count) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        EnsurePhysics(handle, world_id);
        handle.simulator.world(world_id).step_unpaced(count);
    });
}

int sim_step_profiled(SimHandle* simulator, SimWorldHandle world_id, uint64_t count,
                      double* elapsed_ms) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (elapsed_ms == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "elapsed output is null");
        EnsurePhysics(handle, world_id);
        const auto begin = std::chrono::steady_clock::now();
        handle.simulator.world(world_id).step(count);
        const auto end = std::chrono::steady_clock::now();
        *elapsed_ms = std::chrono::duration<double, std::milli>(end - begin).count();
    });
}

int sim_start(SimHandle* simulator, SimWorldHandle world_id) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        handle.simulator.world(world_id).start();
    });
}

int sim_pause(SimHandle* simulator, SimWorldHandle world_id) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        handle.simulator.world(world_id).pause();
    });
}

int sim_get_status(SimHandle* simulator, SimWorldHandle world_id,
                   SimSimulationStatus* status) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (status == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "status output is null");
        switch (handle.simulator.world(world_id).status()) {
            case sim::SimulationStatus::Stopped: *status = SIM_STATUS_STOPPED; break;
            case sim::SimulationStatus::Running: *status = SIM_STATUS_RUNNING; break;
            case sim::SimulationStatus::Paused: *status = SIM_STATUS_PAUSED; break;
        }
    });
}

int sim_poll_event(SimHandle* simulator, SimWorldHandle world_id,
                   SimEvent* event, int* has_event) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (event == nullptr || has_event == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "event output is null");
        auto& queue = handle.events.at(world_id);
        if (queue.empty()) {
            *has_event = 0;
            return;
        }
        const auto source = std::move(queue.front());
        queue.pop_front();
        std::memset(event, 0, sizeof(*event));
        const auto copy_text = [](char* destination, std::size_t capacity,
                                  const std::string& value) {
            const std::size_t count = std::min(capacity - 1, value.size());
            std::memcpy(destination, value.data(), count);
            destination[count] = '\0';
        };
        copy_text(event->type_utf8, sizeof(event->type_utf8), source.type);
        copy_text(event->detail_utf8, sizeof(event->detail_utf8), source.detail);
        event->world = source.world;
        event->entity = source.entity;
        event->simulation_time = source.simulation_time;
        *has_event = 1;
    });
}

int sim_apply_body_wrench(SimHandle* simulator, SimWorldHandle world_id,
                          SimEntityHandle entity, const double force_xyz[3],
                          const double torque_xyz[3]) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (force_xyz == nullptr || torque_xyz == nullptr)
            throw sim::Error(sim::ErrorCode::InvalidArgument, "force or torque is null");
        EnsurePhysics(handle, world_id);
        auto& physics = handle.physics.at(world_id);
        if (physics.dynamics) {
            physics.dynamics->apply_body_wrench(entity,
                {force_xyz[0], force_xyz[1], force_xyz[2]},
                {torque_xyz[0], torque_xyz[1], torque_xyz[2]});
            return;
        }
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "body wrench requires an active physics backend");
    });
}

int sim_get_time(SimHandle* simulator, SimWorldHandle world_id, double* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        *output = handle.simulator.world(world_id).time().state().simulation_time;
    });
}

int sim_save_state(SimHandle* simulator, SimWorldHandle world_id, SimStateHandle* output) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (output == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "output is null");
        EnsurePhysics(handle, world_id);
        SavedState state{handle.simulator.world(world_id).snapshot(), {}};
        if (handle.physics.at(world_id).dynamics)
            state.physics = handle.physics.at(world_id).dynamics->save_state();
        const SimStateHandle id = handle.next_state++;
        handle.states.emplace(id, std::move(state)); *output = id;
    });
}

int sim_restore_state(SimHandle* simulator, SimWorldHandle world_id, SimStateHandle state_id) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        const auto it = handle.states.find(state_id);
        if (it == handle.states.end()) throw sim::Error(sim::ErrorCode::NotFound, "state handle is invalid");
        handle.simulator.world(world_id).restore(it->second.world);
        if (handle.physics.at(world_id).dynamics && !it->second.physics.empty()) {
            handle.physics.at(world_id).dynamics->restore_state(it->second.physics);
            handle.physics.at(world_id).dynamics->sync_to_scene(handle.simulator.world(world_id));
        }
        if (handle.physics.at(world_id).collision)
            handle.physics.at(world_id).collision->sync_from_scene(handle.simulator.world(world_id));
        ++handle.scene_generations[world_id];
        ++handle.scene_revisions[world_id];
    });
}

int sim_save_state_file(SimHandle* simulator, SimWorldHandle world_id, const char* path_utf8) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (path_utf8 == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "state path is null");
        EnsurePhysics(handle, world_id);
        sim::SimulationStateArchive archive{1, handle.simulator.world(world_id).snapshot(), {}};
        if (handle.physics.at(world_id).dynamics)
            archive.backend_state = handle.physics.at(world_id).dynamics->save_state();
        sim::SaveStateFile(Utf8Path(path_utf8), archive);
    });
}

int sim_restore_state_file(SimHandle* simulator, SimWorldHandle world_id, const char* path_utf8) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        if (path_utf8 == nullptr) throw sim::Error(sim::ErrorCode::InvalidArgument, "state path is null");
        auto archive = sim::LoadStateFile(Utf8Path(path_utf8));
        handle.simulator.world(world_id).restore(archive.world);
        handle.physics.at(world_id).dirty = true;
        EnsurePhysics(handle, world_id);
        if (handle.physics.at(world_id).dynamics && !archive.backend_state.empty()) {
            handle.physics.at(world_id).dynamics->restore_state(archive.backend_state);
            handle.physics.at(world_id).dynamics->sync_to_scene(handle.simulator.world(world_id));
        }
        ++handle.scene_generations[world_id];
        ++handle.scene_revisions[world_id];
    });
}

int sim_reset(SimHandle* simulator, SimWorldHandle world_id) {
    return Guard([&] {
        auto& handle = Require(simulator); std::scoped_lock lock(handle.mutex);
        handle.simulator.world(world_id).reset(); handle.physics.at(world_id).dirty = true;
        ++handle.scene_generations[world_id];
        ++handle.scene_revisions[world_id];
    });
}

}  // extern "C"
