#include "sim/assets.hpp"
#include "sim/logging.hpp"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <regex>
#include <sstream>
#include <unordered_map>
#include <unordered_set>

#if defined(SIM_HAS_ASSIMP)
#include <assimp/Importer.hpp>
#include <assimp/config.h>
#include <assimp/material.h>
#include <assimp/postprocess.h>
#include <assimp/scene.h>
#endif

#if defined(SIM_HAS_URDFDOM)
#include <urdf_model/link.h>
#include <urdf_model/model.h>
#include <urdf_parser/urdf_parser.h>
#endif

namespace sim {
namespace {

constexpr std::array<char, 8> kCacheMagic{'S', 'I', 'M', 'C', 'A', 'C', 'H', 'E'};
// Bumped when the imported IR semantics change; older caches may lack OBJ materials.
constexpr std::uint32_t kCacheVersion = 7;
constexpr std::uint64_t kMaxCollectionSize = 10'000'000;

std::string Lowercase(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char character) {
        return static_cast<char>(std::tolower(character));
    });
    return value;
}

std::string PathUtf8(const std::filesystem::path& path) {
    const auto value = path.generic_u8string();
    return {reinterpret_cast<const char*>(value.data()), value.size()};
}

std::filesystem::path PathFromUtf8(const std::string& value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

std::filesystem::path ResolveUrdfResource(const std::filesystem::path& urdf,
                                          const std::string& resource) {
    SIM_LOG_VERBOSE("Resolve URDF Resource: path=" + PathUtf8(urdf) + " resource=" + resource);
    SIM_PROFILE_SCOPE("Assets.ResolveUrdfResource");
    constexpr std::string_view package_prefix{"package://"};
    constexpr std::string_view file_prefix{"file://"};

    // if begins with package:// replace to real files
    if (resource.starts_with(package_prefix)) {
        // cut off the package:// prefix
        const std::string relative = resource.substr(package_prefix.size());
        const auto separator = relative.find('/');
        if (separator == std::string::npos)
            return std::filesystem::absolute(
                urdf.parent_path() / PathFromUtf8(relative)).lexically_normal();
        const std::string package = relative.substr(0, separator);
        const auto package_relative = PathFromUtf8(relative.substr(separator + 1));
        for (auto current = urdf.parent_path(); !current.empty(); current = current.parent_path()) {
            if (PathUtf8(current.filename()) == package)
                return std::filesystem::absolute(current / package_relative).lexically_normal();
            // Uploaded ROS packages live in content-addressed bundle directories,
            // so the directory name no longer matches the package name.
            const auto direct_candidate = current / package_relative;
            if (std::filesystem::exists(direct_candidate))
                return std::filesystem::absolute(direct_candidate).lexically_normal();
            const auto candidate = current / PathFromUtf8(package) / package_relative;
            if (std::filesystem::exists(candidate))
                return std::filesystem::absolute(candidate).lexically_normal();
            if (current == current.root_path()) break;
        }
        return std::filesystem::absolute(
            urdf.parent_path() / package_relative).lexically_normal();
    }
    if (resource.starts_with(file_prefix))
        return std::filesystem::absolute(
            PathFromUtf8(resource.substr(file_prefix.size()))).lexically_normal();
    const auto path = PathFromUtf8(resource);
    return std::filesystem::absolute(
        path.is_absolute() ? path : urdf.parent_path() / path).lexically_normal();
}

std::filesystem::path ResolveAssetReference(const std::filesystem::path& source,
                                            std::string value) {
    // OBJ/MTL files commonly use POSIX separators even on Windows.  Normalize
    // them before resolving so uploaded bundles remain portable.
    std::replace(value.begin(), value.end(), '\\', '/');
    auto path = PathFromUtf8(value);
    if (path.is_absolute()) return std::filesystem::absolute(path).lexically_normal();
    return std::filesystem::absolute(source.parent_path() / path).lexically_normal();
}

Vec3 MultiplyComponents(const Vec3& first, const Vec3& second) {
    return {first.x * second.x, first.y * second.y, first.z * second.z};
}

std::uint64_t Fnv1a(std::span<const char> bytes) {
    std::uint64_t hash = 14695981039346656037ULL;
    for (const unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

std::uint64_t HashFile(const std::filesystem::path& path) {
    SIM_LOG_VERBOSE("Calculate hash for file= " + PathUtf8(path));
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        throw Error(ErrorCode::IoError, "cannot read asset: " + PathUtf8(path));
    }
    std::uint64_t hash = 14695981039346656037ULL;
    std::array<char, 64 * 1024> buffer{};
    while (input) {
        input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const auto count = input.gcount();
        for (std::streamsize index = 0; index < count; ++index) {
            hash ^= static_cast<unsigned char>(buffer[static_cast<std::size_t>(index)]);
            hash *= 1099511628211ULL;
        }
    }
    return hash;
}

struct Fingerprint {
    std::uint64_t size{0};
    std::int64_t modified{0};
    std::uint64_t content_hash{0};
};

Fingerprint FingerprintFile(const std::filesystem::path& source) {
    std::error_code error;
    const auto size = std::filesystem::file_size(source, error);
    if (error) {
        throw Error(ErrorCode::IoError, "cannot stat asset: " + PathUtf8(source));
    }
    const auto modified = std::filesystem::last_write_time(source, error);
    if (error) {
        throw Error(ErrorCode::IoError, "cannot read asset timestamp: " + PathUtf8(source));
    }
    return {static_cast<std::uint64_t>(size),
            static_cast<std::int64_t>(modified.time_since_epoch().count()), HashFile(source)};
}

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
    if (!input) {
        throw Error(ErrorCode::IoError, "baking cache is truncated");
    }
    return value;
}

void WriteString(std::ostream& output, const std::string& value) {
    WritePod(output, static_cast<std::uint64_t>(value.size()));
    output.write(value.data(), static_cast<std::streamsize>(value.size()));
}

std::string ReadString(std::istream& input) {
    const auto size = ReadPod<std::uint64_t>(input);
    if (size > kMaxCollectionSize) {
        throw Error(ErrorCode::ValidationFailed, "baking cache string is unreasonably large");
    }
    std::string result(static_cast<std::size_t>(size), '\0');
    input.read(result.data(), static_cast<std::streamsize>(result.size()));
    if (!input) {
        throw Error(ErrorCode::IoError, "baking cache is truncated");
    }
    return result;
}

void WritePath(std::ostream& output, const std::filesystem::path& path) {
    WriteString(output, PathUtf8(path));
}

std::filesystem::path ReadPath(std::istream& input) { return PathFromUtf8(ReadString(input)); }

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
    value.rotation = {ReadPod<double>(input), ReadPod<double>(input), ReadPod<double>(input),
                      ReadPod<double>(input)};
    value.scale = ReadVec3(input);
    return value;
}

template <typename Collection, typename Writer>
void WriteCollection(std::ostream& output, const Collection& values, Writer writer) {
    WritePod(output, static_cast<std::uint64_t>(values.size()));
    for (const auto& value : values) {
        writer(output, value);
    }
}

template <typename Value, typename Reader>
std::vector<Value> ReadCollection(std::istream& input, Reader reader) {
    const auto size = ReadPod<std::uint64_t>(input);
    if (size > kMaxCollectionSize) {
        throw Error(ErrorCode::ValidationFailed, "baking cache collection is unreasonably large");
    }
    std::vector<Value> values;
    values.reserve(static_cast<std::size_t>(size));
    for (std::uint64_t index = 0; index < size; ++index) {
        values.push_back(reader(input));
    }
    return values;
}

void WriteAsset(std::ostream& output, const AssetIR& asset) {
    SIM_LOG_INFO("Assets", "Writing cache asset");
    WriteCollection(output, asset.nodes, [](std::ostream& stream, const NodeData& node) {
        WriteString(stream, node.name); WritePod(stream, node.parent); WriteTransform(stream, node.transform);
        WriteCollection(stream, node.meshes, [](std::ostream& nested, std::size_t value) {
            WritePod(nested, static_cast<std::uint64_t>(value));
        });
    });
    WriteCollection(output, asset.meshes, [](std::ostream& stream, const MeshData& mesh) {
        WriteString(stream, mesh.name);
        WriteCollection(stream, mesh.positions, WriteVec3);
        WriteCollection(stream, mesh.normals, WriteVec3);
        WriteCollection(stream, mesh.texcoords, [](std::ostream& nested,
                                                   const std::array<double, 2>& value) {
            WritePod(nested, value[0]); WritePod(nested, value[1]);
        });
        WriteCollection(stream, mesh.indices, [](std::ostream& nested, std::uint32_t value) { WritePod(nested, value); });
        WritePod(stream, static_cast<std::uint64_t>(mesh.material_index));
    });
    WriteCollection(output, asset.materials, [](std::ostream& stream, const MaterialData& material) {
        WriteString(stream, material.name);
        for (const double channel : material.base_color) WritePod(stream, channel);
        WritePath(stream, material.base_color_texture);
    });
    WriteCollection(output, asset.textures, [](std::ostream& stream, const TextureData& texture) {
        WritePath(stream, texture.source); WriteString(stream, texture.semantic);
    });
    WriteCollection(output, asset.joints, [](std::ostream& stream, const JointData& joint) {
        WriteString(stream, joint.name); WritePod(stream, joint.type); WriteString(stream, joint.parent_link);
        WriteString(stream, joint.child_link); WriteTransform(stream, joint.origin); WriteVec3(stream, joint.axis);
        WritePod(stream, joint.lower_limit); WritePod(stream, joint.upper_limit);
    });
    WriteCollection(output, asset.links, [](std::ostream& stream, const LinkData& link) {
        WriteString(stream, link.name); WriteTransform(stream, link.origin);
    });
    WriteCollection(output, asset.inertials, [](std::ostream& stream, const InertialData& inertial) {
        WriteString(stream, inertial.link); WritePod(stream, inertial.mass); WriteVec3(stream, inertial.center_of_mass);
        for (const double value : inertial.inertia) WritePod(stream, value);
    });
    WriteCollection(output, asset.colliders, [](std::ostream& stream, const ColliderData& collider) {
        WriteString(stream, collider.link); WritePod(stream, collider.shape); WriteVec3(stream, collider.size);
        WritePath(stream, collider.mesh); WriteTransform(stream, collider.origin);
    });
    WriteCollection(output, asset.visuals, [](std::ostream& stream, const VisualData& visual) {
        WriteString(stream, visual.link); WritePod(stream, visual.shape); WriteVec3(stream, visual.size);
        WritePath(stream, visual.mesh); WriteTransform(stream, visual.origin);
        for (const float channel : visual.color) WritePod(stream, channel);
        WritePath(stream, visual.base_color_texture);
        WritePod(stream, static_cast<std::uint8_t>(visual.use_mesh_material));
    });
    WriteCollection(output, asset.sensors, [](std::ostream& stream, const SensorData& sensor) {
        WriteString(stream, sensor.name); WriteString(stream, sensor.frame); WriteString(stream, sensor.type);
    });
    WritePath(output, asset.metadata.source); WriteString(output, asset.metadata.importer);
    WriteString(output, asset.metadata.importer_version); WritePod(output, asset.metadata.asset_ir_version);
    WriteString(output, asset.metadata.coordinate_system); WritePod(output, asset.metadata.meters_per_unit);
    WriteCollection(output, asset.metadata.dependencies,
                    [](std::ostream& stream, const std::filesystem::path& path) { WritePath(stream, path); });
    WriteCollection(output, asset.metadata.dependency_hashes,
                    [](std::ostream& stream, std::uint64_t hash) { WritePod(stream, hash); });
}

