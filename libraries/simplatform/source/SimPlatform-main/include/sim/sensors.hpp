#pragma once

#include "sim/robot.hpp"

#include <deque>
#include <memory>
#include <variant>

namespace sim {

struct RGBFrame {
    double timestamp{0.0};
    std::string frame_id;
    std::uint32_t width{0};
    std::uint32_t height{0};
    std::vector<std::uint8_t> rgb;
};

struct DepthFrame {
    double timestamp{0.0};
    std::string frame_id;
    std::uint32_t width{0};
    std::uint32_t height{0};
    std::vector<float> linear_depth_meters;
};

struct JointStateFrame {
    double timestamp{0.0};
    std::string frame_id;
    JointState state;
};

struct LiDARFrame {
    double timestamp{0.0};
    std::string frame_id;
    std::uint32_t horizontal_samples{0};
    std::uint32_t vertical_channels{0};
    std::vector<float> distances;
    std::vector<Vec3> points;
};

using SensorFrame = std::variant<RGBFrame, DepthFrame, JointStateFrame, LiDARFrame>;

class ISensorDataProvider {
public:
    virtual ~ISensorDataProvider() = default;
    virtual void render_camera(const Transform& world_from_camera, const CameraComponent& camera,
                               std::vector<std::uint8_t>& rgb,
                               std::vector<float>& linear_depth_meters) = 0;
    [[nodiscard]] virtual std::vector<float> raycast(
        const Vec3& world_origin, std::span<const Vec3> world_directions,
        double min_distance, double max_distance) = 0;
};

class SensorBuffer {
public:
    explicit SensorBuffer(std::size_t capacity = 2);
    void push(SensorFrame frame);
    [[nodiscard]] const SensorFrame& latest() const;
    template <typename Frame>
    [[nodiscard]] const Frame& latest() const {
        for (auto it = frames_.rbegin(); it != frames_.rend(); ++it)
            if (const auto* frame = std::get_if<Frame>(&*it)) return *frame;
        throw Error(ErrorCode::InvalidState, "sensor has not produced the requested frame type");
    }
    [[nodiscard]] std::size_t size() const noexcept { return frames_.size(); }

private:
    std::size_t capacity_;
    std::deque<SensorFrame> frames_;
};

class SensorSystem {
public:
    SensorSystem(World& world, std::shared_ptr<ISensorDataProvider> provider = {});
    ~SensorSystem();
    SensorSystem(const SensorSystem&) = delete;
    SensorSystem& operator=(const SensorSystem&) = delete;

    [[nodiscard]] EntityId create_camera(std::string name, EntityId parent,
                                         std::string frame_id, double update_rate_hz,
                                         CameraComponent camera = {});
    [[nodiscard]] EntityId create_lidar(std::string name, EntityId parent,
                                        std::string frame_id, double update_rate_hz,
                                        LiDARComponent lidar = {});
    [[nodiscard]] EntityId create_joint_state_sensor(std::string name, EntityId robot,
                                                     std::string frame_id,
                                                     double update_rate_hz);
    [[nodiscard]] const SensorBuffer& buffer(EntityId sensor) const;
    void update();

private:
    void validate_rate(double update_rate_hz) const;
    void publish(EntityId sensor, SensorFrame frame);
    void update_camera(EntityId sensor, const SensorComponent& component);
    void update_lidar(EntityId sensor, const SensorComponent& component);
    void update_joint_state(EntityId sensor, const SensorComponent& component);

    World* world_;
    std::shared_ptr<ISensorDataProvider> provider_;
    std::unordered_map<EntityId, SensorBuffer> buffers_;
};

}  // namespace sim
