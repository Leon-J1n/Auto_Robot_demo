#include "sim/physics.hpp"
#include "sim/assets.hpp"
#include "sim/logging.hpp"

#include <mujoco/mujoco.h>
#if defined(SIM_HAS_URDFDOM)
#include <urdf_model/model.h>
#include <urdf_parser/urdf_parser.h>
#endif

#include <algorithm>
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <unordered_map>

namespace sim {
namespace {

std::string EntityName(EntityId entity) { return "entity_" + std::to_string(entity); }
std::string JointName(EntityId entity) { return "joint_" + std::to_string(entity); }
std::string ServoName(EntityId entity) { return "servo_" + std::to_string(entity); }
std::string GeomName(EntityId entity) { return "geom_" + std::to_string(entity); }
std::string MeshName(EntityId entity) { return "mesh_" + std::to_string(entity); }

std::filesystem::path PathFromUtf8(std::string_view value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

std::string XmlEscape(std::string value) {
    const std::array<std::pair<std::string_view, std::string_view>, 5> replacements{{
        {"&", "&amp;"}, {"\"", "&quot;"}, {"'", "&apos;"},
        {"<", "&lt;"}, {">", "&gt;"}}};
    for (const auto& [from, to] : replacements) {
        std::size_t offset = 0;
        while ((offset = value.find(from, offset)) != std::string::npos) {
            value.replace(offset, from.size(), to);
            offset += to.size();
        }
    }
    return value;
}

double DotProduct(const Vec3& first, const Vec3& second) {
    return first.x * second.x + first.y * second.y + first.z * second.z;
}

Vec3 CrossProduct(const Vec3& first, const Vec3& second) {
    return {first.y * second.z - first.z * second.y,
            first.z * second.x - first.x * second.z,
            first.x * second.y - first.y * second.x};
}

MeshData ExtrudePlanarMesh(MeshData mesh) {
    if (mesh.positions.size() < 3 || mesh.indices.size() < 3) return mesh;
    Vec3 normal{};
    for (std::size_t index = 0; index + 2 < mesh.indices.size(); index += 3) {
        const Vec3 first = mesh.positions[mesh.indices[index + 1]] -
                           mesh.positions[mesh.indices[index]];
        const Vec3 second = mesh.positions[mesh.indices[index + 2]] -
                            mesh.positions[mesh.indices[index]];
        normal = CrossProduct(first, second);
        const double length = std::sqrt(DotProduct(normal, normal));
        if (length > 1.0e-12) {
            normal = normal * (1.0 / length);
            break;
        }
    }
    if (DotProduct(normal, normal) < 0.5) return mesh;
    Vec3 minimum = mesh.positions.front();
    Vec3 maximum = minimum;
    for (const Vec3& point : mesh.positions) {
        minimum = {std::min(minimum.x, point.x), std::min(minimum.y, point.y),
                   std::min(minimum.z, point.z)};
        maximum = {std::max(maximum.x, point.x), std::max(maximum.y, point.y),
                   std::max(maximum.z, point.z)};
    }
    const double extent = std::max({maximum.x - minimum.x, maximum.y - minimum.y,
                                    maximum.z - minimum.z, 1.0});
    const Vec3 origin = mesh.positions.front();
    const double planar_tolerance = extent * 1.0e-7;
    for (const Vec3& point : mesh.positions) {
        if (std::abs(DotProduct(point - origin, normal)) > planar_tolerance) return mesh;
    }

    const std::size_t original_count = mesh.positions.size();
    const double half_thickness = extent * 5.0e-5;  // 0.1 mm for a one-metre plane.
    std::vector<Vec3> positions;
    positions.reserve(original_count * 2);
    for (const Vec3& point : mesh.positions) positions.push_back(point - normal * half_thickness);
    for (const Vec3& point : mesh.positions) positions.push_back(point + normal * half_thickness);

    struct EdgeInfo { std::uint32_t first; std::uint32_t second; std::uint32_t count; };
    std::unordered_map<std::uint64_t, EdgeInfo> edges;
    const auto add_edge = [&](std::uint32_t first, std::uint32_t second) {
        const auto low = std::min(first, second);
        const auto high = std::max(first, second);
        const std::uint64_t key = (static_cast<std::uint64_t>(low) << 32U) | high;
        const auto found = edges.find(key);
        if (found == edges.end()) edges.emplace(key, EdgeInfo{first, second, 1});
        else ++found->second.count;
    };
    std::vector<std::uint32_t> indices;
    indices.reserve(mesh.indices.size() * 2 + edges.size() * 6);
    const auto top = static_cast<std::uint32_t>(original_count);
    for (std::size_t index = 0; index + 2 < mesh.indices.size(); index += 3) {
        const auto a = mesh.indices[index];
        const auto b = mesh.indices[index + 1];
        const auto c = mesh.indices[index + 2];
        indices.insert(indices.end(), {c, b, a, top + a, top + b, top + c});
        add_edge(a, b);
        add_edge(b, c);
        add_edge(c, a);
    }
    for (const auto& [unused, edge] : edges) {
        if (edge.count != 1) continue;
        indices.insert(indices.end(), {edge.first, edge.second, top + edge.second,
                                       edge.first, top + edge.second, top + edge.first});
    }
    mesh.positions = std::move(positions);
    mesh.indices = std::move(indices);
    mesh.normals.clear();
    mesh.texcoords.clear();
    return mesh;
}

std::size_t BinaryStlTriangleCount(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input || input.tellg() < 84) return 0;
    const auto size = static_cast<std::uint64_t>(input.tellg());
    input.seekg(80);
    std::uint32_t triangles = 0;
    input.read(reinterpret_cast<char*>(&triangles), sizeof(triangles));
    return input && size == 84ULL + static_cast<std::uint64_t>(triangles) * 50ULL
        ? triangles : 0;
}

std::unordered_map<EntityId, std::size_t> AppendMeshAssets(std::ostringstream& xml,
                                                            const Registry& registry) {
    std::unordered_map<EntityId, std::size_t> assets;
#if defined(SIM_HAS_ASSIMP)
    for (const EntityId entity : registry.view<ColliderComponent, TransformComponent>()) {
        const auto& collider = registry.get<ColliderComponent>(entity);
        if (collider.shape != ColliderShape::Mesh || collider.mesh_resource.empty()) continue;
        try {
            std::string source = collider.mesh_resource;
            std::optional<std::size_t> selected_mesh;
            if (const auto marker = source.rfind("#mesh="); marker != std::string::npos) {
                selected_mesh = static_cast<std::size_t>(
                    std::stoull(source.substr(marker + 6)));
                source.resize(marker);
            }
            const auto& transform = registry.get<TransformComponent>(entity).world;
            const Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                             std::abs(transform.scale.z)};
            auto extension = PathFromUtf8(source).extension().string();
            std::transform(extension.begin(), extension.end(), extension.begin(),
                           [](unsigned char value) { return static_cast<char>(std::tolower(value)); });
            if (!selected_mesh && extension == ".stl") {
                // MuJoCo reads the authoritative URDF STL directly. No proxy, convex
                // substitute, vertex clustering, or application-side LOD is applied.
                const auto source_path = std::filesystem::absolute(PathFromUtf8(source))
                                             .lexically_normal();
                const auto path = source_path.generic_u8string();
                const std::string utf8(reinterpret_cast<const char*>(path.data()), path.size());
                xml << "<mesh name=\"" << MeshName(entity) << "\" file=\""
                    << XmlEscape(utf8) << "\" scale=\"" << scale.x << ' ' << scale.y << ' '
                    << scale.z << "\"/>";
                assets.emplace(entity, BinaryStlTriangleCount(source_path));
                continue;
            }
            auto imported = AssimpImporter{}.import(PathFromUtf8(source)).asset;
            AssetValidator::normalize(imported);
            const bool fbx = extension == ".fbx";
            std::vector<Vec3> vertices;
            std::vector<std::uint32_t> faces;
            for (std::size_t index = 0; index < imported.meshes.size(); ++index) {
                if (selected_mesh && *selected_mesh != index) continue;
                // Preserve the complete source topology. Only zero-volume planar input
                // receives a thin extrusion so that it can form a valid contact surface.
                const auto mesh = ExtrudePlanarMesh(imported.meshes[index]);
                const auto offset = static_cast<std::uint32_t>(vertices.size());
                for (const Vec3& point : mesh.positions) {
                    if (fbx)
                        vertices.push_back({-point.z * scale.x, -point.x * scale.y,
                                            point.y * scale.z});
                    else
                        vertices.push_back({point.x * scale.x, point.y * scale.y,
                                            point.z * scale.z});
                }
                for (const auto face : mesh.indices) faces.push_back(offset + face);
            }
            if (vertices.size() < 4 || faces.size() < 3) continue;
            xml << "<mesh name=\"" << MeshName(entity) << "\" vertex=\"";
            for (const Vec3& point : vertices)
                xml << point.x << ' ' << point.y << ' ' << point.z << ' ';
            xml << "\" face=\"";
            for (const auto face : faces) xml << face << ' ';
            xml << "\"/>";
            assets.emplace(entity, faces.size() / 3);
        } catch (const std::exception&) {
            // A missing/invalid collision mesh must not prevent the rest of the scene loading.
        }
    }
#else
    (void)xml;
    (void)registry;
#endif
    return assets;
}

Quaternion AxisAngle(Vec3 axis, double angle) {
    const double length = std::sqrt(axis.x * axis.x + axis.y * axis.y + axis.z * axis.z);
    if (length <= 1.0e-12) return {};
    axis = axis * (1.0 / length);
    const double sine = std::sin(angle * 0.5);
    return {std::cos(angle * 0.5), axis.x * sine, axis.y * sine, axis.z * sine};
}

void AppendTransform(std::ostringstream& xml, const Transform& transform) {
    xml << std::setprecision(17) << " pos=\"" << transform.translation.x << ' '
        << transform.translation.y << ' ' << transform.translation.z << "\" quat=\""
        << transform.rotation.w << ' ' << transform.rotation.x << ' ' << transform.rotation.y
        << ' ' << transform.rotation.z << "\"";
}

void AppendGeom(std::ostringstream& xml, EntityId entity, const Registry& registry,
                bool needs_mass, bool robot_geometry,
                const std::unordered_map<EntityId, std::size_t>& mesh_assets) {
    xml << "<geom name=\"" << GeomName(entity)
        << "\" solimp=\"0.99 0.999 0.001\"";
    if (!registry.has<ColliderComponent>(entity) ||
        registry.get<ColliderComponent>(entity).restitution <= 0.0)
        xml << " solref=\"0.004 1\"";
    if (registry.has<ColliderComponent>(entity)) {
        const auto& collider = registry.get<ColliderComponent>(entity);
        const auto& transform = registry.get<TransformComponent>(entity).world;
        const Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                         std::abs(transform.scale.z)};
        xml << " friction=\"" << std::max(0.0, collider.friction.x) << ' '
            << std::max(0.0, collider.friction.y) << ' '
            << std::max(0.0, collider.friction.z) << "\" condim=\"6\"";
        if (collider.is_trigger ||
            (collider.shape == ColliderShape::Mesh && !mesh_assets.contains(entity))) {
            xml << " contype=\"0\" conaffinity=\"0\"";
        } else if (robot_geometry) {
            // Robot links ignore one another, but accept contacts from scene objects.
            xml << " contype=\"1\" conaffinity=\"2\"";
        } else {
            // Scene objects collide with both other scene objects and robot links.
            xml << " contype=\"2\" conaffinity=\"3\"";
        }
        if (collider.restitution > 0.0) {
            // Keep elastic contacts stiff enough that a one-kilogram body cannot
            // visibly sink through a thin floor before the spring rebounds.
            constexpr double stiffness = 100000.0;
            const double restitution = std::clamp(collider.restitution, 1.0e-6, 1.0);
            const double logarithm = std::log(restitution);
            const double damping_ratio = -logarithm /
                std::sqrt(3.14159265358979323846 * 3.14159265358979323846 +
                          logarithm * logarithm);
            const double damping = 2.0 * damping_ratio * std::sqrt(stiffness);
            xml << " solref=\"-" << stiffness << ' ' << -damping << "\"";
        }
        switch (collider.shape) {
            case ColliderShape::Box: {
                double half_x = collider.size.x * scale.x * 0.5;
                double half_y = collider.size.y * scale.y * 0.5;
                const double half_z = collider.size.z * scale.z * 0.5;
                const bool static_body = !registry.has<RigidBodyComponent>(entity) ||
                    !registry.get<RigidBodyComponent>(entity).dynamic;
                if (static_body && half_z < std::min(half_x, half_y) * 0.1) {
                    // Exactly matching box footprints can make MuJoCo's native
                    // box-box collider emit 9 contacts although its limit is 8.
                    // A sub-pixel contact-only pad removes that degenerate case.
                    half_x += 1.0e-4;
                    half_y += 1.0e-4;
                }
                xml << " type=\"box\" size=\"" << half_x << ' ' << half_y << ' '
                    << half_z << "\"";
                break;
            }
            case ColliderShape::Sphere:
                xml << " type=\"ellipsoid\" size=\"" << collider.size.x * scale.x << ' '
                    << collider.size.x * scale.y << ' ' << collider.size.x * scale.z << "\"";
                break;
            case ColliderShape::Capsule:
                xml << " type=\"capsule\" size=\""
                    << collider.size.x * std::max(scale.x, scale.y) << ' '
                    << collider.size.z * scale.z * 0.5 << "\"";
                break;
            case ColliderShape::Cylinder:
                xml << " type=\"cylinder\" size=\""
                    << collider.size.x * std::max(scale.x, scale.y) << ' '
                    << collider.size.z * scale.z * 0.5 << "\"";
                break;
            case ColliderShape::Mesh:
                if (mesh_assets.contains(entity)) {
                    xml << " type=\"mesh\" mesh=\"" << MeshName(entity) << "\"";
                } else {
                    // Keep mass/inertia valid when a referenced file is unavailable, but do
                    // not invent collision contact for geometry that could not be decoded.
                    xml << " type=\"sphere\" size=\"0.01\"";
                }
                break;
        }
    } else {
        // Mass-only fallback geometry must not behave as an invented collider.
        xml << " type=\"sphere\" size=\"0.05\" rgba=\"0.4 0.4 0.4 1\""
            << " contype=\"0\" conaffinity=\"0\"";
    }
    if (registry.has<RigidBodyComponent>(entity)) {
        xml << " mass=\"" << registry.get<RigidBodyComponent>(entity).mass << "\"";
    } else if (needs_mass) {
        xml << " mass=\"1\"";
    } else {
        // Collider-only descendants describe their ancestor rigid body's shape,
        // not additional matter. MuJoCo's default density=1000 would otherwise
        // add their volume-derived mass on top of the authored body/link mass.
        xml << " mass=\"0\"";
    }
    xml << "/>";
}