AssetIR ReadAsset(std::istream& input) {
    SIM_LOG_INFO("Assets", "Read Asset");
    AssetIR asset;
    asset.nodes = ReadCollection<NodeData>(input, [](std::istream& stream) {
        NodeData node; node.name = ReadString(stream); node.parent = ReadPod<std::int64_t>(stream);
        node.transform = ReadTransform(stream);
        node.meshes = ReadCollection<std::size_t>(stream, [](std::istream& nested) {
            return static_cast<std::size_t>(ReadPod<std::uint64_t>(nested));
        }); return node;
    });
    asset.meshes = ReadCollection<MeshData>(input, [](std::istream& stream) {
        MeshData mesh; mesh.name = ReadString(stream); mesh.positions = ReadCollection<Vec3>(stream, ReadVec3);
        mesh.normals = ReadCollection<Vec3>(stream, ReadVec3);
        mesh.texcoords = ReadCollection<std::array<double, 2>>(stream, [](std::istream& nested) {
            return std::array<double, 2>{ReadPod<double>(nested), ReadPod<double>(nested)};
        });
        mesh.indices = ReadCollection<std::uint32_t>(stream, [](std::istream& nested) { return ReadPod<std::uint32_t>(nested); });
        mesh.material_index = static_cast<std::size_t>(ReadPod<std::uint64_t>(stream)); return mesh;
    });
    asset.materials = ReadCollection<MaterialData>(input, [](std::istream& stream) {
        MaterialData material; material.name = ReadString(stream);
        for (double& channel : material.base_color) {
            channel = ReadPod<double>(stream);
        }
        material.base_color_texture = ReadPath(stream);
        return material;
    });
    asset.textures = ReadCollection<TextureData>(input, [](std::istream& stream) {
        return TextureData{ReadPath(stream), ReadString(stream)};
    });
    asset.joints = ReadCollection<JointData>(input, [](std::istream& stream) {
        JointData joint; joint.name = ReadString(stream); joint.type = ReadPod<JointType>(stream);
        joint.parent_link = ReadString(stream); joint.child_link = ReadString(stream); joint.origin = ReadTransform(stream);
        joint.axis = ReadVec3(stream); joint.lower_limit = ReadPod<double>(stream); joint.upper_limit = ReadPod<double>(stream);
        return joint;
    });
    asset.links = ReadCollection<LinkData>(input, [](std::istream& stream) {
        return LinkData{ReadString(stream), ReadTransform(stream)};
    });
    asset.inertials = ReadCollection<InertialData>(input, [](std::istream& stream) {
        InertialData value; value.link = ReadString(stream); value.mass = ReadPod<double>(stream);
        value.center_of_mass = ReadVec3(stream); for (double& item : value.inertia) item = ReadPod<double>(stream);
        return value;
    });
    asset.colliders = ReadCollection<ColliderData>(input, [](std::istream& stream) {
        ColliderData value; value.link = ReadString(stream); value.shape = ReadPod<ColliderShape>(stream);
        value.size = ReadVec3(stream); value.mesh = ReadPath(stream); value.origin = ReadTransform(stream); return value;
    });
    asset.visuals = ReadCollection<VisualData>(input, [](std::istream& stream) {
        VisualData value; value.link = ReadString(stream); value.shape = ReadPod<ColliderShape>(stream);
        value.size = ReadVec3(stream); value.mesh = ReadPath(stream); value.origin = ReadTransform(stream);
        for (float& channel : value.color) channel = ReadPod<float>(stream);
        value.base_color_texture = ReadPath(stream);
        value.use_mesh_material = ReadPod<std::uint8_t>(stream) != 0;
        return value;
    });
    asset.sensors = ReadCollection<SensorData>(input, [](std::istream& stream) {
        return SensorData{ReadString(stream), ReadString(stream), ReadString(stream)};
    });
    asset.metadata.source = ReadPath(input); asset.metadata.importer = ReadString(input);
    asset.metadata.importer_version = ReadString(input); asset.metadata.asset_ir_version = ReadPod<std::uint32_t>(input);
    asset.metadata.coordinate_system = ReadString(input); asset.metadata.meters_per_unit = ReadPod<double>(input);
    asset.metadata.dependencies = ReadCollection<std::filesystem::path>(input, ReadPath);
    asset.metadata.dependency_hashes = ReadCollection<std::uint64_t>(input,
        [](std::istream& stream) { return ReadPod<std::uint64_t>(stream); });
    return asset;
}

std::string ReadText(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw Error(ErrorCode::IoError, "cannot open asset: " + PathUtf8(path));
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

std::optional<std::string> Attribute(std::string_view tag, std::string_view name) {
    const std::regex expression(std::string("(?:^|\\s)") + std::string(name) + R"(\s*=\s*["']([^"']*)["'])",
                                std::regex::icase);
    std::match_results<std::string_view::const_iterator> match;
    if (std::regex_search(tag.begin(), tag.end(), match, expression)) return match[1].str();
    return std::nullopt;
}

Vec3 ParseVec3(const std::string& text, Vec3 fallback = {}) {
    std::istringstream input(text);
    Vec3 result;
    if (!(input >> result.x >> result.y >> result.z)) return fallback;
    return result;
}

Transform ParseOrigin(std::string_view block) {
    const std::regex origin_expression(R"(<origin\b([^>]*)/?>)", std::regex::icase);
    std::match_results<std::string_view::const_iterator> match;
    Transform result;
    if (std::regex_search(block.begin(), block.end(), match, origin_expression)) {
        const std::string attributes(match[1].first, match[1].second);
        if (const auto xyz = Attribute(attributes, "xyz")) result.translation = ParseVec3(*xyz);
        if (const auto rpy = Attribute(attributes, "rpy")) {
            const Vec3 angles = ParseVec3(*rpy);
            const double cr = std::cos(angles.x * 0.5), sr = std::sin(angles.x * 0.5);
            const double cp = std::cos(angles.y * 0.5), sp = std::sin(angles.y * 0.5);
            const double cy = std::cos(angles.z * 0.5), sy = std::sin(angles.z * 0.5);
            result.rotation = {cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy,
                               cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy};
        }
    }
    return result;
}

JointType ParseJointType(const std::string& value) {
    const std::string type = Lowercase(value);
    if (type == "revolute") return JointType::Revolute;
    if (type == "continuous") return JointType::Continuous;
    if (type == "prismatic") return JointType::Prismatic;
    if (type == "floating") return JointType::Floating;
    if (type == "planar") return JointType::Planar;
    return JointType::Fixed;
}

std::optional<std::string> ChildAttribute(std::string_view block, const std::string& element,
                                          const std::string& attribute) {
    const std::regex expression("<" + element + R"(\b([^>]*)/?>)", std::regex::icase);
    std::match_results<std::string_view::const_iterator> match;
    if (!std::regex_search(block.begin(), block.end(), match, expression)) return std::nullopt;
    return Attribute(std::string(match[1].first, match[1].second), attribute);
}

