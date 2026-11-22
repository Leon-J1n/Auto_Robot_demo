#pragma once

#include "sim/ecs.hpp"

#include <array>
#include <chrono>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <random>
#include <string_view>
#include <variant>

namespace sim {

enum class SimulationPhase {
    PreUpdate,
    CommandUpdate,
    ControllerUpdate,
    PrePhysics,
    PhysicsStep,
    PostPhysics,
    CollisionSync,
    SensorUpdate,
    RenderSync,
    Render,
    EventDispatch,
    PostUpdate,
};

enum class SimulationStatus { Stopped, Running, Paused };

struct TimeState {
    double simulation_time{0.0};
    std::uint64_t step_index{0};
    double fixed_time_step{0.002};
    double real_time_factor{0.0};
};

class TimeManager {
public:
    explicit TimeManager(double fixed_time_step = 0.002);
    void set_fixed_time_step(double value);
    void set_real_time_factor(double value);
    void advance();
    void reset() noexcept;
    void restore(TimeState state);
    [[nodiscard]] const TimeState& state() const noexcept { return state_; }

private:
    TimeState state_;
};

struct Event {
    std::string type;
    WorldId world{kInvalidEntity};
    EntityId entity{kInvalidEntity};
    double simulation_time{0.0};
    std::string detail;
};

class EventBus {
public:
    using SubscriptionId = std::uint64_t;
    using Handler = std::function<void(const Event&)>;

    [[nodiscard]] SubscriptionId subscribe(std::string type, Handler handler);
    bool unsubscribe(SubscriptionId id);
    void publish(Event event);
    void dispatch();
    [[nodiscard]] std::size_t pending() const noexcept { return pending_.size(); }

private:
    struct Subscription { SubscriptionId id; std::string type; Handler handler; };
    std::vector<Subscription> subscriptions_;
    std::deque<Event> pending_;
    SubscriptionId next_id_{1};
};

struct CreateEntityCommand { std::string name; std::optional<EntityId> parent; };
struct DestroyEntityCommand { EntityId entity{kInvalidEntity}; };
struct SetTransformCommand { EntityId entity{kInvalidEntity}; Transform transform{}; };
struct StartCommand {};
struct PauseCommand {};
struct ResetCommand {};
using CommandPayload = std::variant<CreateEntityCommand, DestroyEntityCommand, SetTransformCommand,
                                    StartCommand, PauseCommand, ResetCommand>;

struct Command {
    std::uint64_t sequence{0};
    std::string source{"local"};
    double target_time{0.0};
    CommandPayload payload;
};

class CommandQueue {
public:
    [[nodiscard]] std::uint64_t submit(Command command);
    [[nodiscard]] std::vector<Command> take_ready(double simulation_time);
    void clear() noexcept;
    [[nodiscard]] std::size_t size() const noexcept { return commands_.size(); }

private:
    std::deque<Command> commands_;
    std::uint64_t next_sequence_{1};
};

class ControlMailbox {
public:
    using Task = std::function<void()>;

    void submit(Task task);
    [[nodiscard]] std::size_t drain(std::size_t maximum = 1024);
    [[nodiscard]] std::size_t pending() const;

private:
    mutable std::mutex mutex_;
    std::deque<Task> tasks_;
};

[[nodiscard]] std::string GenerateObjectId();

class SimulationScheduler {
public:
    using System = std::function<void(double)>;
    void add_system(SimulationPhase phase, std::string name, System system, int priority = 0);
    bool remove_system(std::string_view name);
    void run(double fixed_time_step);

private:
    struct Entry { std::string name; System system; int priority; };
    std::map<SimulationPhase, std::vector<Entry>> systems_;
};

enum class BackendType { Physics, Collision, Render };
enum class BackendKind {
    None, MuJoCo, Bullet3, Bullet3Collision, FCL, Coal, IFPSCPU, IFPSGPU, Vulkan
};

struct BackendCapabilities {
    bool rigid_body_dynamics{false};
    bool articulated_body{false};
    bool collision_query{false};
    bool distance_query{false};
    bool raycast{false};
    bool rgb{false};
    bool depth{false};
    bool state_serialization{false};
};

struct BackendConfig {
    BackendKind physics{BackendKind::None};
    BackendKind collision{BackendKind::FCL};
    BackendKind render{BackendKind::None};
};

class BackendManager {
public:
    [[nodiscard]] static BackendCapabilities capabilities(BackendKind kind);
    [[nodiscard]] static std::vector<Diagnostic> validate(const BackendConfig& config);
};

struct WorldSnapshot {
    std::uint32_t version{1};
    Registry registry;
    FrameTree frames;
    TimeState time;
    SimulationStatus status{SimulationStatus::Stopped};
    std::mt19937_64 random_state;
};

class World {
public:
    explicit World(WorldId id, double fixed_time_step = 0.002, std::uint64_t random_seed = 0);