void AppendEntity(std::ostringstream& xml, const Registry& registry, EntityId entity,
                  const std::unordered_map<EntityId, std::size_t>& mesh_assets,
                  bool robot_tree = false) {
    const bool powered_robot = robot_tree || registry.has<RobotComponent>(entity);
    // A moving joint's body frame is its joint origin, not its current local
    // transform. The two differ by exactly the joint's own motion: the scene keeps
    // local = joint_origin * motion(axis, position), so emitting local here and then
    // a <joint> seeded with the same position would apply the angle twice, putting
    // the model at roughly double every joint value. The axis is already expressed
    // in the joint frame, which is what MuJoCo expects of a joint in this body.
    const bool moving_joint = registry.has<JointComponent>(entity) &&
        registry.get<JointComponent>(entity).type != JointType::Fixed;
    const auto& transform = moving_joint
        ? registry.get<JointComponent>(entity).joint_origin
        : registry.get<TransformComponent>(entity).local;
    xml << "<body name=\"" << EntityName(entity) << "\"";
    AppendTransform(xml, transform);
    if (powered_robot) {
        // Imported robots are powered mechanisms by default. Apply gravity
        // compensation to the whole subtree, including welded collision bodies.
        xml << " gravcomp=\"1\"";
    } else if (registry.has<RigidBodyComponent>(entity)) {
        const auto& body = registry.get<RigidBodyComponent>(entity);
        if (body.dynamic && !body.gravity_enabled) xml << " gravcomp=\"1\"";
    }
    xml << '>';
    bool moving = false;
    if (registry.has<JointComponent>(entity)) {
        const auto& joint = registry.get<JointComponent>(entity);
        if (joint.type != JointType::Fixed) {
            moving = true;
            xml << "<joint name=\"" << JointName(entity) << "\" type=\""
                << (joint.type == JointType::Prismatic ? "slide" : "hinge") << "\" axis=\""
                << joint.axis.x << ' ' << joint.axis.y << ' ' << joint.axis.z
                << "\" damping=\"1\" armature=\"0.01\"";
            if (joint.type != JointType::Continuous)
                xml << " limited=\"true\" range=\"" << joint.lower_limit << ' ' << joint.upper_limit << "\"";
            xml << "/>";
        }
    } else if (!powered_robot && registry.has<RigidBodyComponent>(entity) &&
               registry.get<RigidBodyComponent>(entity).dynamic) {
        moving = true;
        xml << "<freejoint name=\"" << JointName(entity) << "\"/>";
    }
    if (registry.has<ColliderComponent>(entity) || registry.has<RigidBodyComponent>(entity) || moving)
        AppendGeom(xml, entity, registry, moving, powered_robot, mesh_assets);
    for (const EntityId child : registry.get<HierarchyComponent>(entity).children)
        AppendEntity(xml, registry, child, mesh_assets, powered_robot);
    xml << "</body>";
}

