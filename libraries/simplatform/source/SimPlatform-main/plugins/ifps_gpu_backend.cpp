#include "sim/ifps.hpp"
#include "sim/plugin.hpp"

#include <cuda.h>
#include <nvrtc.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

struct DeviceSphere {
    float x, y, z, radius;
    std::uint32_t first_child, child_count;
};

struct DeviceObject {
    std::uint32_t sphere_offset, root, transform_index;
};

struct DeviceTransform {
    float tx, ty, tz, radius_scale;
    float qw, qx, qy, qz;
    float sx, sy, sz, padding;
};

static_assert(sizeof(DeviceSphere) == 24);
static_assert(sizeof(DeviceObject) == 12);
static_assert(sizeof(DeviceTransform) == 48);

constexpr const char* kKernelSource = R"CUDA(
struct DeviceSphere {
    float x, y, z, radius;
    unsigned int first_child, child_count;
};
struct DeviceObject {
    unsigned int sphere_offset, root, transform_index;
};
struct DeviceTransform {
    float tx, ty, tz, radius_scale;
    float qw, qx, qy, qz;
    float sx, sy, sz, padding;
};
__device__ DeviceSphere ToWorld(DeviceSphere sphere, const DeviceTransform transform) {
    const float vx = sphere.x * transform.sx;
    const float vy = sphere.y * transform.sy;
    const float vz = sphere.z * transform.sz;
    const float ux = transform.qy * vz - transform.qz * vy;
    const float uy = transform.qz * vx - transform.qx * vz;
    const float uz = transform.qx * vy - transform.qy * vx;
    const float uux = transform.qy * uz - transform.qz * uy;
    const float uuy = transform.qz * ux - transform.qx * uz;
    const float uuz = transform.qx * uy - transform.qy * ux;
    sphere.x = transform.tx + vx + 2.0f * (transform.qw * ux + uux);
    sphere.y = transform.ty + vy + 2.0f * (transform.qw * uy + uuy);
    sphere.z = transform.tz + vz + 2.0f * (transform.qw * uz + uuz);
    sphere.radius *= transform.radius_scale;
    return sphere;
};
__device__ bool PairCollides(const DeviceSphere* spheres,
                             const DeviceTransform* transforms,
                             const DeviceObject left_object,
                             const DeviceObject right_object) {
    const DeviceTransform left_transform = transforms[left_object.transform_index];
    const DeviceTransform right_transform = transforms[right_object.transform_index];
    const unsigned int capacity = 64;
    unsigned int left_stack[capacity];
    unsigned int right_stack[capacity];
    unsigned int count = 1;
    left_stack[0] = left_object.root;
    right_stack[0] = right_object.root;
    while (count != 0) {
        --count;
        const unsigned int left_index = left_stack[count];
        const unsigned int right_index = right_stack[count];
        const DeviceSphere left = ToWorld(spheres[left_object.sphere_offset + left_index],
                                          left_transform);
        const DeviceSphere right = ToWorld(spheres[right_object.sphere_offset + right_index],
                                           right_transform);
        const float dx = left.x - right.x;
        const float dy = left.y - right.y;
        const float dz = left.z - right.z;
        const float radius = left.radius + right.radius;
        if (dx * dx + dy * dy + dz * dz > radius * radius) continue;
        if (left.child_count == 0 && right.child_count == 0) {
            return true;
        }
        const bool expand_left = left.child_count != 0 &&
            (right.child_count == 0 || left.radius >= right.radius);
        const unsigned int child_count = expand_left ? left.child_count : right.child_count;
        if (count + child_count > capacity) {
            return true;
        }
        for (unsigned int child = 0; child < child_count; ++child) {
            left_stack[count] = expand_left ? left.first_child + child : left_index;
            right_stack[count] = expand_left ? right_index : right.first_child + child;
            ++count;
        }
    }
    return false;
}
extern "C" __global__ void TraverseSceneBatch(const DeviceSphere* spheres,
                                                const DeviceTransform* transforms,
                                                const DeviceObject* objects,
                                                unsigned int object_count,
                                                const unsigned int* queries,
                                                unsigned int work_item_count,
                                                unsigned int* results) {
    const unsigned int work_index = blockIdx.x * blockDim.x + threadIdx.x;
    if (work_index >= work_item_count) return;
    const unsigned int query_index = work_index / object_count;
    if (results[query_index] != 0) return;
    const unsigned int left_index = queries[query_index];
    const unsigned int right_index = work_index - query_index * object_count;
    if (left_index == right_index) return;
    if (PairCollides(spheres, transforms, objects[left_index], objects[right_index]))
        atomicExch(results + query_index, 1U);
}
)CUDA";