    [[nodiscard]] WorldId id() const noexcept { return id_; }
    [[nodiscard]] Registry& registry() noexcept { return registry_; }
    [[nodiscard]] const Registry& registry() const noexcept { return registry_; }
    [[nodiscard]] FrameTree& frames() noexcept { return frames_; }
    [[nodiscard]] const FrameTree& frames() const noexcept { return frames_; }
    [[nodiscard]] TimeManager& time() noexcept { return time_; }
    [[nodiscard]] const TimeManager& time() const noexcept { return time_; }
    [[nodiscard]] EventBus& events() noexcept { return events_; }
    [[nodiscard]] CommandQueue& commands() noexcept { return commands_; }
    [[nodiscard]] ControlMailbox& control_mailbox() noexcept { return control_mailbox_; }
    [[nodiscard]] SimulationScheduler& scheduler() noexcept { return scheduler_; }
    [[nodiscard]] SimulationStatus status() const noexcept { return status_; }

    [[nodiscard]] EntityId create_entity(std::string name, EntityId parent = kInvalidEntity);
    [[nodiscard]] const std::string& object_id(EntityId entity) const;
    [[nodiscard]] EntityId find_entity_by_object_id(std::string_view value) const noexcept;
    bool destroy_entity(EntityId entity);
    void set_parent(EntityId child, EntityId parent);
    void set_parent_preserving_world(EntityId child, EntityId parent);
    void update_world_transforms();
    void start();
    void pause();
    void step(std::uint64_t count = 1);
    // Advance physics without wall-clock pacing. Used by offline Agent execution;
    // browser Live mode continues to use step(), which honors real_time_factor.
    void step_unpaced(std::uint64_t count = 1);
    void reset();
    [[nodiscard]] WorldSnapshot snapshot() const;
    void restore(const WorldSnapshot& state);
    void capture_initial_state();

private:
    void execute_commands();
    void destroy_recursive(EntityId entity);
    void update_subtree(EntityId entity, const Transform& parent_world);

    WorldId id_;
    Registry registry_;
    FrameTree frames_;
    TimeManager time_;
    EventBus events_;
    CommandQueue commands_;
    ControlMailbox control_mailbox_;
    SimulationScheduler scheduler_;
    SimulationStatus status_{SimulationStatus::Stopped};
    std::mt19937_64 random_;
    std::optional<WorldSnapshot> initial_state_;
};

class Simulator {
public:
    explicit Simulator(BackendConfig config = {});
    [[nodiscard]] World& create_world(double fixed_time_step = 0.002, std::uint64_t seed = 0);
    bool destroy_world(WorldId id);
    [[nodiscard]] World& world(WorldId id);
    [[nodiscard]] const World& world(WorldId id) const;
    [[nodiscard]] const BackendConfig& backend_config() const noexcept { return config_; }

private:
    BackendConfig config_;
    std::unordered_map<WorldId, std::unique_ptr<World>> worlds_;
    WorldId next_world_{1};
};

#ifndef SIM_PLUGIN_API_VERSION
#define SIM_PLUGIN_API_VERSION 2
#endif
inline constexpr std::uint32_t kPluginApiVersion = SIM_PLUGIN_API_VERSION;

}  // namespace sim
