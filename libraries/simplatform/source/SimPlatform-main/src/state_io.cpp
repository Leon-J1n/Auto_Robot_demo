#include "sim/state_io.hpp"

#include <array>
#include <cstring>
#include <fstream>
#include <sstream>
#include <unordered_map>

namespace sim {
namespace {

constexpr std::array<char, 8> kMagic{'S', 'I', 'M', 'S', 'T', 'A', 'T', 'E'};
constexpr std::uint32_t kFormatVersion = 11;
constexpr std::uint64_t kMaximumItems = 10'000'000;
constexpr std::uint64_t kMaximumBytes = 1ULL << 30;

enum ComponentBits : std::uint32_t {
    kVisual = 1U << 0,
    kCollider = 1U << 1,
    kRigidBody = 1U << 2,
    kRobot = 1U << 3,
    kJoint = 1U << 4,
    kJointControl = 1U << 5,
    kSensor = 1U << 6,
    kCamera = 1U << 7,
    kLiDAR = 1U << 8,
    kJointStateSensor = 1U << 9,
    kLight = 1U << 10,
};

template <typename Value>
void WritePod(std::ostream& output, const Value& value) {
    static_assert(std::is_trivially_copyable_v<Value>);
    output.write(reinterpret_cast<const char*>(&value), sizeof(Value));
}

template <typename Value>
Value ReadPod(std::istream& input) {
    static_assert(std::is_trivially_copyable_v<Value>);
    Value value{};
    input.read(reinterpret_cast<char*>(&value), sizeof(Value));
    if (!input) throw Error(ErrorCode::IoError, "state file is truncated");
    return value;
}

void WriteString(std::ostream& output, const std::string& value) {
    WritePod(output, static_cast<std::uint64_t>(value.size()));
    output.write(value.data(), static_cast<std::streamsize>(value.size()));
}

std::string ReadString(std::istream& input) {
    const auto size = ReadPod<std::uint64_t>(input);
    if (size > kMaximumBytes) throw Error(ErrorCode::ValidationFailed, "state string is too large");
    std::string value(static_cast<std::size_t>(size), '\0');
    input.read(value.data(), static_cast<std::streamsize>(value.size()));
    if (!input) throw Error(ErrorCode::IoError, "state file is truncated");
    return value;
}

void WriteVec3(std::ostream& output, const Vec3& value) {
    WritePod(output, value.x); WritePod(output, value.y); WritePod(output, value.z);
}

Vec3 ReadVec3(std::istream& input) {
    return {ReadPod<double>(input), ReadPod<double>(input), ReadPod<double>(input)};
}

void WriteTransform(std::ostream& output, const Transform& value) {
    WriteVec3(output, value.translation);
    WritePod(output, value.rotation.w); WritePod(output, value.rotation.x);
    WritePod(output, value.rotation.y); WritePod(output, value.rotation.z);
    WriteVec3(output, value.scale);
}

Transform ReadTransform(std::istream& input) {
    Transform value;
    value.translation = ReadVec3(input);
    value.rotation = {ReadPod<double>(input), ReadPod<double>(input),
                      ReadPod<double>(input), ReadPod<double>(input)};
    value.scale = ReadVec3(input);
    return value;
}

template <typename Value, typename Writer>
void WriteVector(std::ostream& output, const std::vector<Value>& values, Writer writer) {
    WritePod(output, static_cast<std::uint64_t>(values.size()));
    for (const auto& value : values) writer(output, value);
}

template <typename Value, typename Reader>
std::vector<Value> ReadVector(std::istream& input, Reader reader) {
    const auto size = ReadPod<std::uint64_t>(input);
    if (size > kMaximumItems) throw Error(ErrorCode::ValidationFailed, "state collection is too large");
    std::vector<Value> values;
    values.reserve(static_cast<std::size_t>(size));
    for (std::uint64_t index = 0; index < size; ++index) values.push_back(reader(input));
    return values;
}

void WriteEntity(std::ostream& output, const Registry& registry, EntityId entity) {
    WritePod(output, entity);
    WriteString(output, registry.get<NameComponent>(entity).name);
    WriteString(output, registry.get<ObjectIdentityComponent>(entity).value);
    const auto& transform = registry.get<TransformComponent>(entity);
    WriteTransform(output, transform.local); WriteTransform(output, transform.world);
    WritePod(output, registry.get<HierarchyComponent>(entity).parent);

    std::uint32_t bits = 0;
    if (registry.has<VisualComponent>(entity)) bits |= kVisual;
    if (registry.has<ColliderComponent>(entity)) bits |= kCollider;
    if (registry.has<RigidBodyComponent>(entity)) bits |= kRigidBody;
    if (registry.has<RobotComponent>(entity)) bits |= kRobot;
    if (registry.has<JointComponent>(entity)) bits |= kJoint;
    if (registry.has<JointControlComponent>(entity)) bits |= kJointControl;
    if (registry.has<SensorComponent>(entity)) bits |= kSensor;
    if (registry.has<CameraComponent>(entity)) bits |= kCamera;
    if (registry.has<LiDARComponent>(entity)) bits |= kLiDAR;
    if (registry.has<JointStateSensorComponent>(entity)) bits |= kJointStateSensor;
    if (registry.has<LightComponent>(entity)) bits |= kLight;
    WritePod(output, bits);

    if (bits & kVisual) {
        const auto& value = registry.get<VisualComponent>(entity);
        WriteString(output, value.mesh_resource); WriteString(output, value.material_resource);
        WritePod(output, static_cast<std::uint8_t>(value.visible));
        WritePod(output, value.shape); WriteVec3(output, value.size);
        for (const float channel : value.color) WritePod(output, channel);
        WritePod(output, value.min_lod); WritePod(output, value.max_lod);
        WritePod(output, static_cast<std::uint8_t>(value.use_mesh_material));
    }
    if (bits & kCollider) {
        const auto& value = registry.get<ColliderComponent>(entity);
        WritePod(output, value.shape); WriteVec3(output, value.size);
        WriteString(output, value.mesh_resource); WritePod(output, static_cast<std::uint8_t>(value.is_trigger));
        WriteVec3(output, value.friction);
        WritePod(output, value.restitution);
        WritePod(output, value.ifps_sphere_count);
    }
    if (bits & kRigidBody) {
        const auto& value = registry.get<RigidBodyComponent>(entity);
        WritePod(output, value.mass); WriteVec3(output, value.linear_velocity);
        WriteVec3(output, value.angular_velocity); WritePod(output, static_cast<std::uint8_t>(value.dynamic));
        WritePod(output, static_cast<std::uint8_t>(value.gravity_enabled));
    }
    if (bits & kRobot) {
        const auto& value = registry.get<RobotComponent>(entity);
        WriteString(output, value.model_resource);
        WriteVector(output, value.links, [](std::ostream& stream, EntityId item) { WritePod(stream, item); });
    }
    if (bits & kJoint) {
        const auto& value = registry.get<JointComponent>(entity);
        WriteString(output, value.name); WritePod(output, value.type); WritePod(output, value.parent_link);
        WritePod(output, value.child_link); WriteVec3(output, value.axis); WriteTransform(output, value.joint_origin);
        WritePod(output, value.lower_limit); WritePod(output, value.upper_limit); WritePod(output, value.position);
        WritePod(output, value.velocity); WritePod(output, value.effort);
    }
    if (bits & kJointControl) {
        const auto& value = registry.get<JointControlComponent>(entity);
        WritePod(output, value.mode); WritePod(output, value.target); WritePod(output, value.position_gain);
        WritePod(output, value.velocity_limit);
    }
    if (bits & kSensor) {
        const auto& value = registry.get<SensorComponent>(entity);
        WriteString(output, value.frame_id); WritePod(output, value.update_rate_hz);
        WritePod(output, value.next_update_time);
    }
    if (bits & kCamera) {
        const auto& value = registry.get<CameraComponent>(entity);
        WritePod(output, value.width); WritePod(output, value.height); WritePod(output, value.vertical_fov_radians);
        WritePod(output, value.near_plane); WritePod(output, value.far_plane);
        WritePod(output, value.output);
    }
    if (bits & kLiDAR) {
        const auto& value = registry.get<LiDARComponent>(entity);
        WritePod(output, value.horizontal_fov_radians); WritePod(output, value.vertical_fov_radians);
        WritePod(output, value.horizontal_samples); WritePod(output, value.vertical_channels);
        WritePod(output, value.min_distance); WritePod(output, value.max_distance);
    }
    if (bits & kJointStateSensor)
        WritePod(output, registry.get<JointStateSensorComponent>(entity).robot);
    if (bits & kLight) {
        const auto& value = registry.get<LightComponent>(entity);
        WritePod(output, value.type);
        for (const auto channel : value.color) WritePod(output, channel);
        WritePod(output, value.intensity); WritePod(output, value.range); WritePod(output, value.decay);
        WritePod(output, value.inner_cone_radians); WritePod(output, value.outer_cone_radians);
        WritePod(output, value.angular_diameter_radians); WritePod(output, static_cast<std::uint8_t>(value.enabled));
    }
}

struct PendingHierarchy { EntityId entity; EntityId parent; };

PendingHierarchy ReadEntity(std::istream& input, Registry& registry, std::uint32_t format_version) {
    const EntityId entity = ReadPod<EntityId>(input);
    const EntityId created = registry.create_with_id(entity, ReadString(input));
    (void)created;
    registry.emplace<ObjectIdentityComponent>(
        entity, ObjectIdentityComponent{format_version >= 9 ? ReadString(input) : GenerateObjectId()});
    auto& transform = registry.get<TransformComponent>(entity);
    transform.local = ReadTransform(input); transform.world = ReadTransform(input);
    const EntityId parent = ReadPod<EntityId>(input);
    const auto bits = ReadPod<std::uint32_t>(input);
    if (bits & kVisual) {
        VisualComponent visual{ReadString(input), ReadString(input),
                               ReadPod<std::uint8_t>(input) != 0};
        if (format_version >= 3) {
            visual.shape = ReadPod<ColliderShape>(input);
            visual.size = ReadVec3(input);
            for (float& channel : visual.color) channel = ReadPod<float>(input);
            if (format_version >= 6) {
                visual.min_lod = ReadPod<std::uint8_t>(input);
                visual.max_lod = ReadPod<std::uint8_t>(input);
            }
        }
        if (format_version >= 11)
            visual.use_mesh_material = ReadPod<std::uint8_t>(input) != 0;
        registry.emplace<VisualComponent>(entity, std::move(visual));
    }
    if (bits & kCollider) {
        ColliderComponent collider{ReadPod<ColliderShape>(input), ReadVec3(input), ReadString(input),
                                   ReadPod<std::uint8_t>(input) != 0, ReadVec3(input)};
        if (format_version >= 5) collider.restitution = ReadPod<double>(input);
        if (format_version >= 8) collider.ifps_sphere_count = ReadPod<std::uint32_t>(input);
        registry.emplace<ColliderComponent>(entity, std::move(collider));
    }
    if (bits & kRigidBody) {
        RigidBodyComponent rigid_body{ReadPod<double>(input), ReadVec3(input), ReadVec3(input),
                                      ReadPod<std::uint8_t>(input) != 0};
        if (format_version >= 4)
            rigid_body.gravity_enabled = ReadPod<std::uint8_t>(input) != 0;
        registry.emplace<RigidBodyComponent>(entity, rigid_body);
    }
    if (bits & kRobot) registry.emplace<RobotComponent>(entity, RobotComponent{
        ReadString(input), ReadVector<EntityId>(input, [](std::istream& stream) { return ReadPod<EntityId>(stream); })});
    if (bits & kJoint) registry.emplace<JointComponent>(entity, JointComponent{
        ReadString(input), ReadPod<JointType>(input), ReadPod<EntityId>(input), ReadPod<EntityId>(input),
        ReadVec3(input), ReadTransform(input), ReadPod<double>(input), ReadPod<double>(input),
        ReadPod<double>(input), ReadPod<double>(input), ReadPod<double>(input)});
    if (bits & kJointControl) registry.emplace<JointControlComponent>(entity, JointControlComponent{
        ReadPod<JointControlMode>(input), ReadPod<double>(input), ReadPod<double>(input), ReadPod<double>(input)});
    if (bits & kSensor) registry.emplace<SensorComponent>(entity, SensorComponent{
        ReadString(input), ReadPod<double>(input), ReadPod<double>(input)});
    if (bits & kCamera) {
        CameraComponent camera{ReadPod<std::uint32_t>(input), ReadPod<std::uint32_t>(input),
                               ReadPod<double>(input), ReadPod<double>(input),
                               ReadPod<double>(input)};
        if (format_version >= 7) camera.output = ReadPod<CameraOutput>(input);
        registry.emplace<CameraComponent>(entity, camera);
    }
    if (bits & kLiDAR) registry.emplace<LiDARComponent>(entity, LiDARComponent{
        ReadPod<double>(input), ReadPod<double>(input), ReadPod<std::uint32_t>(input),
        ReadPod<std::uint32_t>(input), ReadPod<double>(input), ReadPod<double>(input)});
    if (bits & kJointStateSensor) registry.emplace<JointStateSensorComponent>(
        entity, JointStateSensorComponent{ReadPod<EntityId>(input)});
    if (bits & kLight) {
        LightComponent value;
        value.type = ReadPod<LightType>(input);
        for (auto& channel : value.color) channel = ReadPod<float>(input);
        value.intensity = ReadPod<float>(input); value.range = ReadPod<float>(input); value.decay = ReadPod<float>(input);
        value.inner_cone_radians = ReadPod<float>(input); value.outer_cone_radians = ReadPod<float>(input);
        value.angular_diameter_radians = ReadPod<float>(input); value.enabled = ReadPod<std::uint8_t>(input) != 0;
        registry.emplace<LightComponent>(entity, value);
    }
    return {entity, parent};
}

void CommitFile(const std::filesystem::path& temporary, const std::filesystem::path& target) {
    std::error_code error;
    std::filesystem::remove(target, error); error.clear();
    std::filesystem::rename(temporary, target, error);
    if (error) throw Error(ErrorCode::IoError, "cannot commit state file");
}

}  // namespace

void SaveStateFile(const std::filesystem::path& path, const SimulationStateArchive& archive) {
    auto temporary = path;
    temporary += ".tmp";
    std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
    if (!output) throw Error(ErrorCode::IoError, "cannot create state file");
    output.write(kMagic.data(), static_cast<std::streamsize>(kMagic.size()));
    WritePod(output, kFormatVersion); WritePod(output, archive.version);
    WritePod(output, archive.world.version);
    WritePod(output, archive.world.time.simulation_time); WritePod(output, archive.world.time.step_index);
    WritePod(output, archive.world.time.fixed_time_step); WritePod(output, archive.world.time.real_time_factor);
    WritePod(output, archive.world.status);
    std::ostringstream random; random << archive.world.random_state; WriteString(output, random.str());

    WritePod(output, static_cast<std::uint64_t>(archive.world.registry.size()));
    for (const EntityId entity : archive.world.registry.entities()) WriteEntity(output, archive.world.registry, entity);
    WriteString(output, archive.world.frames.root());
    const auto records = archive.world.frames.records();
    WriteVector(output, records, [](std::ostream& stream, const FrameTree::Record& record) {
        WriteString(stream, record.frame); WriteString(stream, record.parent);
        WriteTransform(stream, record.parent_from_frame);
    });
    WriteVector(output, archive.backend_state,
                [](std::ostream& stream, std::byte value) { WritePod(stream, value); });
    output.close();
    if (!output) throw Error(ErrorCode::IoError, "cannot write state file");
    CommitFile(temporary, path);
}

SimulationStateArchive LoadStateFile(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw Error(ErrorCode::IoError, "cannot open state file");
    std::array<char, 8> magic{}; input.read(magic.data(), static_cast<std::streamsize>(magic.size()));
    const auto format_version = ReadPod<std::uint32_t>(input);
    if (!input || magic != kMagic || format_version < 2 || format_version > kFormatVersion)
        throw Error(ErrorCode::ValidationFailed, "unsupported state file format");
    SimulationStateArchive archive;
    archive.version = ReadPod<std::uint32_t>(input);
    if (archive.version != 1) throw Error(ErrorCode::ValidationFailed, "unsupported state archive version");
    archive.world.version = ReadPod<std::uint32_t>(input);
    archive.world.time.simulation_time = ReadPod<double>(input);
    archive.world.time.step_index = ReadPod<std::uint64_t>(input);
    archive.world.time.fixed_time_step = ReadPod<double>(input);
    archive.world.time.real_time_factor = ReadPod<double>(input);
    archive.world.status = ReadPod<SimulationStatus>(input);
    std::istringstream random(ReadString(input)); random >> archive.world.random_state;
    if (!random) throw Error(ErrorCode::ValidationFailed, "state file has invalid random generator state");

    const auto entity_count = ReadPod<std::uint64_t>(input);
    if (entity_count > kMaximumItems) throw Error(ErrorCode::ValidationFailed, "state has too many entities");
    std::vector<PendingHierarchy> hierarchy;
    hierarchy.reserve(static_cast<std::size_t>(entity_count));
    for (std::uint64_t index = 0; index < entity_count; ++index)
        hierarchy.push_back(ReadEntity(input, archive.world.registry, format_version));
    for (const auto& relation : hierarchy) {
        auto& component = archive.world.registry.get<HierarchyComponent>(relation.entity);
        component.parent = relation.parent;
        if (relation.parent != kInvalidEntity) {
            if (!archive.world.registry.valid(relation.parent))
                throw Error(ErrorCode::ValidationFailed, "state hierarchy references an invalid parent");
            archive.world.registry.get<HierarchyComponent>(relation.parent).children.push_back(relation.entity);
        }
    }

    archive.world.frames = FrameTree(ReadString(input));
    auto records = ReadVector<FrameTree::Record>(input, [](std::istream& stream) {
        return FrameTree::Record{ReadString(stream), ReadString(stream), ReadTransform(stream)};
    });
    while (!records.empty()) {
        bool progress = false;
        for (auto it = records.begin(); it != records.end();) {
            if (archive.world.frames.contains(it->parent)) {
                archive.world.frames.set_frame(it->frame, it->parent, it->parent_from_frame);
                it = records.erase(it); progress = true;
            } else {
                ++it;
            }
        }
        if (!progress) throw Error(ErrorCode::ValidationFailed, "state frame tree has missing parents or a cycle");
    }
    archive.backend_state = ReadVector<std::byte>(input,
        [](std::istream& stream) { return ReadPod<std::byte>(stream); });
    return archive;
}

}  // namespace sim