bool IsFinite(const Vec3& value) {
    return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

#if defined(SIM_HAS_URDFDOM)
Transform FromUrdfPose(const urdf::Pose& pose) {
    Transform result;
    result.translation = {pose.position.x, pose.position.y, pose.position.z};
    result.rotation = {pose.rotation.w, pose.rotation.x, pose.rotation.y, pose.rotation.z};
    return result;
}

JointType FromUrdfJointType(int type) {
    switch (type) {
        case urdf::Joint::REVOLUTE: return JointType::Revolute;
        case urdf::Joint::CONTINUOUS: return JointType::Continuous;
        case urdf::Joint::PRISMATIC: return JointType::Prismatic;
        case urdf::Joint::FLOATING: return JointType::Floating;
        case urdf::Joint::PLANAR: return JointType::Planar;
        default: return JointType::Fixed;
    }
}

ImportResult ImportWithUrdfdom(const std::filesystem::path& path) {
    SIM_LOG_VERBOSE("Import with urdfdom, path=" + PathUtf8(path));
    const auto model = urdf::parseURDF(ReadText(path));
    if (!model) throw Error(ErrorCode::ValidationFailed, "urdfdom failed to parse URDF");
    ImportResult result;
    result.asset.metadata.source = std::filesystem::absolute(path);
    result.asset.metadata.importer = "urdfdom";
    result.asset.nodes.push_back({model->getName(), -1, Transform::Identity(), {}});
    for (const auto& [name, link] : model->links_) {
        result.asset.links.push_back({name, Transform::Identity()});
        if (link->inertial) {
            const auto& source = *link->inertial;
            result.asset.inertials.push_back(
                {name, source.mass,
                 {source.origin.position.x, source.origin.position.y, source.origin.position.z},
                 {source.ixx, source.ixy, source.ixz, source.iyy, source.iyz, source.izz}});
        }
        for (const auto& source_visual : link->visual_array) {
            if (!source_visual || !source_visual->geometry) continue;
            VisualData visual;
            visual.link = name;
            visual.origin = FromUrdfPose(source_visual->origin);
            switch (source_visual->geometry->type) {
                case urdf::Geometry::BOX: {
                    const auto box = urdf::dynamic_pointer_cast<urdf::Box>(source_visual->geometry);
                    visual.shape = ColliderShape::Box;
                    visual.size = {box->dim.x, box->dim.y, box->dim.z};
                    break;
                }
                case urdf::Geometry::SPHERE: {
                    const auto sphere = urdf::dynamic_pointer_cast<urdf::Sphere>(source_visual->geometry);
                    visual.shape = ColliderShape::Sphere;
                    visual.size = {sphere->radius, sphere->radius, sphere->radius};
                    break;
                }
                case urdf::Geometry::CYLINDER: {
                    const auto cylinder = urdf::dynamic_pointer_cast<urdf::Cylinder>(source_visual->geometry);
                    visual.shape = ColliderShape::Cylinder;
                    visual.size = {cylinder->radius, cylinder->radius, cylinder->length};
                    break;
                }
                case urdf::Geometry::MESH: {
                    const auto mesh = urdf::dynamic_pointer_cast<urdf::Mesh>(source_visual->geometry);
                    visual.shape = ColliderShape::Mesh;
                    visual.mesh = ResolveUrdfResource(path, mesh->filename);
                    visual.origin.scale = MultiplyComponents(
                        visual.origin.scale, {mesh->scale.x, mesh->scale.y, mesh->scale.z});
                    result.asset.metadata.dependencies.push_back(visual.mesh);
                    break;
                }
                default: continue;
            }
            if (source_visual->material) {
                visual.use_mesh_material = false;
                const auto& color = source_visual->material->color;
                visual.color = {static_cast<float>(color.r), static_cast<float>(color.g),
                                static_cast<float>(color.b), static_cast<float>(color.a)};
                if (!source_visual->material->texture_filename.empty()) {
                    visual.base_color_texture =
                        ResolveUrdfResource(path, source_visual->material->texture_filename);
                    result.asset.metadata.dependencies.push_back(visual.base_color_texture);
                    result.asset.textures.push_back({visual.base_color_texture, "base_color"});
                }
            }
            result.asset.visuals.push_back(std::move(visual));
        }
        for (const auto& collision : link->collision_array) {
            if (!collision || !collision->geometry) continue;
            ColliderData collider;
            collider.link = name;
            collider.origin = FromUrdfPose(collision->origin);
            switch (collision->geometry->type) {
                case urdf::Geometry::BOX: {
                    const auto box = urdf::dynamic_pointer_cast<urdf::Box>(collision->geometry);
                    collider.shape = ColliderShape::Box;
                    collider.size = {box->dim.x, box->dim.y, box->dim.z};
                    break;
                }
                case urdf::Geometry::SPHERE: {
                    const auto sphere = urdf::dynamic_pointer_cast<urdf::Sphere>(collision->geometry);
                    collider.shape = ColliderShape::Sphere;
                    collider.size = {sphere->radius, sphere->radius, sphere->radius};
                    break;
                }
                case urdf::Geometry::CYLINDER: {
                    const auto cylinder = urdf::dynamic_pointer_cast<urdf::Cylinder>(collision->geometry);
                    collider.shape = ColliderShape::Cylinder;
                    collider.size = {cylinder->radius, cylinder->radius, cylinder->length};
                    break;
                }
                case urdf::Geometry::MESH: {
                    const auto mesh = urdf::dynamic_pointer_cast<urdf::Mesh>(collision->geometry);
                    collider.shape = ColliderShape::Mesh;
                    collider.mesh = ResolveUrdfResource(path, mesh->filename);
                    collider.origin.scale = MultiplyComponents(
                        collider.origin.scale, {mesh->scale.x, mesh->scale.y, mesh->scale.z});
                    result.asset.metadata.dependencies.push_back(collider.mesh);
                    break;
                }
                default: continue;
            }
            result.asset.colliders.push_back(std::move(collider));
        }
    }
    for (const auto& [name, source] : model->joints_) {
        JointData joint;
        joint.name = name;
        joint.type = FromUrdfJointType(source->type);
        joint.parent_link = source->parent_link_name;
        joint.child_link = source->child_link_name;
        joint.origin = FromUrdfPose(source->parent_to_joint_origin_transform);
        joint.axis = {source->axis.x, source->axis.y, source->axis.z};
        if (source->limits) {
            joint.lower_limit = source->limits->lower;
            joint.upper_limit = source->limits->upper;
        }
        result.asset.joints.push_back(std::move(joint));
    }
    result.diagnostics = AssetValidator::validate(result.asset);
    return result;
}
#endif

}  // namespace

MeshData GenerateMeshLod(const MeshData& source, std::uint8_t level) {
    level = std::min<std::uint8_t>(level, 7);
    if (level == 0 || source.positions.empty() || source.indices.size() < 3)
        return source;

    constexpr std::array<int, 8> resolutions{0, 64, 42, 28, 20, 14, 8, 4};
    Vec3 minimum{std::numeric_limits<double>::max(), std::numeric_limits<double>::max(),
                 std::numeric_limits<double>::max()};
    Vec3 maximum{std::numeric_limits<double>::lowest(), std::numeric_limits<double>::lowest(),
                 std::numeric_limits<double>::lowest()};
    for (const auto& point : source.positions) {
        minimum = {std::min(minimum.x, point.x), std::min(minimum.y, point.y),
                   std::min(minimum.z, point.z)};
        maximum = {std::max(maximum.x, point.x), std::max(maximum.y, point.y),
                   std::max(maximum.z, point.z)};
    }
    const Vec3 extent = maximum - minimum;
    const int resolution = resolutions[level];
    struct Cluster {
        Vec3 position{};
        std::array<double, 2> texcoord{};
        std::size_t count{0};
    };
    std::unordered_map<std::uint64_t, std::uint32_t> cluster_indices;
    std::vector<Cluster> clusters;
    std::vector<std::uint32_t> remap(source.positions.size());
    const auto cell = [&](double value, double low, double span) {
        if (span <= 1.0e-12) return 0;
        return std::clamp(static_cast<int>((value - low) / span * resolution),
                          0, resolution - 1);
    };
    for (std::size_t index = 0; index < source.positions.size(); ++index) {
        const auto& point = source.positions[index];
        const auto x = static_cast<std::uint64_t>(cell(point.x, minimum.x, extent.x));
        const auto y = static_cast<std::uint64_t>(cell(point.y, minimum.y, extent.y));
        const auto z = static_cast<std::uint64_t>(cell(point.z, minimum.z, extent.z));
        const std::uint64_t key = x | (y << 8U) | (z << 16U);
        auto [found, inserted] = cluster_indices.emplace(
            key, static_cast<std::uint32_t>(clusters.size()));
        if (inserted) clusters.push_back({});
        remap[index] = found->second;
        auto& cluster = clusters[found->second];
        cluster.position = cluster.position + point;
        if (index < source.texcoords.size()) {
            cluster.texcoord[0] += source.texcoords[index][0];
            cluster.texcoord[1] += source.texcoords[index][1];
        }
        ++cluster.count;
    }

    MeshData result;
    result.name = source.name;
    result.material_index = source.material_index;
    result.positions.reserve(clusters.size());
    result.texcoords.reserve(clusters.size());
    for (const auto& cluster : clusters) {
        const double inverse = 1.0 / static_cast<double>(cluster.count);
        result.positions.push_back(cluster.position * inverse);
        result.texcoords.push_back({cluster.texcoord[0] * inverse,
                                    cluster.texcoord[1] * inverse});
    }
    result.indices.reserve(source.indices.size());
    for (std::size_t index = 0; index + 2 < source.indices.size(); index += 3) {
        const auto a = source.indices[index];
        const auto b = source.indices[index + 1];
        const auto c = source.indices[index + 2];
        if (a >= remap.size() || b >= remap.size() || c >= remap.size()) continue;
        const auto first = remap[a], second = remap[b], third = remap[c];
        if (first == second || second == third || third == first) continue;
        result.indices.insert(result.indices.end(), {first, second, third});
    }
    if (result.indices.empty()) return source;

    result.normals.assign(result.positions.size(), {});
    for (std::size_t index = 0; index + 2 < result.indices.size(); index += 3) {
        const auto first = result.indices[index];
        const auto second = result.indices[index + 1];
        const auto third = result.indices[index + 2];
        const Vec3 first_edge = result.positions[second] - result.positions[first];
        const Vec3 second_edge = result.positions[third] - result.positions[first];
        const Vec3 normal{first_edge.y * second_edge.z - first_edge.z * second_edge.y,
                          first_edge.z * second_edge.x - first_edge.x * second_edge.z,
                          first_edge.x * second_edge.y - first_edge.y * second_edge.x};
        result.normals[first] = result.normals[first] + normal;
        result.normals[second] = result.normals[second] + normal;
        result.normals[third] = result.normals[third] + normal;
    }
    for (auto& normal : result.normals) {
        const double length = std::sqrt(normal.x * normal.x + normal.y * normal.y +
                                        normal.z * normal.z);
        normal = length > 1.0e-12 ? normal * (1.0 / length) : Vec3{1.0, 0.0, 0.0};
    }
    return result;
}

