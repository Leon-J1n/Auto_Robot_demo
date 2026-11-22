#include "sim/plugin.hpp"
#include "sim/ifps.hpp"
#include "sim/collision.hpp"
#include "sim/sensors.hpp"
#include "sim/foundation.hpp"
#include "sim/physics.hpp"
#include "sim/render.hpp"
#include "sim/state_io.hpp"

#include <functional>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

namespace {

std::filesystem::path UniqueTempPath(const std::string& name) {
#ifdef _WIN32
    const auto process_id = _getpid();
#else
    const auto process_id = getpid();
#endif
    return std::filesystem::temp_directory_path() /
           (name + "-" + std::to_string(process_id));
}

void Require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

template <typename Function>
void RequireError(sim::ErrorCode code, Function&& function) {
    try {
        function();
    } catch (const sim::Error& error) {
        Require(error.code() == code, "unexpected error code");
        return;
    }
    throw std::runtime_error("expected sim::Error");
}

void TestRegistryLifecycleAndClone() {
    sim::Registry registry;
    const auto entity = registry.create("robot");
    registry.emplace<sim::RobotComponent>(entity, sim::RobotComponent{"robot.urdf", {}});
    Require(registry.valid(entity), "created entity should be valid");
    Require(registry.get<sim::NameComponent>(entity).name == "robot", "entity name mismatch");
    Require(registry.view<sim::RobotComponent>().size() == 1, "component view mismatch");

    auto clone = registry.clone();
    registry.get<sim::NameComponent>(entity).name = "changed";
    Require(clone.get<sim::NameComponent>(entity).name == "robot", "registry clone must be deep");
    Require(registry.destroy(entity), "destroy should report success");
    RequireError(sim::ErrorCode::NotFound, [&] { registry.get<sim::NameComponent>(entity); });
}

void TestFrameTree() {
    sim::FrameTree frames;
    frames.set_frame("base", "world", {.translation = {1.0, 0.0, 0.0}});
    frames.set_frame("camera", "base", {.translation = {0.0, 2.0, 0.0}});
    const auto world_from_camera = frames.lookup("camera", "world");
    Require(sim::NearlyEqual(world_from_camera.translation, {1.0, 2.0, 0.0}),
            "composed frame transform mismatch");
    const auto camera_from_world = frames.lookup("world", "camera");
    Require(sim::NearlyEqual(camera_from_world.translation, {-1.0, -2.0, 0.0}),
            "inverse frame transform mismatch");
    RequireError(sim::ErrorCode::ValidationFailed, [&] {
        frames.set_frame("base", "camera", sim::Transform::Identity());
    });
}

void TestHierarchyAndTransforms() {
    sim::World world(1);
    const auto parent = world.create_entity("parent");
    const auto second_parent = world.create_entity("second_parent");
    const auto child = world.create_entity("child", parent);
    world.registry().get<sim::TransformComponent>(parent).local.translation = {1.0, 0.0, 0.0};
    world.registry().get<sim::TransformComponent>(second_parent).local.translation = {-3.0, 0.0, 0.0};
    world.registry().get<sim::TransformComponent>(child).local.translation = {0.0, 2.0, 0.0};
    world.update_world_transforms();
    Require(sim::NearlyEqual(world.registry().get<sim::TransformComponent>(child).world.translation,
                             {1.0, 2.0, 0.0}),
            "hierarchy transform mismatch");
    world.set_parent_preserving_world(child, second_parent);
    Require(world.registry().get<sim::HierarchyComponent>(child).parent == second_parent,
            "reparent did not update the hierarchy");
    Require(sim::NearlyEqual(world.registry().get<sim::TransformComponent>(child).world.translation,
                             {1.0, 2.0, 0.0}),
            "reparent changed the child world transform");
    world.registry().get<sim::TransformComponent>(second_parent).local.translation.x = -2.0;
    world.update_world_transforms();
    Require(sim::NearlyEqual(world.registry().get<sim::TransformComponent>(child).world.translation,
                             {2.0, 2.0, 0.0}),
            "reparented child did not follow its new parent");
    world.set_parent_preserving_world(child, sim::kInvalidEntity);
    Require(world.registry().get<sim::HierarchyComponent>(child).parent == sim::kInvalidEntity &&
            sim::NearlyEqual(world.registry().get<sim::TransformComponent>(child).world.translation,
                             {2.0, 2.0, 0.0}),
            "detaching a child did not preserve its world transform");
    world.set_parent(child, parent);
    RequireError(sim::ErrorCode::ValidationFailed, [&] { world.set_parent(parent, child); });
    Require(world.destroy_entity(parent), "recursive destroy failed");
    Require(!world.registry().valid(child), "child should be recursively destroyed");
}

void TestCommandsEventsAndTime() {
    sim::World world(1, 0.01);
    int created_events = 0;
    const auto subscription =
        world.events().subscribe("EntityCreated", [&](const sim::Event&) { ++created_events; });
    const auto late_sequence = world.commands().submit(
        {.source = "test",
         .target_time = 0.02,
         .payload = sim::CreateEntityCommand{"late", std::nullopt}});
    const auto now_sequence = world.commands().submit(
        {.source = "test",
         .target_time = 0.0,
         .payload = sim::CreateEntityCommand{"now", std::nullopt}});
    Require(subscription != 0 && late_sequence < now_sequence, "sequence allocation failed");
    world.step();
    Require(world.registry().size() == 1, "ready command should execute on first step");
    Require(created_events == 1, "event should dispatch in the event phase of the same step");
    world.step(2);
    Require(world.registry().size() == 2, "scheduled command did not execute at target time");
    Require(created_events == 2, "late event did not dispatch");
    Require(sim::NearlyEqual(world.time().state().simulation_time, 0.03), "fixed time drift");
    Require(world.time().state().step_index == 3, "step index mismatch");
}

void TestObjectIdentityAndControlMailbox() {
    sim::World world(1);
    const auto first = world.create_entity("first");
    const auto second = world.create_entity("second");
    const std::string first_id = world.object_id(first);
    const std::string second_id = world.object_id(second);
    Require(first_id.size() == 36 && second_id.size() == 36 && first_id != second_id,
            "world object ids must be unique UUIDs");
    Require(world.find_entity_by_object_id(first_id) == first &&
            world.find_entity_by_object_id(second_id) == second,
            "world object id lookup failed");

    int executed = 0;
    std::thread producer([&] {
        world.control_mailbox().submit([&] { ++executed; });
    });
    producer.join();
    Require(world.control_mailbox().pending() == 1,
            "control mailbox did not accept a cross-thread task");
    world.step();
    Require(executed == 1 && world.control_mailbox().pending() == 0,
            "world did not drain control tasks at the tick boundary");
}

void TestSnapshotRestoreAndReset() {
    sim::World world(1, 0.005, 42);
    const auto robot = world.create_entity("robot");
    world.registry().get<sim::TransformComponent>(robot).local.translation = {1.0, 0.0, 0.0};
    world.capture_initial_state();
    world.step(3);
    const auto saved = world.snapshot();
    world.registry().get<sim::TransformComponent>(robot).local.translation = {9.0, 0.0, 0.0};
    world.step(2);
    world.restore(saved);
    Require(sim::NearlyEqual(world.time().state().simulation_time, 0.015),
            "snapshot simulation time not restored");
    Require(sim::NearlyEqual(world.registry().get<sim::TransformComponent>(robot).local.translation,
                             {1.0, 0.0, 0.0}),
            "snapshot ECS state not restored");
    world.reset();
    Require(sim::NearlyEqual(world.time().state().simulation_time, 0.0),
            "reset should restore captured initial time");
}

void TestRealTimeFactorPacing() {
    sim::World world(1, 0.02);
    world.time().set_real_time_factor(2.0);
    const auto start = std::chrono::steady_clock::now();
    world.step();
    const double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    Require(elapsed >= 0.007, "real-time factor did not pace the fixed simulation step");
}

void TestUnpacedStepPreservesClockConfiguration() {
    sim::World world(1, 0.002);
    world.time().set_real_time_factor(0.01);
    int ticks = 0;
    world.scheduler().add_system(sim::SimulationPhase::PhysicsStep, "test.unpaced",
        [&](double dt) {
            Require(sim::NearlyEqual(dt, 0.002), "unpaced step changed physics dt");
            Require(sim::NearlyEqual(world.time().state().real_time_factor, 0.01),
                    "unpaced step changed pacing factor during callback");
            ++ticks;
        });
    world.step_unpaced(3);
    Require(ticks == 3 && sim::NearlyEqual(world.time().state().simulation_time, 0.006),
            "unpaced step must advance all fixed ticks");
    world.scheduler().add_system(sim::SimulationPhase::PhysicsStep, "test.throw",
        [](double) { throw sim::Error(sim::ErrorCode::InvalidState, "test failure"); });
    RequireError(sim::ErrorCode::InvalidState, [&] { world.step_unpaced(); });
    Require(sim::NearlyEqual(world.time().state().real_time_factor, 0.01) &&
            sim::NearlyEqual(world.time().state().fixed_time_step, 0.002),
            "unpaced step exception changed clock configuration");
}

void TestBackends() {
    const auto valid = sim::BackendManager::validate(
        {sim::BackendKind::MuJoCo, sim::BackendKind::FCL, sim::BackendKind::Vulkan});
    Require(std::none_of(valid.begin(), valid.end(), [](const sim::Diagnostic& item) {
        return item.severity == sim::Severity::Fatal;
    }), "documented backend combination should be valid");

    RequireError(sim::ErrorCode::BackendCombinationInvalid, [] {
        sim::Simulator invalid({sim::BackendKind::None, sim::BackendKind::MuJoCo,
                                sim::BackendKind::None});
    });
}

#if defined(SIM_HAS_FCL)
void TestFCLCollisionAndQueryContext() {
    sim::World world(1, 0.01);
    const auto first = world.create_entity("first_box");
    const auto second = world.create_entity("second_box");
    world.registry().emplace<sim::ColliderComponent>(first, sim::ColliderComponent{});
    world.registry().emplace<sim::ColliderComponent>(second, sim::ColliderComponent{});
    world.registry().get<sim::TransformComponent>(second).local.translation = {0.4, 0.0, 0.0};
    sim::FCLBackend backend;
    backend.build(world);
    Require(backend.capabilities().collision_query && backend.capabilities().distance_query,
            "FCL capabilities mismatch");
    Require(backend.check_collision(first, second), "FCL failed to detect overlapping boxes");
    const auto overlapping = backend.check_scene_collisions(std::array{first, second});
    Require(overlapping.size() == 2 && overlapping[0] && overlapping[1],
            "FCL broad-phase batch query failed to flag both overlapping objects");

    world.registry().get<sim::TransformComponent>(second).local.translation = {3.0, 0.0, 0.0};
    backend.sync_from_scene(world);
    Require(!backend.check_collision(first, second) &&
                sim::NearlyEqual(backend.distance(first, second), 2.0, 1e-6),
            "FCL transform sync or distance query mismatch");
    const auto separated = backend.check_scene_collisions(std::array{first, second});
    Require(!separated[0] && !separated[1],
            "FCL broad-phase batch query reported separated objects");

    auto context = backend.create_query_context();
    context.set_transform(second, sim::Transform{{0.25, 0.0, 0.0}});
    Require(context.check_collision(first, second),
            "FCL query context did not use its independent transform");
    Require(!backend.check_collision(first, second),
            "FCL query context unexpectedly modified the main backend");
    const auto rays = backend.raycast({-3.0, 0.0, 0.0},
                                      std::array{sim::Vec3{1.0, 0.0, 0.0}}, 0.0, 10.0);
    Require(rays.size() == 1 && rays.front() > 2.4F && rays.front() < 2.6F,
            "FCL raycast distance mismatch");

    sim::World robot_world(2, 0.01);
    const auto robot_root = robot_world.create_entity("robot");
    robot_world.registry().emplace<sim::RobotComponent>(robot_root,
                                                        sim::RobotComponent{"", {}});
    const auto base = robot_world.create_entity("base", robot_root);
    const auto moving = robot_world.create_entity("moving", base);
    robot_world.registry().get<sim::RobotComponent>(robot_root).links = {base, moving};
    robot_world.registry().emplace<sim::ColliderComponent>(base, sim::ColliderComponent{});
    robot_world.registry().emplace<sim::ColliderComponent>(moving, sim::ColliderComponent{});
    sim::JointComponent prismatic;
    prismatic.name = "slide";
    prismatic.type = sim::JointType::Prismatic;
    prismatic.parent_link = base;
    prismatic.child_link = moving;
    prismatic.axis = {1.0, 0.0, 0.0};
    prismatic.joint_origin.translation = {2.0, 0.0, 0.0};
    prismatic.lower_limit = 0.0;
    prismatic.upper_limit = 2.0;
    robot_world.registry().emplace<sim::JointComponent>(moving, prismatic);
    robot_world.registry().get<sim::TransformComponent>(moving).local = prismatic.joint_origin;
    const auto obstacle = robot_world.create_entity("obstacle");
    robot_world.registry().get<sim::TransformComponent>(obstacle).local.translation = {4.0, 0.0, 0.0};
    robot_world.registry().emplace<sim::ColliderComponent>(obstacle, sim::ColliderComponent{});
    sim::FCLBackend robot_backend;
    robot_backend.build(robot_world);
    Require(!robot_backend.check_robot_configuration(robot_world, robot_root,
                                                      std::array{0.0}),
            "FCL reported a false robot configuration collision");
    Require(robot_backend.check_robot_configuration(robot_world, robot_root,
                                                     std::array{2.0}),
            "FCL missed a robot configuration collision");
    const auto batch = robot_backend.check_robot_configurations(
        robot_world, robot_root, std::array{0.0, 2.0}, 2);
    Require(batch.size() == 2 && !batch[0] && batch[1],
            "FCL batch robot configuration results mismatch");
#if defined(SIM_HAS_ASSIMP)
    sim::World mesh_world(4, 0.01);
    const auto mesh_entity = mesh_world.create_entity("mesh");
    sim::ColliderComponent mesh_collider;
    mesh_collider.shape = sim::ColliderShape::Mesh;
    mesh_collider.mesh_resource =
        (std::filesystem::path(SIM_TEST_DATA_DIR) / "triangle.obj").string();
    mesh_world.registry().emplace<sim::ColliderComponent>(mesh_entity, mesh_collider);
    const auto sphere_entity = mesh_world.create_entity("sphere");
    mesh_world.registry().get<sim::TransformComponent>(sphere_entity).local.translation =
        {0.25, 0.25, 0.05};
    mesh_world.registry().emplace<sim::ColliderComponent>(
        sphere_entity,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    sim::FCLBackend mesh_backend;
    mesh_backend.build(mesh_world);
    Require(mesh_backend.check_collision(mesh_entity, sphere_entity),
            "FCL Assimp triangle mesh BVH collision failed");
#endif
}
#endif

#if defined(SIM_HAS_VULKAN)
void TestVulkanOffscreenReadback() {
    sim::VulkanBackend backend;
    Require(!backend.device_name().empty() && backend.capabilities().rgb &&
                backend.capabilities().depth,
            "Vulkan initialization or capabilities mismatch");
    sim::World world(1, 0.01);
    const auto box = world.create_entity("rendered_box");
    world.registry().emplace<sim::ColliderComponent>(box, sim::ColliderComponent{});
    world.registry().get<sim::TransformComponent>(box).local.translation = {3.0, 0.0, 0.0};
    const auto light = world.create_entity("render_test_light");
    world.registry().emplace<sim::LightComponent>(light, sim::LightComponent{});
    backend.sync_from_scene(world);
    Require(backend.snapshot().instances.size() == 1,
            "Vulkan render snapshot did not contain the scene collider");
    sim::CameraComponent camera;
    camera.width = 64;
    camera.height = 64;
    camera.near_plane = 0.1;
    camera.far_plane = 10.0;
    std::vector<std::uint8_t> rgb;
    std::vector<float> depth;
    backend.render_camera(sim::Transform::Identity(), camera, rgb, depth);
    Require(rgb.size() == 64 * 64 * 3 && depth.size() == 64 * 64,
            "Vulkan offscreen readback dimensions mismatch");
    const auto center = static_cast<std::size_t>(32 * 64 + 32);
    Require(depth[center] > 2.0F && depth[center] < 3.0F,
            "Vulkan mesh rendering did not write linear center depth");
    Require(rgb[center * 3] != rgb[0] || rgb[center * 3 + 1] != rgb[1] ||
                rgb[center * 3 + 2] != rgb[2],
            "Vulkan mesh rendering did not shade the center pixel");

    auto& spot = world.registry().get<sim::LightComponent>(light);
    spot.type = sim::LightType::Spot;
    spot.inner_cone_radians = 0.2F;
    spot.outer_cone_radians = 0.4F;
    world.registry().get<sim::TransformComponent>(light).local.rotation =
        {0.7071067811865476, 0.0, -0.7071067811865476, 0.0};
    backend.sync_from_scene(world);
    backend.render_camera(sim::Transform::Identity(), camera, rgb, depth);
    Require(rgb[center * 3] != rgb[0] || rgb[center * 3 + 1] != rgb[1] ||
                rgb[center * 3 + 2] != rgb[2],
            "Vulkan spotlight did not illuminate along its rotated local -Z axis");
}
#endif

void TestColladaTransformsAndMaterials() {
#if defined(SIM_HAS_ASSIMP)
    auto asset = sim::AssimpImporter{}.import(
        std::filesystem::path(SIM_TEST_DATA_DIR) / "transformed.dae").asset;
    sim::AssetValidator::normalize(asset);
    Require(asset.meshes.size() == 2, "Collada lost material-separated mesh instances");
    bool blue = false, gray = false;
    for (const auto& mesh : asset.meshes) {
        const auto& color = asset.materials.at(mesh.material_index).base_color;
        blue = blue || sim::NearlyEqual(color[2], 0.8, 1e-6);
        gray = gray || sim::NearlyEqual(color[0], 0.7, 1e-6);
        for (const auto& point : mesh.positions) {
            Require(point.x >= 0.89999 && point.x <= 1.00001 &&
                    point.y >= 1.99999 && point.y <= 2.10001 &&
                    point.z >= 2.99999 && point.z <= 3.10001,
                    "Collada authored scale/rotation/translation or Z-up basis was lost");
        }
    }
    Require(blue && gray, "Collada material bindings were lost");
#endif
}

void TestExternalRobotVisualRegression() {
#if defined(SIM_HAS_ASSIMP) && defined(SIM_HAS_VULKAN)
    const char* source = std::getenv("SIM_TEST_ROBOT_URDF");
    if (!source) return;
    sim::ResourceManager resources(UniqueTempPath("robot-visual-cache"));
    const auto asset = resources.load(source).asset;
    sim::World world(1, 0.002);
    const auto root = resources.instantiate(world, asset);
    // Turn the robot across the editor's default camera for a useful side view.
    world.registry().get<sim::TransformComponent>(root).local.rotation =
        {0.7071067811865476, 0, 0, 0.7071067811865476};
    const auto light = world.create_entity("ambient");
    world.registry().emplace<sim::LightComponent>(light,
        sim::LightComponent{sim::LightType::Ambient, {1, 1, 1}, 1.0F});
    sim::VulkanBackend renderer;
    renderer.sync_from_scene(world);
    const auto& snapshot = renderer.snapshot();
    const auto size = snapshot.bounds_max - snapshot.bounds_min;
    const double extent = std::max({size.x, size.y, size.z});
    Require(extent > 0.5 && extent < 1.5, "robot visual bounds are not in metre scale");
    Require(renderer.consume_diagnostics().empty(), "robot mesh import reported errors");
    if (const char* output = std::getenv("SIM_TEST_ROBOT_STATE"))
        sim::SaveStateFile(output, {1, world.snapshot(), {}});
    std::cout << "robot visual extent: " << size.x << ", " << size.y << ", " << size.z << '\n';
#endif
}

void TestUrdfImportAndRuntimeInstantiation() {
    const auto path = std::filesystem::path(SIM_TEST_DATA_DIR) / "two_link.urdf";
    sim::UrdfImporter importer;
    const auto result = importer.import(path);
#if defined(SIM_HAS_URDFDOM)
    Require(result.asset.metadata.importer == "urdfdom", "urdfdom importer was not selected");
#endif
    Require(result.asset.links.size() == 2, "URDF link count mismatch");
    Require(result.asset.joints.size() == 1, "URDF joint count mismatch");
    Require(result.asset.inertials.size() == 2 && result.asset.colliders.size() == 2,
            "URDF inertial or collision geometry count mismatch");
    Require(result.asset.visuals.size() == 2 &&
                sim::NearlyEqual(result.asset.visuals.front().origin.translation, {0.01, 0.0, 0.0}),
            "URDF visual geometry or origin mismatch");
    Require(result.asset.joints.front().parent_link == "base_link", "URDF parent link mismatch");
    Require(sim::NearlyEqual(result.asset.joints.front().origin.translation, {0.0, 0.0, 1.0}),
            "URDF origin mismatch");

    const auto cache = UniqueTempPath("sim-platform-test-cache-urdf");
    std::filesystem::remove_all(cache);
    sim::ResourceManager resources(cache);
    sim::World world(1);
    const auto root = resources.instantiate(world, result.asset);
    Require(world.registry().has<sim::RobotComponent>(root), "runtime robot component missing");
    Require(world.registry().view<sim::JointComponent>().size() == 1,
            "runtime joint component missing");
    Require(world.registry().view<sim::RigidBodyComponent>().size() == 2 &&
            world.registry().view<sim::ColliderComponent>().size() == 2,
            "runtime inertial or collider component missing");
    Require(world.registry().view<sim::VisualComponent>().size() == 2,
            "runtime visual components missing");
    std::filesystem::remove_all(cache);
}

void TestUrdfRelativeMeshPathsBecomeAbsolute() {
    const auto path = std::filesystem::path(SIM_TEST_DATA_DIR) / "mesh_two_link.urdf";
    const auto result = sim::UrdfImporter{}.import(path);
    Require(!result.asset.colliders.empty(), "mesh URDF did not import collision geometry");
    for (const auto& collider : result.asset.colliders) {
        Require(collider.mesh.is_absolute(),
                "relative URDF mesh path was not normalized to an absolute path");
    }

    const auto bundle = UniqueTempPath("sim-platform-test-ros-package-bundle");
    const auto urdf = bundle / "urdf" / "robot.urdf";
    const auto mesh = bundle / "meshes" / "link.stl";
    std::filesystem::remove_all(bundle);
    std::filesystem::create_directories(urdf.parent_path());
    std::filesystem::create_directories(mesh.parent_path());
    std::filesystem::copy_file(std::filesystem::path(SIM_TEST_DATA_DIR) / "triangle.stl", mesh);
    {
        std::ofstream output(urdf);
        output << R"(<robot name="package_robot"><link name="base"><collision><geometry>)"
               << R"(<mesh filename="package://jaka_description/meshes/link.stl"/>)"
               << R"(</geometry></collision></link></robot>)";
    }
    const auto package_result = sim::UrdfImporter{}.import(urdf);
    Require(!package_result.asset.colliders.empty() &&
                package_result.asset.colliders.front().mesh == std::filesystem::absolute(mesh),
            "package URI did not resolve from a content-addressed bundle root");
    std::filesystem::remove_all(bundle);
}

void TestObjImportAndBakingCache() {
    const auto source = std::filesystem::path(SIM_TEST_DATA_DIR) / "triangle.obj";
    const auto root = UniqueTempPath("sim-platform-test-cache-obj");
    const auto cache = root / "cache";
    const auto copy = root / "triangle.obj";
    const auto material = root / "triangle.mtl";
    const auto texture = root / "triangle.png";
    std::filesystem::remove_all(root);
    std::filesystem::create_directories(root);
    std::filesystem::copy_file(source, copy);
    std::filesystem::copy_file(std::filesystem::path(SIM_TEST_DATA_DIR) / "triangle.mtl", material);
    {
        std::ofstream output(material, std::ios::app);
        output << "map_Kd triangle.png\n";
    }
    {
        std::ofstream output(texture, std::ios::binary);
        output << "test texture payload";
    }

    sim::ResourceManager resources(cache);
    const auto first = resources.load(copy);
    Require(!first.from_cache, "first asset load cannot be a cache hit");
    Require(first.asset.meshes.size() == 1, "OBJ mesh count mismatch");
    Require(first.asset.meshes.front().indices.size() == 3, "OBJ triangulation mismatch");
    Require(first.asset.materials.size() == 1 &&
                first.asset.materials.front().name == "default" &&
                sim::NearlyEqual(first.asset.materials.front().base_color[0], 0.8),
            "OBJ MTL material was not imported");
    Require(first.asset.meshes.front().material_index == 0 &&
                first.asset.materials.front().base_color_texture ==
                    std::filesystem::absolute(texture).lexically_normal() &&
                first.asset.textures.size() == 1,
            "OBJ usemtl or MTL texture was not imported");
    Require(!first.asset.metadata.dependencies.empty() &&
                std::all_of(first.asset.metadata.dependencies.begin(),
                            first.asset.metadata.dependencies.end(),
                            [](const auto& dependency) { return dependency.is_absolute(); }),
            "OBJ dependencies must be normalized to absolute paths");
    {
        sim::World world(42, 0.01);
        const auto root = resources.instantiate(world, first.asset, "obj_mesh");
        (void)root;
        const auto mesh_colliders = world.registry().view<sim::ColliderComponent>();
        Require(mesh_colliders.size() == 1,
                "mesh-only OBJ instantiation must create a default triangle collider");
        const auto collider = world.registry().get<sim::ColliderComponent>(*mesh_colliders.begin());
        Require(collider.shape == sim::ColliderShape::Mesh &&
                    collider.mesh_resource.find("#mesh=0") != std::string::npos,
                "default OBJ collider must reference the imported triangle mesh");
    }
    const auto second = resources.load(copy);
    Require(second.from_cache, "second asset load should hit baking cache");
    const auto asynchronous = resources.load_async(copy).get();
    Require(asynchronous.from_cache && asynchronous.asset.meshes.size() == 1,
            "asynchronous resource load did not return the cached asset");

    {
        std::ofstream output(material, std::ios::app);
        output << "\n# dependency invalidates cache\n";
    }
    const auto dependency_changed = resources.load(copy);
    Require(!dependency_changed.from_cache, "modified dependency must invalidate baking cache");
    const auto after_dependency = resources.load(copy);
    Require(after_dependency.from_cache, "rebaked dependency should produce a cache hit");

    {
        std::ofstream output(copy, std::ios::app);
        output << "\n# invalidates cache\n";
    }
    const auto third = resources.load(copy);
    Require(!third.from_cache, "modified source must invalidate baking cache");
    std::filesystem::remove_all(root);
}

#if defined(SIM_HAS_ASSIMP)
void TestAssimpImport() {
    const auto obj_source = std::filesystem::path(SIM_TEST_DATA_DIR) / "triangle.obj";
    const auto obj = sim::AssimpImporter{}.import(obj_source);
    Require(obj.asset.metadata.importer == "assimp", "Assimp metadata mismatch");
    Require(!obj.asset.nodes.empty() && obj.asset.meshes.size() == 1,
            "Assimp did not create the expected Asset IR hierarchy and mesh");
    Require(obj.asset.meshes.front().indices.size() == 3 &&
                obj.asset.meshes.front().normals.size() == 3,
            "Assimp triangulation or normal generation mismatch");
    const auto fbx_source = std::filesystem::path(SIM_TEST_DATA_DIR) / "phong_cube.fbx";
    const auto fbx = sim::AssimpImporter{}.import(fbx_source);
    Require(fbx.asset.metadata.importer == "assimp" && !fbx.asset.meshes.empty() &&
                !fbx.asset.nodes.empty(),
            "Assimp did not import the FBX fixture into Asset IR");
    const auto cache = UniqueTempPath("sim-platform-test-cache-assimp");
    std::filesystem::remove_all(cache);
    const auto stl_source = std::filesystem::path(SIM_TEST_DATA_DIR) / "triangle.stl";
    sim::ResourceManager resources(cache);
    const auto stl = resources.load(stl_source);
    Require(stl.asset.metadata.importer == "assimp" && stl.asset.meshes.size() == 1 &&
                stl.asset.meshes.front().indices.size() == 3,
            "ResourceManager did not import STL through Assimp");
    const auto unicode_directory = cache / std::filesystem::path(u8"模型素材");
    std::filesystem::create_directories(unicode_directory);
    const auto unicode_fbx = unicode_directory / "phong_cube.fbx";
    std::filesystem::copy_file(fbx_source, unicode_fbx,
                               std::filesystem::copy_options::overwrite_existing);
    const auto unicode_result = resources.load(unicode_fbx);
    Require(!unicode_result.asset.meshes.empty(),
            "Assimp could not import an FBX through a Unicode filesystem path");
    const auto formats = resources.supported_formats();
    Require(std::find(formats.begin(), formats.end(), ".fbx") != formats.end(),
            "Assimp-enabled ResourceManager does not advertise FBX");
    Require(std::find(formats.begin(), formats.end(), ".stl") != formats.end(),
            "Assimp-enabled ResourceManager does not advertise STL");
#if defined(SIM_HAS_VULKAN)
    sim::World world(3, 0.01);
    const auto normalized_fbx = resources.load(fbx_source);
    const auto root = resources.instantiate(world, normalized_fbx.asset, "fbx_box");
    world.registry().get<sim::TransformComponent>(root).local.translation = {3.0, 0.0, 0.0};
    world.registry().get<sim::TransformComponent>(root).local.scale = {100.0, 100.0, 100.0};
    sim::VulkanBackend renderer;
    renderer.sync_from_scene(world);
    Require(!renderer.snapshot().instances.empty(),
            "FBX to ECS to Vulkan render snapshot integration failed");
    Require(renderer.snapshot().has_bounds &&
                renderer.snapshot().instances.front().mesh_resource.find("#mesh=") != std::string::npos,
            "FBX render snapshot did not preserve per-node mesh selection or scene bounds");
    sim::World unicode_world(4, 0.01);
    const auto unicode_root = resources.instantiate(unicode_world, unicode_result.asset, "unicode_fbx");
    unicode_world.registry().get<sim::TransformComponent>(unicode_root).local.translation = {3.0, 0.0, 0.0};
    unicode_world.registry().get<sim::TransformComponent>(unicode_root).local.scale = {100.0, 100.0, 100.0};
    renderer.sync_from_scene(unicode_world);
    Require(renderer.consume_diagnostics().empty(),
            "Vulkan mesh upload failed for a Unicode FBX path");
    sim::CameraComponent camera;
    camera.width = 64;
    camera.height = 64;
    camera.near_plane = 0.1;
    camera.far_plane = 20.0;
    std::vector<std::uint8_t> rgb;
    std::vector<float> depth;
    renderer.render_camera(sim::Transform::Identity(), camera, rgb, depth);
    Require(*std::min_element(depth.begin(), depth.end()) < 20.0F,
            "FBX to ECS to Vulkan integration did not render geometry");
#endif
    std::filesystem::remove_all(cache);
}
#endif

void TestMeshLodGeneration() {
    sim::MeshData mesh;
    constexpr std::uint32_t side = 24;
    for (std::uint32_t y = 0; y <= side; ++y)
        for (std::uint32_t x = 0; x <= side; ++x)
            mesh.positions.push_back({static_cast<double>(x) / side,
                                      static_cast<double>(y) / side,
                                      0.05 * std::sin(static_cast<double>(x + y))});
    for (std::uint32_t y = 0; y < side; ++y) {
        for (std::uint32_t x = 0; x < side; ++x) {
            const auto first = y * (side + 1) + x;
            const auto second = first + side + 1;
            mesh.indices.insert(mesh.indices.end(),
                {first, second, first + 1, first + 1, second, second + 1});
        }
    }
    const auto lod = sim::GenerateMeshLod(mesh, 7);
    Require(lod.positions.size() < mesh.positions.size() &&
            lod.indices.size() < mesh.indices.size() && !lod.indices.empty(),
            "coarse LOD did not reduce a dense mesh");
    for (const auto index : lod.indices)
        Require(index < lod.positions.size(), "LOD generated an invalid triangle index");
}

void TestRobotControlKinematicsAndLimits() {
    const auto path = std::filesystem::path(SIM_TEST_DATA_DIR) / "two_link.urdf";
    sim::UrdfImporter importer;
    const auto asset = importer.import(path).asset;
    const auto cache = UniqueTempPath("sim-platform-test-cache-robot");
    std::filesystem::remove_all(cache);
    sim::ResourceManager resources(cache);
    sim::World world(1, 0.01);
    const auto root = resources.instantiate(world, asset);
    sim::InstallRobotControlSystem(world);
    sim::Robot robot(world, root);
    Require(robot.joints().size() == 1, "robot joint mapping mismatch");
    robot.set_joint_positions(std::array{0.25});
    Require(sim::NearlyEqual(robot.joint_state().positions.front(), 0.25),
            "direct joint state set failed");
    RequireError(sim::ErrorCode::ValidationFailed, [&] {
        robot.command_joint_positions(std::array{2.0});
    });
    robot.command_joint_positions(std::array{1.0}, 10.0, 2.0);
    world.step(10);
    const auto state = robot.joint_state();
    Require(state.positions.front() > 0.25 && state.positions.front() <= 1.0,
            "position controller did not advance toward target");
    const auto joint_entity = robot.joints().front();
    const auto pose = robot.forward_kinematics(joint_entity);
    Require(sim::NearlyEqual(pose.translation, {0.0, 0.0, 1.0}),
            "forward kinematics translation mismatch");
    const auto jacobian = robot.jacobian(joint_entity);
    Require(jacobian.size() == 1 && sim::NearlyEqual(jacobian.front()[4], 1.0),
            "revolute joint Jacobian axis mismatch");
    std::filesystem::remove_all(cache);
}

class DeterministicSensorProvider final : public sim::ISensorDataProvider {
public:
    void render_camera(const sim::Transform&, const sim::CameraComponent& camera,
                       std::vector<std::uint8_t>& rgb,
                       std::vector<float>& depth) override {
        ++camera_calls;
        const auto pixels = static_cast<std::size_t>(camera.width) * camera.height;
        rgb.assign(pixels * 3, 127);
        depth.assign(pixels, 2.5F);
    }

    std::vector<float> raycast(const sim::Vec3&, std::span<const sim::Vec3> directions,
                               double, double) override {
        ++lidar_calls;
        return std::vector<float>(directions.size(), 3.0F);
    }

    int camera_calls{0};
    int lidar_calls{0};
};

void TestSensorSchedulingAndBuffers() {
    sim::World world(1, 0.01);
    const auto root = world.create_entity("root");
    auto provider = std::make_shared<DeterministicSensorProvider>();
    sim::SensorSystem sensors(world, provider);
    const auto camera = sensors.create_camera("camera", root, "camera_optical", 50.0,
                                               {.width = 2, .height = 2});
    const auto rgb_camera = sensors.create_camera("rgb_camera", root, "rgb_optical", 50.0,
        {.width = 2, .height = 2, .output = sim::CameraOutput::RGB});
    const auto lidar = sensors.create_lidar("lidar", root, "lidar", 20.0,
        {.horizontal_samples = 4, .vertical_channels = 2, .min_distance = 0.1,
         .max_distance = 10.0});
    world.step(5);
    Require(provider->camera_calls == 6, "camera multi-frequency scheduling mismatch");
    Require(provider->lidar_calls == 1, "LiDAR multi-frequency scheduling mismatch");
    const auto& camera_latest = sensors.buffer(camera).latest();
    Require(std::holds_alternative<sim::DepthFrame>(camera_latest),
            "camera buffer should contain paired RGB/depth frames");
    const auto& depth = std::get<sim::DepthFrame>(camera_latest);
    Require(depth.frame_id == "camera_optical" && depth.linear_depth_meters.size() == 4,
            "depth frame metadata or storage mismatch");
    Require(std::holds_alternative<sim::RGBFrame>(sensors.buffer(rgb_camera).latest()),
            "RGB camera should publish color without a depth frame");
    const auto& scan = std::get<sim::LiDARFrame>(sensors.buffer(lidar).latest());
    Require(scan.points.size() == 8 && sim::NearlyEqual(scan.points.front().x * scan.points.front().x +
                                                        scan.points.front().y * scan.points.front().y +
                                                        scan.points.front().z * scan.points.front().z,
                                                        9.0, 1e-6),
            "LiDAR point generation mismatch");
}

void TestPluginManifestAndDiscovery() {
    const auto path = std::filesystem::path(SIM_TEST_DATA_DIR) / "sample.simplugin.json";
    const auto manifest = sim::PluginManager::parse_manifest(path);
    Require(manifest.name == "sim.collision.sample" && manifest.plugin_api_version == 2,
            "plugin manifest identity mismatch");
    Require(manifest.capabilities.at("collision_query"), "plugin capability parsing mismatch");
    Require(!manifest.capabilities.at("state_serialization"), "false plugin capability mismatch");
    sim::PluginManager manager;
    manager.discover(SIM_TEST_DATA_DIR);
    Require(manager.manifests().size() == 1, "plugin discovery count mismatch");
    RequireError(sim::ErrorCode::IoError, [&] { manager.load("sim.collision.sample"); });
}

void TestDynamicPluginLoadAndUnload() {
    const auto root = UniqueTempPath("sim-platform-dynamic-plugin-test");
    std::filesystem::remove_all(root);
    std::filesystem::create_directories(root);
    const auto source = std::filesystem::path(SIM_TEST_PLUGIN_PATH);
    const auto library = root / source.filename();
    std::filesystem::copy_file(source, library);
    const auto manifest_path = root / "test.simplugin.json";
    {
        std::ofstream manifest(manifest_path, std::ios::trunc);
        manifest << "{\n"
                 << "  \"name\": \"sim.test.plugin\",\n"
                 << "  \"version\": \"0.1.0\",\n"
                 << "  \"plugin_api_version\": 2,\n"
                 << "  \"type\": \"physics_backend\",\n"
                 << "  \"library\": \"" << library.filename().string() << "\",\n"
                 << "  \"platforms\": [\"" << sim::PluginManager::current_platform() << "\"]\n"
                 << "}\n";
    }
    sim::PluginManager manager;
    manager.discover(root);
    manager.load("sim.test.plugin");
    Require(manager.loaded("sim.test.plugin"), "dynamic plugin did not enter loaded state");
    auto physics = manager.create_physics_backend("sim.test.plugin");
    Require(physics->capabilities().rigid_body_dynamics &&
                physics->save_state() == std::vector<std::byte>{std::byte{42}},
            "physics plugin factory returned an invalid backend");
    RequireError(sim::ErrorCode::InvalidState,
                 [&] { manager.unload("sim.test.plugin"); });
    physics.reset();
    Require(manager.unload("sim.test.plugin") && !manager.loaded("sim.test.plugin"),
            "dynamic plugin did not unload cleanly");
    std::filesystem::remove_all(root);
}

void TestIFPSTreeInvariants() {
    class InspectableIFPSBackend final : public sim::IFPSCollisionBackend {
    public:
        using sim::IFPSCollisionBackend::IFPSCollisionBackend;
        [[nodiscard]] const sim::IFPSSphereTree& tree(sim::EntityId entity) const {
            return object(entity).tree;
        }
        [[nodiscard]] const sim::IFPSSphereTreeCache& cache() const {
            return tree_cache();
        }
    };
    sim::World world(60, 0.01, 0);
    const auto entity = world.create_entity("ifps-count");
    sim::ColliderComponent collider{sim::ColliderShape::Box, {2.0, 1.0, 0.5}};
    collider.ifps_sphere_count = 100;
    world.registry().emplace<sim::ColliderComponent>(entity, collider);
    InspectableIFPSBackend backend;
    backend.build(world);
    const auto& tree = backend.tree(entity);
    Require(tree.leaf_sphere_count == 100 && tree.nodes.size() == 173,
            "IFPS did not honor the collider's partially filled 1/8/64/100 hierarchy");
    Require(tree.root == 0 && sim::NearlyEqual(tree.nodes[0].center, sim::Vec3{}),
            "IFPS root must be the prepended origin sample");
    for (std::size_t index = 0; index < tree.nodes.size(); ++index) {
        const auto& node = tree.nodes[index];
        Require(node.child_count == 0 ||
                    node.first_child + node.child_count <= tree.nodes.size(),
                "IFPS child range is invalid");
        for (std::uint32_t child = 0; child < node.child_count; ++child) {
            const auto& candidate = tree.nodes.at(node.first_child + child);
            Require(candidate.radius > 0.0, "IFPS inverse-FPS radius must be positive");
        }
    }
    Require(backend.cache().cached_tree_count() == 1 &&
                backend.cache().generated_leaf_count(collider) == 100,
            "IFPS cache did not retain its initial generated prefix");

    world.registry().get<sim::ColliderComponent>(entity).ifps_sphere_count = 64;
    backend.build(world);
    Require(backend.tree(entity).leaf_sphere_count == 64 &&
                backend.cache().cached_tree_count() == 2 &&
                backend.cache().generated_leaf_count(collider) == 100,
            "IFPS smaller request regenerated or discarded the larger prefix");

    world.registry().get<sim::ColliderComponent>(entity).ifps_sphere_count = 128;
    backend.build(world);
    Require(backend.tree(entity).leaf_sphere_count == 128 &&
                backend.cache().cached_tree_count() == 3 &&
                backend.cache().generated_leaf_count(collider) == 128,
            "IFPS larger request did not incrementally extend the cached prefix");

    world.registry().get<sim::ColliderComponent>(entity).ifps_sphere_count = 100;
    backend.build(world);
    Require(backend.tree(entity).leaf_sphere_count == 100 &&
                backend.cache().cached_tree_count() == 3 &&
                backend.cache().generated_leaf_count(collider) == 128,
            "IFPS repeated request did not reuse the retained tree copy");
}

#if defined(SIM_HAS_IFPS_CPU_PLUGIN)
void TestIFPSCollisionPlugins() {
    sim::ColliderComponent exact_sphere;
    exact_sphere.shape = sim::ColliderShape::Sphere;
    exact_sphere.size = {0.75, 0.75, 0.75};
    const auto exact_tree = sim::BuildIFPSSphereTree(exact_sphere);
    Require(exact_tree.nodes.size() == 1 && exact_tree.root == 0 &&
                exact_tree.leaf_sphere_count == 1 &&
                sim::NearlyEqual(exact_tree.nodes[0].radius, 0.75),
            "IFPS sphere primitive should use one exact sphere");

    sim::World world(61, 0.01, 0);
    const auto first = world.create_entity("first");
    const auto second = world.create_entity("second");
    world.registry().emplace<sim::ColliderComponent>(
        first, sim::ColliderComponent{sim::ColliderShape::Sphere, {1.0, 1.0, 1.0}});
    world.registry().emplace<sim::ColliderComponent>(
        second, sim::ColliderComponent{sim::ColliderShape::Sphere, {1.0, 1.0, 1.0}});
    world.registry().get<sim::TransformComponent>(second).local.translation = {1.5, 0.0, 0.0};

    sim::PluginManager cpu_plugins;
    cpu_plugins.discover(SIM_TEST_IFPS_CPU_PLUGIN_DIR);
    auto cpu = cpu_plugins.create_collision_backend("sim.collision.ifps-cpu");
    cpu->build(world);
    Require(cpu->capabilities().collision_query && cpu->check_collision(first, second),
            "IFPS-CPU missed overlapping spheres");
    world.registry().get<sim::TransformComponent>(second).local.translation = {3.0, 0.0, 0.0};
    cpu->sync_from_scene(world);
    Require(!cpu->check_collision(first, second) && cpu->distance(first, second) > 0.0,
            "IFPS-CPU transform sync or separation query failed");
    const auto cpu_separated = cpu->check_scene_collisions(std::array{first, second});
    Require(cpu_separated.size() == 2 && !cpu_separated[0] && !cpu_separated[1],
            "IFPS-CPU batch separation result mismatch");

    sim::World coverage_world(62, 0.01, 0);
    const auto floor = coverage_world.create_entity("coverage-floor");
    const auto resting_ball = coverage_world.create_entity("coverage-ball");
    coverage_world.registry().emplace<sim::ColliderComponent>(
        floor, sim::ColliderComponent{sim::ColliderShape::Box, {1.0, 1.0, 1.0}});
    coverage_world.registry().emplace<sim::ColliderComponent>(
        resting_ball, sim::ColliderComponent{sim::ColliderShape::Sphere, {0.5, 0.5, 0.5}});
    auto& floor_transform =
        coverage_world.registry().get<sim::TransformComponent>(floor).local;
    floor_transform.translation = {0.0, 0.0, -4.99};
    floor_transform.scale = {20.0, 20.0, 10.0};
    auto& ball_transform =
        coverage_world.registry().get<sim::TransformComponent>(resting_ball).local;
    ball_transform.translation = {-5.503090395076964, 1.5237739920749196,
                                  0.2596328181618229};
    ball_transform.scale = {0.5, 0.5, 0.5};
    auto coverage_cpu = cpu_plugins.create_collision_backend("sim.collision.ifps-cpu");
    coverage_cpu->build(coverage_world);
    Require(coverage_cpu->check_collision(resting_ball, floor),
            "IFPS-CPU missed the benchmark's known near-contact position");

#if defined(SIM_HAS_IFPS_GPU_PLUGIN)
    sim::PluginManager gpu_plugins;
    gpu_plugins.discover(SIM_TEST_IFPS_GPU_PLUGIN_DIR);
    auto gpu = gpu_plugins.create_collision_backend("sim.collision.ifps-gpu");
    gpu->build(world);
    Require(gpu->check_collision(first, second) == cpu->check_collision(first, second),
            "IFPS-GPU separated result differs from IFPS-CPU");
    Require(gpu->check_scene_collisions(std::array{first, second}) == cpu_separated,
            "IFPS-GPU batch separation result differs from IFPS-CPU");
    auto coverage_gpu = gpu_plugins.create_collision_backend("sim.collision.ifps-gpu");
    coverage_gpu->build(coverage_world);
    Require(coverage_gpu->check_collision(resting_ball, floor),
            "IFPS-GPU missed the benchmark's known near-contact position");
    world.registry().get<sim::TransformComponent>(second).local.translation = {1.5, 0.0, 0.0};
    cpu->sync_from_scene(world); gpu->sync_from_scene(world);
    Require(gpu->check_collision(first, second) == cpu->check_collision(first, second),
            "IFPS-GPU overlapping result differs from IFPS-CPU");
    const auto cpu_overlapping = cpu->check_scene_collisions(std::array{first, second});
    Require(gpu->check_scene_collisions(std::array{first, second}) == cpu_overlapping &&
                cpu_overlapping[0] && cpu_overlapping[1],
            "IFPS-GPU batch overlap result differs from IFPS-CPU");

    std::vector<sim::EntityId> dense_batch{first, second};
    for (std::size_t index = 0; index < 180; ++index) {
        const auto entity = world.create_entity("dense-" + std::to_string(index));
        world.registry().emplace<sim::ColliderComponent>(
            entity, sim::ColliderComponent{sim::ColliderShape::Sphere, {1.0, 1.0, 1.0}});
        dense_batch.push_back(entity);
    }
    cpu->build(world);
    gpu->build(world);
    const auto cpu_dense = cpu->check_scene_collisions(dense_batch);
    Require(gpu->check_scene_collisions(dense_batch) == cpu_dense &&
                std::ranges::all_of(cpu_dense, [](std::uint8_t value) { return value != 0U; }),
            "IFPS-GPU CUDA batch result differs from IFPS-CPU");
#endif
}
#endif

void TestFoundationServices() {
    const auto config = sim::Config::load(std::filesystem::path(SIM_TEST_DATA_DIR) / "test-config.toml");
    Require(sim::NearlyEqual(config.number("simulation.fixed_time_step"), 0.002),
            "numeric configuration mismatch");
    Require(config.boolean("simulation.headless"), "boolean configuration mismatch");
    Require(config.string("backends.physics") == "mujoco", "string configuration mismatch");

    sim::Logger logger;
    int received = 0;
    const auto sink = logger.add_sink([&](const sim::LogRecord& record) {
        if (record.category == "test") ++received;
    });
    logger.log(sim::LogLevel::Info, "test", "message");
    Require(received == 1 && logger.remove_sink(sink), "logger sink lifecycle mismatch");

    sim::Profiler profiler;
    {
        sim::ScopedProfile sample(profiler, "unit");
    }
    Require(profiler.snapshot().at("unit").calls == 1, "profiler sample mismatch");
}

void TestVersionedStateFileRoundTrip() {
    sim::World world(1, 0.01, 123);
    const auto root = world.create_entity("root");
    const auto child = world.create_entity("child", root);
    world.registry().emplace<sim::VisualComponent>(child,
        sim::VisualComponent{"mesh", "material", true});
    world.registry().get<sim::VisualComponent>(child).min_lod = 2;
    world.registry().get<sim::VisualComponent>(child).max_lod = 5;
    world.registry().emplace<sim::ColliderComponent>(child,
        sim::ColliderComponent{sim::ColliderShape::Box, {1.0, 2.0, 3.0}, {}, false,
                               {0.7, 0.02, 0.003}, 0.65, 512});
    world.registry().emplace<sim::RigidBodyComponent>(child,
        sim::RigidBodyComponent{2.0, {}, {}, true, false});
    world.registry().emplace<sim::SensorComponent>(child,
        sim::SensorComponent{"test_camera", 30.0, 0.25});
    world.registry().emplace<sim::CameraComponent>(child,
        sim::CameraComponent{848, 480, 1.2, 0.1, 12.0, sim::CameraOutput::RGB});
    world.frames().set_frame("base", "world", {.translation = {1.0, 2.0, 3.0}});
    world.step(3);
    sim::SimulationStateArchive archive{1, world.snapshot(), {std::byte{1}, std::byte{2}}};
    const auto path = UniqueTempPath("sim-platform-state-roundtrip.bin");
    sim::SaveStateFile(path, archive);
    const auto loaded = sim::LoadStateFile(path);
    Require(loaded.version == 1 && loaded.world.registry.valid(root) &&
            loaded.world.registry.valid(child), "state file did not preserve entity handles");
    Require(loaded.world.registry.get<sim::ObjectIdentityComponent>(root).value ==
                world.object_id(root) &&
            loaded.world.registry.get<sim::ObjectIdentityComponent>(child).value ==
                world.object_id(child),
            "state file did not preserve stable object ids");
    Require(loaded.world.registry.has<sim::VisualComponent>(child) &&
            loaded.world.registry.has<sim::ColliderComponent>(child),
            "state file did not preserve optional components");
    Require(loaded.world.registry.get<sim::VisualComponent>(child).min_lod == 2 &&
            loaded.world.registry.get<sim::VisualComponent>(child).max_lod == 5,
            "state file did not preserve the visual LOD range");
    Require(sim::NearlyEqual(loaded.world.registry.get<sim::ColliderComponent>(child).friction,
                             {0.7, 0.02, 0.003}) &&
            sim::NearlyEqual(loaded.world.registry.get<sim::ColliderComponent>(child).restitution,
                             0.65) &&
            loaded.world.registry.get<sim::ColliderComponent>(child).ifps_sphere_count == 512,
            "state file did not preserve collider properties");
    Require(loaded.world.registry.has<sim::RigidBodyComponent>(child) &&
            !loaded.world.registry.get<sim::RigidBodyComponent>(child).gravity_enabled,
            "state file did not preserve per-body gravity state");
    Require(loaded.world.registry.has<sim::CameraComponent>(child) &&
            loaded.world.registry.get<sim::CameraComponent>(child).output ==
                sim::CameraOutput::RGB &&
            loaded.world.registry.get<sim::CameraComponent>(child).width == 848,
            "state file did not preserve camera sensor configuration");
    Require(loaded.world.registry.get<sim::HierarchyComponent>(child).parent == root,
            "state file did not preserve hierarchy");
    Require(sim::NearlyEqual(loaded.world.frames.lookup("base", "world").translation,
                             {1.0, 2.0, 3.0}), "state file did not preserve frame tree");
    Require(sim::NearlyEqual(loaded.world.time.simulation_time, 0.03) &&
            loaded.backend_state == archive.backend_state,
            "state file did not preserve time or backend state");
    std::filesystem::remove(path);
}

#if defined(SIM_HAS_MUJOCO)
void TestMuJoCoPhysicsStateAndRaycast() {
    const auto path = std::filesystem::path(SIM_TEST_DATA_DIR) / "two_link.urdf";
    const auto asset = sim::UrdfImporter{}.import(path).asset;
    const auto cache = UniqueTempPath("sim-platform-test-cache-mujoco");
    std::filesystem::remove_all(cache);
    sim::ResourceManager resources(cache);
    sim::World world(1, 0.002);
    const auto root = resources.instantiate(world, asset);
    const auto obstacle = world.create_entity("obstacle");
    world.registry().get<sim::TransformComponent>(obstacle).local.translation = {2.0, 0.0, 0.0};
    world.registry().emplace<sim::ColliderComponent>(
        obstacle, sim::ColliderComponent{sim::ColliderShape::Sphere, {0.5, 0.5, 0.5}, {}, false});
    const auto body = world.create_entity("forced_body");
    world.registry().get<sim::TransformComponent>(body).local.translation = {0.0, 0.0, 3.0};
    world.registry().emplace<sim::ColliderComponent>(
        body, sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    world.registry().emplace<sim::RigidBodyComponent>(body, sim::RigidBodyComponent{});
    world.update_world_transforms();

    sim::MuJoCoBackend backend;
    sim::Robot held_robot(world, root);
    held_robot.set_joint_positions(std::array{0.5});
    backend.build(world);
    for (int index = 0; index < 250; ++index) backend.step(world);
    Require(std::abs(held_robot.joint_state().positions.front() - 0.5) < 1.0e-3,
            "MuJoCo uncommanded robot joint did not hold its imported pose");
    held_robot.set_joint_positions(std::array{0.0});
    backend.build(world);
    const auto initial = backend.save_state();
    backend.apply_body_wrench(body, {1000.0, 0.0, 0.0});
    backend.step(world);
    backend.step(world);
    Require(world.registry().get<sim::TransformComponent>(body).world.translation.x > 0.0,
            "MuJoCo external body force did not move the body");
    backend.restore_state(initial);
    backend.sync_to_scene(world);
    sim::Robot robot(world, root);
    robot.command_joint_positions(std::array{0.5}, 2.0, 10.0);
    for (int index = 0; index < 50; ++index) backend.step(world);
    Require(std::abs(robot.joint_state().positions.front()) > 1e-6,
            "MuJoCo position control did not change joint state");
    backend.restore_state(initial);
    backend.sync_to_scene(world);
    Require(sim::NearlyEqual(robot.joint_state().positions.front(), 0.0, 1e-9),
            "MuJoCo state restore did not restore joint position");
    const auto distances = backend.raycast({1.0, 0.0, 0.0}, std::array{sim::Vec3{1.0, 0.0, 0.0}},
                                           0.0, 10.0);
    Require(distances.size() == 1 && distances.front() > 0.4F && distances.front() < 0.6F,
            "MuJoCo raycast distance mismatch");

    // Lightweight finger-sized hinges must track bounded speed without the old
    // damping=1 consuming their complete finite-effort budget before contact.
    sim::World servo_world(11, 0.002);
    const auto servo_root = servo_world.create_entity("servo_root");
    servo_world.registry().emplace<sim::RobotComponent>(servo_root);
    const auto finger = servo_world.create_entity("finger", servo_root);
    sim::JointComponent finger_joint;
    finger_joint.type = sim::JointType::Revolute;
    finger_joint.lower_limit = 0.0;
    finger_joint.upper_limit = 0.8;
    servo_world.registry().emplace<sim::JointComponent>(finger, finger_joint);
    auto& finger_control = servo_world.registry().emplace<sim::JointControlComponent>(finger);
    finger_control.mode = sim::JointControlMode::Position;
    finger_control.target = 0.8;
    finger_control.position_gain = 20.0;
    finger_control.velocity_limit = 1.6;
    finger_control.max_effort = 0.18;
    sim::MuJoCoBackend servo_backend;
    servo_backend.build(servo_world);
    // Before native servo actuators existed the same integration state omitted
    // CTRL. This one-hinge model has time, qpos, qvel, then qacc_warmstart before
    // its single CTRL slot (no activation/history fields).
    auto legacy_servo_state = servo_backend.save_state();
    const std::size_t ctrl_offset = 2 * sizeof(std::uint32_t) + 4 * sizeof(double);
    legacy_servo_state.erase(legacy_servo_state.begin() + ctrl_offset,
                            legacy_servo_state.begin() + ctrl_offset + sizeof(double));
    std::uint32_t legacy_count{};
    std::memcpy(&legacy_count, legacy_servo_state.data() + sizeof(std::uint32_t), sizeof(legacy_count));
    --legacy_count;
    std::memcpy(legacy_servo_state.data() + sizeof(std::uint32_t), &legacy_count, sizeof(legacy_count));
    servo_backend.restore_state(legacy_servo_state);
    servo_backend.sync_to_scene(servo_world);
    Require(sim::NearlyEqual(servo_world.registry().get<sim::JointComponent>(finger).position, 0.0),
            "MuJoCo legacy state without native servo CTRL did not restore joint pose");
    for (int index = 0; index < 500; ++index) servo_backend.step(servo_world);
    const auto& reached_finger = servo_world.registry().get<sim::JointComponent>(finger);
    Require(reached_finger.position > 0.79 && std::abs(reached_finger.velocity) < 0.03,
            "MuJoCo finite-effort finger servo stalled or oscillated without contact");

#if defined(SIM_HAS_URDFDOM)
    sim::World mimic_world(12, 0.002);
    const auto mimic_asset = sim::UrdfImporter{}.import(
        std::filesystem::path(SIM_TEST_DATA_DIR) / "mimic_gripper.urdf").asset;
    (void)resources.instantiate(mimic_world, mimic_asset);
    sim::EntityId mimic_driver = sim::kInvalidEntity;
    sim::EntityId mimic_follower = sim::kInvalidEntity;
    for (const auto entity : mimic_world.registry().view<sim::JointComponent>()) {
        const bool driver = mimic_world.registry().get<sim::JointComponent>(entity).name == "driver_joint";
        auto& control = mimic_world.registry().emplace<sim::JointControlComponent>(entity);
        control.mode = driver ? sim::JointControlMode::Position : sim::JointControlMode::Effort;
        control.target = driver ? 0.4 : 0.2; // Follower load tries to violate q_follower = -q_driver.
        control.max_effort = 1.0;
        if (driver) mimic_driver = entity;
        else mimic_follower = entity;
    }
    sim::MuJoCoBackend mimic_backend;
    mimic_backend.build(mimic_world);
    for (int index = 0; index < 500; ++index) mimic_backend.step(mimic_world);
    const double driver_position = mimic_world.registry().get<sim::JointComponent>(mimic_driver).position;
    const double follower_position = mimic_world.registry().get<sim::JointComponent>(mimic_follower).position;
    Require(std::abs(driver_position - 0.4) < 0.01 &&
            std::abs(driver_position + follower_position) < 0.001,
            "MuJoCo lost the authored URDF mimic coupling under follower load");
#endif

    sim::World compound_world(13, 0.002);
    const auto compound = compound_world.create_entity("compound_body");
    compound_world.registry().emplace<sim::RigidBodyComponent>(compound,
        sim::RigidBodyComponent{0.2, {}, {}, true, false});
    const auto compound_shape = compound_world.create_entity("compound_shape", compound);
    compound_world.registry().emplace<sim::ColliderComponent>(compound_shape,
        sim::ColliderComponent{sim::ColliderShape::Box, {0.1, 0.1, 0.1}, {}, false});
    sim::MuJoCoBackend compound_backend;
    compound_backend.build(compound_world);
    compound_backend.apply_body_wrench(compound, {1.0, 0.0, 0.0}, {});
    compound_backend.step(compound_world);
    Require(std::abs(compound_world.registry().get<sim::TransformComponent>(compound).world.translation.x
                     - 0.002 * 0.002 / 0.2) < 1e-9,
            "MuJoCo compound colliders added density-derived mass to the authored 0.2kg body");

    sim::World link_mass_world(14, 0.002);
    const auto mass_root = link_mass_world.create_entity("mass_robot");
    link_mass_world.registry().emplace<sim::RobotComponent>(mass_root);
    const auto mass_link = link_mass_world.create_entity("mass_link", mass_root);
    sim::JointComponent mass_joint;
    mass_joint.type = sim::JointType::Prismatic;
    mass_joint.lower_limit = -1.0;
    mass_joint.upper_limit = 1.0;
    link_mass_world.registry().emplace<sim::JointComponent>(mass_link, mass_joint);
    link_mass_world.registry().emplace<sim::RigidBodyComponent>(mass_link,
        sim::RigidBodyComponent{0.2, {}, {}, true, false});
    auto& mass_control = link_mass_world.registry().emplace<sim::JointControlComponent>(mass_link);
    mass_control.mode = sim::JointControlMode::Effort;
    mass_control.target = 1.0;
    mass_control.max_effort = 1.0;
    const auto link_shape = link_mass_world.create_entity("link_collision", mass_link);
    link_mass_world.registry().emplace<sim::ColliderComponent>(link_shape,
        sim::ColliderComponent{sim::ColliderShape::Box, {0.1, 0.1, 0.1}, {}, false});
    sim::MuJoCoBackend link_mass_backend;
    link_mass_backend.build(link_mass_world);
    link_mass_backend.step(link_mass_world);
    const double expected_link_motion = 0.002 * 0.002 / (0.2 + 0.01 + 0.002 * 0.01);
    Require(std::abs(link_mass_world.registry().get<sim::JointComponent>(mass_link).position
                     - expected_link_motion) < 1e-9,
            "MuJoCo robot collider added density-derived mass to the authored link mass");

    sim::World gravity_world(2, 0.002);
    const auto floor = gravity_world.create_entity("floor");
    gravity_world.registry().get<sim::TransformComponent>(floor).local.translation =
        {0.0, 0.0, -0.05};
    gravity_world.registry().emplace<sim::ColliderComponent>(floor,
        sim::ColliderComponent{sim::ColliderShape::Box, {4.0, 4.0, 0.1}, {}, false});
    gravity_world.registry().emplace<sim::RigidBodyComponent>(floor,
        sim::RigidBodyComponent{1.0, {}, {}, false, true});
    const auto falling = gravity_world.create_entity("falling_ball");
    gravity_world.registry().get<sim::TransformComponent>(falling).local.translation =
        {0.0, 0.0, 1.0};
    gravity_world.registry().emplace<sim::ColliderComponent>(falling,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false,
                               {0.4, 0.001, 0.0001}, 0.8});
    gravity_world.registry().emplace<sim::RigidBodyComponent>(falling,
        sim::RigidBodyComponent{1.0, {}, {}, true, true});
    const auto floating = gravity_world.create_entity("gravity_disabled_ball");
    gravity_world.registry().get<sim::TransformComponent>(floating).local.translation =
        {1.0, 0.0, 1.0};
    gravity_world.registry().emplace<sim::ColliderComponent>(floating,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    gravity_world.registry().emplace<sim::RigidBodyComponent>(floating,
        sim::RigidBodyComponent{1.0, {}, {}, true, false});
    const auto scaled = gravity_world.create_entity("scaled_ball");
    auto& scaled_transform = gravity_world.registry().get<sim::TransformComponent>(scaled).local;
    scaled_transform.translation = {-1.0, 0.0, 1.0};
    scaled_transform.scale = {2.0, 2.0, 2.0};
    gravity_world.registry().emplace<sim::ColliderComponent>(scaled,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    gravity_world.registry().emplace<sim::RigidBodyComponent>(scaled,
        sim::RigidBodyComponent{1.0, {}, {}, true, true});
    const auto primitive_floor = gravity_world.create_entity("primitive_plane_1m");
    gravity_world.registry().get<sim::TransformComponent>(primitive_floor).local.translation =
        {3.0, 0.0, -0.01};
    gravity_world.registry().emplace<sim::ColliderComponent>(primitive_floor,
        sim::ColliderComponent{sim::ColliderShape::Box, {1.0, 1.0, 0.02}, {}, false});
    gravity_world.registry().emplace<sim::RigidBodyComponent>(primitive_floor,
        sim::RigidBodyComponent{1.0, {}, {}, false, true});
    const auto falling_cube = gravity_world.create_entity("primitive_cube_1m");
    gravity_world.registry().get<sim::TransformComponent>(falling_cube).local.translation =
        // Start intersecting the Plane, matching a newly-created WebEditor Cube.
        {3.0, 0.0, 0.0};
    gravity_world.registry().emplace<sim::ColliderComponent>(falling_cube,
        sim::ColliderComponent{sim::ColliderShape::Box, {1.0, 1.0, 1.0}, {}, false});
    gravity_world.registry().emplace<sim::RigidBodyComponent>(falling_cube,
        sim::RigidBodyComponent{1.0, {}, {}, true, true});
    const auto elastic_floor = gravity_world.create_entity("elastic_floor");
    gravity_world.registry().get<sim::TransformComponent>(elastic_floor).local.translation =
        {5.0, 0.0, -0.01};
    gravity_world.registry().emplace<sim::ColliderComponent>(elastic_floor,
        sim::ColliderComponent{sim::ColliderShape::Box, {1.0, 1.0, 0.02}, {}, false,
                               {1.0, 0.005, 0.0001}, 1.0});
    gravity_world.registry().emplace<sim::RigidBodyComponent>(elastic_floor,
        sim::RigidBodyComponent{1.0, {}, {}, false, true});
    const auto elastic_cube = gravity_world.create_entity("elastic_cube");
    gravity_world.registry().get<sim::TransformComponent>(elastic_cube).local.translation =
        {5.0, 0.0, 1.5};
    gravity_world.registry().emplace<sim::ColliderComponent>(elastic_cube,
        sim::ColliderComponent{sim::ColliderShape::Box, {1.0, 1.0, 1.0}, {}, false,
                               {1.0, 0.005, 0.0001}, 1.0});
    gravity_world.registry().emplace<sim::RigidBodyComponent>(elastic_cube,
        sim::RigidBodyComponent{1.0, {}, {}, true, true});
    gravity_world.update_world_transforms();
    sim::MuJoCoBackend gravity_backend;
    gravity_backend.build(gravity_world);
    bool contacted = false;
    double rebound_peak = 0.0;
    bool elastic_contacted = false;
    double elastic_minimum = 1.5;
    double elastic_rebound_peak = 0.0;
    for (int index = 0; index < 1250; ++index) {
        gravity_backend.step(gravity_world);
        const double height = gravity_world.registry()
            .get<sim::TransformComponent>(falling).world.translation.z;
        if (height < 0.14) contacted = true;
        if (contacted) rebound_peak = std::max(rebound_peak, height);
        const double elastic_height = gravity_world.registry()
            .get<sim::TransformComponent>(elastic_cube).world.translation.z;
        elastic_minimum = std::min(elastic_minimum, elastic_height);
        if (elastic_height < 0.55) elastic_contacted = true;
        if (elastic_contacted)
            elastic_rebound_peak = std::max(elastic_rebound_peak, elastic_height);
    }
    const double floating_height = gravity_world.registry()
        .get<sim::TransformComponent>(floating).world.translation.z;
    const double scaled_height = gravity_world.registry()
        .get<sim::TransformComponent>(scaled).world.translation.z;
    const double cube_height = gravity_world.registry()
        .get<sim::TransformComponent>(falling_cube).world.translation.z;
    Require(contacted && rebound_peak > 0.35,
            "restitution did not make the falling sphere bounce");
    Require(std::abs(floating_height - 1.0) < 1.0e-4,
            "per-body gravity disable did not keep the body in place");
    Require(scaled_height > 0.18 && scaled_height < 0.23,
            "MuJoCo collision geometry did not follow entity scale");
    Require(cube_height > 0.49 && cube_height < 0.53,
            "dynamic Cube 1m fell through the static Plane 1m collider");
    Require(elastic_contacted && elastic_minimum > 0.45 && elastic_rebound_peak > 1.2,
            "elastic Cube contact penetrated the Plane or failed to rebound");

    const auto state_before_contact_query = gravity_backend.save_state();
    const auto solver_contacts = gravity_backend.contacts();
    double supported_force = 0.0;
    for (const auto& contact : solver_contacts) {
        Require(std::isfinite(contact.distance) && std::isfinite(contact.normal_force),
                "MuJoCo contact query returned non-finite measurements");
        if ((contact.first == scaled && contact.second == floor) ||
            (contact.second == scaled && contact.first == floor)) {
            Require(contact.distance < 0.001, "resting body contact separation is invalid");
            supported_force += contact.normal_force;
        }
    }
    Require(supported_force > 9.0 && supported_force < 11.0,
            "MuJoCo contact query did not report the resting body's weight in newtons");
    Require(state_before_contact_query == gravity_backend.save_state(),
            "MuJoCo contact query advanced or mutated physics state");

    const auto mesh_robot_path = std::filesystem::path(SIM_TEST_DATA_DIR) /
                                 "mesh_two_link.urdf";
    const auto mesh_robot_asset = sim::UrdfImporter{}.import(mesh_robot_path).asset;
    sim::World mesh_robot_world(3, 0.002);
    (void)resources.instantiate(mesh_robot_world, mesh_robot_asset);
    const auto dynamic_sphere = mesh_robot_world.create_entity("dynamic_sphere");
    mesh_robot_world.registry().get<sim::TransformComponent>(dynamic_sphere).local.translation =
        {0.0, 0.0, 1.0};
    mesh_robot_world.registry().emplace<sim::ColliderComponent>(dynamic_sphere,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    mesh_robot_world.registry().emplace<sim::RigidBodyComponent>(dynamic_sphere,
        sim::RigidBodyComponent{1.0, {}, {}, true, true});
    mesh_robot_world.update_world_transforms();
    sim::MuJoCoBackend mesh_robot_backend;
    mesh_robot_backend.build(mesh_robot_world);
    for (int index = 0; index < 20; ++index) mesh_robot_backend.step(mesh_robot_world);
    Require(std::isfinite(mesh_robot_world.registry()
                .get<sim::TransformComponent>(dynamic_sphere).world.translation.z),
            "MuJoCo mesh robot plus dynamic body produced an invalid state");

    const auto contact_robot_path = std::filesystem::path(SIM_TEST_DATA_DIR) /
                                    "mesh_contact_robot.urdf";
    const auto contact_robot_asset = sim::UrdfImporter{}.import(contact_robot_path).asset;
    sim::World contact_world(2, 0.002);
    (void)resources.instantiate(contact_world, contact_robot_asset);
    const auto contact_sphere = contact_world.create_entity("contact_sphere");
    contact_world.registry().get<sim::TransformComponent>(contact_sphere).local.translation =
        {0.0, 0.0, 1.5};
    contact_world.registry().emplace<sim::ColliderComponent>(contact_sphere,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    contact_world.registry().emplace<sim::RigidBodyComponent>(contact_sphere,
        sim::RigidBodyComponent{1.0, {}, {}, true, true});
    contact_world.update_world_transforms();
    sim::MuJoCoBackend contact_backend;
    contact_backend.build(contact_world);
    for (int index = 0; index < 1000; ++index) contact_backend.step(contact_world);
    const double contact_height = contact_world.registry()
        .get<sim::TransformComponent>(contact_sphere).world.translation.z;
    Require(contact_height > 0.58 && contact_height < 0.63,
            "dynamic sphere fell through an imported robot mesh collider");

    const auto planar_robot_path = std::filesystem::path(SIM_TEST_DATA_DIR) /
                                   "planar_contact_robot.urdf";
    const auto planar_robot_asset = sim::UrdfImporter{}.import(planar_robot_path).asset;
    sim::World planar_world(2, 0.002);
    (void)resources.instantiate(planar_world, planar_robot_asset);
    const auto planar_sphere = planar_world.create_entity("planar_sphere");
    planar_world.registry().get<sim::TransformComponent>(planar_sphere).local.translation =
        {0.25, 0.25, 1.0};
    planar_world.registry().emplace<sim::ColliderComponent>(planar_sphere,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    planar_world.registry().emplace<sim::RigidBodyComponent>(planar_sphere,
        sim::RigidBodyComponent{1.0, {}, {}, true, true});
    planar_world.update_world_transforms();
    sim::MuJoCoBackend planar_backend;
    planar_backend.build(planar_world);
    for (int index = 0; index < 1000; ++index) planar_backend.step(planar_world);
    const double planar_height = planar_world.registry()
        .get<sim::TransformComponent>(planar_sphere).world.translation.z;
    Require(planar_height > 0.095 && planar_height < 0.12,
            "dynamic sphere fell through an extruded single-sided mesh collider");
    std::filesystem::remove_all(cache);
}
#endif

#if defined(SIM_HAS_BULLET_PLUGIN)
void TestBulletPluginRigidBody() {
    sim::PluginManager plugins;
    plugins.discover(SIM_TEST_BULLET_PLUGIN_DIR);
    auto physics = plugins.create_physics_backend("sim.physics.bullet3");
    Require(physics->capabilities().rigid_body_dynamics &&
                physics->capabilities().state_serialization,
            "Bullet3 plugin capabilities mismatch");

    sim::World world(1, 0.01);
    const auto floor = world.create_entity("floor");
    world.registry().emplace<sim::ColliderComponent>(floor, sim::ColliderComponent{
        sim::ColliderShape::Box, {10.0, 10.0, 1.0}});
    world.registry().get<sim::TransformComponent>(floor).local.translation = {0.0, 0.0, -0.5};
    const auto body = world.create_entity("falling_box");
    world.registry().emplace<sim::ColliderComponent>(body, sim::ColliderComponent{
        sim::ColliderShape::Box, {1.0, 1.0, 1.0}});
    world.registry().emplace<sim::RigidBodyComponent>(body, sim::RigidBodyComponent{});
    world.registry().get<sim::TransformComponent>(body).local.translation = {0.0, 0.0, 3.0};
    physics->build(world);
    for (int index = 0; index < 500; ++index) physics->step(world);
    const double height = world.registry().get<sim::TransformComponent>(body).world.translation.z;
    Require(height > 0.4 && height < 0.7, "Bullet3 falling box did not settle on the floor");

    sim::World assembly(2, 0.01);
    const auto carrier = assembly.create_entity("dynamic_carrier");
    assembly.registry().emplace<sim::ColliderComponent>(carrier, sim::ColliderComponent{
        sim::ColliderShape::Box, {0.5, 0.5, 0.5}});
    assembly.registry().emplace<sim::RigidBodyComponent>(carrier,
        sim::RigidBodyComponent{1.0, {1.0, 0.0, 0.0}, {}, true, false});
    const auto attachment = assembly.create_entity("static_attachment", carrier);
    assembly.registry().get<sim::TransformComponent>(attachment).local.translation =
        {1.0, 0.0, 0.0};
    assembly.registry().emplace<sim::ColliderComponent>(attachment, sim::ColliderComponent{
        sim::ColliderShape::Box, {0.5, 0.5, 0.5}});
    assembly.registry().emplace<sim::RigidBodyComponent>(attachment,
        sim::RigidBodyComponent{1.0, {}, {}, false, false});
    const auto probe = assembly.create_entity("dynamic_probe");
    assembly.registry().get<sim::TransformComponent>(probe).local.translation =
        {2.0, 0.0, 0.0};
    assembly.registry().emplace<sim::ColliderComponent>(probe, sim::ColliderComponent{
        sim::ColliderShape::Box, {0.5, 0.5, 0.5}});
    assembly.registry().emplace<sim::RigidBodyComponent>(probe,
        sim::RigidBodyComponent{1.0, {}, {}, true, false});
    physics->build(assembly);
    for (int index = 0; index < 80; ++index) physics->step(assembly);
    const double probe_x = assembly.registry()
        .get<sim::TransformComponent>(probe).world.translation.x;
    Require(probe_x > 2.1,
            "Bullet3 static attachment collider did not follow its dynamic parent");
}
#endif

#if defined(SIM_HAS_BULLET_COLLISION_PLUGIN)
void TestBulletCollisionPlugin() {
    sim::PluginManager plugins;
    plugins.discover(SIM_TEST_BULLET_COLLISION_PLUGIN_DIR);
    auto collision = plugins.create_collision_backend("sim.collision.bullet3");
    Require(collision->capabilities().collision_query &&
                collision->capabilities().distance_query,
            "Bullet3 collision plugin capabilities mismatch");

    sim::World world(1, 0.01);
    const auto first = world.create_entity("first_box");
    const auto second = world.create_entity("second_box");
    const auto distant = world.create_entity("distant_box");
    for (const auto entity : std::array{first, second, distant})
        world.registry().emplace<sim::ColliderComponent>(entity,
            sim::ColliderComponent{sim::ColliderShape::Box, {1.0, 1.0, 1.0}});
    world.registry().get<sim::TransformComponent>(second).local.translation =
        {0.4, 0.0, 0.0};
    world.registry().get<sim::TransformComponent>(distant).local.translation =
        {3.0, 0.0, 0.0};
    collision->build(world);
    Require(collision->check_collision(first, second),
            "Bullet3 failed to detect overlapping boxes");
    const auto initial = collision->check_scene_collisions(
        std::array{first, second, distant});
    Require(initial.size() == 3 && initial[0] && initial[1] && !initial[2],
            "Bullet3 DBVT batch query results mismatch");
    Require(sim::NearlyEqual(collision->distance(first, distant), 2.0, 1e-6),
            "Bullet3 separated box distance mismatch");

    world.registry().get<sim::TransformComponent>(second).local.translation =
        {4.0, 0.0, 0.0};
    collision->sync_from_scene(world);
    Require(!collision->check_collision(first, second),
            "Bullet3 DBVT proxy did not update after scene synchronization");
    const auto separated = collision->check_scene_collisions(
        std::array{first, second, distant});
    Require(!separated[0] && separated[1] && separated[2],
            "Bullet3 DBVT batch query did not reflect synchronized transforms");

#if defined(SIM_HAS_ASSIMP)
    sim::World mesh_world(2, 0.01);
    const auto mesh = mesh_world.create_entity("triangle_mesh");
    sim::ColliderComponent mesh_collider;
    mesh_collider.shape = sim::ColliderShape::Mesh;
    mesh_collider.mesh_resource =
        (std::filesystem::path(SIM_TEST_DATA_DIR) / "triangle.obj").string();
    mesh_world.registry().emplace<sim::ColliderComponent>(mesh, mesh_collider);
    const auto sphere = mesh_world.create_entity("sphere");
    mesh_world.registry().get<sim::TransformComponent>(sphere).local.translation =
        {0.25, 0.25, 0.05};
    mesh_world.registry().emplace<sim::ColliderComponent>(sphere,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    collision->build(mesh_world);
    Require(collision->check_collision(mesh, sphere),
            "Bullet3 GImpact mesh BVH collision failed");
#endif
}
#endif

#if defined(SIM_HAS_COAL_COLLISION_PLUGIN)
void TestCoalCollisionPlugin() {
    sim::PluginManager plugins;
    plugins.discover(SIM_TEST_COAL_COLLISION_PLUGIN_DIR);
    auto collision = plugins.create_collision_backend("sim.collision.coal");
    Require(collision->capabilities().collision_query &&
                collision->capabilities().distance_query,
            "Coal collision plugin capabilities mismatch");

    sim::World world(1, 0.01);
    const auto first = world.create_entity("first_box");
    const auto second = world.create_entity("second_box");
    const auto distant = world.create_entity("distant_box");
    for (const auto entity : std::array{first, second, distant})
        world.registry().emplace<sim::ColliderComponent>(entity,
            sim::ColliderComponent{sim::ColliderShape::Box, {1.0, 1.0, 1.0}});
    world.registry().get<sim::TransformComponent>(second).local.translation =
        {0.4, 0.0, 0.0};
    world.registry().get<sim::TransformComponent>(distant).local.translation =
        {3.0, 0.0, 0.0};
    collision->build(world);
    Require(collision->check_collision(first, second),
            "Coal failed to detect overlapping boxes");
    const auto initial = collision->check_scene_collisions(
        std::array{first, second, distant});
    Require(initial.size() == 3 && initial[0] && initial[1] && !initial[2],
            "Coal Dynamic AABB Tree batch query results mismatch");
    Require(sim::NearlyEqual(collision->distance(first, distant), 2.0, 1e-6),
            "Coal separated box distance mismatch");

    world.registry().get<sim::TransformComponent>(second).local.translation =
        {3.9, 0.0, 0.0};
    collision->sync_from_scene(world);
    Require(!collision->check_collision(first, second),
            "Coal broad-phase object did not update after scene synchronization");
    const auto separated = collision->check_scene_collisions(
        std::array{first, second, distant});
    Require(!separated[0] && separated[1] && separated[2],
            "Coal batch query did not reflect synchronized transforms");

#if defined(SIM_HAS_ASSIMP)
    sim::World mesh_world(2, 0.01);
    const auto mesh = mesh_world.create_entity("triangle_mesh");
    sim::ColliderComponent mesh_collider;
    mesh_collider.shape = sim::ColliderShape::Mesh;
    mesh_collider.mesh_resource =
        (std::filesystem::path(SIM_TEST_DATA_DIR) / "triangle.obj").string();
    mesh_world.registry().emplace<sim::ColliderComponent>(mesh, mesh_collider);
    const auto sphere = mesh_world.create_entity("sphere");
    mesh_world.registry().get<sim::TransformComponent>(sphere).local.translation =
        {0.25, 0.25, 0.05};
    mesh_world.registry().emplace<sim::ColliderComponent>(sphere,
        sim::ColliderComponent{sim::ColliderShape::Sphere, {0.1, 0.1, 0.1}, {}, false});
    collision->build(mesh_world);
    Require(collision->check_collision(mesh, sphere),
            "Coal OBBRSS mesh BVH collision failed");
#endif
}
#endif

}  // namespace

int main() {
    const std::vector<std::pair<std::string, std::function<void()>>> tests{
        {"registry lifecycle and clone", TestRegistryLifecycleAndClone},
        {"frame tree", TestFrameTree},
        {"hierarchy and transforms", TestHierarchyAndTransforms},
        {"commands, events, and time", TestCommandsEventsAndTime},
        {"object identity and control mailbox", TestObjectIdentityAndControlMailbox},
        {"snapshot restore and reset", TestSnapshotRestoreAndReset},
        {"real-time factor pacing", TestRealTimeFactorPacing},
        {"unpaced step preserves clock configuration", TestUnpacedStepPreservesClockConfiguration},
        {"backend combinations", TestBackends},
#if defined(SIM_HAS_FCL)
        {"FCL collision and query context", TestFCLCollisionAndQueryContext},
#endif
#if defined(SIM_HAS_VULKAN)
        {"Vulkan offscreen readback", TestVulkanOffscreenReadback},
#endif
        {"URDF import and runtime instantiation", TestUrdfImportAndRuntimeInstantiation},
        {"Collada transforms and materials", TestColladaTransformsAndMaterials},
        {"external robot visual regression", TestExternalRobotVisualRegression},
        {"URDF relative mesh paths become absolute", TestUrdfRelativeMeshPathsBecomeAbsolute},
        {"OBJ import and baking cache", TestObjImportAndBakingCache},
#if defined(SIM_HAS_ASSIMP)
        {"Assimp import", TestAssimpImport},
#endif
        {"mesh LOD generation", TestMeshLodGeneration},
        {"robot control, kinematics, and limits", TestRobotControlKinematicsAndLimits},
        {"sensor scheduling and buffers", TestSensorSchedulingAndBuffers},
        {"plugin manifest and discovery", TestPluginManifestAndDiscovery},
        {"dynamic plugin load and unload", TestDynamicPluginLoadAndUnload},
        {"IFPS tree invariants", TestIFPSTreeInvariants},
#if defined(SIM_HAS_IFPS_CPU_PLUGIN)
        {"IFPS collision plugins", TestIFPSCollisionPlugins},
#endif
        {"foundation services", TestFoundationServices},
        {"versioned state file round trip", TestVersionedStateFileRoundTrip},
#if defined(SIM_HAS_MUJOCO)
        {"MuJoCo physics, state, and raycast", TestMuJoCoPhysicsStateAndRaycast},
#endif
#if defined(SIM_HAS_BULLET_PLUGIN)
        {"Bullet3 plugin rigid body", TestBulletPluginRigidBody},
#endif
#if defined(SIM_HAS_BULLET_COLLISION_PLUGIN)
        {"Bullet3 collision plugin", TestBulletCollisionPlugin},
#endif
#if defined(SIM_HAS_COAL_COLLISION_PLUGIN)
        {"Coal collision plugin", TestCoalCollisionPlugin},
#endif
    };

    std::size_t failed = 0;
    for (const auto& [name, test] : tests) {
        try {
            test();
            std::cout << "[pass] " << name << '\n';
        } catch (const std::exception& error) {
            ++failed;
            std::cerr << "[fail] " << name << ": " << error.what() << '\n';
        }
    }
    std::cout << (tests.size() - failed) << '/' << tests.size() << " tests passed\n";
    return failed == 0 ? 0 : 1;
}
