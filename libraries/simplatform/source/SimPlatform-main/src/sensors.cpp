#include "sim/sensors.hpp"
#include "sim/logging.hpp"

#include <numbers>

namespace sim {

SensorBuffer::SensorBuffer(std::size_t capacity) : capacity_(capacity) {
    SIM_LOG_VERBOSE("SensorBuffer::SensorBuffer capacity=" + std::to_string(capacity));
    if (capacity == 0) throw Error(ErrorCode::InvalidArgument, "sensor buffer capacity must be positive");
}

void SensorBuffer::push(SensorFrame frame) {
    SIM_LOG_VERBOSE("SensorBuffer::push appends frame and enforces capacity=" +
                    std::to_string(capacity_));
    frames_.push_back(std::move(frame));
    while (frames_.size() > capacity_) frames_.pop_front();
    SIM_LOG_VERBOSE("SensorBuffer::push completed buffered_frames=" + std::to_string(frames_.size()));
}

const SensorFrame& SensorBuffer::latest() const {
    if (frames_.empty()) {
        SIM_LOG_VERBOSE("SensorBuffer::latest early return empty buffer");
        throw Error(ErrorCode::InvalidState, "sensor has not produced a frame");
    }
    SIM_LOG_VERBOSE("SensorBuffer::latest returns newest frame");
    return frames_.back();
}

SensorSystem::SensorSystem(World& world, std::shared_ptr<ISensorDataProvider> provider)
    : world_(&world), provider_(std::move(provider)) {
    SIM_LOG_VERBOSE("SensorSystem::SensorSystem registers scheduler world=" +
                    std::to_string(world.id()));
    world_->scheduler().add_system(SimulationPhase::SensorUpdate, "sensor.standard",
                                   [this](double) { update(); });
}

SensorSystem::~SensorSystem() {
    SIM_LOG_VERBOSE("SensorSystem::~SensorSystem unregisters scheduler system");
    if (world_ != nullptr) world_->scheduler().remove_system("sensor.standard");
}

void SensorSystem::validate_rate(double update_rate_hz) const {
    if (!std::isfinite(update_rate_hz) || update_rate_hz <= 0.0) {
        throw Error(ErrorCode::InvalidArgument, "sensor update rate must be finite and positive");
    }
}

EntityId SensorSystem::create_camera(std::string name, EntityId parent, std::string frame_id,
                                     double update_rate_hz, CameraComponent camera) {
    SIM_LOG_VERBOSE("SensorSystem::create_camera name=" + name + " frame=" + frame_id +
                    " rate_hz=" + std::to_string(update_rate_hz) + " size=" +
                    std::to_string(camera.width) + "x" + std::to_string(camera.height));
    validate_rate(update_rate_hz);
    if (camera.width == 0 || camera.height == 0 || camera.near_plane <= 0.0 ||
        camera.far_plane <= camera.near_plane) {
        throw Error(ErrorCode::InvalidArgument, "camera dimensions or clipping planes are invalid");
    }
    const EntityId entity = world_->create_entity(std::move(name), parent);
    world_->registry().emplace<SensorComponent>(entity,
        SensorComponent{std::move(frame_id), update_rate_hz, world_->time().state().simulation_time});
    world_->registry().emplace<CameraComponent>(entity, camera);
    buffers_.emplace(entity, SensorBuffer{});
    SIM_LOG_VERBOSE("SensorSystem::create_camera completed entity=" + std::to_string(entity));
    return entity;
}

EntityId SensorSystem::create_lidar(std::string name, EntityId parent, std::string frame_id,
                                    double update_rate_hz, LiDARComponent lidar) {
    SIM_LOG_VERBOSE("SensorSystem::create_lidar name=" + name + " frame=" + frame_id +
                    " rate_hz=" + std::to_string(update_rate_hz) + " samples=" +
                    std::to_string(lidar.horizontal_samples) + "x" +
                    std::to_string(lidar.vertical_channels));
    validate_rate(update_rate_hz);
    if (lidar.horizontal_samples == 0 || lidar.vertical_channels == 0 ||
        lidar.min_distance < 0.0 || lidar.max_distance <= lidar.min_distance) {
        throw Error(ErrorCode::InvalidArgument, "LiDAR sampling or distance range is invalid");
    }
    const EntityId entity = world_->create_entity(std::move(name), parent);
    world_->registry().emplace<SensorComponent>(entity,
        SensorComponent{std::move(frame_id), update_rate_hz, world_->time().state().simulation_time});
    world_->registry().emplace<LiDARComponent>(entity, lidar);
    buffers_.emplace(entity, SensorBuffer{});
    SIM_LOG_VERBOSE("SensorSystem::create_lidar completed entity=" + std::to_string(entity));
    return entity;
}

EntityId SensorSystem::create_joint_state_sensor(std::string name, EntityId robot,
                                                 std::string frame_id, double update_rate_hz) {
    SIM_LOG_VERBOSE("SensorSystem::create_joint_state_sensor name=" + name + " robot=" +
                    std::to_string(robot) + " frame=" + frame_id);
    validate_rate(update_rate_hz);
    if (!world_->registry().valid(robot) || !world_->registry().has<RobotComponent>(robot)) {
        throw Error(ErrorCode::NotFound, "joint state sensor robot handle is invalid");
    }
    const EntityId entity = world_->create_entity(std::move(name), robot);
    world_->registry().emplace<SensorComponent>(entity,
        SensorComponent{std::move(frame_id), update_rate_hz, world_->time().state().simulation_time});
    world_->registry().emplace<JointStateSensorComponent>(entity, JointStateSensorComponent{robot});
    buffers_.emplace(entity, SensorBuffer{});
    SIM_LOG_VERBOSE("SensorSystem::create_joint_state_sensor completed entity=" +
                    std::to_string(entity));
    return entity;
}

const SensorBuffer& SensorSystem::buffer(EntityId sensor) const {
    const auto it = buffers_.find(sensor);
    if (it == buffers_.end()) {
        SIM_LOG_VERBOSE("SensorSystem::buffer early return invalid sensor=" + std::to_string(sensor));
        throw Error(ErrorCode::NotFound, "invalid sensor handle");
    }
    SIM_LOG_VERBOSE("SensorSystem::buffer returns sensor=" + std::to_string(sensor));
    return it->second;
}

void SensorSystem::update() {
    SIM_LOG_VERBOSE("SensorSystem::update schedules due camera, LiDAR, and joint-state captures");
    const double simulation_time = world_->time().state().simulation_time;
    for (const EntityId sensor : world_->registry().view<SensorComponent>()) {
        auto& component = world_->registry().get<SensorComponent>(sensor);
        if (simulation_time + 1e-12 < component.next_update_time) continue;
        if (world_->registry().has<CameraComponent>(sensor)) update_camera(sensor, component);
        else if (world_->registry().has<LiDARComponent>(sensor)) update_lidar(sensor, component);
        else if (world_->registry().has<JointStateSensorComponent>(sensor)) update_joint_state(sensor, component);
        component.next_update_time += 1.0 / component.update_rate_hz;
        if (component.next_update_time <= simulation_time)
            component.next_update_time = simulation_time + 1.0 / component.update_rate_hz;
    }
    SIM_LOG_VERBOSE("SensorSystem::update completed simulation_time=" + std::to_string(simulation_time));
}

void SensorSystem::publish(EntityId sensor, SensorFrame frame) {
    buffers_.try_emplace(sensor, SensorBuffer{}).first->second.push(std::move(frame));
    world_->events().publish({"SensorFrameReady", world_->id(), sensor,
                              world_->time().state().simulation_time, {}});
    SIM_LOG_VERBOSE("SensorSystem::publish completed sensor=" + std::to_string(sensor));
}

void SensorSystem::update_camera(EntityId sensor, const SensorComponent& component) {
    SIM_LOG_VERBOSE("SensorSystem::update_camera captures sensor=" + std::to_string(sensor));
    SIM_PROFILE_SCOPE("Sensor.CameraCapture");
    if (!provider_) {
        world_->events().publish({"BackendError", world_->id(), sensor,
                                  world_->time().state().simulation_time,
                                  "camera requires a render data provider"});
        SIM_LOG_VERBOSE("SensorSystem::update_camera early return missing provider sensor=" +
                        std::to_string(sensor));
        return;
    }
    const auto& camera = world_->registry().get<CameraComponent>(sensor);
    std::vector<std::uint8_t> rgb;
    std::vector<float> depth;
    provider_->render_camera(world_->registry().get<TransformComponent>(sensor).world, camera, rgb, depth);
    const std::size_t pixels = static_cast<std::size_t>(camera.width) * camera.height;
    if (rgb.size() != pixels * 3 || depth.size() != pixels) {
        throw Error(ErrorCode::BackendUnavailable, "render provider returned invalid camera buffer sizes");
    }
    const double time = world_->time().state().simulation_time;
    publish(sensor, RGBFrame{time, component.frame_id, camera.width, camera.height, std::move(rgb)});
    if (camera.output == CameraOutput::RGBD)
        publish(sensor, DepthFrame{time, component.frame_id, camera.width, camera.height, std::move(depth)});
    SIM_LOG_VERBOSE("SensorSystem::update_camera completed pixels=" + std::to_string(pixels));
}

void SensorSystem::update_lidar(EntityId sensor, const SensorComponent& component) {
    SIM_LOG_VERBOSE("SensorSystem::update_lidar raycasts sensor=" + std::to_string(sensor));
    SIM_PROFILE_SCOPE("Sensor.LidarCapture");
    if (!provider_) {
        world_->events().publish({"BackendError", world_->id(), sensor,
                                  world_->time().state().simulation_time,
                                  "LiDAR requires a collision query data provider"});
        SIM_LOG_VERBOSE("SensorSystem::update_lidar early return missing provider sensor=" +
                        std::to_string(sensor));
        return;
    }
    const auto& lidar = world_->registry().get<LiDARComponent>(sensor);
    const auto& pose = world_->registry().get<TransformComponent>(sensor).world;
    std::vector<Vec3> directions;
    directions.reserve(static_cast<std::size_t>(lidar.horizontal_samples) * lidar.vertical_channels);
    for (std::uint32_t channel = 0; channel < lidar.vertical_channels; ++channel) {
        const double vertical_ratio = lidar.vertical_channels == 1 ? 0.5 :
            static_cast<double>(channel) / static_cast<double>(lidar.vertical_channels - 1);
        const double elevation = (vertical_ratio - 0.5) * lidar.vertical_fov_radians;
        for (std::uint32_t sample = 0; sample < lidar.horizontal_samples; ++sample) {
            const double ratio = static_cast<double>(sample) / static_cast<double>(lidar.horizontal_samples);
            const double azimuth = (ratio - 0.5) * lidar.horizontal_fov_radians;
            const Vec3 local{std::cos(elevation) * std::cos(azimuth),
                             std::cos(elevation) * std::sin(azimuth), std::sin(elevation)};
            directions.push_back(pose.rotation.rotate(local));
        }
    }
    std::vector<float> distances = provider_->raycast(pose.translation, directions,
                                                       lidar.min_distance, lidar.max_distance);
    if (distances.size() != directions.size()) {
        throw Error(ErrorCode::BackendUnavailable, "collision provider returned invalid LiDAR buffer size");
    }
    std::vector<Vec3> points; points.reserve(directions.size());
    for (std::size_t index = 0; index < directions.size(); ++index)
        points.push_back(directions[index] * static_cast<double>(distances[index]));
    publish(sensor, LiDARFrame{world_->time().state().simulation_time, component.frame_id,
                               lidar.horizontal_samples, lidar.vertical_channels,
                               std::move(distances), std::move(points)});
    SIM_LOG_VERBOSE("SensorSystem::update_lidar completed rays=" + std::to_string(directions.size()));
}

void SensorSystem::update_joint_state(EntityId sensor, const SensorComponent& component) {
    SIM_LOG_VERBOSE("SensorSystem::update_joint_state captures sensor=" +
                    std::to_string(sensor));
    const EntityId robot_entity = world_->registry().get<JointStateSensorComponent>(sensor).robot;
    Robot robot(*world_, robot_entity);
    publish(sensor, JointStateFrame{world_->time().state().simulation_time, component.frame_id,
                                    robot.joint_state()});
    SIM_LOG_VERBOSE("SensorSystem::update_joint_state completed sensor=" + std::to_string(sensor));
}

}  // namespace sim
