#include "sim/render.hpp"

#include <vulkan/vulkan.h>

#define STB_IMAGE_IMPLEMENTATION
#include <stb_image.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>
#include <optional>
#include <unordered_map>
#include <unordered_set>

namespace sim {
namespace {

std::filesystem::path PathFromUtf8(std::string_view value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

std::string PathToUtf8(const std::filesystem::path& value) {
    const auto text = value.generic_u8string();
    return {reinterpret_cast<const char*>(text.data()), text.size()};
}

void Check(VkResult result, std::string_view operation) {
    if (result != VK_SUCCESS)
        throw Error(ErrorCode::BackendUnavailable,
                    std::string(operation) + " failed with Vulkan result " +
                        std::to_string(static_cast<int>(result)));
}

struct Mat4 { std::array<float, 16> value{}; };

struct SceneUniform {
    Mat4 view_projection;
    std::array<std::array<float, 4>, 8> light_position_type{};
    std::array<std::array<float, 4>, 8> light_direction_range{};
    std::array<std::array<float, 4>, 8> light_color_intensity{};
    std::array<std::array<float, 4>, 8> light_params{};
    std::array<std::int32_t, 4> light_meta{};
};

std::array<float, 2> SpotConeCosines(const RenderLight& light) {
    constexpr float max_angle = 1.5676547F; // 89.8 degrees.
    const float outer = std::clamp(light.outer_cone_radians, 0.001F, max_angle);
    const float inner = std::clamp(light.inner_cone_radians, 0.0F, outer);
    return {std::cos(inner), std::cos(outer)};
}

Mat4 Identity() {
    Mat4 result;
    result.value[0] = result.value[5] = result.value[10] = result.value[15] = 1.0F;
    return result;
}

Mat4 Multiply(const Mat4& first, const Mat4& second) {
    Mat4 result;
    for (int column = 0; column < 4; ++column)
        for (int row = 0; row < 4; ++row)
            for (int index = 0; index < 4; ++index)
                result.value[column * 4 + row] +=
                    first.value[index * 4 + row] * second.value[column * 4 + index];
    return result;
}

Mat4 ModelMatrix(const Transform& transform, const Vec3& geometry_size = {1.0, 1.0, 1.0}) {
    const Quaternion q = transform.rotation.normalized();
    const float x = static_cast<float>(q.x);
    const float y = static_cast<float>(q.y);
    const float z = static_cast<float>(q.z);
    const float w = static_cast<float>(q.w);
    const float sx = static_cast<float>(transform.scale.x * geometry_size.x);
    const float sy = static_cast<float>(transform.scale.y * geometry_size.y);
    const float sz = static_cast<float>(transform.scale.z * geometry_size.z);
    Mat4 result = Identity();
    result.value[0] = (1.0F - 2.0F * (y * y + z * z)) * sx;
    result.value[1] = (2.0F * (x * y + z * w)) * sx;
    result.value[2] = (2.0F * (x * z - y * w)) * sx;
    result.value[4] = (2.0F * (x * y - z * w)) * sy;
    result.value[5] = (1.0F - 2.0F * (x * x + z * z)) * sy;
    result.value[6] = (2.0F * (y * z + x * w)) * sy;
    result.value[8] = (2.0F * (x * z + y * w)) * sz;
    result.value[9] = (2.0F * (y * z - x * w)) * sz;
    result.value[10] = (1.0F - 2.0F * (x * x + y * y)) * sz;
    result.value[12] = static_cast<float>(transform.translation.x);
    result.value[13] = static_cast<float>(transform.translation.y);
    result.value[14] = static_cast<float>(transform.translation.z);
    return result;
}

Vec3 TransformPoint(const Transform& transform, const Vec3& point,
                    const Vec3& geometry_size = {1.0, 1.0, 1.0}) {
    const Vec3 scaled{point.x * transform.scale.x * geometry_size.x,
                      point.y * transform.scale.y * geometry_size.y,
                      point.z * transform.scale.z * geometry_size.z};
    return transform.translation + transform.rotation.rotate(scaled);
}

Mat4 Projection(const CameraComponent& camera) {
    const float near_value = static_cast<float>(camera.near_plane);
    const float far_value = static_cast<float>(camera.far_plane);
    const float aspect = static_cast<float>(camera.width) / static_cast<float>(camera.height);
    const float focal = 1.0F / std::tan(static_cast<float>(camera.vertical_fov_radians * 0.5));
    const float a = far_value / (far_value - near_value);
    const float b = -(far_value * near_value) / (far_value - near_value);
    Mat4 result;
    // Platform coordinates are X forward, Y left, Z up. Vulkan depth is [0, 1].
    result.value[2] = a;
    result.value[3] = 1.0F;
    result.value[4] = -focal / aspect;
    result.value[9] = -focal;
    result.value[14] = b;
    return result;
}

std::uint8_t SelectLod(const RenderInstance& instance, const Transform& world_from_camera,
                       const CameraComponent& camera) {
    const Transform camera_from_world = world_from_camera.inverse();
    const double tangent = std::tan(camera.vertical_fov_radians * 0.5);
    const double aspect = static_cast<double>(camera.width) / camera.height;
    double left = 1.0, right = -1.0, bottom = 1.0, top = -1.0;
    for (int corner = 0; corner < 8; ++corner) {
        const Vec3 point{(corner & 1) ? instance.bounds_max.x : instance.bounds_min.x,
                         (corner & 2) ? instance.bounds_max.y : instance.bounds_min.y,
                         (corner & 4) ? instance.bounds_max.z : instance.bounds_min.z};
        const auto camera_point = (camera_from_world * Transform{point}).translation;
        if (camera_point.x <= camera.near_plane) return instance.min_lod;
        const double horizontal = -camera_point.y / (camera_point.x * tangent * aspect);
        const double vertical = camera_point.z / (camera_point.x * tangent);
        left = std::min(left, horizontal); right = std::max(right, horizontal);
        bottom = std::min(bottom, vertical); top = std::max(top, vertical);
    }
    left = std::clamp(left, -1.0, 1.0); right = std::clamp(right, -1.0, 1.0);
    bottom = std::clamp(bottom, -1.0, 1.0); top = std::clamp(top, -1.0, 1.0);
    const double coverage = std::max(0.0, right - left) *
                            std::max(0.0, top - bottom) * 0.25;
    std::uint8_t desired = 7;
    if (coverage >= 0.30) desired = 0;
    else if (coverage >= 0.15) desired = 1;
    else if (coverage >= 0.07) desired = 2;
    else if (coverage >= 0.03) desired = 3;
    else if (coverage >= 0.012) desired = 4;
    else if (coverage >= 0.005) desired = 5;
    else if (coverage >= 0.002) desired = 6;
    return std::clamp(desired, instance.min_lod, instance.max_lod);
}

struct Vertex {
    float position[3];
    float normal[3];
    float texcoord[2];
};

std::vector<Vertex> CubeVertices() {
    std::vector<Vertex> result;
    result.reserve(36);
    const auto add_face = [&](const std::array<float, 3>& first,
                              const std::array<float, 3>& second,
                              const std::array<float, 3>& third,
                              const std::array<float, 3>& fourth,
                              const std::array<float, 3>& normal) {
        const std::array<std::array<float, 3>, 4> corners{first, second, third, fourth};
        constexpr std::array<int, 6> indices{0, 1, 2, 0, 2, 3};
        for (const int index : indices) {
            const auto& point = corners[static_cast<std::size_t>(index)];
            const std::array<std::array<float, 2>, 4> uv{{{0, 0}, {1, 0}, {1, 1}, {0, 1}}};
            result.push_back({{point[0], point[1], point[2]},
                              {normal[0], normal[1], normal[2]},
                              {uv[static_cast<std::size_t>(index)][0],
                               uv[static_cast<std::size_t>(index)][1]}});
        }
    };
    add_face({0.5F, -0.5F, -0.5F}, {0.5F, 0.5F, -0.5F},
             {0.5F, 0.5F, 0.5F}, {0.5F, -0.5F, 0.5F}, {1, 0, 0});
    add_face({-0.5F, 0.5F, -0.5F}, {-0.5F, -0.5F, -0.5F},
             {-0.5F, -0.5F, 0.5F}, {-0.5F, 0.5F, 0.5F}, {-1, 0, 0});
    add_face({-0.5F, 0.5F, -0.5F}, {0.5F, 0.5F, -0.5F},
             {0.5F, 0.5F, 0.5F}, {-0.5F, 0.5F, 0.5F}, {0, 1, 0});
    add_face({0.5F, -0.5F, -0.5F}, {-0.5F, -0.5F, -0.5F},
             {-0.5F, -0.5F, 0.5F}, {0.5F, -0.5F, 0.5F}, {0, -1, 0});
    add_face({-0.5F, -0.5F, 0.5F}, {0.5F, -0.5F, 0.5F},
             {0.5F, 0.5F, 0.5F}, {-0.5F, 0.5F, 0.5F}, {0, 0, 1});
    add_face({-0.5F, 0.5F, -0.5F}, {0.5F, 0.5F, -0.5F},
             {0.5F, -0.5F, -0.5F}, {-0.5F, -0.5F, -0.5F}, {0, 0, -1});
    return result;
}

std::optional<MeshData> BuiltinMesh(std::string_view resource) {
    MeshData mesh;
    mesh.name = std::string(resource);
    if (resource == "builtin://plane") {
        mesh.positions = {{-0.5, -0.5, 0.0}, {0.5, -0.5, 0.0},
                          {0.5, 0.5, 0.0}, {-0.5, 0.5, 0.0}};
        mesh.normals.assign(4, {0.0, 0.0, 1.0});
        mesh.texcoords = {{{0.0, 0.0}}, {{1.0, 0.0}}, {{1.0, 1.0}}, {{0.0, 1.0}}};
        mesh.indices = {0, 1, 2, 0, 2, 3};
        return mesh;
    }
    if (resource == "builtin://sphere" || resource == "builtin://capsule") {
        constexpr int latitude_count = 20;
        constexpr int longitude_count = 32;
        const double radius = resource == "builtin://capsule" ? 0.25 : 0.5;
        const double half_cylinder = resource == "builtin://capsule" ? 0.25 : 0.0;
        for (int latitude = 0; latitude <= latitude_count; ++latitude) {
            const double v = static_cast<double>(latitude) / latitude_count;
            const double phi = v * 3.14159265358979323846;
            for (int longitude = 0; longitude <= longitude_count; ++longitude) {
                const double u = static_cast<double>(longitude) / longitude_count;
                const double theta = u * 2.0 * 3.14159265358979323846;
                const Vec3 normal{std::sin(phi) * std::cos(theta),
                                  std::sin(phi) * std::sin(theta), std::cos(phi)};
                Vec3 point = normal * radius;
                if (half_cylinder > 0.0)
                    point.z += normal.z >= 0.0 ? half_cylinder : -half_cylinder;
                mesh.positions.push_back(point);
                mesh.normals.push_back(normal);
                mesh.texcoords.push_back({u, v});
            }
        }
        for (int latitude = 0; latitude < latitude_count; ++latitude) {
            for (int longitude = 0; longitude < longitude_count; ++longitude) {
                const auto first = static_cast<std::uint32_t>(
                    latitude * (longitude_count + 1) + longitude);
                const auto second = first + longitude_count + 1;
                mesh.indices.insert(mesh.indices.end(),
                    {first, second, first + 1, first + 1, second, second + 1});
            }
        }
        return mesh;
    }
    if (resource == "builtin://cylinder") {
        constexpr int segments = 32;
        for (int segment = 0; segment <= segments; ++segment) {
            const double u = static_cast<double>(segment) / segments;
            const double angle = u * 2.0 * 3.14159265358979323846;
            const Vec3 normal{std::cos(angle), std::sin(angle), 0.0};
            for (double z : {-0.5, 0.5}) {
                mesh.positions.push_back({0.5 * normal.x, 0.5 * normal.y, z});
                mesh.normals.push_back(normal);
                mesh.texcoords.push_back({u, z + 0.5});
            }
        }
        for (int segment = 0; segment < segments; ++segment) {
            const auto first = static_cast<std::uint32_t>(segment * 2);
            mesh.indices.insert(mesh.indices.end(),
                {first, first + 1, first + 2, first + 2, first + 1, first + 3});
        }
        for (double z : {-0.5, 0.5}) {
            const auto center = static_cast<std::uint32_t>(mesh.positions.size());
            mesh.positions.push_back({0.0, 0.0, z});
            mesh.normals.push_back({0.0, 0.0, z > 0.0 ? 1.0 : -1.0});
            mesh.texcoords.push_back({0.5, 0.5});
            for (int segment = 0; segment <= segments; ++segment) {
                const double angle = static_cast<double>(segment) / segments *
                                     2.0 * 3.14159265358979323846;
                mesh.positions.push_back({0.5 * std::cos(angle), 0.5 * std::sin(angle), z});
                mesh.normals.push_back({0.0, 0.0, z > 0.0 ? 1.0 : -1.0});
                mesh.texcoords.push_back({0.5 + 0.5 * std::cos(angle),
                                          0.5 + 0.5 * std::sin(angle)});
            }
            for (int segment = 0; segment < segments; ++segment) {
                const auto edge = center + 1 + static_cast<std::uint32_t>(segment);
                if (z > 0.0) mesh.indices.insert(mesh.indices.end(), {center, edge, edge + 1});
                else mesh.indices.insert(mesh.indices.end(), {center, edge + 1, edge});
            }
        }
        return mesh;
    }
    return std::nullopt;
}

std::vector<std::uint32_t> ReadShader(const char* path) {
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream) throw Error(ErrorCode::IoError, std::string("cannot open Vulkan shader: ") + path);
    const auto size = stream.tellg();
    if (size <= 0 || size % 4 != 0)
        throw Error(ErrorCode::ValidationFailed, std::string("invalid SPIR-V shader: ") + path);
    std::vector<std::uint32_t> code(static_cast<std::size_t>(size) / 4);
    stream.seekg(0);
    stream.read(reinterpret_cast<char*>(code.data()), size);
    return code;
}

}  // namespace

struct VulkanBackend::Impl {
    struct GpuMesh {
        struct Part {
            std::uint32_t first{0}, count{0};
            std::array<float, 4> color{1, 1, 1, 1};
            std::string texture;
            bool material{false};
        };
        std::vector<Part> parts;
        VkBuffer buffer{VK_NULL_HANDLE};
        VkDeviceMemory memory{VK_NULL_HANDLE};
        VkBuffer index_buffer{VK_NULL_HANDLE};
        VkDeviceMemory index_memory{VK_NULL_HANDLE};
        std::uint32_t index_count{0};
        Vec3 bounds_min{};
        Vec3 bounds_max{};
    };
    struct GpuTexture {
        VkImage image{VK_NULL_HANDLE};
        VkDeviceMemory memory{VK_NULL_HANDLE};
        VkImageView view{VK_NULL_HANDLE};
        VkSampler sampler{VK_NULL_HANDLE};
        VkDescriptorSet descriptor_set{VK_NULL_HANDLE};
        std::uint64_t descriptor_generation{0};
    };
    struct Offscreen {
        std::uint32_t width{0};
        std::uint32_t height{0};
        VkImage color_image{VK_NULL_HANDLE};
        VkImage depth_image{VK_NULL_HANDLE};
        VkDeviceMemory color_memory{VK_NULL_HANDLE};
        VkDeviceMemory depth_memory{VK_NULL_HANDLE};
        VkImageView color_view{VK_NULL_HANDLE};
        VkImageView depth_view{VK_NULL_HANDLE};
        VkBuffer color_buffer{VK_NULL_HANDLE};
        VkBuffer depth_buffer{VK_NULL_HANDLE};
        VkDeviceMemory color_buffer_memory{VK_NULL_HANDLE};
        VkDeviceMemory depth_buffer_memory{VK_NULL_HANDLE};
        VkRenderPass render_pass{VK_NULL_HANDLE};
        VkFramebuffer framebuffer{VK_NULL_HANDLE};
        VkShaderModule vertex_shader{VK_NULL_HANDLE};
        VkShaderModule fragment_shader{VK_NULL_HANDLE};
        VkPipelineLayout pipeline_layout{VK_NULL_HANDLE};
        VkPipeline pipeline{VK_NULL_HANDLE};
        VkDescriptorSetLayout descriptor_layout{VK_NULL_HANDLE};
        VkDescriptorPool descriptor_pool{VK_NULL_HANDLE};
        VkDescriptorSet descriptor_set{VK_NULL_HANDLE};
        VkBuffer camera_buffer{VK_NULL_HANDLE};
        VkDeviceMemory camera_memory{VK_NULL_HANDLE};
        VkCommandBuffer command{VK_NULL_HANDLE};
        std::uint64_t generation{0};
    };
    VkInstance instance{VK_NULL_HANDLE};
    VkPhysicalDevice physical_device{VK_NULL_HANDLE};
    VkDevice device{VK_NULL_HANDLE};
    VkQueue queue{VK_NULL_HANDLE};
    std::uint32_t queue_family{0};
    VkCommandPool command_pool{VK_NULL_HANDLE};
    VkPhysicalDeviceMemoryProperties memory_properties{};
    VkBuffer cube_buffer{VK_NULL_HANDLE};
    VkDeviceMemory cube_memory{VK_NULL_HANDLE};
    std::uint32_t cube_vertex_count{0};
    std::unordered_map<std::string, GpuMesh> meshes;
    std::unordered_map<std::string, GpuTexture> textures;
    std::unordered_set<std::string> failed_meshes;
    std::vector<std::string> diagnostics;
    std::string device_name;
    RenderSnapshot snapshot;
    Offscreen offscreen;
    std::uint64_t descriptor_generation{0};