void Require(CUresult status, const char* context) {
    if (status == CUDA_SUCCESS) return;
    const char* detail = nullptr;
    cuGetErrorString(status, &detail);
    throw sim::Error(sim::ErrorCode::BackendUnavailable,
                     std::string(context) + (detail ? std::string(": ") + detail : ""));
}

std::string CompileKernel() {
    nvrtcProgram program{};
    if (nvrtcCreateProgram(&program, kKernelSource, "ifps_gpu.cu", 0, nullptr, nullptr) !=
        NVRTC_SUCCESS)
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "cannot create IFPS NVRTC program");
    const char* options[]{"--std=c++11", "--gpu-architecture=compute_52"};
    const nvrtcResult compiled = nvrtcCompileProgram(program, 2, options);
    if (compiled != NVRTC_SUCCESS) {
        std::size_t size = 0;
        nvrtcGetProgramLogSize(program, &size);
        std::string log(size, '\0');
        nvrtcGetProgramLog(program, log.data());
        nvrtcDestroyProgram(&program);
        throw sim::Error(sim::ErrorCode::BackendUnavailable,
                         "IFPS NVRTC compile failed: " + log);
    }
    std::size_t size = 0;
    nvrtcGetPTXSize(program, &size);
    std::string ptx(size, '\0');
    nvrtcGetPTX(program, ptx.data());
    nvrtcDestroyProgram(&program);
    return ptx;
}

class IFPSGpuCollisionBackend final : public sim::IFPSCollisionBackend {
public:
    IFPSGpuCollisionBackend() {
        Require(cuInit(0), "CUDA initialization failed");
        CUdevice device{};
        Require(cuDeviceGet(&device, 0), "CUDA device discovery failed");
        Require(cuDevicePrimaryCtxRetain(&context_, device), "CUDA context creation failed");
        Require(cuCtxSetCurrent(context_), "CUDA context activation failed");
        const std::string ptx = CompileKernel();
        Require(cuModuleLoadData(&module_, ptx.data()), "IFPS CUDA module load failed");
        Require(cuModuleGetFunction(&kernel_, module_, "TraverseSceneBatch"),
                "IFPS CUDA kernel lookup failed");
    }

    ~IFPSGpuCollisionBackend() override {
        if (context_) cuCtxSetCurrent(context_);
        release_buffer(scene_buffer_);
        release_buffer(object_buffer_);
        release_buffer(transform_buffer_);
        release_buffer(query_buffer_);
        release_buffer(result_buffer_);
        if (module_) cuModuleUnload(module_);
        if (context_) {
            CUdevice device{};
            if (cuCtxGetDevice(&device) == CUDA_SUCCESS) cuDevicePrimaryCtxRelease(device);
        }
    }

    void build(sim::World& world) override {
        IFPSCollisionBackend::build(world);
        ++topology_generation_;
        ++transform_generation_;
    }

    void sync_from_scene(sim::World& world) override {
        IFPSCollisionBackend::sync_from_scene(world);
        ++transform_generation_;
    }

    std::vector<std::uint8_t> check_scene_collisions(
        std::span<const sim::EntityId> entities) const override {
        constexpr std::size_t gpu_work_item_threshold = 32768;
        if (entities.size() * objects().size() < gpu_work_item_threshold)
            return IFPSCollisionBackend::check_scene_collisions(entities);
        return execute_scene(entities);
    }

protected:
    bool hierarchical_collision(const Object& first, const Object& second) const override {
        return IFPSCollisionBackend::hierarchical_collision(first, second);
    }

private:
    struct Buffer { CUdeviceptr pointer{}; std::size_t capacity{}; };
    struct SceneRecord {
        std::uint32_t object_index{};
    };