void AppendMimicConstraints(std::ostringstream& xml, const Registry& registry) {
#if defined(SIM_HAS_URDFDOM)
    for (const EntityId root : registry.view<RobotComponent>()) {
        const auto& robot = registry.get<RobotComponent>(root);
        std::ifstream source(PathFromUtf8(robot.model_resource), std::ios::binary);
        if (!source) continue;
        const std::string text((std::istreambuf_iterator<char>(source)),
                               std::istreambuf_iterator<char>());
        const auto model = urdf::parseURDF(text);
        if (!model) continue;
        std::unordered_map<std::string, EntityId> joints;
        for (const EntityId link : robot.links) {
            if (registry.has<JointComponent>(link) &&
                registry.get<JointComponent>(link).type != JointType::Fixed)
                joints.emplace(registry.get<JointComponent>(link).name, link);
        }
        for (const auto& [name, joint] : model->joints_) {
            if (!joint->mimic || !joints.contains(name) ||
                !joints.contains(joint->mimic->joint_name)) continue;
            // Preserve the authored mechanical transmission under load. Sending
            // equal servo targets alone allows follower joints to bend freely.
            xml << "<joint joint1=\"" << JointName(joints.at(name))
                << "\" joint2=\"" << JointName(joints.at(joint->mimic->joint_name))
                << "\" polycoef=\"" << joint->mimic->offset << ' '
                << joint->mimic->multiplier
                << " 0 0 0\" solref=\"0.004 1\" solimp=\"0.99 0.999 0.001\"/>";
        }
    }
#else
    (void)xml;
    (void)registry;
#endif
}

}  // namespace