    void destroy_offscreen() {
        auto& value = offscreen;
        if (value.command != VK_NULL_HANDLE)
            vkFreeCommandBuffers(device, command_pool, 1, &value.command);
        if (value.pipeline != VK_NULL_HANDLE) vkDestroyPipeline(device, value.pipeline, nullptr);
        if (value.pipeline_layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(device, value.pipeline_layout, nullptr);
        if (value.descriptor_pool != VK_NULL_HANDLE)
            vkDestroyDescriptorPool(device, value.descriptor_pool, nullptr);
        if (value.descriptor_layout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(device, value.descriptor_layout, nullptr);
        if (value.camera_buffer != VK_NULL_HANDLE)
            vkDestroyBuffer(device, value.camera_buffer, nullptr);
        if (value.camera_memory != VK_NULL_HANDLE)
            vkFreeMemory(device, value.camera_memory, nullptr);
        if (value.vertex_shader != VK_NULL_HANDLE)
            vkDestroyShaderModule(device, value.vertex_shader, nullptr);
        if (value.fragment_shader != VK_NULL_HANDLE)
            vkDestroyShaderModule(device, value.fragment_shader, nullptr);
        if (value.framebuffer != VK_NULL_HANDLE)
            vkDestroyFramebuffer(device, value.framebuffer, nullptr);
        if (value.render_pass != VK_NULL_HANDLE)
            vkDestroyRenderPass(device, value.render_pass, nullptr);
        if (value.color_view != VK_NULL_HANDLE) vkDestroyImageView(device, value.color_view, nullptr);
        if (value.depth_view != VK_NULL_HANDLE) vkDestroyImageView(device, value.depth_view, nullptr);
        if (value.color_buffer != VK_NULL_HANDLE) vkDestroyBuffer(device, value.color_buffer, nullptr);
        if (value.depth_buffer != VK_NULL_HANDLE) vkDestroyBuffer(device, value.depth_buffer, nullptr);
        if (value.color_buffer_memory != VK_NULL_HANDLE)
            vkFreeMemory(device, value.color_buffer_memory, nullptr);
        if (value.depth_buffer_memory != VK_NULL_HANDLE)
            vkFreeMemory(device, value.depth_buffer_memory, nullptr);
        if (value.color_image != VK_NULL_HANDLE) vkDestroyImage(device, value.color_image, nullptr);
        if (value.depth_image != VK_NULL_HANDLE) vkDestroyImage(device, value.depth_image, nullptr);
        if (value.color_memory != VK_NULL_HANDLE) vkFreeMemory(device, value.color_memory, nullptr);
        if (value.depth_memory != VK_NULL_HANDLE) vkFreeMemory(device, value.depth_memory, nullptr);
        value = {};
    }

    ~Impl() {
        if (device != VK_NULL_HANDLE) {
            vkDeviceWaitIdle(device);
            destroy_offscreen();
            for (auto& [name, mesh] : meshes) {
                (void)name;
                if (mesh.buffer != VK_NULL_HANDLE) vkDestroyBuffer(device, mesh.buffer, nullptr);
                if (mesh.memory != VK_NULL_HANDLE) vkFreeMemory(device, mesh.memory, nullptr);
                if (mesh.index_buffer != VK_NULL_HANDLE) vkDestroyBuffer(device, mesh.index_buffer, nullptr);
                if (mesh.index_memory != VK_NULL_HANDLE) vkFreeMemory(device, mesh.index_memory, nullptr);
            }
            for (auto& [name, texture] : textures) {
                (void)name;
                if (texture.sampler != VK_NULL_HANDLE) vkDestroySampler(device, texture.sampler, nullptr);
                if (texture.view != VK_NULL_HANDLE) vkDestroyImageView(device, texture.view, nullptr);
                if (texture.image != VK_NULL_HANDLE) vkDestroyImage(device, texture.image, nullptr);
                if (texture.memory != VK_NULL_HANDLE) vkFreeMemory(device, texture.memory, nullptr);
            }
            if (cube_buffer != VK_NULL_HANDLE) vkDestroyBuffer(device, cube_buffer, nullptr);
            if (cube_memory != VK_NULL_HANDLE) vkFreeMemory(device, cube_memory, nullptr);
            if (command_pool != VK_NULL_HANDLE) vkDestroyCommandPool(device, command_pool, nullptr);
            vkDestroyDevice(device, nullptr);
        }
        if (instance != VK_NULL_HANDLE) vkDestroyInstance(instance, nullptr);
    }

    std::uint32_t memory_type(std::uint32_t bits, VkMemoryPropertyFlags required,
                              VkMemoryPropertyFlags preferred = 0) const {
        for (std::uint32_t index = 0; index < memory_properties.memoryTypeCount; ++index)
            if ((bits & (1U << index)) != 0 &&
                (memory_properties.memoryTypes[index].propertyFlags & required) == required &&
                (memory_properties.memoryTypes[index].propertyFlags & preferred) == preferred)
                return index;
        for (std::uint32_t index = 0; index < memory_properties.memoryTypeCount; ++index)
            if ((bits & (1U << index)) != 0 &&
                (memory_properties.memoryTypes[index].propertyFlags & required) == required)
                return index;
        throw Error(ErrorCode::BackendUnavailable, "Vulkan memory type is unavailable");
    }

    void create_buffer(VkDeviceSize size, VkBufferUsageFlags usage,
                       VkMemoryPropertyFlags properties, VkBuffer& buffer,
                       VkDeviceMemory& memory,
                       VkMemoryPropertyFlags preferred = 0) const {
        VkBufferCreateInfo info{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
        info.size = size;
        info.usage = usage;
        info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        Check(vkCreateBuffer(device, &info, nullptr, &buffer), "vkCreateBuffer");
        VkMemoryRequirements requirements{};
        vkGetBufferMemoryRequirements(device, buffer, &requirements);
        VkMemoryAllocateInfo allocation{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        allocation.allocationSize = requirements.size;
        allocation.memoryTypeIndex =
            memory_type(requirements.memoryTypeBits, properties, preferred);
        Check(vkAllocateMemory(device, &allocation, nullptr, &memory), "vkAllocateMemory(buffer)");
        Check(vkBindBufferMemory(device, buffer, memory, 0), "vkBindBufferMemory");
    }

    void create_image(std::uint32_t width, std::uint32_t height, VkFormat format,
                      VkImageUsageFlags usage, VkImage& image, VkDeviceMemory& memory) const {
        VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
        info.imageType = VK_IMAGE_TYPE_2D;
        info.format = format;
        info.extent = {width, height, 1};
        info.mipLevels = 1;
        info.arrayLayers = 1;
        info.samples = VK_SAMPLE_COUNT_1_BIT;
        info.tiling = VK_IMAGE_TILING_OPTIMAL;
        info.usage = usage;
        info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        Check(vkCreateImage(device, &info, nullptr, &image), "vkCreateImage");
        VkMemoryRequirements requirements{};
        vkGetImageMemoryRequirements(device, image, &requirements);
        VkMemoryAllocateInfo allocation{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        allocation.allocationSize = requirements.size;
        allocation.memoryTypeIndex = memory_type(requirements.memoryTypeBits,
                                                  VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        Check(vkAllocateMemory(device, &allocation, nullptr, &memory), "vkAllocateMemory(image)");
        Check(vkBindImageMemory(device, image, memory, 0), "vkBindImageMemory");
    }

    const GpuTexture* ensure_texture(const std::string& resource) {
        const std::string key = resource.empty() ? "__white__" : resource;
        auto found = textures.find(key);
        if (found == textures.end()) {
            int width = 1, height = 1, channels = 4;
            std::vector<std::uint8_t> file_bytes;
            stbi_uc* decoded = nullptr;
            const std::array<std::uint8_t, 4> white{255, 255, 255, 255};
            const std::uint8_t* pixels = white.data();
            if (!resource.empty()) {
                std::ifstream input(PathFromUtf8(resource), std::ios::binary | std::ios::ate);
                if (!input) {
                    diagnostics.push_back("Texture cannot be opened: " + resource);
                } else {
                    const auto size = input.tellg();
                    if (size > 0) {
                        file_bytes.resize(static_cast<std::size_t>(size));
                        input.seekg(0);
                        input.read(reinterpret_cast<char*>(file_bytes.data()), size);
                        decoded = stbi_load_from_memory(file_bytes.data(),
                            static_cast<int>(file_bytes.size()), &width, &height, &channels, 4);
                        if (decoded) pixels = decoded;
                        else diagnostics.push_back("Texture decode failed: " + resource);
                    }
                }
            }
            GpuTexture texture;
            VkBuffer staging = VK_NULL_HANDLE;
            VkDeviceMemory staging_memory = VK_NULL_HANDLE;
            VkCommandBuffer upload = VK_NULL_HANDLE;
            try {
                const VkDeviceSize bytes = static_cast<VkDeviceSize>(width) * height * 4;
                create_buffer(bytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
                    VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                    staging, staging_memory);
                void* mapped = nullptr;
                Check(vkMapMemory(device, staging_memory, 0, bytes, 0, &mapped),
                      "vkMapMemory(texture staging)");
                std::memcpy(mapped, pixels, static_cast<std::size_t>(bytes));
                vkUnmapMemory(device, staging_memory);
                create_image(static_cast<std::uint32_t>(width), static_cast<std::uint32_t>(height),
                    VK_FORMAT_R8G8B8A8_SRGB,
                    VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
                    texture.image, texture.memory);
                VkCommandBufferAllocateInfo allocate{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
                allocate.commandPool = command_pool; allocate.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
                allocate.commandBufferCount = 1;
                Check(vkAllocateCommandBuffers(device, &allocate, &upload),
                      "vkAllocateCommandBuffers(texture)");
                VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
                begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
                Check(vkBeginCommandBuffer(upload, &begin), "vkBeginCommandBuffer(texture)");
                VkImageMemoryBarrier to_transfer{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
                to_transfer.srcAccessMask = 0;
                to_transfer.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
                to_transfer.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
                to_transfer.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
                to_transfer.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                to_transfer.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                to_transfer.image = texture.image;
                to_transfer.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
                vkCmdPipelineBarrier(upload, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                    VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &to_transfer);
                const VkBufferImageCopy copy{0, 0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
                                             {0, 0, 0},
                                             {static_cast<std::uint32_t>(width),
                                              static_cast<std::uint32_t>(height), 1}};
                vkCmdCopyBufferToImage(upload, staging, texture.image,
                    VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
                VkImageMemoryBarrier to_shader = to_transfer;
                to_shader.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
                to_shader.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
                to_shader.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
                to_shader.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
                vkCmdPipelineBarrier(upload, VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0, nullptr, 0, nullptr, 1, &to_shader);
                Check(vkEndCommandBuffer(upload), "vkEndCommandBuffer(texture)");
                VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};
                submit.commandBufferCount = 1; submit.pCommandBuffers = &upload;
                Check(vkQueueSubmit(queue, 1, &submit, VK_NULL_HANDLE), "vkQueueSubmit(texture)");
                Check(vkQueueWaitIdle(queue), "vkQueueWaitIdle(texture)");
                VkImageViewCreateInfo view{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
                view.image = texture.image; view.viewType = VK_IMAGE_VIEW_TYPE_2D;
                view.format = VK_FORMAT_R8G8B8A8_SRGB;
                view.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
                Check(vkCreateImageView(device, &view, nullptr, &texture.view),
                      "vkCreateImageView(texture)");
                VkSamplerCreateInfo sampler{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
                sampler.magFilter = VK_FILTER_LINEAR; sampler.minFilter = VK_FILTER_LINEAR;
                sampler.addressModeU = sampler.addressModeV = sampler.addressModeW =
                    VK_SAMPLER_ADDRESS_MODE_REPEAT;
                sampler.maxAnisotropy = 1.0F;
                Check(vkCreateSampler(device, &sampler, nullptr, &texture.sampler),
                      "vkCreateSampler");
            } catch (...) {
                if (upload != VK_NULL_HANDLE)
                    vkFreeCommandBuffers(device, command_pool, 1, &upload);
                if (staging != VK_NULL_HANDLE) vkDestroyBuffer(device, staging, nullptr);
                if (staging_memory != VK_NULL_HANDLE) vkFreeMemory(device, staging_memory, nullptr);
                if (decoded) stbi_image_free(decoded);
                throw;
            }
            if (upload != VK_NULL_HANDLE) vkFreeCommandBuffers(device, command_pool, 1, &upload);
            if (staging != VK_NULL_HANDLE) vkDestroyBuffer(device, staging, nullptr);
            if (staging_memory != VK_NULL_HANDLE) vkFreeMemory(device, staging_memory, nullptr);
            if (decoded) stbi_image_free(decoded);
            found = textures.emplace(key, texture).first;
        }
        auto& texture = found->second;
        if (texture.descriptor_generation != offscreen.generation) {
            VkDescriptorSetAllocateInfo allocate{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
            allocate.descriptorPool = offscreen.descriptor_pool;
            allocate.descriptorSetCount = 1;
            allocate.pSetLayouts = &offscreen.descriptor_layout;
            Check(vkAllocateDescriptorSets(device, &allocate, &texture.descriptor_set),
                  "vkAllocateDescriptorSets(texture)");
            const VkDescriptorBufferInfo camera{offscreen.camera_buffer, 0, sizeof(SceneUniform)};
            const VkDescriptorImageInfo image{texture.sampler, texture.view,
                                               VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
            std::array<VkWriteDescriptorSet, 2> writes{};
            writes[0] = {VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, nullptr,
                         texture.descriptor_set, 0, 0, 1,
                         VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, nullptr, &camera, nullptr};
            writes[1] = {VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, nullptr,
                         texture.descriptor_set, 1, 0, 1,
                         VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, &image, nullptr, nullptr};
            vkUpdateDescriptorSets(device, static_cast<std::uint32_t>(writes.size()),
                                   writes.data(), 0, nullptr);
            texture.descriptor_generation = offscreen.generation;
        }
        return &texture;
    }

    const GpuMesh* ensure_mesh(const std::string& resource, std::uint8_t lod = 0) {
        if (resource.empty()) return nullptr;
        lod = std::min<std::uint8_t>(lod, 7);
        const std::string cache_key = resource + "\x1fLOD=" + std::to_string(lod);
        if (const auto found = meshes.find(cache_key); found != meshes.end()) return &found->second;
        if (failed_meshes.contains(cache_key)) return nullptr;
#if defined(SIM_HAS_ASSIMP)
        try {
            std::string source = resource;
            std::optional<std::size_t> selected_mesh;
            if (const auto marker = resource.rfind("#mesh="); marker != std::string::npos) {
                source = resource.substr(0, marker);
                selected_mesh = static_cast<std::size_t>(std::stoull(resource.substr(marker + 6)));
            }
            AssetIR asset;
            if (const auto builtin = BuiltinMesh(source)) {
                asset.meshes.push_back(*builtin);
            } else {
                asset = AssimpImporter{}.import(PathFromUtf8(source)).asset;
                AssetValidator::normalize(asset);
            }
            std::vector<Vertex> vertices;
            std::vector<std::uint32_t> indices;
            std::vector<GpuMesh::Part> parts;
            Vec3 bounds_min{std::numeric_limits<double>::max(), std::numeric_limits<double>::max(),
                            std::numeric_limits<double>::max()};
            Vec3 bounds_max{std::numeric_limits<double>::lowest(), std::numeric_limits<double>::lowest(),
                            std::numeric_limits<double>::lowest()};
            for (std::size_t mesh_index = 0; mesh_index < asset.meshes.size(); ++mesh_index) {
                if (selected_mesh && *selected_mesh != mesh_index) continue;
                const auto mesh = GenerateMeshLod(asset.meshes[mesh_index], lod);
                GpuMesh::Part part;
                part.first = static_cast<std::uint32_t>(indices.size());
                auto extension = PathFromUtf8(source).extension().string();
                std::transform(extension.begin(), extension.end(), extension.begin(),
                    [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
                if (extension != ".stl" && mesh.material_index < asset.materials.size()) {
                    const auto& material = asset.materials[mesh.material_index];
                    part.material = true;
                    for (std::size_t channel = 0; channel < 4; ++channel)
                        part.color[channel] = static_cast<float>(material.base_color[channel]);
                    part.texture = PathToUtf8(material.base_color_texture);
                }
                const auto vertex_offset = static_cast<std::uint32_t>(vertices.size());
                vertices.reserve(vertices.size() + mesh.positions.size());
                for (std::size_t index = 0; index < mesh.positions.size(); ++index) {
                    const auto& point = mesh.positions[index];
                    bounds_min = {std::min(bounds_min.x, point.x), std::min(bounds_min.y, point.y),
                                  std::min(bounds_min.z, point.z)};
                    bounds_max = {std::max(bounds_max.x, point.x), std::max(bounds_max.y, point.y),
                                  std::max(bounds_max.z, point.z)};
                    const Vec3 normal = index < mesh.normals.size() ? mesh.normals[index]
                                                                     : Vec3{1.0, 0.0, 0.0};
                    const auto uv = index < mesh.texcoords.size()
                        ? mesh.texcoords[index] : std::array<double, 2>{0.0, 0.0};
                    vertices.push_back({{static_cast<float>(point.x), static_cast<float>(point.y),
                                         static_cast<float>(point.z)},
                                        {static_cast<float>(normal.x), static_cast<float>(normal.y),
                                         static_cast<float>(normal.z)},
                                        {static_cast<float>(uv[0]), static_cast<float>(uv[1])}});
                }
                indices.reserve(indices.size() + mesh.indices.size());
                for (const auto index : mesh.indices)
                    if (index < mesh.positions.size()) indices.push_back(vertex_offset + index);
                part.count = static_cast<std::uint32_t>(indices.size()) - part.first;
                parts.push_back(std::move(part));
            }
            if (!vertices.empty() && !indices.empty()) {
                GpuMesh mesh;
                mesh.parts = std::move(parts);
                mesh.index_count = static_cast<std::uint32_t>(indices.size());
                mesh.bounds_min = bounds_min;
                mesh.bounds_max = bounds_max;
                const VkDeviceSize bytes = vertices.size() * sizeof(Vertex);
                create_buffer(bytes, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
                              VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                  VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                              mesh.buffer, mesh.memory);
                void* mapped = nullptr;
                Check(vkMapMemory(device, mesh.memory, 0, bytes, 0, &mapped),
                      "vkMapMemory(imported mesh)");
                std::memcpy(mapped, vertices.data(), static_cast<std::size_t>(bytes));
                vkUnmapMemory(device, mesh.memory);
                const VkDeviceSize index_bytes = indices.size() * sizeof(std::uint32_t);
                create_buffer(index_bytes, VK_BUFFER_USAGE_INDEX_BUFFER_BIT,
                              VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                  VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                              mesh.index_buffer, mesh.index_memory);
                mapped = nullptr;
                Check(vkMapMemory(device, mesh.index_memory, 0, index_bytes, 0, &mapped),
                      "vkMapMemory(imported mesh indices)");
                std::memcpy(mapped, indices.data(), static_cast<std::size_t>(index_bytes));
                vkUnmapMemory(device, mesh.index_memory);
                return &meshes.emplace(cache_key, mesh).first->second;
            }
            diagnostics.push_back("Mesh has no drawable triangles at LOD " +
                                  std::to_string(lod) + ": " + resource);
        } catch (const std::exception& error) {
            diagnostics.push_back("Mesh upload failed for " + resource + " at LOD " +
                                  std::to_string(lod) + ": " + error.what());
        }
#endif
        failed_meshes.insert(cache_key);
        return nullptr;
    }
};

VulkanBackend::VulkanBackend() : impl_(std::make_unique<Impl>()) {
    VkApplicationInfo application{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    application.pApplicationName = "sim-platform";
    application.applicationVersion = VK_MAKE_VERSION(0, 1, 0);
    application.pEngineName = "sim-platform";
    application.engineVersion = VK_MAKE_VERSION(0, 1, 0);
    application.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo instance_info{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    instance_info.pApplicationInfo = &application;
    Check(vkCreateInstance(&instance_info, nullptr, &impl_->instance), "vkCreateInstance");

    std::uint32_t device_count = 0;
    Check(vkEnumeratePhysicalDevices(impl_->instance, &device_count, nullptr),
          "vkEnumeratePhysicalDevices");
    if (device_count == 0)
        throw Error(ErrorCode::BackendUnavailable, "no Vulkan physical device is available");
    std::vector<VkPhysicalDevice> devices(device_count);
    Check(vkEnumeratePhysicalDevices(impl_->instance, &device_count, devices.data()),
          "vkEnumeratePhysicalDevices");
    for (const VkPhysicalDevice candidate : devices) {
        std::uint32_t family_count = 0;
        vkGetPhysicalDeviceQueueFamilyProperties(candidate, &family_count, nullptr);
        std::vector<VkQueueFamilyProperties> families(family_count);
        vkGetPhysicalDeviceQueueFamilyProperties(candidate, &family_count, families.data());
        for (std::uint32_t family = 0; family < family_count; ++family) {
            if ((families[family].queueFlags & VK_QUEUE_GRAPHICS_BIT) != 0) {
                impl_->physical_device = candidate;
                impl_->queue_family = family;
                break;
            }
        }
        if (impl_->physical_device != VK_NULL_HANDLE) break;
    }
    if (impl_->physical_device == VK_NULL_HANDLE)
        throw Error(ErrorCode::BackendUnavailable, "no Vulkan graphics queue is available");

    const float priority = 1.0F;
    VkDeviceQueueCreateInfo queue_info{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    queue_info.queueFamilyIndex = impl_->queue_family;
    queue_info.queueCount = 1;
    queue_info.pQueuePriorities = &priority;
    VkDeviceCreateInfo device_info{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    device_info.queueCreateInfoCount = 1;
    device_info.pQueueCreateInfos = &queue_info;
    Check(vkCreateDevice(impl_->physical_device, &device_info, nullptr, &impl_->device),
          "vkCreateDevice");
    vkGetDeviceQueue(impl_->device, impl_->queue_family, 0, &impl_->queue);
    VkCommandPoolCreateInfo pool_info{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pool_info.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pool_info.queueFamilyIndex = impl_->queue_family;
    Check(vkCreateCommandPool(impl_->device, &pool_info, nullptr, &impl_->command_pool),
          "vkCreateCommandPool");
    vkGetPhysicalDeviceMemoryProperties(impl_->physical_device, &impl_->memory_properties);
    VkPhysicalDeviceProperties properties{};
    vkGetPhysicalDeviceProperties(impl_->physical_device, &properties);
    impl_->device_name = properties.deviceName;

    const auto vertices = CubeVertices();
    impl_->cube_vertex_count = static_cast<std::uint32_t>(vertices.size());
    const VkDeviceSize vertex_bytes = vertices.size() * sizeof(Vertex);
    impl_->create_buffer(vertex_bytes, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
                         VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                         impl_->cube_buffer, impl_->cube_memory);
    void* mapped = nullptr;
    Check(vkMapMemory(impl_->device, impl_->cube_memory, 0, vertex_bytes, 0, &mapped),
          "vkMapMemory(cube)");
    std::memcpy(mapped, vertices.data(), static_cast<std::size_t>(vertex_bytes));
    vkUnmapMemory(impl_->device, impl_->cube_memory);
}

VulkanBackend::~VulkanBackend() = default;
BackendCapabilities VulkanBackend::capabilities() const noexcept { return {.rgb = true, .depth = true}; }
std::string VulkanBackend::device_name() const { return impl_->device_name; }
const RenderSnapshot& VulkanBackend::snapshot() const noexcept { return impl_->snapshot; }
std::vector<std::string> VulkanBackend::consume_diagnostics() {
    std::vector<std::string> result;
    result.swap(impl_->diagnostics);
    return result;
}

void VulkanBackend::sync_from_scene(World& world) {
    world.update_world_transforms();
    auto& snapshot = impl_->snapshot;
    ++snapshot.revision;
    snapshot.instances.clear();
    snapshot.lights.clear();
    snapshot.has_bounds = false;
    std::unordered_set<EntityId> visual_ancestors;
    for (const EntityId visual_entity : world.registry().view<VisualComponent>()) {
        if (!world.registry().get<VisualComponent>(visual_entity).visible) continue;
        EntityId current = visual_entity;
        while (current != kInvalidEntity && world.registry().valid(current)) {
            visual_ancestors.insert(current);
            current = world.registry().get<HierarchyComponent>(current).parent;
        }
    }
    for (const EntityId entity : world.registry().view<TransformComponent>()) {
        if (world.registry().has<LightComponent>(entity)) {
            const auto& light = world.registry().get<LightComponent>(entity);
            if (light.enabled && snapshot.lights.size() < 8) {
                snapshot.lights.push_back({entity, light.type,
                    world.registry().get<TransformComponent>(entity).world, light.color,
                    light.intensity, light.range, light.decay,
                    light.inner_cone_radians, light.outer_cone_radians});
            }
            continue;
        }
        const bool has_collider = world.registry().has<ColliderComponent>(entity);
        const bool has_visual = world.registry().has<VisualComponent>(entity) &&
                                world.registry().get<VisualComponent>(entity).visible;
        if (!has_collider && !has_visual) continue;
        if (!has_visual && has_collider) {
            bool covered_by_visual = false;
            EntityId current = entity;
            while (current != kInvalidEntity && world.registry().valid(current)) {
                if (visual_ancestors.contains(current)) {
                    covered_by_visual = true;
                    break;
                }
                current = world.registry().get<HierarchyComponent>(current).parent;
            }
            if (covered_by_visual) continue;
        }
        const Vec3 size = has_visual ? world.registry().get<VisualComponent>(entity).size
                                     : world.registry().get<ColliderComponent>(entity).size;
        std::string mesh_resource;
        std::string texture_resource;
        if (has_visual)
            mesh_resource = world.registry().get<VisualComponent>(entity).mesh_resource;
        else if (has_collider &&
                 world.registry().get<ColliderComponent>(entity).shape == ColliderShape::Mesh)
            mesh_resource = world.registry().get<ColliderComponent>(entity).mesh_resource;
        if (has_visual)
            texture_resource = world.registry().get<VisualComponent>(entity).material_resource;
        const auto* imported = impl_->ensure_mesh(mesh_resource, 0);
        const std::uint32_t hash = static_cast<std::uint32_t>(entity * 2654435761ULL);
        const std::array<float, 4> fallback_color{
            0.35F + static_cast<float>((hash >> 0U) & 0xFFU) / 1024.0F,
            0.40F + static_cast<float>((hash >> 8U) & 0xFFU) / 1024.0F,
            0.45F + static_cast<float>((hash >> 16U) & 0xFFU) / 1024.0F, 1.0F};
        const auto color = has_visual ? world.registry().get<VisualComponent>(entity).color
                                      : fallback_color;
        const Vec3 local_min = imported ? imported->bounds_min : Vec3{-0.5, -0.5, -0.5};
        const Vec3 local_max = imported ? imported->bounds_max : Vec3{0.5, 0.5, 0.5};
        const auto& transform = world.registry().get<TransformComponent>(entity).world;
        Vec3 world_min{std::numeric_limits<double>::max(), std::numeric_limits<double>::max(),
                       std::numeric_limits<double>::max()};
        Vec3 world_max{std::numeric_limits<double>::lowest(), std::numeric_limits<double>::lowest(),
                       std::numeric_limits<double>::lowest()};
        for (int corner = 0; corner < 8; ++corner) {
            const Vec3 local{(corner & 1) ? local_max.x : local_min.x,
                             (corner & 2) ? local_max.y : local_min.y,
                             (corner & 4) ? local_max.z : local_min.z};
            const Vec3 point = TransformPoint(transform, local,
                                              imported ? Vec3{1.0, 1.0, 1.0} : size);
            world_min = {std::min(world_min.x, point.x), std::min(world_min.y, point.y),
                         std::min(world_min.z, point.z)};
            world_max = {std::max(world_max.x, point.x), std::max(world_max.y, point.y),
                         std::max(world_max.z, point.z)};
        }
        std::uint8_t min_lod = 0, max_lod = 7;
        if (has_visual) {
            const auto& visual = world.registry().get<VisualComponent>(entity);
            min_lod = std::min<std::uint8_t>(visual.min_lod, 7);
            max_lod = std::clamp<std::uint8_t>(visual.max_lod, min_lod, 7);
        }
        snapshot.instances.push_back({entity, transform, size, std::move(mesh_resource),
                                      std::move(texture_resource), color, world_min, world_max,
                                      min_lod, max_lod,
                                      has_visual && world.registry().get<VisualComponent>(entity).use_mesh_material});
        if (!snapshot.has_bounds) {
            snapshot.bounds_min = world_min;
            snapshot.bounds_max = world_max;
            snapshot.has_bounds = true;
        } else {
            snapshot.bounds_min = {std::min(snapshot.bounds_min.x, world_min.x),
                                   std::min(snapshot.bounds_min.y, world_min.y),
                                   std::min(snapshot.bounds_min.z, world_min.z)};
            snapshot.bounds_max = {std::max(snapshot.bounds_max.x, world_max.x),
                                   std::max(snapshot.bounds_max.y, world_max.y),
                                   std::max(snapshot.bounds_max.z, world_max.z)};
        }
    }
}

void VulkanBackend::render_camera(const Transform& world_from_camera, const CameraComponent& camera,
                                  std::vector<std::uint8_t>& rgb,
                                  std::vector<float>& linear_depth_meters) {
    render_camera_internal(world_from_camera, camera, rgb, linear_depth_meters, false, true);
}

void VulkanBackend::render_camera_rgba(const Transform& world_from_camera,
                                       const CameraComponent& camera,
                                       std::vector<std::uint8_t>& rgba) {
    std::vector<float> unused_depth;
    render_camera_internal(world_from_camera, camera, rgba, unused_depth, true, false);
}

void VulkanBackend::render_camera_internal(const Transform& world_from_camera,
                                           const CameraComponent& camera,
                                           std::vector<std::uint8_t>& color,
                                           std::vector<float>& linear_depth_meters,
                                           bool rgba_output, bool read_depth) {
    if (camera.width == 0 || camera.height == 0 || camera.near_plane <= 0.0 ||
        camera.far_plane <= camera.near_plane)
        throw Error(ErrorCode::InvalidArgument, "camera dimensions or clipping planes are invalid");

    const VkDeviceSize pixels = static_cast<VkDeviceSize>(camera.width) * camera.height;
    if (impl_->offscreen.width != camera.width || impl_->offscreen.height != camera.height)
        impl_->destroy_offscreen();
    const bool cached = impl_->offscreen.pipeline != VK_NULL_HANDLE;
    VkImage color_image = impl_->offscreen.color_image;
    VkImage depth_image = impl_->offscreen.depth_image;
    VkDeviceMemory color_memory = impl_->offscreen.color_memory;
    VkDeviceMemory depth_memory = impl_->offscreen.depth_memory;
    VkImageView color_view = impl_->offscreen.color_view;
    VkImageView depth_view = impl_->offscreen.depth_view;
    VkBuffer color_buffer = impl_->offscreen.color_buffer;
    VkBuffer depth_buffer = impl_->offscreen.depth_buffer;
    VkDeviceMemory color_buffer_memory = impl_->offscreen.color_buffer_memory;
    VkDeviceMemory depth_buffer_memory = impl_->offscreen.depth_buffer_memory;
    VkRenderPass render_pass = impl_->offscreen.render_pass;
    VkFramebuffer framebuffer = impl_->offscreen.framebuffer;
    VkShaderModule vertex_shader = impl_->offscreen.vertex_shader;
    VkShaderModule fragment_shader = impl_->offscreen.fragment_shader;
    VkPipelineLayout pipeline_layout = impl_->offscreen.pipeline_layout;
    VkPipeline pipeline = impl_->offscreen.pipeline;
    VkDescriptorSetLayout descriptor_layout = impl_->offscreen.descriptor_layout;
    VkDescriptorPool descriptor_pool = impl_->offscreen.descriptor_pool;
    VkDescriptorSet descriptor_set = impl_->offscreen.descriptor_set;
    VkBuffer camera_buffer = impl_->offscreen.camera_buffer;
    VkDeviceMemory camera_memory = impl_->offscreen.camera_memory;
    VkCommandBuffer command = impl_->offscreen.command;
    struct Push { Mat4 model; std::array<float, 4> color; };

    const auto cleanup = [&] {
        if (command != VK_NULL_HANDLE)
            vkFreeCommandBuffers(impl_->device, impl_->command_pool, 1, &command);
        if (pipeline != VK_NULL_HANDLE) vkDestroyPipeline(impl_->device, pipeline, nullptr);
        if (pipeline_layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(impl_->device, pipeline_layout, nullptr);
        if (descriptor_pool != VK_NULL_HANDLE)
            vkDestroyDescriptorPool(impl_->device, descriptor_pool, nullptr);
        if (descriptor_layout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(impl_->device, descriptor_layout, nullptr);
        if (camera_buffer != VK_NULL_HANDLE) vkDestroyBuffer(impl_->device, camera_buffer, nullptr);
        if (camera_memory != VK_NULL_HANDLE) vkFreeMemory(impl_->device, camera_memory, nullptr);
        if (vertex_shader != VK_NULL_HANDLE)
            vkDestroyShaderModule(impl_->device, vertex_shader, nullptr);
        if (fragment_shader != VK_NULL_HANDLE)
            vkDestroyShaderModule(impl_->device, fragment_shader, nullptr);
        if (framebuffer != VK_NULL_HANDLE) vkDestroyFramebuffer(impl_->device, framebuffer, nullptr);
        if (render_pass != VK_NULL_HANDLE) vkDestroyRenderPass(impl_->device, render_pass, nullptr);
        if (color_view != VK_NULL_HANDLE) vkDestroyImageView(impl_->device, color_view, nullptr);
        if (depth_view != VK_NULL_HANDLE) vkDestroyImageView(impl_->device, depth_view, nullptr);
        if (color_buffer != VK_NULL_HANDLE) vkDestroyBuffer(impl_->device, color_buffer, nullptr);
        if (depth_buffer != VK_NULL_HANDLE) vkDestroyBuffer(impl_->device, depth_buffer, nullptr);
        if (color_buffer_memory != VK_NULL_HANDLE)
            vkFreeMemory(impl_->device, color_buffer_memory, nullptr);
        if (depth_buffer_memory != VK_NULL_HANDLE)
            vkFreeMemory(impl_->device, depth_buffer_memory, nullptr);
        if (color_image != VK_NULL_HANDLE) vkDestroyImage(impl_->device, color_image, nullptr);
        if (depth_image != VK_NULL_HANDLE) vkDestroyImage(impl_->device, depth_image, nullptr);
        if (color_memory != VK_NULL_HANDLE) vkFreeMemory(impl_->device, color_memory, nullptr);
        if (depth_memory != VK_NULL_HANDLE) vkFreeMemory(impl_->device, depth_memory, nullptr);
    };

    try {
        if (!cached) {
        impl_->create_image(camera.width, camera.height, VK_FORMAT_R8G8B8A8_UNORM,
                            VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
                            color_image, color_memory);
        impl_->create_image(camera.width, camera.height, VK_FORMAT_D32_SFLOAT,
                            VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT |
                                VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
                            depth_image, depth_memory);
        impl_->create_buffer(pixels * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                             VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                 VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                             color_buffer, color_buffer_memory,
                             VK_MEMORY_PROPERTY_HOST_CACHED_BIT);
        impl_->create_buffer(pixels * sizeof(float), VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                             VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                 VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                             depth_buffer, depth_buffer_memory,
                             VK_MEMORY_PROPERTY_HOST_CACHED_BIT);

        const auto make_view = [&](VkImage image, VkFormat format, VkImageAspectFlags aspect,
                                   VkImageView& view) {
            VkImageViewCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
            info.image = image;
            info.viewType = VK_IMAGE_VIEW_TYPE_2D;
            info.format = format;
            info.subresourceRange = {aspect, 0, 1, 0, 1};
            Check(vkCreateImageView(impl_->device, &info, nullptr, &view), "vkCreateImageView");
        };
        make_view(color_image, VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_ASPECT_COLOR_BIT, color_view);
        make_view(depth_image, VK_FORMAT_D32_SFLOAT, VK_IMAGE_ASPECT_DEPTH_BIT, depth_view);

        std::array<VkAttachmentDescription, 2> attachments{};
        attachments[0].format = VK_FORMAT_R8G8B8A8_UNORM;
        attachments[0].samples = VK_SAMPLE_COUNT_1_BIT;
        attachments[0].loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        attachments[0].storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        attachments[0].stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        attachments[0].stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        attachments[0].initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        attachments[0].finalLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
        attachments[1].format = VK_FORMAT_D32_SFLOAT;
        attachments[1].samples = VK_SAMPLE_COUNT_1_BIT;
        attachments[1].loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        attachments[1].storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        attachments[1].stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        attachments[1].stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        attachments[1].initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        attachments[1].finalLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
        const VkAttachmentReference color_reference{0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
        const VkAttachmentReference depth_reference{1, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL};
        VkSubpassDescription subpass{};
        subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
        subpass.colorAttachmentCount = 1;
        subpass.pColorAttachments = &color_reference;
        subpass.pDepthStencilAttachment = &depth_reference;
        std::array<VkSubpassDependency, 2> dependencies{};
        dependencies[0].srcSubpass = VK_SUBPASS_EXTERNAL;
        dependencies[0].dstSubpass = 0;
        dependencies[0].srcStageMask = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        dependencies[0].dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT |
                                       VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT;
        dependencies[0].dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT |
                                        VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
        dependencies[1].srcSubpass = 0;
        dependencies[1].dstSubpass = VK_SUBPASS_EXTERNAL;
        dependencies[1].srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT |
                                       VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
        dependencies[1].dstStageMask = VK_PIPELINE_STAGE_TRANSFER_BIT;
        dependencies[1].srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT |
                                        VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
        dependencies[1].dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
        VkRenderPassCreateInfo render_pass_info{VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO};
        render_pass_info.attachmentCount = static_cast<std::uint32_t>(attachments.size());
        render_pass_info.pAttachments = attachments.data();
        render_pass_info.subpassCount = 1;
        render_pass_info.pSubpasses = &subpass;
        render_pass_info.dependencyCount = static_cast<std::uint32_t>(dependencies.size());
        render_pass_info.pDependencies = dependencies.data();
        Check(vkCreateRenderPass(impl_->device, &render_pass_info, nullptr, &render_pass),
              "vkCreateRenderPass");
        const std::array<VkImageView, 2> views{color_view, depth_view};
        VkFramebufferCreateInfo framebuffer_info{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
        framebuffer_info.renderPass = render_pass;
        framebuffer_info.attachmentCount = static_cast<std::uint32_t>(views.size());
        framebuffer_info.pAttachments = views.data();
        framebuffer_info.width = camera.width;
        framebuffer_info.height = camera.height;
        framebuffer_info.layers = 1;
        Check(vkCreateFramebuffer(impl_->device, &framebuffer_info, nullptr, &framebuffer),
              "vkCreateFramebuffer");

        const auto make_shader = [&](const char* path, VkShaderModule& module) {
            const auto code = ReadShader(path);
            VkShaderModuleCreateInfo info{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
            info.codeSize = code.size() * sizeof(std::uint32_t);
            info.pCode = code.data();
            Check(vkCreateShaderModule(impl_->device, &info, nullptr, &module),
                  "vkCreateShaderModule");
        };
        make_shader(SIM_VULKAN_VERTEX_SHADER, vertex_shader);
        make_shader(SIM_VULKAN_FRAGMENT_SHADER, fragment_shader);
        const std::array<VkPipelineShaderStageCreateInfo, 2> shader_stages{{
            {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, nullptr, 0,
             VK_SHADER_STAGE_VERTEX_BIT, vertex_shader, "main", nullptr},
            {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, nullptr, 0,
             VK_SHADER_STAGE_FRAGMENT_BIT, fragment_shader, "main", nullptr},
        }};
        const VkVertexInputBindingDescription binding{0, sizeof(Vertex), VK_VERTEX_INPUT_RATE_VERTEX};
        const std::array<VkVertexInputAttributeDescription, 3> attributes{{
            {0, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vertex, position)},
            {1, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vertex, normal)},
            {2, 0, VK_FORMAT_R32G32_SFLOAT, offsetof(Vertex, texcoord)},
        }};
        VkPipelineVertexInputStateCreateInfo vertex_input{
            VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};
        vertex_input.vertexBindingDescriptionCount = 1;
        vertex_input.pVertexBindingDescriptions = &binding;
        vertex_input.vertexAttributeDescriptionCount = static_cast<std::uint32_t>(attributes.size());
        vertex_input.pVertexAttributeDescriptions = attributes.data();
        VkPipelineInputAssemblyStateCreateInfo assembly{
            VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO};
        assembly.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
        VkViewport viewport{0.0F, 0.0F, static_cast<float>(camera.width),
                            static_cast<float>(camera.height), 0.0F, 1.0F};
        const VkRect2D scissor{{0, 0}, {camera.width, camera.height}};
        VkPipelineViewportStateCreateInfo viewport_state{
            VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO};
        viewport_state.viewportCount = 1;
        viewport_state.pViewports = &viewport;
        viewport_state.scissorCount = 1;
        viewport_state.pScissors = &scissor;
        VkPipelineRasterizationStateCreateInfo raster{
            VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO};
        raster.polygonMode = VK_POLYGON_MODE_FILL;
        raster.cullMode = VK_CULL_MODE_NONE;
        raster.frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE;
        raster.lineWidth = 1.0F;
        VkPipelineMultisampleStateCreateInfo multisample{
            VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO};
        multisample.rasterizationSamples = VK_SAMPLE_COUNT_1_BIT;
        VkPipelineDepthStencilStateCreateInfo depth_state{
            VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO};
        depth_state.depthTestEnable = VK_TRUE;
        depth_state.depthWriteEnable = VK_TRUE;
        depth_state.depthCompareOp = VK_COMPARE_OP_LESS;
        VkPipelineColorBlendAttachmentState blend_attachment{};
        blend_attachment.colorWriteMask = VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT |
                                          VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
        VkPipelineColorBlendStateCreateInfo blend{
            VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO};
        blend.attachmentCount = 1;
        blend.pAttachments = &blend_attachment;
        const Mat4 view_projection = Multiply(Projection(camera),
                                              ModelMatrix(world_from_camera.inverse()));
        impl_->create_buffer(sizeof(SceneUniform), VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT,
                             VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                 VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                             camera_buffer, camera_memory);
        void* camera_mapped = nullptr;
        Check(vkMapMemory(impl_->device, camera_memory, 0, sizeof(SceneUniform), 0, &camera_mapped),
              "vkMapMemory(camera uniform)");
        SceneUniform scene_uniform;
        scene_uniform.view_projection = view_projection;
        scene_uniform.light_meta[0] = static_cast<std::int32_t>(impl_->snapshot.lights.size());
        for (std::size_t index = 0; index < impl_->snapshot.lights.size(); ++index) {
            const auto& light = impl_->snapshot.lights[index];
            const auto direction = light.world_transform.rotation.rotate(
                light.type == LightType::Spot ? Vec3{0.0, 0.0, -1.0}
                                              : Vec3{1.0, 0.0, 0.0});
            scene_uniform.light_position_type[index] = {
                static_cast<float>(light.world_transform.translation.x),
                static_cast<float>(light.world_transform.translation.y),
                static_cast<float>(light.world_transform.translation.z),
                static_cast<float>(light.type)};
            scene_uniform.light_direction_range[index] = {
                static_cast<float>(direction.x), static_cast<float>(direction.y),
                static_cast<float>(direction.z), light.range};
            scene_uniform.light_color_intensity[index] = {
                light.color[0], light.color[1], light.color[2], light.intensity};
            const auto cone = SpotConeCosines(light);
            scene_uniform.light_params[index] = {
                cone[0], cone[1], light.decay, 0.0F};
        }
        std::memcpy(camera_mapped, &scene_uniform, sizeof(SceneUniform));
        vkUnmapMemory(impl_->device, camera_memory);
        std::array<VkDescriptorSetLayoutBinding, 2> bindings{};
        bindings[0].binding = 0;
        bindings[0].descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
        bindings[0].descriptorCount = 1;
        bindings[0].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
        bindings[1].binding = 1;
        bindings[1].descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
        bindings[1].descriptorCount = 1;
        bindings[1].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
        VkDescriptorSetLayoutCreateInfo descriptor_layout_info{
            VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
        descriptor_layout_info.bindingCount = static_cast<std::uint32_t>(bindings.size());
        descriptor_layout_info.pBindings = bindings.data();
        Check(vkCreateDescriptorSetLayout(impl_->device, &descriptor_layout_info, nullptr,
                                          &descriptor_layout),
              "vkCreateDescriptorSetLayout");
        const std::array<VkDescriptorPoolSize, 2> pool_sizes{{
            {VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 257},
            {VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 256},
        }};
        VkDescriptorPoolCreateInfo pool_info{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
        pool_info.maxSets = 257;
        pool_info.poolSizeCount = static_cast<std::uint32_t>(pool_sizes.size());
        pool_info.pPoolSizes = pool_sizes.data();
        Check(vkCreateDescriptorPool(impl_->device, &pool_info, nullptr, &descriptor_pool),
              "vkCreateDescriptorPool");
        VkDescriptorSetAllocateInfo set_allocate{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
        set_allocate.descriptorPool = descriptor_pool;
        set_allocate.descriptorSetCount = 1;
        set_allocate.pSetLayouts = &descriptor_layout;
        Check(vkAllocateDescriptorSets(impl_->device, &set_allocate, &descriptor_set),
              "vkAllocateDescriptorSets");
        const VkDescriptorBufferInfo camera_descriptor{camera_buffer, 0, sizeof(SceneUniform)};
        VkWriteDescriptorSet descriptor_write{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
        descriptor_write.dstSet = descriptor_set;
        descriptor_write.dstBinding = 0;
        descriptor_write.descriptorCount = 1;
        descriptor_write.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
        descriptor_write.pBufferInfo = &camera_descriptor;
        vkUpdateDescriptorSets(impl_->device, 1, &descriptor_write, 0, nullptr);
        const VkPushConstantRange push_range{VK_SHADER_STAGE_VERTEX_BIT, 0, sizeof(Push)};
        VkPipelineLayoutCreateInfo layout_info{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
        layout_info.setLayoutCount = 1;
        layout_info.pSetLayouts = &descriptor_layout;
        layout_info.pushConstantRangeCount = 1;
        layout_info.pPushConstantRanges = &push_range;
        Check(vkCreatePipelineLayout(impl_->device, &layout_info, nullptr, &pipeline_layout),
              "vkCreatePipelineLayout");
        VkGraphicsPipelineCreateInfo pipeline_info{VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO};
        pipeline_info.stageCount = static_cast<std::uint32_t>(shader_stages.size());
        pipeline_info.pStages = shader_stages.data();
        pipeline_info.pVertexInputState = &vertex_input;
        pipeline_info.pInputAssemblyState = &assembly;
        pipeline_info.pViewportState = &viewport_state;
        pipeline_info.pRasterizationState = &raster;
        pipeline_info.pMultisampleState = &multisample;
        pipeline_info.pDepthStencilState = &depth_state;
        pipeline_info.pColorBlendState = &blend;
        pipeline_info.layout = pipeline_layout;
        pipeline_info.renderPass = render_pass;
        pipeline_info.subpass = 0;
        Check(vkCreateGraphicsPipelines(impl_->device, VK_NULL_HANDLE, 1, &pipeline_info,
                                        nullptr, &pipeline),
              "vkCreateGraphicsPipelines");

        VkCommandBufferAllocateInfo allocate{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
        allocate.commandPool = impl_->command_pool;
        allocate.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        allocate.commandBufferCount = 1;
        Check(vkAllocateCommandBuffers(impl_->device, &allocate, &command),
              "vkAllocateCommandBuffers");
        auto& target = impl_->offscreen;
        target.width = camera.width; target.height = camera.height;
        target.color_image = color_image; target.depth_image = depth_image;
        target.color_memory = color_memory; target.depth_memory = depth_memory;
        target.color_view = color_view; target.depth_view = depth_view;
        target.color_buffer = color_buffer; target.depth_buffer = depth_buffer;
        target.color_buffer_memory = color_buffer_memory;
        target.depth_buffer_memory = depth_buffer_memory;
        target.render_pass = render_pass; target.framebuffer = framebuffer;
        target.vertex_shader = vertex_shader; target.fragment_shader = fragment_shader;
        target.pipeline_layout = pipeline_layout; target.pipeline = pipeline;
        target.descriptor_layout = descriptor_layout; target.descriptor_pool = descriptor_pool;
        target.descriptor_set = descriptor_set; target.camera_buffer = camera_buffer;
        target.camera_memory = camera_memory; target.command = command;
        target.generation = ++impl_->descriptor_generation;
        }
        const Mat4 view_projection = Multiply(Projection(camera),
                                              ModelMatrix(world_from_camera.inverse()));
        void* camera_mapped = nullptr;
        Check(vkMapMemory(impl_->device, camera_memory, 0, sizeof(SceneUniform), 0, &camera_mapped),
              "vkMapMemory(camera uniform)");
        SceneUniform scene_uniform;
        scene_uniform.view_projection = view_projection;
        scene_uniform.light_meta[0] = static_cast<std::int32_t>(impl_->snapshot.lights.size());
        for (std::size_t index = 0; index < impl_->snapshot.lights.size(); ++index) {
            const auto& light = impl_->snapshot.lights[index];
            const auto direction = light.world_transform.rotation.rotate(
                light.type == LightType::Spot ? Vec3{0.0, 0.0, -1.0}
                                              : Vec3{1.0, 0.0, 0.0});
            scene_uniform.light_position_type[index] = {
                static_cast<float>(light.world_transform.translation.x),
                static_cast<float>(light.world_transform.translation.y),
                static_cast<float>(light.world_transform.translation.z),
                static_cast<float>(light.type)};
            scene_uniform.light_direction_range[index] = {
                static_cast<float>(direction.x), static_cast<float>(direction.y),
                static_cast<float>(direction.z), light.range};
            scene_uniform.light_color_intensity[index] = {
                light.color[0], light.color[1], light.color[2], light.intensity};
            const auto cone = SpotConeCosines(light);
            scene_uniform.light_params[index] = {
                cone[0], cone[1], light.decay, 0.0F};
        }
        std::memcpy(camera_mapped, &scene_uniform, sizeof(SceneUniform));
        vkUnmapMemory(impl_->device, camera_memory);
        Check(vkResetCommandBuffer(command, 0), "vkResetCommandBuffer");
        VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        Check(vkBeginCommandBuffer(command, &begin), "vkBeginCommandBuffer");
        const std::array<VkClearValue, 2> clear{{
            {.color = {{0.0F, 0.0F, 0.0F, 1.0F}}},
            {.depthStencil = {1.0F, 0}},
        }};
        VkRenderPassBeginInfo pass_begin{VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};
        pass_begin.renderPass = render_pass;
        pass_begin.framebuffer = framebuffer;
        pass_begin.renderArea = {{0, 0}, {camera.width, camera.height}};
        pass_begin.clearValueCount = static_cast<std::uint32_t>(clear.size());
        pass_begin.pClearValues = clear.data();
        vkCmdBeginRenderPass(command, &pass_begin, VK_SUBPASS_CONTENTS_INLINE);
        vkCmdBindPipeline(command, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline);
        for (const auto& instance : impl_->snapshot.instances) {
            const auto lod = SelectLod(instance, world_from_camera, camera);
            const auto* imported = impl_->ensure_mesh(instance.mesh_resource, lod);
            const auto* texture = impl_->ensure_texture(instance.texture_resource);
            vkCmdBindDescriptorSets(command, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_layout,
                                    0, 1, &texture->descriptor_set, 0, nullptr);
            const VkBuffer vertex_buffer = imported ? imported->buffer : impl_->cube_buffer;
            const VkDeviceSize vertex_offset = 0;
            vkCmdBindVertexBuffers(command, 0, 1, &vertex_buffer, &vertex_offset);
            const Push push{ModelMatrix(instance.world_transform,
                                        imported ? Vec3{1.0, 1.0, 1.0} : instance.size),
                            instance.color};
            vkCmdPushConstants(command, pipeline_layout, VK_SHADER_STAGE_VERTEX_BIT, 0,
                               sizeof(Push), &push);
            if (imported) {
                vkCmdBindIndexBuffer(command, imported->index_buffer, 0, VK_INDEX_TYPE_UINT32);
                for (const auto& part : imported->parts) {
                    const auto* part_texture = impl_->ensure_texture(
                        instance.use_mesh_material && instance.texture_resource.empty()
                            ? part.texture : instance.texture_resource);
                    vkCmdBindDescriptorSets(command, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_layout,
                        0, 1, &part_texture->descriptor_set, 0, nullptr);
                    const Push part_push{push.model,
                        instance.use_mesh_material && part.material ? part.color : instance.color};
                    vkCmdPushConstants(command, pipeline_layout, VK_SHADER_STAGE_VERTEX_BIT, 0,
                        sizeof(Push), &part_push);
                    vkCmdDrawIndexed(command, part.count, 1, part.first, 0, 0);
                }
            } else {
                vkCmdDraw(command, impl_->cube_vertex_count, 1, 0, 0);
            }
        }
        vkCmdEndRenderPass(command);
        const VkBufferImageCopy color_copy{0, 0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
                                           {0, 0, 0}, {camera.width, camera.height, 1}};
        const VkBufferImageCopy depth_copy{0, 0, 0, {VK_IMAGE_ASPECT_DEPTH_BIT, 0, 0, 1},
                                           {0, 0, 0}, {camera.width, camera.height, 1}};
        vkCmdCopyImageToBuffer(command, color_image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                               color_buffer, 1, &color_copy);
        if (read_depth)
            vkCmdCopyImageToBuffer(command, depth_image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                                   depth_buffer, 1, &depth_copy);
        Check(vkEndCommandBuffer(command), "vkEndCommandBuffer");
        VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};
        submit.commandBufferCount = 1;
        submit.pCommandBuffers = &command;
        Check(vkQueueSubmit(impl_->queue, 1, &submit, VK_NULL_HANDLE), "vkQueueSubmit");
        Check(vkQueueWaitIdle(impl_->queue), "vkQueueWaitIdle");

        void* mapped = nullptr;
        Check(vkMapMemory(impl_->device, color_buffer_memory, 0, VK_WHOLE_SIZE, 0, &mapped),
              "vkMapMemory(color)");
        std::vector<std::uint8_t> rgba(static_cast<std::size_t>(pixels) * 4);
        std::memcpy(rgba.data(), mapped, rgba.size());
        vkUnmapMemory(impl_->device, color_buffer_memory);
        if (rgba_output) {
            color = std::move(rgba);
        } else {
            color.resize(static_cast<std::size_t>(pixels) * 3);
            for (std::size_t index = 0; index < static_cast<std::size_t>(pixels); ++index) {
                color[index * 3] = rgba[index * 4];
                color[index * 3 + 1] = rgba[index * 4 + 1];
                color[index * 3 + 2] = rgba[index * 4 + 2];
            }
        }
        if (read_depth) {
            Check(vkMapMemory(impl_->device, depth_buffer_memory, 0, VK_WHOLE_SIZE, 0, &mapped),
                  "vkMapMemory(depth)");
            std::vector<float> nonlinear_depth(static_cast<std::size_t>(pixels));
            std::memcpy(nonlinear_depth.data(), mapped,
                        nonlinear_depth.size() * sizeof(float));
            vkUnmapMemory(impl_->device, depth_buffer_memory);
            linear_depth_meters.resize(static_cast<std::size_t>(pixels));
            const float near_value = static_cast<float>(camera.near_plane);
            const float far_value = static_cast<float>(camera.far_plane);
            const float a = far_value / (far_value - near_value);
            const float b = -(far_value * near_value) / (far_value - near_value);
            for (std::size_t index = 0; index < static_cast<std::size_t>(pixels); ++index) {
                const float value = nonlinear_depth[index];
                linear_depth_meters[index] = value >= 1.0F - 1e-7F
                    ? far_value : std::clamp(b / (value - a), near_value, far_value);
            }
        }
    } catch (...) {
        cleanup();
        impl_->offscreen = {};
        throw;
    }
}

std::vector<float> VulkanBackend::raycast(const Vec3&, std::span<const Vec3>, double, double) {
    throw Error(ErrorCode::Unsupported, "Vulkan render backend does not provide collision raycasts");
}

}  // namespace sim