    static void release_buffer(Buffer& buffer) {
        if (buffer.pointer) cuMemFree(buffer.pointer);
        buffer = {};
    }

    static void reserve(Buffer& buffer, std::size_t bytes) {
        if (buffer.capacity >= bytes) return;
        release_buffer(buffer);
        Require(cuMemAlloc(&buffer.pointer, bytes), "IFPS CUDA buffer allocation failed");
        buffer.capacity = bytes;
    }

    void ensure_scene_buffers() const {
        Require(cuCtxSetCurrent(context_), "CUDA context activation failed");
        if (uploaded_topology_generation_ != topology_generation_) {
            std::vector<DeviceSphere> values;
            std::vector<DeviceObject> device_objects;
            scene_records_.clear();
            scene_objects_.clear();
            for (const auto& [entity, source] : objects()) {
                (void)entity;
                if (scene_objects_.size() >= std::numeric_limits<std::uint32_t>::max() ||
                    source.tree.nodes.size() >
                        std::numeric_limits<std::uint32_t>::max() - values.size())
                    throw sim::Error(sim::ErrorCode::Unsupported,
                                     "IFPS-GPU scene topology exceeds 32-bit indexing");
                const auto transform_index = static_cast<std::uint32_t>(scene_objects_.size());
                const auto object_index = transform_index;
                scene_records_.emplace(&source, SceneRecord{object_index});
                scene_objects_.push_back(&source);
                device_objects.push_back({static_cast<std::uint32_t>(values.size()),
                                          source.tree.root, transform_index});
                for (const auto& node : source.tree.nodes) {
                    values.push_back({static_cast<float>(node.center.x),
                                      static_cast<float>(node.center.y),
                                      static_cast<float>(node.center.z),
                                      static_cast<float>(node.radius),
                                      node.first_child, node.child_count});
                }
            }
            const std::size_t bytes = values.size() * sizeof(DeviceSphere);
            reserve(scene_buffer_, bytes);
            Require(cuMemcpyHtoD(scene_buffer_.pointer, values.data(), bytes),
                    "IFPS CUDA scene upload failed");
            const std::size_t object_bytes = device_objects.size() * sizeof(DeviceObject);
            reserve(object_buffer_, object_bytes);
            Require(cuMemcpyHtoD(object_buffer_.pointer, device_objects.data(), object_bytes),
                    "IFPS CUDA object table upload failed");
            uploaded_topology_generation_ = topology_generation_;
            uploaded_transform_generation_ = 0;
        }
        if (uploaded_transform_generation_ == transform_generation_) return;
        std::vector<DeviceTransform> transforms;
        transforms.reserve(scene_objects_.size());
        for (const Object* source : scene_objects_) {
            const auto rotation = source->transform.rotation.normalized();
            const auto& scale = source->transform.scale;
            transforms.push_back({
                static_cast<float>(source->transform.translation.x),
                static_cast<float>(source->transform.translation.y),
                static_cast<float>(source->transform.translation.z),
                static_cast<float>(std::max({std::abs(scale.x), std::abs(scale.y),
                                             std::abs(scale.z)})),
                static_cast<float>(rotation.w), static_cast<float>(rotation.x),
                static_cast<float>(rotation.y), static_cast<float>(rotation.z),
                static_cast<float>(std::abs(scale.x)), static_cast<float>(std::abs(scale.y)),
                static_cast<float>(std::abs(scale.z)), 0.0F});
        }
        const std::size_t transform_bytes = transforms.size() * sizeof(DeviceTransform);
        reserve(transform_buffer_, transform_bytes);
        Require(cuMemcpyHtoD(transform_buffer_.pointer, transforms.data(), transform_bytes),
                "IFPS CUDA transform upload failed");
        uploaded_transform_generation_ = transform_generation_;
    }