struct MuJoCoBackend::Impl {
    struct Wrench { Vec3 force; Vec3 torque; };
    mjModel* model{nullptr};
    mjData* data{nullptr};
    std::unordered_map<EntityId, int> body_ids;
    std::unordered_map<EntityId, int> joint_ids;
    std::unordered_map<EntityId, int> servo_ids;
    std::unordered_map<EntityId, int> geom_ids;
    std::unordered_map<int, EntityId> geom_entities;
    std::unordered_map<EntityId, double> hold_positions;
    std::unordered_map<EntityId, Wrench> pending_wrenches;
    bool trace_servos{false};
    std::size_t trace_step{0};

    ~Impl() {
        if (data != nullptr) mj_deleteData(data);
        if (model != nullptr) mj_deleteModel(model);
    }

    void clear() {
        if (data != nullptr) mj_deleteData(data);
        if (model != nullptr) mj_deleteModel(model);
        data = nullptr; model = nullptr; body_ids.clear(); joint_ids.clear(); geom_ids.clear();
        geom_entities.clear();
        hold_positions.clear();
        servo_ids.clear();
        pending_wrenches.clear();
        trace_step = 0;
    }
};

MuJoCoBackend::MuJoCoBackend() : impl_(std::make_unique<Impl>()) {}
MuJoCoBackend::~MuJoCoBackend() = default;

BackendCapabilities MuJoCoBackend::capabilities() const noexcept {
    return {.rigid_body_dynamics = true, .articulated_body = true, .collision_query = true,
            .distance_query = true, .raycast = true, .state_serialization = true};
}

void MuJoCoBackend::build(World& world) {
    SIM_LOG_VERBOSE("MuJoCoBackend::build compiles world=" + std::to_string(world.id()) +
                    " entities=" + std::to_string(world.registry().size()));
    SIM_PROFILE_SCOPE("MuJoCo.Build");
    impl_->clear();
    world.update_world_transforms();
    std::ostringstream xml;
    xml << "<mujoco model=\"sim_platform\"><compiler angle=\"radian\"/>"
        << "<option timestep=\"" << std::setprecision(17) << world.time().state().fixed_time_step
        << "\" gravity=\"0 0 -9.81\" integrator=\"implicitfast\" cone=\"elliptic\" impratio=\"10\" iterations=\"100\" noslip_iterations=\"10\"/><asset>";
    const auto mesh_assets = AppendMeshAssets(xml, world.registry());
    xml << "</asset><worldbody>";
    for (const EntityId entity : world.registry().view<TransformComponent, HierarchyComponent>()) {
        if (world.registry().get<HierarchyComponent>(entity).parent == kInvalidEntity)
            AppendEntity(xml, world.registry(), entity, mesh_assets);
    }
    xml << "</worldbody><actuator>";
    for (const EntityId entity : world.registry().view<JointComponent>()) {
        if (world.registry().get<JointComponent>(entity).type == JointType::Fixed) continue;
        // MuJoCo integrates velocity feedback implicitly. A hand-written explicit
        // high-gain qfrc_applied servo chatters on lightweight finger joints.
        xml << "<velocity name=\"" << ServoName(entity) << "\" joint=\""
            << JointName(entity) << "\" kv=\"0\"/>";
    }
    xml << "</actuator><equality>";
    AppendMimicConstraints(xml, world.registry());
    xml << "</equality></mujoco>";
    const std::string contents = xml.str();
    mjVFS vfs; mj_defaultVFS(&vfs);
    if (mj_addBufferVFS(&vfs, "model.xml", contents.data(), static_cast<int>(contents.size())) != 0) {
        mj_deleteVFS(&vfs);
        throw Error(ErrorCode::BackendUnavailable, "MuJoCo failed to create an in-memory model");
    }
    std::array<char, 2048> error{};
    impl_->model = mj_loadXML("model.xml", &vfs, error.data(), static_cast<int>(error.size()));
    mj_deleteVFS(&vfs);
    if (impl_->model == nullptr)
        throw Error(ErrorCode::BackendUnavailable, "MuJoCo model compilation failed: " + std::string(error.data()));
    for (const auto& [entity, expected_faces] : mesh_assets) {
        const int mesh = mj_name2id(impl_->model, mjOBJ_MESH, MeshName(entity).c_str());
        if (mesh < 0 || (expected_faces != 0 &&
                         static_cast<std::size_t>(impl_->model->mesh_facenum[mesh]) != expected_faces)) {
            impl_->clear();
            throw Error(ErrorCode::BackendUnavailable,
                        "MuJoCo collision mesh audit failed for entity " +
                            std::to_string(entity) + ": source topology was not preserved");
        }
    }
    impl_->data = mj_makeData(impl_->model);
    if (impl_->data == nullptr) {
        impl_->clear();
        throw Error(ErrorCode::BackendUnavailable, "MuJoCo failed to allocate simulation data");
    }
    const char* servo_trace = std::getenv("SIM_MUJOCO_DEBUG_SERVO");
    impl_->trace_servos = servo_trace != nullptr && std::strcmp(servo_trace, "1") == 0;
    if (impl_->trace_servos)
        SIM_LOG_DEBUG("MuJoCo", "articulated model servos=" +
            std::to_string(impl_->model->nu) + " mimic_constraints=" +
            std::to_string(impl_->model->neq));
    for (const EntityId entity : world.registry().entities()) {
        const int body = mj_name2id(impl_->model, mjOBJ_BODY, EntityName(entity).c_str());
        if (body >= 0) impl_->body_ids.emplace(entity, body);
        const int joint = mj_name2id(impl_->model, mjOBJ_JOINT, JointName(entity).c_str());
        if (joint >= 0) {
            impl_->joint_ids.emplace(entity, joint);
            const int servo = mj_name2id(impl_->model, mjOBJ_ACTUATOR, ServoName(entity).c_str());
            if (servo >= 0) impl_->servo_ids.emplace(entity, servo);
            if (world.registry().has<JointComponent>(entity)) {
                const double position = world.registry().get<JointComponent>(entity).position;
                impl_->data->qpos[impl_->model->jnt_qposadr[joint]] = position;
                impl_->hold_positions.emplace(entity, position);
            }
        }
        const int geom = mj_name2id(impl_->model, mjOBJ_GEOM, GeomName(entity).c_str());
        if (geom >= 0) {
            impl_->geom_ids.emplace(entity, geom);
            impl_->geom_entities.emplace(geom, entity);
        }
    }
    mj_forward(impl_->model, impl_->data);
    sync_to_scene(world);
    SIM_LOG_VERBOSE("MuJoCoBackend::build completed bodies=" + std::to_string(impl_->body_ids.size()) +
                    " joints=" + std::to_string(impl_->joint_ids.size()));
}