std::span<const std::string_view> UrdfImporter::extensions() const noexcept {
    static constexpr std::array values{std::string_view{".urdf"}};
    return values;
}

ImportResult UrdfImporter::import(const std::filesystem::path& path) const {
    SIM_LOG_VERBOSE("UrdfImporter::import parses path=" + PathUtf8(path));
    SIM_PROFILE_SCOPE("Asset.ImportUrdf");
#if defined(SIM_HAS_URDFDOM)
    return ImportWithUrdfdom(path);
#else
    const std::string text = ReadText(path);
    ImportResult result;
    result.asset.metadata.source = std::filesystem::absolute(path);
    result.asset.metadata.importer = std::string(name());
    const std::regex robot_expression(R"(<robot\b([^>]*)>)", std::regex::icase);
    std::smatch robot_match;
    if (!std::regex_search(text, robot_match, robot_expression)) {
        throw Error(ErrorCode::ValidationFailed, "URDF has no robot root element");
    }
    const std::string robot_name = Attribute(robot_match[1].str(), "name").value_or(path.stem().string());
    result.asset.nodes.push_back({robot_name, -1, Transform::Identity(), {}});

    const std::regex link_expression(R"(<link\b([^>]*?)(?:/>|>([\s\S]*?)</link>))", std::regex::icase);
    for (auto it = std::sregex_iterator(text.begin(), text.end(), link_expression);
         it != std::sregex_iterator(); ++it) {
        if (const auto link_name = Attribute((*it)[1].str(), "name")) {
            result.asset.links.push_back({*link_name, Transform::Identity()});
            const std::string block = (*it)[2].str();
            const std::regex inertial_expression(R"(<inertial\b[^>]*>([\s\S]*?)</inertial>)",
                                                  std::regex::icase);
            std::smatch inertial_match;
            if (std::regex_search(block, inertial_match, inertial_expression)) {
                const std::string inertial_block = inertial_match[1].str();
                InertialData inertial;
                inertial.link = *link_name;
                inertial.center_of_mass = ParseOrigin(inertial_block).translation;
                if (const auto mass = ChildAttribute(inertial_block, "mass", "value"))
                    inertial.mass = std::stod(*mass);
                const std::regex matrix_expression(R"(<inertia\b([^>]*)/?>)", std::regex::icase);
                std::smatch matrix_match;
                if (std::regex_search(inertial_block, matrix_match, matrix_expression)) {
                    const std::string matrix = matrix_match[1].str();
                    const std::array<const char*, 6> names{"ixx", "ixy", "ixz", "iyy", "iyz", "izz"};
                    for (std::size_t index = 0; index < names.size(); ++index)
                        if (const auto value = Attribute(matrix, names[index]))
                            inertial.inertia[index] = std::stod(*value);
                }
                result.asset.inertials.push_back(std::move(inertial));
            }

            const std::regex visual_expression(R"(<visual\b[^>]*>([\s\S]*?)</visual>)",
                                                std::regex::icase);
            for (auto visual_it = std::sregex_iterator(block.begin(), block.end(), visual_expression);
                 visual_it != std::sregex_iterator(); ++visual_it) {
                const std::string visual_block = (*visual_it)[1].str();
                VisualData visual;
                visual.link = *link_name;
                visual.origin = ParseOrigin(visual_block);
                bool recognized = false;
                const std::regex box_expression(R"(<box\b([^>]*)/?>)", std::regex::icase);
                const std::regex sphere_expression(R"(<sphere\b([^>]*)/?>)", std::regex::icase);
                const std::regex cylinder_expression(R"(<cylinder\b([^>]*)/?>)", std::regex::icase);
                const std::regex mesh_expression(R"(<mesh\b([^>]*)/?>)", std::regex::icase);
                std::smatch geometry_match;
                if (std::regex_search(visual_block, geometry_match, box_expression)) {
                    visual.shape = ColliderShape::Box;
                    visual.size = ParseVec3(Attribute(geometry_match[1].str(), "size").value_or("1 1 1"),
                                            {1.0, 1.0, 1.0});
                    recognized = true;
                } else if (std::regex_search(visual_block, geometry_match, sphere_expression)) {
                    visual.shape = ColliderShape::Sphere;
                    const double radius = std::stod(Attribute(geometry_match[1].str(), "radius").value_or("1"));
                    visual.size = {radius, radius, radius};
                    recognized = true;
                } else if (std::regex_search(visual_block, geometry_match, cylinder_expression)) {
                    visual.shape = ColliderShape::Cylinder;
                    const double radius = std::stod(Attribute(geometry_match[1].str(), "radius").value_or("1"));
                    const double length = std::stod(Attribute(geometry_match[1].str(), "length").value_or("1"));
                    visual.size = {radius, radius, length};
                    recognized = true;
                } else if (std::regex_search(visual_block, geometry_match, mesh_expression)) {
                    visual.shape = ColliderShape::Mesh;
                    if (const auto filename = Attribute(geometry_match[1].str(), "filename")) {
                        visual.mesh = ResolveUrdfResource(path, *filename);
                        result.asset.metadata.dependencies.push_back(visual.mesh);
                    }
                    if (const auto scale = Attribute(geometry_match[1].str(), "scale"))
                        visual.origin.scale = MultiplyComponents(visual.origin.scale,
                                                                 ParseVec3(*scale, {1.0, 1.0, 1.0}));
                    recognized = true;
                }
                const std::regex color_expression(R"(<color\b([^>]*)/?>)", std::regex::icase);
                std::smatch color_match;
                if (std::regex_search(visual_block, color_match, color_expression)) {
                    if (const auto rgba = Attribute(color_match[1].str(), "rgba")) {
                        visual.use_mesh_material = false;
                        std::istringstream values(*rgba);
                        values >> visual.color[0] >> visual.color[1] >> visual.color[2] >> visual.color[3];
                    }
                }
                if (recognized) result.asset.visuals.push_back(std::move(visual));
            }

            const std::regex collision_expression(R"(<collision\b[^>]*>([\s\S]*?)</collision>)",
                                                   std::regex::icase);
            for (auto collision_it = std::sregex_iterator(block.begin(), block.end(), collision_expression);
                 collision_it != std::sregex_iterator(); ++collision_it) {
                const std::string collision_block = (*collision_it)[1].str();
                ColliderData collider;
                collider.link = *link_name;
                collider.origin = ParseOrigin(collision_block);
                bool recognized = false;
                const std::regex box_expression(R"(<box\b([^>]*)/?>)", std::regex::icase);
                const std::regex sphere_expression(R"(<sphere\b([^>]*)/?>)", std::regex::icase);
                const std::regex cylinder_expression(R"(<cylinder\b([^>]*)/?>)", std::regex::icase);
                const std::regex mesh_expression(R"(<mesh\b([^>]*)/?>)", std::regex::icase);
                std::smatch geometry_match;
                if (std::regex_search(collision_block, geometry_match, box_expression)) {
                    collider.shape = ColliderShape::Box;
                    collider.size = ParseVec3(Attribute(geometry_match[1].str(), "size").value_or("1 1 1"),
                                              {1.0, 1.0, 1.0});
                    recognized = true;
                } else if (std::regex_search(collision_block, geometry_match, sphere_expression)) {
                    collider.shape = ColliderShape::Sphere;
                    const double radius = std::stod(Attribute(geometry_match[1].str(), "radius").value_or("1"));
                    collider.size = {radius, radius, radius};
                    recognized = true;
                } else if (std::regex_search(collision_block, geometry_match, cylinder_expression)) {
                    collider.shape = ColliderShape::Cylinder;
                    const double radius = std::stod(Attribute(geometry_match[1].str(), "radius").value_or("1"));
                    const double length = std::stod(Attribute(geometry_match[1].str(), "length").value_or("1"));
                    collider.size = {radius, radius, length};
                    recognized = true;
                } else if (std::regex_search(collision_block, geometry_match, mesh_expression)) {
                    collider.shape = ColliderShape::Mesh;
                    if (const auto filename = Attribute(geometry_match[1].str(), "filename")) {
                        collider.mesh = ResolveUrdfResource(path, *filename);
                        result.asset.metadata.dependencies.push_back(collider.mesh);
                    }
                    if (const auto scale = Attribute(geometry_match[1].str(), "scale"))
                        collider.origin.scale = MultiplyComponents(collider.origin.scale,
                                                                   ParseVec3(*scale, {1.0, 1.0, 1.0}));
                    recognized = true;
                }
                if (recognized) result.asset.colliders.push_back(std::move(collider));
            }
        }
    }

    // A URDF transmission contains a nested <joint> element which describes
    // the actuator mapping, not a kinematic joint in the robot tree.  The
    // fallback importer used on builds without urdfdom used to scan the whole
    // document and consequently imported those transmission joints as if they
    // were links in the kinematic graph.  Keep the fallback parser aligned
    // with urdfdom: only direct children of <robot> are motion joints.
    const std::regex robot_body_expression(
        R"(<robot\b[^>]*>([\s\S]*?)</robot\s*>)", std::regex::icase);
    std::smatch robot_body_match;
    if (!std::regex_search(text, robot_body_match, robot_body_expression)) {
        throw Error(ErrorCode::ValidationFailed, "URDF has no complete robot element");
    }
    const std::string robot_body = robot_body_match[1].str();
    const std::regex top_level_tag_expression(
        R"(<(/?)([A-Za-z_][A-Za-z0-9_.:-]*)(?:\s[^<>]*?)?(/?)>)", std::regex::icase);
    const std::regex joint_expression(
        R"(<joint\b([^>]*)>([\s\S]*?)</joint\s*>)", std::regex::icase);
    std::size_t depth = 0;
    std::size_t joint_start = std::string::npos;
    for (auto tag = std::sregex_iterator(robot_body.begin(), robot_body.end(), top_level_tag_expression);
         tag != std::sregex_iterator(); ++tag) {
        const auto match = *tag;
        const bool closing = !match[1].str().empty();
        const bool self_closing = !match[3].str().empty();
        const std::string name = Lowercase(match[2].str());
        if (!closing) {
            // A motion joint is a direct child of the robot element.  Record
            // its complete block and parse it with the existing field logic.
            if (depth == 0 && name == "joint" && !self_closing)
                joint_start = static_cast<std::size_t>(match.position());
            if (!self_closing) ++depth;
        } else {
            if (depth > 0) --depth;
            if (name == "joint" && depth == 0 && joint_start != std::string::npos) {
                const auto end = static_cast<std::size_t>(match.position() + match.length());
                const std::string block = robot_body.substr(joint_start, end - joint_start);
                std::smatch parsed;
                if (std::regex_search(block, parsed, joint_expression)) {
                    // Reuse the same parser below by handling this one block.
                    const std::string attributes = parsed[1].str();
                    const std::string contents = parsed[2].str();
                    JointData joint;
                    joint.name = Attribute(attributes, "name").value_or("");
                    joint.type = ParseJointType(Attribute(attributes, "type").value_or("fixed"));
                    joint.parent_link = ChildAttribute(contents, "parent", "link").value_or("");
                    joint.child_link = ChildAttribute(contents, "child", "link").value_or("");
                    joint.axis = ParseVec3(ChildAttribute(contents, "axis", "xyz").value_or("1 0 0"),
                                           {1.0, 0.0, 0.0});
                    joint.origin = ParseOrigin(contents);
                    if (const auto lower = ChildAttribute(contents, "limit", "lower"))
                        joint.lower_limit = std::stod(*lower);
                    if (const auto upper = ChildAttribute(contents, "limit", "upper"))
                        joint.upper_limit = std::stod(*upper);
                    result.asset.joints.push_back(std::move(joint));
                }
                joint_start = std::string::npos;
            }
        }
    }
    result.diagnostics = AssetValidator::validate(result.asset);
    return result;
#endif
}