    std::vector<std::uint8_t> execute_scene(
        std::span<const sim::EntityId> entities) const {
        if (entities.empty()) return {};
        ensure_scene_buffers();
        const std::size_t work_items = entities.size() * scene_objects_.size();
        if (work_items > std::numeric_limits<std::uint32_t>::max())
            throw sim::Error(sim::ErrorCode::Unsupported,
                             "IFPS-GPU scene batch exceeds 32-bit indexing");
        std::vector<std::uint32_t> query_indices;
        query_indices.reserve(entities.size());
        for (const sim::EntityId entity : entities)
            query_indices.push_back(scene_records_.at(&object(entity)).object_index);
        const std::size_t query_bytes = query_indices.size() * sizeof(std::uint32_t);
        reserve(query_buffer_, query_bytes);
        reserve(result_buffer_, query_bytes);
        Require(cuMemcpyHtoD(query_buffer_.pointer, query_indices.data(), query_bytes),
                "IFPS CUDA query upload failed");
        Require(cuMemsetD32(result_buffer_.pointer, 0, entities.size()),
                "IFPS CUDA result initialization failed");
        std::uint32_t object_count = static_cast<std::uint32_t>(scene_objects_.size());
        std::uint32_t work_item_count = static_cast<std::uint32_t>(work_items);
        CUdeviceptr spheres = scene_buffer_.pointer;
        CUdeviceptr transforms = transform_buffer_.pointer;
        CUdeviceptr device_objects = object_buffer_.pointer;
        CUdeviceptr queries = query_buffer_.pointer;
        CUdeviceptr results = result_buffer_.pointer;
        void* arguments[]{&spheres, &transforms, &device_objects, &object_count,
                          &queries, &work_item_count, &results};
        constexpr std::uint32_t threads = 256;
        const std::uint32_t blocks = (work_item_count + threads - 1U) / threads;
        Require(cuLaunchKernel(kernel_, blocks, 1, 1, threads, 1, 1, 0, nullptr,
                               arguments, nullptr),
                "IFPS CUDA batch traversal launch failed");
        std::vector<std::uint32_t> device_result(entities.size());
        Require(cuMemcpyDtoH(device_result.data(), result_buffer_.pointer, query_bytes),
                "IFPS CUDA batch result download failed");
        std::vector<std::uint8_t> result(entities.size());
        std::transform(device_result.begin(), device_result.end(), result.begin(),
                       [](std::uint32_t value) { return value != 0U ? 1U : 0U; });
        return result;
    }

    CUcontext context_{};
    CUmodule module_{};
    CUfunction kernel_{};
    mutable Buffer scene_buffer_{};
    mutable Buffer object_buffer_{};
    mutable Buffer transform_buffer_{};
    mutable Buffer query_buffer_{};
    mutable Buffer result_buffer_{};
    mutable std::unordered_map<const Object*, SceneRecord> scene_records_;
    mutable std::vector<const Object*> scene_objects_;
    mutable std::uint64_t uploaded_topology_generation_{0};
    mutable std::uint64_t uploaded_transform_generation_{0};
    std::uint64_t topology_generation_{1};
    std::uint64_t transform_generation_{1};
};

}  // namespace

struct SimPluginHandle { const SimHostApi* host{nullptr}; };
extern "C" {
SIM_PLUGIN_API std::uint32_t SimPluginApiVersion() { return sim::kPluginApiVersion; }
SIM_PLUGIN_API SimPluginHandle* SimCreatePlugin(const SimHostApi* host) {
    if (!host || host->api_version != sim::kPluginApiVersion) return nullptr;
    if (host->log) host->log(0, "sim.collision.ifps_gpu", "IFPS GPU plugin loaded");
    return new SimPluginHandle{host};
}
SIM_PLUGIN_API void SimDestroyPlugin(SimPluginHandle* plugin) { delete plugin; }
SIM_PLUGIN_API sim::ICollisionBackend* SimCreateCollisionBackend(SimPluginHandle* plugin) {
    if (!plugin) return nullptr;
    try { return new IFPSGpuCollisionBackend{}; } catch (...) { return nullptr; }
}
SIM_PLUGIN_API void SimDestroyCollisionBackend(SimPluginHandle*, sim::ICollisionBackend* backend) {
    delete backend;
}
}