std::vector<PhysicsContact> MuJoCoBackend::contacts() const {
    SIM_LOG_VERBOSE("MuJoCoBackend::contacts begins");
    SIM_PROFILE_SCOPE("MuJoCo.Contacts");
    if (impl_->model == nullptr || impl_->data == nullptr)
        throw Error(ErrorCode::InvalidState, "MuJoCo backend has not been built");
    std::vector<PhysicsContact> result;
    result.reserve(impl_->data->ncon);
    for (int index = 0; index < impl_->data->ncon; ++index) {
        const auto& contact = impl_->data->contact[index];
        if (contact.efc_address < 0) continue;
        const auto first = impl_->geom_entities.find(contact.geom[0]);
        const auto second = impl_->geom_entities.find(contact.geom[1]);
        if (first == impl_->geom_entities.end() || second == impl_->geom_entities.end()) continue;
        mjtNum force[6]{};
        mj_contactForce(impl_->model, impl_->data, index, force);
        result.push_back({first->second, second->second, contact.dist, force[0]});
    }
    SIM_LOG_VERBOSE("MuJoCoBackend::contacts completed count=" + std::to_string(result.size()));
    return result;
}

void MuJoCoBackend::step(World& world) {
    SIM_LOG_VERBOSE("MuJoCoBackend::step advances world=" + std::to_string(world.id()));
    SIM_PROFILE_SCOPE("MuJoCo.Step");
    SIM_LOG_VERBOSE("MuJoCoBackend::step begins time=" + std::to_string(impl_->data ? impl_->data->time : 0.0));
    if (impl_->model == nullptr || impl_->data == nullptr)
        throw Error(ErrorCode::InvalidState, "MuJoCo backend has not been built");
    std::fill_n(impl_->data->qfrc_applied, impl_->model->nv, 0.0);
    for (const auto& [entity, joint_id] : impl_->joint_ids) {
        const int dof = impl_->model->jnt_dofadr[joint_id];
        const int qpos = impl_->model->jnt_qposadr[joint_id];
        const auto* control = world.registry().has<JointControlComponent>(entity)
            ? &world.registry().get<JointControlComponent>(entity) : nullptr;
        const auto servo = impl_->servo_ids.find(entity);
        if (servo != impl_->servo_ids.end()) {
            const int actuator = servo->second;
            impl_->model->actuator_gainprm[actuator * mjNGAIN] = 0.0;
            impl_->model->actuator_biasprm[actuator * mjNBIAS + 2] = 0.0;
            impl_->data->ctrl[actuator] = 0.0;
            // The generic damping=1 consumes nearly all of a small gripper's
            // torque budget even at 0.1 rad/s. Feedback supplies its own damping.
            impl_->model->dof_damping[dof] = control && std::isfinite(control->max_effort)
                ? 0.01 : 1.0;
        }
        // The gravity and Coriolis term a servo has to supply to stand still, less
        // whatever MuJoCo is already supplying for it.
        //
        // A robot subtree is emitted with gravcomp="1", so MuJoCo passively adds
        // qfrc_gravcomp. Feeding raw qfrc_bias forward on top of that compensates
        // gravity twice and leaves a spurious antigravity torque on every joint, which
        // pushes the robot off on its own. It went unnoticed because the kinematic
        // override below rewrites qpos afterwards and hides the drift -- but the bogus
        // force is applied *during* mj_step, so it corrupts the contact forces the
        // robot exerts on whatever it touches, and a payload gets a push nobody asked
        // for. Subtracting leaves exactly the term still missing, and changes nothing
        // for joints whose bodies carry no gravcomp, where qfrc_gravcomp is zero.
        const double feedforward = impl_->data->qfrc_bias[dof] -
                                   impl_->data->qfrc_gravcomp[dof];
        if (control == nullptr || control->mode == JointControlMode::Disabled) {
            // Gravity compensation does most of the work; modest gains remain stable
            // even for lightweight wrist links with very small joint inertia.
            constexpr double hold_gain = 20.0;
            constexpr double hold_damping = 4.0;
            const double target = impl_->hold_positions.contains(entity)
                ? impl_->hold_positions.at(entity) : impl_->data->qpos[qpos];
            // The servo holds the pose at which the articulated model was built,
            // matching a powered robot arm.
            impl_->data->qfrc_applied[dof] = feedforward +
                hold_gain * (target - impl_->data->qpos[qpos]) -
                hold_damping * impl_->data->qvel[dof];
        } else if (control->mode == JointControlMode::Position ||
                   control->mode == JointControlMode::Velocity) {
            impl_->data->qfrc_applied[dof] = feedforward;
            if (std::isfinite(control->max_effort)) {
                // Cascaded position/velocity servo with an actuator torque ceiling.
                // Contact may stall it short of its target; physics owns the state.
                const double velocity_target = control->mode == JointControlMode::Position
                    ? std::clamp(control->position_gain *
                                     (control->target - impl_->data->qpos[qpos]),
                                 -control->velocity_limit, control->velocity_limit)
                    : std::clamp(control->target, -control->velocity_limit,
                                 control->velocity_limit);
                constexpr double effort_gain = 1.0;
                const int actuator = impl_->servo_ids.at(entity);
                if (impl_->trace_servos &&
                    (!impl_->model->actuator_forcelimited[actuator] ||
                     impl_->model->actuator_forcerange[actuator * 2 + 1] != control->max_effort)) {
                    SIM_LOG_DEBUG("MuJoCo",
                        "finite-effort servo entity=" + std::to_string(entity) +
                        " max_effort=" + std::to_string(control->max_effort) +
                        " damping=" + std::to_string(impl_->model->dof_damping[dof]));
                }
                impl_->model->actuator_gainprm[actuator * mjNGAIN] = effort_gain;
                impl_->model->actuator_biasprm[actuator * mjNBIAS + 2] = -effort_gain;
                impl_->model->actuator_forcelimited[actuator] = 1;
                impl_->model->actuator_forcerange[actuator * 2] = -control->max_effort;
                impl_->model->actuator_forcerange[actuator * 2 + 1] = control->max_effort;
                impl_->data->ctrl[actuator] = velocity_target;
            } else {
                // Drive the arm during contact resolution, without a post-step pose
                // overwrite. Excessive velocity gain (e.g. 10000) destabilizes the
                // coupled arm/finger/contact solve and can eject the held payload.
                const int actuator = impl_->servo_ids.at(entity);
                constexpr double stiff_servo_gain = 100.0;
                impl_->model->actuator_gainprm[actuator * mjNGAIN] = stiff_servo_gain;
                impl_->model->actuator_biasprm[actuator * mjNBIAS + 2] = -stiff_servo_gain;
                impl_->model->actuator_forcelimited[actuator] = 0;
                impl_->data->ctrl[actuator] = control->mode == JointControlMode::Position
                    ? std::clamp(control->position_gain *
                                     (control->target - impl_->data->qpos[qpos]),
                                 -control->velocity_limit, control->velocity_limit)
                    : std::clamp(control->target, -control->velocity_limit,
                                 control->velocity_limit);
            }
        } else if (control->mode == JointControlMode::Effort) {
            impl_->data->qfrc_applied[dof] = control->target;
        }
    }
    for (const auto& [entity, wrench] : impl_->pending_wrenches) {
        const auto found = impl_->body_ids.find(entity);
        if (found == impl_->body_ids.end()) continue;
        const int body = found->second;
        const mjtNum force[3]{wrench.force.x, wrench.force.y, wrench.force.z};
        const mjtNum torque[3]{wrench.torque.x, wrench.torque.y, wrench.torque.z};
        mj_applyFT(impl_->model, impl_->data, force, torque,
                   impl_->data->xpos + 3 * body, body, impl_->data->qfrc_applied);
    }
    impl_->pending_wrenches.clear();
    mj_step(impl_->model, impl_->data);
    if (impl_->trace_servos && (++impl_->trace_step % 100 == 0)) {
        for (const auto& [entity, actuator] : impl_->servo_ids) {
            if (impl_->model->actuator_gainprm[actuator * mjNGAIN] == 0.0) continue;
            const int joint = impl_->joint_ids.at(entity);
            SIM_LOG_DEBUG("MuJoCo", "servo t=" +
                std::to_string(impl_->data->time) + " entity=" + std::to_string(entity) +
                " q=" + std::to_string(impl_->data->qpos[impl_->model->jnt_qposadr[joint]]) +
                " v=" + std::to_string(impl_->data->qvel[impl_->model->jnt_dofadr[joint]]) +
                " effort=" + std::to_string(impl_->data->actuator_force[actuator]));
        }
        for (int contact = 0; contact < impl_->data->ncon; ++contact) {
            const auto& pair = impl_->data->contact[contact];
            mjtNum force[6]{};
            mj_contactForce(impl_->model, impl_->data, contact, force);
            SIM_LOG_DEBUG("MuJoCo", "contact t=" +
                std::to_string(impl_->data->time) + " geom1=" + std::to_string(pair.geom[0]) +
                " geom2=" + std::to_string(pair.geom[1]) + " distance=" +
                std::to_string(pair.dist) + " normal_force=" + std::to_string(force[0]) +
                " tangent_force=" + std::to_string(std::hypot(force[1], force[2])) +
                " normal=" + std::to_string(pair.frame[0]) + "," + std::to_string(pair.frame[1]) +
                "," + std::to_string(pair.frame[2]) + " friction=" +
                std::to_string(pair.friction[0]) + "," + std::to_string(pair.friction[1]) +
                " dim=" + std::to_string(pair.dim));
        }
    }

    // MuJoCo can choose the downward contact normal when a dynamic box starts
    // inside a static box (the common WebEditor case: a new Cube at the origin
    // on a Plane at the origin). Resolve vertical box penetration explicitly so
    // the body is placed on the upper surface instead of being expelled below it.
    bool penetration_fixed = false;
    for (const auto& [dynamic_entity, dynamic_body] : impl_->body_ids) {
        if (!world.registry().has<ColliderComponent>(dynamic_entity) ||
            world.registry().get<ColliderComponent>(dynamic_entity).shape != ColliderShape::Box ||
            !world.registry().has<RigidBodyComponent>(dynamic_entity) ||
            !world.registry().get<RigidBodyComponent>(dynamic_entity).dynamic)
            continue;
        const int dynamic_geom = impl_->geom_ids.at(dynamic_entity);
        const auto dynamic_center = impl_->data->geom_xpos + 3 * dynamic_geom;
        const auto* dynamic_size = impl_->model->geom_size + 3 * dynamic_geom;
        for (const auto& [static_entity, static_body] : impl_->body_ids) {
            if (dynamic_entity == static_entity ||
                !world.registry().has<ColliderComponent>(static_entity) ||
                world.registry().get<ColliderComponent>(static_entity).shape != ColliderShape::Box ||
                (world.registry().has<RigidBodyComponent>(static_entity) &&
                 world.registry().get<RigidBodyComponent>(static_entity).dynamic))
                continue;
            const int static_geom = impl_->geom_ids.at(static_entity);
            const auto static_center = impl_->data->geom_xpos + 3 * static_geom;
            const auto* static_size = impl_->model->geom_size + 3 * static_geom;
            const bool overlap_xy = std::abs(dynamic_center[0] - static_center[0]) <
                    dynamic_size[0] + static_size[0] &&
                std::abs(dynamic_center[1] - static_center[1]) <
                    dynamic_size[1] + static_size[1];
            const double static_top = static_center[2] + static_size[2];
            const double dynamic_bottom = dynamic_center[2] - dynamic_size[2];
            const double dynamic_top = dynamic_center[2] + dynamic_size[2];
            const double static_bottom = static_center[2] - static_size[2];
            if (!overlap_xy || dynamic_center[2] > static_center[2] ||
                dynamic_bottom >= static_top || dynamic_top <= static_bottom)
                continue;
            const int joint = impl_->model->body_jntadr[dynamic_body];
            if (joint < 0 || impl_->model->jnt_type[joint] != mjJNT_FREE) continue;
            const int qpos = impl_->model->jnt_qposadr[joint];
            const int dof = impl_->model->jnt_dofadr[joint];
            impl_->data->qpos[qpos + 2] = static_top + dynamic_size[2];
            if (impl_->data->qvel[dof + 2] < 0.0) {
                const double restitution = std::max(
                    world.registry().get<ColliderComponent>(dynamic_entity).restitution,
                    world.registry().get<ColliderComponent>(static_entity).restitution);
                impl_->data->qvel[dof + 2] *= -restitution;
            }
            penetration_fixed = true;
            break;
        }
    }
    if (penetration_fixed) mj_forward(impl_->model, impl_->data);
    for (const auto& [entity, joint_id] : impl_->joint_ids) {
        if (!world.registry().has<JointComponent>(entity)) continue;
        const auto* control = world.registry().has<JointControlComponent>(entity)
            ? &world.registry().get<JointControlComponent>(entity) : nullptr;
        const int qpos = impl_->model->jnt_qposadr[joint_id];
        const int dof = impl_->model->jnt_dofadr[joint_id];
        if (control != nullptr && control->mode != JointControlMode::Disabled) continue;
        const auto target = impl_->hold_positions.find(entity);
        if (target == impl_->hold_positions.end()) continue;
        impl_->data->qpos[qpos] = target->second;
        impl_->data->qvel[dof] = 0.0;
    }
    // mj_step integrates qpos after evaluating xpos/xquat. Refresh derived poses
    // even when no idle joint was locked, or free bodies publish the previous step.
    mj_forward(impl_->model, impl_->data);
    sync_to_scene(world);
    SIM_LOG_VERBOSE("MuJoCoBackend::step completed time=" + std::to_string(impl_->data->time));
}