std::span<const std::string_view> ObjImporter::extensions() const noexcept {
    static constexpr std::array values{std::string_view{".obj"}};
    return values;
}

ImportResult ObjImporter::import(const std::filesystem::path& path) const {
    SIM_LOG_VERBOSE("ObjImporter::import parses path=" + PathUtf8(path));
    SIM_PROFILE_SCOPE("Asset.ImportObj");
    std::ifstream input(path);
    if (!input) throw Error(ErrorCode::IoError, "cannot open OBJ: " + PathUtf8(path));
    ImportResult result;
    result.asset.metadata.source = std::filesystem::absolute(path);
    result.asset.metadata.importer = std::string(name());
    MeshData mesh; mesh.name = path.stem().string();
    // Position indices are global across OBJ object/group boundaries.
    std::vector<Vec3> obj_positions;
    std::vector<Vec3> obj_normals;
    std::vector<MeshData> object_meshes;
    std::vector<std::string> object_materials;
    std::string active_material;
    std::string object_name = mesh.name;
    const auto begin_object = [&] {
        if (!mesh.indices.empty()) {
            mesh.name = object_name;
            object_meshes.push_back(std::move(mesh));
            object_materials.push_back(active_material);
            mesh = MeshData{};
        }
        mesh.name = object_name;
    };
    std::vector<std::array<double, 2>> obj_texcoords;
    std::vector<std::filesystem::path> material_libraries;
    std::unordered_map<std::string, std::size_t> material_indices;
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        std::istringstream values(line);
        std::string kind; values >> kind;
        if (kind == "o" || kind == "g") {
            std::string name;
            values >> name;
            if (!name.empty()) {
                begin_object();
                object_name = name;
                mesh.name = object_name;
            }
        } else if (kind == "v") {
            Vec3 position;
            if (values >> position.x >> position.y >> position.z) obj_positions.push_back(position);
        } else if (kind == "vn") {
            Vec3 normal;
            if (values >> normal.x >> normal.y >> normal.z) obj_normals.push_back(normal);
        } else if (kind == "vt") {
            std::array<double, 2> texcoord{};
            if (values >> texcoord[0] >> texcoord[1]) obj_texcoords.push_back(texcoord);
        } else if (kind == "f") {
            std::vector<std::uint32_t> face;
            std::string vertex;
            while (values >> vertex) {
                const auto slash = vertex.find('/');
                const std::string index_text = vertex.substr(0, slash);
                const long long index = std::stoll(index_text);
                const long long resolved = index > 0 ? index - 1 : static_cast<long long>(obj_positions.size()) + index;
                if (resolved < 0 || resolved >= static_cast<long long>(obj_positions.size())) {
                    throw Error(ErrorCode::ValidationFailed, "OBJ face index out of range at line " +
                                                                std::to_string(line_number));
                }
                face.push_back(static_cast<std::uint32_t>(resolved));
            }
            for (std::size_t index = 1; index + 1 < face.size(); ++index) {
                mesh.indices.insert(mesh.indices.end(), {face[0], face[index], face[index + 1]});
            }
        } else if (kind == "mtllib") {
            std::string dependency;
            while (values >> dependency) {
                const auto resolved = ResolveAssetReference(path, dependency);
                material_libraries.push_back(resolved);
                result.asset.metadata.dependencies.push_back(resolved);
            }
        } else if (kind == "usemtl") {
            values >> active_material;
            const auto found = material_indices.find(active_material);
            if (found != material_indices.end()) mesh.material_index = found->second;
        }
    }

    // Parse the small, portable subset of MTL needed by the renderer.  Keep
    // paths absolute and normalized so cache/upload dependency handling is
    // deterministic across platforms.
    for (const auto& library : material_libraries) {
        std::ifstream material_file(library);
        if (!material_file) continue;  // ResourceManager reports missing deps.
        MaterialData* current = nullptr;
        std::string material_line;
        while (std::getline(material_file, material_line)) {
            std::istringstream values(material_line);
            std::string kind; values >> kind;
            if (kind == "newmtl") {
                std::string name; values >> name;
                if (name.empty()) continue;
                const auto [it, inserted] = material_indices.emplace(name, result.asset.materials.size());
                if (inserted) {
                    result.asset.materials.push_back(MaterialData{});
                    result.asset.materials.back().name = name;
                }
                current = &result.asset.materials[it->second];
            } else if (current != nullptr && kind == "Kd") {
                double r, g, b;
                if (values >> r >> g >> b) {
                    current->base_color = {r, g, b, current->base_color[3]};
                }
            } else if (current != nullptr && (kind == "map_Kd" || kind == "map_kd")) {
                std::string texture, token;
                while (values >> token) texture = token;
                if (!texture.empty()) {
                    current->base_color_texture = ResolveAssetReference(library, texture);
                    result.asset.metadata.dependencies.push_back(current->base_color_texture);
                    result.asset.textures.push_back({current->base_color_texture, "base_color"});
                }
            }
        }
    }
    if (!active_material.empty()) {
        const auto found = material_indices.find(active_material);
        if (found != material_indices.end()) mesh.material_index = found->second;
    }
    if (!mesh.indices.empty()) {
        mesh.name = object_name;
        object_meshes.push_back(std::move(mesh));
        object_materials.push_back(active_material);
    }
    if (object_meshes.empty()) object_meshes.push_back(std::move(mesh));
    for (std::size_t index = 0; index < object_meshes.size(); ++index) {
        auto& object = object_meshes[index];
        if (index < object_materials.size() && material_indices.contains(object_materials[index]))
            object.material_index = material_indices.at(object_materials[index]);
        // OBJ indices are global. Compact each object's referenced vertices so
        // neither its bounds nor its convex collision hull includes other objects.
        std::unordered_map<std::uint32_t, std::uint32_t> remap;
        for (auto& vertex : object.indices) {
            const auto original = vertex;
            auto [it, inserted] = remap.emplace(original, static_cast<std::uint32_t>(object.positions.size()));
            if (inserted) {
                object.positions.push_back(obj_positions[original]);
                if (obj_texcoords.size() == obj_positions.size()) object.texcoords.push_back(obj_texcoords[original]);
                if (obj_normals.size() == obj_positions.size()) object.normals.push_back(obj_normals[original]);
            }
            vertex = it->second;
        }
    }
    std::sort(result.asset.metadata.dependencies.begin(), result.asset.metadata.dependencies.end());
    result.asset.metadata.dependencies.erase(
        std::unique(result.asset.metadata.dependencies.begin(), result.asset.metadata.dependencies.end()),
        result.asset.metadata.dependencies.end());
    std::vector<std::size_t> mesh_indices;
    for (auto& object : object_meshes) {
        mesh_indices.push_back(result.asset.meshes.size());
        result.asset.meshes.push_back(std::move(object));
    }
    result.asset.nodes.push_back({path.stem().string(), -1, Transform::Identity(), mesh_indices});
    result.diagnostics = AssetValidator::validate(result.asset);
    if (!material_libraries.empty()) {
        result.diagnostics.push_back(
            {Severity::Info, "asset.obj.materials",
             "OBJ material scan completed: " + std::to_string(result.asset.materials.size()) +
                 " materials, " + std::to_string(result.asset.textures.size()) + " textures"});
    }
    return result;
}