void MuJoCoBackend::apply_body_wrench(EntityId entity, const Vec3& force, const Vec3& torque) {
    SIM_LOG_VERBOSE("MuJoCoBackend::apply_body_wrench entity=" + std::to_string(entity));
    if (impl_->model == nullptr || impl_->data == nullptr)
        throw Error(ErrorCode::InvalidState, "MuJoCo backend has not been built");
    if (!impl_->body_ids.contains(entity))
        throw Error(ErrorCode::NotFound, "entity has no MuJoCo body");
    auto& pending = impl_->pending_wrenches[entity];
    pending.force = pending.force + force;
    pending.torque = pending.torque + torque;
    SIM_LOG_VERBOSE("MuJoCoBackend::apply_body_wrench completed entity=" + std::to_string(entity));
}

void MuJoCoBackend::sync_to_scene(World& world) {
    SIM_LOG_VERBOSE("MuJoCoBackend::sync_to_scene publishes dynamics world=" +
                    std::to_string(world.id()));
    for (const auto& [entity, joint_id] : impl_->joint_ids) {
        if (!world.registry().has<JointComponent>(entity)) continue;
        auto& joint = world.registry().get<JointComponent>(entity);
        joint.position = impl_->data->qpos[impl_->model->jnt_qposadr[joint_id]];
        joint.velocity = impl_->data->qvel[impl_->model->jnt_dofadr[joint_id]];
        Transform motion;
        if (joint.type == JointType::Revolute || joint.type == JointType::Continuous)
            motion.rotation = AxisAngle(joint.axis, joint.position);
        else if (joint.type == JointType::Prismatic)
            motion.translation = joint.axis * joint.position;
        world.registry().get<TransformComponent>(entity).local = joint.joint_origin * motion;
    }
    std::vector<EntityId> dynamic_bodies;
    for (const auto& [entity, body_id] : impl_->body_ids) {
        if (!world.registry().has<RigidBodyComponent>(entity) ||
            !world.registry().get<RigidBodyComponent>(entity).dynamic) continue;
        if (world.registry().has<JointComponent>(entity)) continue;
        auto& transform = world.registry().get<TransformComponent>(entity).world;
        transform.translation = {impl_->data->xpos[3 * body_id], impl_->data->xpos[3 * body_id + 1],
                                 impl_->data->xpos[3 * body_id + 2]};
        transform.rotation = {impl_->data->xquat[4 * body_id], impl_->data->xquat[4 * body_id + 1],
                              impl_->data->xquat[4 * body_id + 2], impl_->data->xquat[4 * body_id + 3]};
        dynamic_bodies.push_back(entity);
    }
    for (const EntityId entity : dynamic_bodies) {
        auto& transform = world.registry().get<TransformComponent>(entity);
        const EntityId parent = world.registry().get<HierarchyComponent>(entity).parent;
        transform.local = parent == kInvalidEntity
            ? transform.world
            : world.registry().get<TransformComponent>(parent).world.inverse() * transform.world;
    }
    world.update_world_transforms();
    SIM_LOG_VERBOSE("MuJoCoBackend::sync_to_scene completed dynamic_bodies=" +
                    std::to_string(dynamic_bodies.size()));
}