std::span<const std::string_view> AssimpImporter::extensions() const noexcept {
    static constexpr std::array values{std::string_view{".dae"}, std::string_view{".fbx"},
                                      std::string_view{".stl"}};
    return values;
}

ImportResult AssimpImporter::import(const std::filesystem::path& path) const {
    SIM_LOG_VERBOSE("AssimpImporter::import parses path=" + PathUtf8(path));
    SIM_PROFILE_SCOPE("Asset.ImportAssimp");
#if defined(SIM_HAS_ASSIMP)
    Assimp::Importer importer;
    // URDF mesh coordinates must retain the authored basis (including Z-up).
    importer.SetPropertyBool(AI_CONFIG_IMPORT_COLLADA_IGNORE_UP_DIRECTION, true);
    std::ifstream source_file(path, std::ios::binary | std::ios::ate);
    if (!source_file)
        throw Error(ErrorCode::IoError, "cannot open Assimp asset: " + PathUtf8(path));
    const auto source_size = source_file.tellg();
    if (source_size <= 0)
        throw Error(ErrorCode::ValidationFailed, "Assimp asset is empty: " + PathUtf8(path));
    std::vector<char> source_bytes(static_cast<std::size_t>(source_size));
    source_file.seekg(0);
    source_file.read(source_bytes.data(), source_size);
    if (!source_file)
        throw Error(ErrorCode::IoError, "cannot read Assimp asset: " + PathUtf8(path));
    const std::string format_hint = path.extension().string();
    const aiScene* scene = importer.ReadFileFromMemory(
        source_bytes.data(), source_bytes.size(),
        aiProcess_Triangulate | aiProcess_JoinIdenticalVertices |
            aiProcess_GenSmoothNormals | aiProcess_ValidateDataStructure |
            aiProcess_SortByPType |
            (Lowercase(path.extension().string()) == ".dae" ? aiProcess_PreTransformVertices : 0),
        format_hint.empty() ? nullptr : format_hint.c_str() + 1);
    if (scene == nullptr || scene->mRootNode == nullptr) {
        throw Error(ErrorCode::ValidationFailed,
                    "Assimp import failed for " + PathUtf8(path) + ": " + importer.GetErrorString());
    }

    ImportResult result;
    result.asset.metadata.source = std::filesystem::absolute(path);
    result.asset.metadata.importer = std::string(name());
    double meters_per_unit = 1.0;
    if (Lowercase(path.extension().string()) == ".fbx") {
        double centimeters_per_unit = 1.0;
        if (scene->mMetaData != nullptr)
            (void)scene->mMetaData->Get("UnitScaleFactor", centimeters_per_unit);
        meters_per_unit = centimeters_per_unit * 0.01;
    }
    result.asset.metadata.meters_per_unit = meters_per_unit;
    result.asset.materials.reserve(scene->mNumMaterials);
    for (unsigned int index = 0; index < scene->mNumMaterials; ++index) {
        const aiMaterial* source = scene->mMaterials[index];
        MaterialData material;
        aiString material_name;
        if (source->Get(AI_MATKEY_NAME, material_name) == AI_SUCCESS)
            material.name = material_name.C_Str();
        aiColor4D color{1.0F, 1.0F, 1.0F, 1.0F};
        if (aiGetMaterialColor(source, AI_MATKEY_BASE_COLOR, &color) != AI_SUCCESS)
            (void)aiGetMaterialColor(source, AI_MATKEY_COLOR_DIFFUSE, &color);
        material.base_color = {color.r, color.g, color.b, color.a};
        aiString texture;
        if (source->GetTexture(aiTextureType_BASE_COLOR, 0, &texture) != AI_SUCCESS)
            (void)source->GetTexture(aiTextureType_DIFFUSE, 0, &texture);
        if (texture.length > 0 && texture.C_Str()[0] != '*') {
            auto texture_path = PathFromUtf8(texture.C_Str());
            if (texture_path.is_relative()) {
                auto candidate = path.parent_path() / texture_path;
                if (!std::filesystem::exists(candidate)) {
                    const auto fbx_media = path.parent_path() /
                        (path.stem().wstring() + std::wstring(L".fbm")) /
                        texture_path.filename();
                    if (std::filesystem::exists(fbx_media)) candidate = fbx_media;
                }
                texture_path = std::move(candidate);
            }
            material.base_color_texture = std::filesystem::absolute(texture_path).lexically_normal();
            result.asset.metadata.dependencies.push_back(material.base_color_texture);
            result.asset.textures.push_back({material.base_color_texture, "base_color"});
        }
        result.asset.materials.push_back(std::move(material));
    }

    result.asset.meshes.reserve(scene->mNumMeshes);
    for (unsigned int index = 0; index < scene->mNumMeshes; ++index) {
        const aiMesh* source = scene->mMeshes[index];
        MeshData mesh;
        mesh.name = source->mName.length > 0 ? source->mName.C_Str()
                                             : "mesh_" + std::to_string(index);
        mesh.material_index = source->mMaterialIndex;
        mesh.positions.reserve(source->mNumVertices);
        if (source->HasNormals()) mesh.normals.reserve(source->mNumVertices);
        if (source->HasTextureCoords(0)) mesh.texcoords.reserve(source->mNumVertices);
        for (unsigned int vertex = 0; vertex < source->mNumVertices; ++vertex) {
            const auto& position = source->mVertices[vertex];
            mesh.positions.push_back({position.x, position.y, position.z});
            if (source->HasNormals()) {
                const auto& normal = source->mNormals[vertex];
                mesh.normals.push_back({normal.x, normal.y, normal.z});
            }
            if (source->HasTextureCoords(0)) {
                const auto& uv = source->mTextureCoords[0][vertex];
                mesh.texcoords.push_back({uv.x, uv.y});
            }
        }
        for (unsigned int face_index = 0; face_index < source->mNumFaces; ++face_index) {
            const aiFace& face = source->mFaces[face_index];
            if (face.mNumIndices != 3) continue;
            mesh.indices.insert(mesh.indices.end(), {face.mIndices[0], face.mIndices[1],
                                                      face.mIndices[2]});
        }
        result.asset.meshes.push_back(std::move(mesh));
    }

    const auto append_node = [&](const auto& self, const aiNode* source,
                                 std::int64_t parent) -> void {
        aiVector3D scale;
        aiVector3D translation;
        aiQuaternion rotation;
        source->mTransformation.Decompose(scale, rotation, translation);
        NodeData node;
        node.name = source->mName.C_Str();
        node.parent = parent;
        node.transform.translation = {translation.x, translation.y, translation.z};
        node.transform.rotation = {rotation.w, rotation.x, rotation.y, rotation.z};
        node.transform.scale = {scale.x, scale.y, scale.z};
        if (parent < 0) {
            Transform source_to_platform;
            // Assimp's conventional X-right/Y-up/-Z-forward basis becomes the
            // platform's right-handed X-forward/Y-left/Z-up basis.
            source_to_platform.rotation = {0.5, 0.5, -0.5, -0.5};
            node.transform = source_to_platform * node.transform;
        }
        node.meshes.assign(source->mMeshes, source->mMeshes + source->mNumMeshes);
        const auto current = static_cast<std::int64_t>(result.asset.nodes.size());
        result.asset.nodes.push_back(std::move(node));
        for (unsigned int child = 0; child < source->mNumChildren; ++child)
            self(self, source->mChildren[child], current);
    };
    append_node(append_node, scene->mRootNode, -1);
    result.diagnostics = AssetValidator::validate(result.asset);
    return result;
#else
    (void)path;
    throw Error(ErrorCode::Unsupported,
                "Assimp importer requires SIM_ENABLE_ASSIMP=ON");
#endif
}

void AssetImporterRegistry::add(std::unique_ptr<IAssetImporter> importer) {
    SIM_LOG_VERBOSE("AssetImporterRegistry::add registers importer formats=" +
                    (importer ? std::to_string(importer->extensions().size()) : "0"));
    if (!importer) throw Error(ErrorCode::InvalidArgument, "asset importer cannot be null");
    for (const auto extension : importer->extensions()) {
        for (const auto& existing : importers_) {
            for (const auto existing_extension : existing->extensions()) {
                if (Lowercase(std::string(extension)) == Lowercase(std::string(existing_extension))) {
                    throw Error(ErrorCode::AlreadyExists, "asset extension already has an importer");
                }
            }
        }
    }
    importers_.push_back(std::move(importer));
}

const IAssetImporter& AssetImporterRegistry::importer_for(const std::filesystem::path& path) const {
    SIM_LOG_VERBOSE("AssetImporterRegistry::importer_for selects path=" + PathUtf8(path));
    const std::string extension = Lowercase(path.extension().string());
    for (const auto& importer : importers_) {
        for (const auto supported : importer->extensions()) {
            if (extension == Lowercase(std::string(supported))) {
                SIM_LOG_VERBOSE("AssetImporterRegistry::importer_for selected extension=" + extension);
                return *importer;
            }
        }
    }
    if (extension == ".fbx") {
        throw Error(ErrorCode::Unsupported,
                    "FBX requires an Assimp-enabled build (configure SIM_ENABLE_ASSIMP=ON)");
    }
    if (extension == ".stl") {
        throw Error(ErrorCode::Unsupported,
                    "STL requires an Assimp-enabled build (configure SIM_ENABLE_ASSIMP=ON)");
    }
    throw Error(ErrorCode::Unsupported, "no importer for asset extension: " + extension);
}