void MuJoCoBackend::sync_from_scene(World& world) {
    SIM_LOG_VERBOSE("MuJoCoBackend::sync_from_scene consumes scene edits world=" +
                    std::to_string(world.id()));
    if (impl_->model == nullptr || impl_->data == nullptr)
        throw Error(ErrorCode::InvalidState, "MuJoCo backend has not been built");
    for (const auto& [entity, joint_id] : impl_->joint_ids) {
        if (!world.registry().has<JointComponent>(entity)) continue;
        const double position = world.registry().get<JointComponent>(entity).position;
        impl_->data->qpos[impl_->model->jnt_qposadr[joint_id]] = position;
        impl_->data->qvel[impl_->model->jnt_dofadr[joint_id]] = 0.0;
        // An uncontrolled joint servos to its hold position, which was captured at
        // build time. Re-seed it, or the joint would crawl back to the built pose.
        impl_->hold_positions.insert_or_assign(entity, position);
    }
    for (const auto& [entity, body_id] : impl_->body_ids) {
        if (!world.registry().has<RigidBodyComponent>(entity) ||
            !world.registry().get<RigidBodyComponent>(entity).dynamic) continue;
        if (world.registry().has<JointComponent>(entity)) continue;
        const int joint = impl_->model->body_jntadr[body_id];
        if (joint < 0 || impl_->model->jnt_type[joint] != mjJNT_FREE) continue;
        const auto& transform = world.registry().get<TransformComponent>(entity).world;
        const int qpos = impl_->model->jnt_qposadr[joint];
        impl_->data->qpos[qpos] = transform.translation.x;
        impl_->data->qpos[qpos + 1] = transform.translation.y;
        impl_->data->qpos[qpos + 2] = transform.translation.z;
        impl_->data->qpos[qpos + 3] = transform.rotation.w;
        impl_->data->qpos[qpos + 4] = transform.rotation.x;
        impl_->data->qpos[qpos + 5] = transform.rotation.y;
        impl_->data->qpos[qpos + 6] = transform.rotation.z;
        std::fill_n(impl_->data->qvel + impl_->model->jnt_dofadr[joint], 6, 0.0);
    }
    mj_forward(impl_->model, impl_->data);
    SIM_LOG_VERBOSE("MuJoCoBackend::sync_from_scene completed world=" + std::to_string(world.id()));
}