std::vector<std::string> AssetImporterRegistry::formats() const {
    std::vector<std::string> result;
    for (const auto& importer : importers_) {
        for (const auto extension : importer->extensions()) result.emplace_back(extension);
    }
    std::sort(result.begin(), result.end());
    SIM_LOG_VERBOSE("AssetImporterRegistry::formats completed count=" + std::to_string(result.size()));
    return result;
}

std::vector<Diagnostic> AssetValidator::validate(const AssetIR& asset) {
    SIM_LOG_VERBOSE("AssetValidator::validate checks nodes=" +
                    std::to_string(asset.nodes.size()) + " links=" +
                    std::to_string(asset.links.size()) + " joints=" +
                    std::to_string(asset.joints.size()) + " meshes=" +
                    std::to_string(asset.meshes.size()));
    std::vector<Diagnostic> diagnostics;
    std::unordered_set<std::string> links;
    for (const auto& link : asset.links) {
        if (link.name.empty()) diagnostics.push_back({Severity::Fatal, "asset.link.name", "link name cannot be empty"});
        else if (!links.insert(link.name).second) diagnostics.push_back({Severity::Fatal, "asset.link.duplicate", "duplicate link: " + link.name});
    }
    std::unordered_map<std::string, std::string> parent_of;
    for (const auto& joint : asset.joints) {
        if (!links.contains(joint.parent_link) || !links.contains(joint.child_link))
            diagnostics.push_back({Severity::Fatal, "asset.joint.link", "joint references an unknown link: " + joint.name});
        if (joint.parent_link == joint.child_link)
            diagnostics.push_back({Severity::Fatal, "asset.joint.self", "joint connects a link to itself: " + joint.name});
        if (!parent_of.emplace(joint.child_link, joint.parent_link).second)
            diagnostics.push_back({Severity::Fatal, "asset.joint.multiple_parent", "link has multiple parent joints: " + joint.child_link});
        if (joint.type != JointType::Continuous && joint.type != JointType::Fixed && joint.lower_limit > joint.upper_limit)
            diagnostics.push_back({Severity::Fatal, "asset.joint.limit", "joint lower limit exceeds upper limit: " + joint.name});
    }
    for (const auto& link : links) {
        std::unordered_set<std::string> visited;
        std::string current = link;
        while (parent_of.contains(current)) {
            if (!visited.insert(current).second) {
                diagnostics.push_back({Severity::Fatal, "asset.joint.cycle", "robot joint graph contains a cycle"});
                break;
            }
            current = parent_of.at(current);
        }
    }
    for (const auto& inertial : asset.inertials) {
        if (!std::isfinite(inertial.mass) || inertial.mass <= 0.0)
            diagnostics.push_back({Severity::Fatal, "asset.inertial.mass", "inertial mass must be finite and positive: " + inertial.link});
    }
    for (const auto& mesh : asset.meshes) {
        if (!mesh.texcoords.empty() && mesh.texcoords.size() != mesh.positions.size())
            diagnostics.push_back({Severity::Fatal, "asset.mesh.texcoords",
                "mesh texture-coordinate count does not match its vertex count: " + mesh.name});
        for (const Vec3& position : mesh.positions) {
            if (!IsFinite(position)) diagnostics.push_back({Severity::Fatal, "asset.mesh.vertex", "mesh contains a non-finite vertex: " + mesh.name});
        }
        if (mesh.indices.size() % 3 != 0)
            diagnostics.push_back({Severity::Fatal, "asset.mesh.topology", "mesh index count is not divisible by three: " + mesh.name});
        for (const auto index : mesh.indices) {
            if (index >= mesh.positions.size()) diagnostics.push_back({Severity::Fatal, "asset.mesh.index", "mesh index is out of range: " + mesh.name});
        }
    }
    SIM_LOG_VERBOSE("AssetValidator::validate completed diagnostics=" + std::to_string(diagnostics.size()));
    return diagnostics;
}

void AssetValidator::normalize(AssetIR& asset) {
    SIM_LOG_VERBOSE("AssetValidator::normalize begins source=" + PathUtf8(asset.metadata.source));
    SIM_PROFILE_SCOPE("Asset.Normalize");
    if (!std::isfinite(asset.metadata.meters_per_unit) || asset.metadata.meters_per_unit <= 0.0)
        throw Error(ErrorCode::ValidationFailed, "asset unit scale must be finite and positive");
    if (!NearlyEqual(asset.metadata.meters_per_unit, 1.0)) {
        const double scale = asset.metadata.meters_per_unit;
        for (auto& mesh : asset.meshes) for (auto& position : mesh.positions) position = position * scale;
        for (auto& node : asset.nodes) node.transform.translation = node.transform.translation * scale;
        for (auto& link : asset.links) link.origin.translation = link.origin.translation * scale;
        for (auto& joint : asset.joints) joint.origin.translation = joint.origin.translation * scale;
        for (auto& visual : asset.visuals) {
            visual.origin.translation = visual.origin.translation * scale;
            if (visual.shape != ColliderShape::Mesh) visual.size = visual.size * scale;
        }
        for (auto& collider : asset.colliders) {
            collider.origin.translation = collider.origin.translation * scale;
            if (collider.shape != ColliderShape::Mesh) collider.size = collider.size * scale;
        }
        asset.metadata.meters_per_unit = 1.0;
    }
    const auto diagnostics = validate(asset);
    const auto fatal = std::find_if(diagnostics.begin(), diagnostics.end(), [](const Diagnostic& item) { return item.severity == Severity::Fatal; });
    if (fatal != diagnostics.end()) throw Error(ErrorCode::ValidationFailed, fatal->message);
    SIM_LOG_VERBOSE("AssetValidator::normalize completed source=" + PathUtf8(asset.metadata.source));
}

BakingCache::BakingCache(std::filesystem::path directory) : directory_(std::move(directory)) {
    std::error_code error;
    std::filesystem::create_directories(directory_, error);
    if (error) throw Error(ErrorCode::IoError, "cannot create baking cache directory");
}

std::filesystem::path BakingCache::entry_path(const std::filesystem::path& source) const {
    const std::string canonical = PathUtf8(std::filesystem::absolute(source).lexically_normal());
    std::ostringstream name;
    name << std::hex << std::setfill('0') << std::setw(16) << Fnv1a(canonical) << ".simcache";
    return directory_ / name.str();
}

std::optional<AssetIR> BakingCache::load(const std::filesystem::path& source) const {
    SIM_LOG_VERBOSE("BakingCache::load checks source=" + PathUtf8(source));
    const auto path = entry_path(source);
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        SIM_LOG_VERBOSE("BakingCache::load miss no cache entry");
        return std::nullopt;
    }
    std::array<char, 8> magic{}; input.read(magic.data(), static_cast<std::streamsize>(magic.size()));
    if (!input || magic != kCacheMagic || ReadPod<std::uint32_t>(input) != kCacheVersion) {
        SIM_LOG_VERBOSE("BakingCache::load miss invalid header");
        return std::nullopt;
    }
    const Fingerprint cached{ReadPod<std::uint64_t>(input), ReadPod<std::int64_t>(input), ReadPod<std::uint64_t>(input)};
    const Fingerprint current = FingerprintFile(source);
    if (cached.size != current.size || cached.modified != current.modified ||
        cached.content_hash != current.content_hash) {
        SIM_LOG_VERBOSE("BakingCache::load miss source fingerprint changed");
        return std::nullopt;
    }
    AssetIR asset = ReadAsset(input);
    if (asset.metadata.dependencies.size() != asset.metadata.dependency_hashes.size()) {
        SIM_LOG_VERBOSE("BakingCache::load miss dependency metadata mismatch");
        return std::nullopt;
    }
    for (std::size_t index = 0; index < asset.metadata.dependencies.size(); ++index) {
        const auto& dependency = asset.metadata.dependencies[index];
        if (!std::filesystem::exists(dependency) ||
            HashFile(dependency) != asset.metadata.dependency_hashes[index]) {
            SIM_LOG_VERBOSE("BakingCache::load miss dependency changed");
            return std::nullopt;
        }
    }
    SIM_LOG_VERBOSE("BakingCache::load hit source=" + PathUtf8(source));
    return asset;
}

void BakingCache::store(const AssetIR& asset) const {
    SIM_LOG_VERBOSE("BakingCache::store writes source=" + PathUtf8(asset.metadata.source) +
                    " nodes=" + std::to_string(asset.nodes.size()));
    SIM_PROFILE_SCOPE("Asset.CacheStore");
    if (asset.metadata.source.empty()) throw Error(ErrorCode::InvalidArgument, "asset has no source path");
    const Fingerprint fingerprint = FingerprintFile(asset.metadata.source);
    const auto target = entry_path(asset.metadata.source);
    const auto temporary = target.string() + ".tmp";
    std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
    if (!output) throw Error(ErrorCode::IoError, "cannot create baking cache entry");
    output.write(kCacheMagic.data(), static_cast<std::streamsize>(kCacheMagic.size()));
    WritePod(output, kCacheVersion); WritePod(output, fingerprint.size);
    WritePod(output, fingerprint.modified); WritePod(output, fingerprint.content_hash);
    WriteAsset(output, asset); output.close();
    if (!output) throw Error(ErrorCode::IoError, "cannot write baking cache entry");
    std::error_code error; std::filesystem::remove(target, error); error.clear();
    std::filesystem::rename(temporary, target, error);
    if (error) throw Error(ErrorCode::IoError, "cannot commit baking cache entry");
    SIM_LOG_VERBOSE("BakingCache::store completed source=" + PathUtf8(asset.metadata.source));
}

ResourceManager::ResourceManager(std::filesystem::path cache_directory) : cache_(std::move(cache_directory)) {
    importers_.add(std::make_unique<UrdfImporter>());
    importers_.add(std::make_unique<ObjImporter>());
#if defined(SIM_HAS_ASSIMP)
    importers_.add(std::make_unique<AssimpImporter>());
#endif
}

ImportResult ResourceManager::load(const std::filesystem::path& source) {
    SIM_LOG_VERBOSE("ResourceManager::load checks cache and imports source=" + PathUtf8(source));
    SIM_PROFILE_SCOPE("Asset.ResourceLoad");
    std::scoped_lock lock(load_mutex_);
    if (auto cached = cache_.load(source)) {
        SIM_LOG_VERBOSE("ResourceManager::load returns cache hit source=" + PathUtf8(source));
        auto result = ImportResult{std::move(*cached), {}, true};
        SIM_LOG_VERBOSE("ResourceManager::load completed cache_hit=true source=" + PathUtf8(source));
        return result;
    }
    SIM_LOG_INFO("Assets", "importing source=" + PathUtf8(source));
    ImportResult result = importers_.importer_for(source).import(source);
    AssetValidator::normalize(result.asset);
    result.asset.metadata.dependency_hashes.clear();
    for (const auto& dependency : result.asset.metadata.dependencies) {
        if (!std::filesystem::exists(dependency))
            throw Error(ErrorCode::ValidationFailed,
                        "asset dependency does not exist: " + PathUtf8(dependency));
        result.asset.metadata.dependency_hashes.push_back(HashFile(dependency));
    }
    cache_.store(result.asset);
    SIM_LOG_VERBOSE("ResourceManager::load stores imported source=" + PathUtf8(source) +
                    " dependencies=" + std::to_string(result.asset.metadata.dependencies.size()));
    SIM_LOG_VERBOSE("ResourceManager::load completed cache_hit=false source=" + PathUtf8(source));
    return result;
}

std::future<ImportResult> ResourceManager::load_async(std::filesystem::path source) {
    SIM_LOG_VERBOSE("ResourceManager::load_async schedules source=" + PathUtf8(source));
    return std::async(std::launch::async,
                      [this, source = std::move(source)] { return load(source); });
}

EntityId ResourceManager::instantiate(World& world, const AssetIR& asset, std::string root_name) {
    SIM_LOG_VERBOSE("ResourceManager::instantiate creates root for source=" +
                    PathUtf8(asset.metadata.source) + " links=" +
                    std::to_string(asset.links.size()) + " joints=" +
                    std::to_string(asset.joints.size()) + " visuals=" +
                    std::to_string(asset.visuals.size()));
    SIM_PROFILE_SCOPE("Asset.Instantiate");
    if (root_name.empty()) root_name = asset.metadata.source.stem().string();
    const EntityId root = world.create_entity(std::move(root_name));
    if (!asset.links.empty())
        world.registry().emplace<RobotComponent>(root, RobotComponent{PathUtf8(asset.metadata.source), {}});
    std::unordered_map<std::string, EntityId> links;
    for (const auto& link : asset.links) {
        const EntityId entity = world.create_entity(link.name, root);
        world.registry().get<TransformComponent>(entity).local = link.origin;
        links.emplace(link.name, entity);
        world.registry().get<RobotComponent>(root).links.push_back(entity);
    }
    for (const auto& joint : asset.joints) {
        if (!links.contains(joint.child_link) || !links.contains(joint.parent_link)) continue;
        const EntityId child = links.at(joint.child_link);
        world.set_parent(child, links.at(joint.parent_link));
        world.registry().get<TransformComponent>(child).local = joint.origin;
        world.registry().emplace<JointComponent>(child, JointComponent{
            joint.name, joint.type, links.at(joint.parent_link), child, joint.axis,
            joint.origin, joint.lower_limit, joint.upper_limit, 0.0, 0.0, 0.0});
    }
    for (const auto& inertial : asset.inertials) {
        const auto it = links.find(inertial.link);
        if (it == links.end()) continue;
        const EntityId entity = it->second;
        if (!world.registry().has<RigidBodyComponent>(entity))
            world.registry().emplace<RigidBodyComponent>(entity);
        auto& rigid_body = world.registry().get<RigidBodyComponent>(entity);
        rigid_body.mass = inertial.mass;
        rigid_body.dynamic = world.registry().has<JointComponent>(entity);
    }
    std::unordered_map<std::string, std::size_t> collider_counts;
    for (const auto& collider : asset.colliders) {
        const auto it = links.find(collider.link);
        if (it == links.end()) continue;
        const auto index = collider_counts[collider.link]++;
        const EntityId entity = world.create_entity(
            collider.link + "::collision" + (index == 0 ? "" : "_" + std::to_string(index)),
            it->second);
        world.registry().get<TransformComponent>(entity).local = collider.origin;
        // Robot links use a unit friction coefficient on all contact axes.  Keep
        // this on the collider material so it does not alter the link rigid-body
        // dynamic/gravity settings managed by the articulation importer.
        world.registry().emplace<ColliderComponent>(entity,
            ColliderComponent{collider.shape, collider.size, PathUtf8(collider.mesh), false,
                              {1.0, 1.0, 1.0}});
    }
    std::unordered_map<std::string, std::size_t> visual_counts;
    // Mesh-only imports (OBJ/Assimp) have no authored collision section. Reuse
    // each rendered triangle mesh as its default collision geometry so editor
    // worlds remain collidable without requiring a separate proxy asset.
    const bool default_mesh_colliders = asset.colliders.empty();
    const Vec3 robot_friction = asset.links.empty() ? Vec3{1.0, 0.005, 0.0001}
                                                    : Vec3{1.0, 1.0, 1.0};
    for (const auto& visual : asset.visuals) {
        const auto it = links.find(visual.link);
        if (it == links.end()) continue;
        const auto index = visual_counts[visual.link]++;
        const EntityId entity = world.create_entity(
            visual.link + "::visual" + (index == 0 ? "" : "_" + std::to_string(index)),
            it->second);
        world.registry().get<TransformComponent>(entity).local = visual.origin;
        const auto mesh_resource = PathUtf8(visual.mesh);
        world.registry().emplace<VisualComponent>(entity, VisualComponent{
            mesh_resource, PathUtf8(visual.base_color_texture), true,
            visual.shape, visual.size, visual.color, 0, 7, visual.use_mesh_material});
        if (default_mesh_colliders && visual.shape == ColliderShape::Mesh && !mesh_resource.empty())
            world.registry().emplace<ColliderComponent>(entity,
                ColliderComponent{ColliderShape::Mesh, visual.size, mesh_resource, false,
                                  robot_friction});
    }
    std::vector<EntityId> node_entities(asset.nodes.size(), kInvalidEntity);
    for (std::size_t index = 0; index < asset.nodes.size(); ++index) {
        const auto& node = asset.nodes[index];
        if (!asset.links.empty() && index == 0) continue;
        EntityId parent = root;
        if (node.parent >= 0 && static_cast<std::size_t>(node.parent) < node_entities.size() &&
            node_entities[static_cast<std::size_t>(node.parent)] != kInvalidEntity)
            parent = node_entities[static_cast<std::size_t>(node.parent)];
        const EntityId entity = world.create_entity(node.name, parent);
        node_entities[index] = entity;
        world.registry().get<TransformComponent>(entity).local = node.transform;
        for (std::size_t mesh_slot = 0; mesh_slot < node.meshes.size(); ++mesh_slot) {
            const std::size_t mesh_index = node.meshes[mesh_slot];
            if (mesh_index >= asset.meshes.size()) continue;
            const auto& mesh = asset.meshes[mesh_index];
            std::array<float, 4> color{0.65F, 0.72F, 0.82F, 1.0F};
            if (mesh.material_index < asset.materials.size()) {
                const auto& source = asset.materials[mesh.material_index].base_color;
                color = {static_cast<float>(source[0]), static_cast<float>(source[1]),
                         static_cast<float>(source[2]), static_cast<float>(source[3])};
            }
            std::string texture_resource;
            if (mesh.material_index < asset.materials.size())
                texture_resource = PathUtf8(asset.materials[mesh.material_index].base_color_texture);
            const EntityId visual = world.create_entity(
                mesh.name.empty() ? "mesh_" + std::to_string(mesh_index) : mesh.name, entity);
            const std::string mesh_resource = PathUtf8(asset.metadata.source) +
                                              "#mesh=" + std::to_string(mesh_index);
            world.registry().emplace<VisualComponent>(visual, VisualComponent{
                mesh_resource,
                std::move(texture_resource), true,
                ColliderShape::Mesh, {1.0, 1.0, 1.0}, color});
            if (default_mesh_colliders)
                world.registry().emplace<ColliderComponent>(visual,
                    ColliderComponent{ColliderShape::Mesh, {1.0, 1.0, 1.0}, mesh_resource, false,
                                      robot_friction});
        }
    }
    world.update_world_transforms();
    SIM_LOG_VERBOSE("ResourceManager::instantiate completed root=" + std::to_string(root));
    return root;
}

std::vector<std::string> ResourceManager::supported_formats() const { return importers_.formats(); }

}  // namespace sim