std::vector<std::byte> MuJoCoBackend::save_state() const {
    SIM_LOG_VERBOSE("MuJoCoBackend::save_state begins");
    if (impl_->model == nullptr) throw Error(ErrorCode::InvalidState, "MuJoCo backend has not been built");
    constexpr int signature = mjSTATE_INTEGRATION;
    const int count = mj_stateSize(impl_->model, signature);
    std::vector<mjtNum> values(static_cast<std::size_t>(count));
    mj_getState(impl_->model, impl_->data, values.data(), signature);
    std::vector<std::byte> result(sizeof(std::uint32_t) * 2 + values.size() * sizeof(mjtNum));
    const std::uint32_t header[2]{static_cast<std::uint32_t>(signature), static_cast<std::uint32_t>(count)};
    std::memcpy(result.data(), header, sizeof(header));
    std::memcpy(result.data() + sizeof(header), values.data(), values.size() * sizeof(mjtNum));
    SIM_LOG_VERBOSE("MuJoCoBackend::save_state completed bytes=" + std::to_string(result.size()));
    return result;
}

void MuJoCoBackend::restore_state(std::span<const std::byte> state) {
    SIM_LOG_VERBOSE("MuJoCoBackend::restore_state begins bytes=" + std::to_string(state.size()));
    if (impl_->model == nullptr || state.size() < sizeof(std::uint32_t) * 2)
        throw Error(ErrorCode::ValidationFailed, "MuJoCo state is invalid");
    std::uint32_t header[2]{}; std::memcpy(header, state.data(), sizeof(header));
    int signature = mjSTATE_INTEGRATION;
    // Old scenes can lack servo CTRL and/or mimic EQ_ACTIVE fields. All physical
    // positions/velocities remain intact; only newly introduced controls/flags
    // get their compiled defaults.
    if (header[1] != static_cast<std::uint32_t>(mj_stateSize(impl_->model, signature))) {
        for (const int absent : std::array<int, 3>{mjSTATE_CTRL, mjSTATE_EQ_ACTIVE,
                                                  mjSTATE_CTRL | mjSTATE_EQ_ACTIVE}) {
            const int candidate = mjSTATE_INTEGRATION & ~absent;
            if (header[1] == static_cast<std::uint32_t>(mj_stateSize(impl_->model, candidate))) {
                signature = candidate;
                break;
            }
        }
    }
    if (header[0] != mjSTATE_INTEGRATION || header[1] != static_cast<std::uint32_t>(mj_stateSize(impl_->model, signature)) ||
        state.size() != sizeof(header) + static_cast<std::size_t>(header[1]) * sizeof(mjtNum))
        throw Error(ErrorCode::ValidationFailed, "MuJoCo state version or size mismatch");
    if (!(signature & mjSTATE_CTRL)) std::fill_n(impl_->data->ctrl, impl_->model->nu, 0.0);
    if (!(signature & mjSTATE_EQ_ACTIVE))
        std::copy_n(impl_->model->eq_active0, impl_->model->neq, impl_->data->eq_active);
    const auto* values = reinterpret_cast<const mjtNum*>(state.data() + sizeof(header));
    mj_setState(impl_->model, impl_->data, values, signature);
    mj_forward(impl_->model, impl_->data);
    SIM_LOG_VERBOSE("MuJoCoBackend::restore_state completed signature=" + std::to_string(signature));
}

void MuJoCoBackend::render_camera(const Transform&, const CameraComponent&,
                                  std::vector<std::uint8_t>&, std::vector<float>&) {
    throw Error(ErrorCode::Unsupported, "MuJoCo physics backend does not provide Vulkan camera rendering");
}

std::vector<float> MuJoCoBackend::raycast(const Vec3& origin, std::span<const Vec3> directions,
                                          double min_distance, double max_distance) {
    SIM_LOG_VERBOSE("MuJoCoBackend::raycast begins rays=" + std::to_string(directions.size()));
    SIM_PROFILE_SCOPE("MuJoCo.Raycast");
    if (impl_->model == nullptr) throw Error(ErrorCode::InvalidState, "MuJoCo backend has not been built");
    std::vector<float> result; result.reserve(directions.size());
    const mjtNum point[3]{origin.x, origin.y, origin.z};
    for (const Vec3& direction : directions) {
        const mjtNum vector[3]{direction.x, direction.y, direction.z};
        int geom = -1;
        const mjtNum distance =
            mj_ray(impl_->model, impl_->data, point, vector, nullptr, 1, -1, &geom, nullptr);
        result.push_back(distance >= min_distance && distance <= max_distance ?
                             static_cast<float>(distance) : static_cast<float>(max_distance));
    }
    SIM_LOG_VERBOSE("MuJoCoBackend::raycast completed rays=" + std::to_string(result.size()));
    return result;
}

double MuJoCoBackend::distance(EntityId first, EntityId second, double maximum_distance) {
    SIM_LOG_VERBOSE("MuJoCoBackend::distance begins first=" + std::to_string(first) +
                    " second=" + std::to_string(second));
    SIM_PROFILE_SCOPE("MuJoCo.Distance");
    if (!impl_->geom_ids.contains(first) || !impl_->geom_ids.contains(second))
        throw Error(ErrorCode::NotFound, "entity has no MuJoCo collision geometry");
    std::array<mjtNum, 6> from_to{};
    const double result = mj_geomDistance(impl_->model, impl_->data, impl_->geom_ids.at(first), impl_->geom_ids.at(second),
                           maximum_distance, from_to.data());
    SIM_LOG_VERBOSE("MuJoCoBackend::distance completed value=" + std::to_string(result));
    return result;
}

}  // namespace sim
