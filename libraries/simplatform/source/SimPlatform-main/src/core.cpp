#include "sim/core.hpp"
#include "sim/logging.hpp"

#include <algorithm>
#include <iomanip>
#include <limits>
#include <optional>
#include <sstream>
#include <thread>
#include <unordered_set>

namespace sim {

namespace {

Vec3 MultiplyComponents(const Vec3& lhs, const Vec3& rhs) noexcept {
    return {lhs.x * rhs.x, lhs.y * rhs.y, lhs.z * rhs.z};
}

Vec3 DivideComponents(const Vec3& lhs, const Vec3& rhs) {
    if (NearlyEqual(rhs.x, 0.0) || NearlyEqual(rhs.y, 0.0) || NearlyEqual(rhs.z, 0.0)) {
        throw Error(ErrorCode::InvalidArgument, "transform scale must be non-zero");
    }
    return {lhs.x / rhs.x, lhs.y / rhs.y, lhs.z / rhs.z};
}

}  // namespace

Quaternion Quaternion::normalized() const {
    const double norm = std::sqrt(w * w + x * x + y * y + z * z);
    if (NearlyEqual(norm, 0.0)) {
        throw Error(ErrorCode::InvalidArgument, "cannot normalize a zero quaternion");
    }
    return {w / norm, x / norm, y / norm, z / norm};
}

Quaternion Quaternion::operator*(const Quaternion& rhs) const noexcept {
    return {
        w * rhs.w - x * rhs.x - y * rhs.y - z * rhs.z,
        w * rhs.x + x * rhs.w + y * rhs.z - z * rhs.y,
        w * rhs.y - x * rhs.z + y * rhs.w + z * rhs.x,
        w * rhs.z + x * rhs.y - y * rhs.x + z * rhs.w,
    };
}

Vec3 Quaternion::rotate(const Vec3& value) const {
    const Quaternion unit = normalized();
    const Quaternion vector{0.0, value.x, value.y, value.z};
    const Quaternion rotated = unit * vector * unit.conjugate();
    return {rotated.x, rotated.y, rotated.z};
}

Transform Transform::inverse() const {
    const Vec3 inverse_scale = DivideComponents({1.0, 1.0, 1.0}, scale);
    const Quaternion inverse_rotation = rotation.normalized().conjugate();
    const Vec3 inverse_translation = MultiplyComponents(
        inverse_rotation.rotate(translation * -1.0), inverse_scale);
    return {inverse_translation, inverse_rotation, inverse_scale};
}

Transform Transform::operator*(const Transform& child) const {
    return {
        translation + rotation.rotate(MultiplyComponents(scale, child.translation)),
        (rotation * child.rotation).normalized(),
        MultiplyComponents(scale, child.scale),
    };
}

bool NearlyEqual(double lhs, double rhs, double epsilon) noexcept {
    return std::abs(lhs - rhs) <= epsilon;
}

bool NearlyEqual(const Vec3& lhs, const Vec3& rhs, double epsilon) noexcept {
    return NearlyEqual(lhs.x, rhs.x, epsilon) && NearlyEqual(lhs.y, rhs.y, epsilon) &&
           NearlyEqual(lhs.z, rhs.z, epsilon);
}

FrameTree::FrameTree(std::string root) : root_(std::move(root)) {
    if (root_.empty()) {
        throw Error(ErrorCode::InvalidArgument, "frame tree root cannot be empty");
    }
    frames_.emplace(root_, FrameNode{"", Transform::Identity()});
}

void FrameTree::set_frame(std::string frame, std::string parent, Transform parent_from_frame) {
    if (frame.empty() || parent.empty()) {
        throw Error(ErrorCode::InvalidArgument, "frame and parent names cannot be empty");
    }
    if (frame == root_) {
        throw Error(ErrorCode::InvalidArgument, "root frame cannot be reparented");
    }
    if (!contains(parent)) {
        throw Error(ErrorCode::NotFound, "parent frame does not exist: " + parent);
    }
    if (would_create_cycle(frame, parent)) {
        throw Error(ErrorCode::ValidationFailed, "frame relationship would create a cycle");
    }
    frames_.insert_or_assign(std::move(frame), FrameNode{std::move(parent), parent_from_frame});
}

bool FrameTree::remove_frame(const std::string& frame) {
    if (frame == root_ || !contains(frame)) {
        return false;
    }
    for (const auto& [_, node] : frames_) {
        if (node.parent == frame) {
            return false;
        }
    }
    return frames_.erase(frame) != 0;
}

bool FrameTree::contains(const std::string& frame) const { return frames_.contains(frame); }

std::vector<FrameTree::Record> FrameTree::records() const {
    std::vector<Record> result;
    result.reserve(frames_.size() > 0 ? frames_.size() - 1 : 0);
    for (const auto& [frame, node] : frames_) {
        if (frame != root_) result.push_back({frame, node.parent, node.parent_from_frame});
    }
    std::sort(result.begin(), result.end(), [](const Record& lhs, const Record& rhs) {
        return lhs.frame < rhs.frame;
    });
    return result;
}

Transform FrameTree::lookup(const std::string& source, const std::string& target) const {
    return root_from(target).inverse() * root_from(source);
}

Transform FrameTree::root_from(const std::string& frame) const {
    if (!contains(frame)) {
        throw Error(ErrorCode::NotFound, "frame does not exist: " + frame);
    }
    Transform result = Transform::Identity();
    std::string current = frame;
    std::unordered_set<std::string> visited;
    while (current != root_) {
        if (!visited.insert(current).second) {
            throw Error(ErrorCode::ValidationFailed, "cycle detected in frame tree");
        }
        const auto& node = frames_.at(current);
        result = node.parent_from_frame * result;
        current = node.parent;
    }
    return result;
}

bool FrameTree::would_create_cycle(const std::string& frame, const std::string& parent) const {
    std::string current = parent;
    std::unordered_set<std::string> visited;
    while (!current.empty()) {
        if (current == frame) {
            return true;
        }
        if (!visited.insert(current).second) {
            return true;
        }
        const auto it = frames_.find(current);
        current = it == frames_.end() ? "" : it->second.parent;
    }
    return false;
}

TimeManager::TimeManager(double fixed_time_step) { set_fixed_time_step(fixed_time_step); }

void TimeManager::set_fixed_time_step(double value) {
    SIM_LOG_VERBOSE("TimeManager::set_fixed_time_step validates seconds=" + std::to_string(value));
    if (!std::isfinite(value) || value <= 0.0) {
        throw Error(ErrorCode::InvalidArgument, "fixed time step must be finite and positive");
    }
    state_.fixed_time_step = value;
}

void TimeManager::set_real_time_factor(double value) {
    SIM_LOG_VERBOSE("TimeManager::set_real_time_factor validates factor=" + std::to_string(value));
    if (!std::isfinite(value) || value < 0.0) {
        throw Error(ErrorCode::InvalidArgument, "real-time factor must be finite and non-negative");
    }
    state_.real_time_factor = value;
}

void TimeManager::advance() {
    state_.simulation_time += state_.fixed_time_step;
    ++state_.step_index;
}

void TimeManager::reset() noexcept {
    state_.simulation_time = 0.0;
    state_.step_index = 0;
}

void TimeManager::restore(TimeState state) {
    set_fixed_time_step(state.fixed_time_step);
    set_real_time_factor(state.real_time_factor);
    if (!std::isfinite(state.simulation_time) || state.simulation_time < 0.0) {
        throw Error(ErrorCode::ValidationFailed, "snapshot has invalid simulation time");
    }
    state_ = state;
}

EventBus::SubscriptionId EventBus::subscribe(std::string type, Handler handler) {
    if (!handler) {
        throw Error(ErrorCode::InvalidArgument, "event handler cannot be empty");
    }
    const SubscriptionId id = next_id_++;
    SIM_LOG_VERBOSE("EventBus::subscribe registers subscription id=" + std::to_string(id) +
                    " type=" + type);
    subscriptions_.push_back({id, std::move(type), std::move(handler)});
    return id;
}

bool EventBus::unsubscribe(SubscriptionId id) {
    const auto before = subscriptions_.size();
    std::erase_if(subscriptions_, [id](const Subscription& item) { return item.id == id; });
    const bool removed = subscriptions_.size() != before;
    SIM_LOG_VERBOSE("EventBus::unsubscribe id=" + std::to_string(id) +
                    " removed=" + (removed ? "true" : "false"));
    return removed;
}

void EventBus::publish(Event event) {
    SIM_LOG_VERBOSE("EventBus::publish queues type=" + event.type + " world=" +
                    std::to_string(event.world) + " entity=" + std::to_string(event.entity));
    pending_.push_back(std::move(event));
}

void EventBus::dispatch() {
    SIM_LOG_VERBOSE("EventBus::dispatch begins pending=" + std::to_string(pending_.size()));
    std::deque<Event> current;
    current.swap(pending_);
#if SIM_LOG_ENABLE_PERF && SIM_LOG_ENABLE_INFO
    std::optional<logging::ScopedProfilerTimer> profile;
    if (!current.empty()) profile.emplace("EventBus.Dispatch");
#endif
    for (const auto& event : current) {
        for (const auto& subscription : subscriptions_) {
            if (subscription.type.empty() || subscription.type == event.type) {
                subscription.handler(event);
            }
        }
    }
    SIM_LOG_VERBOSE("EventBus::dispatch completed events=" + std::to_string(current.size()));
}

std::uint64_t CommandQueue::submit(Command command) {
    if (!std::isfinite(command.target_time) || command.target_time < 0.0) {
        throw Error(ErrorCode::InvalidArgument, "command target time must be finite and non-negative");
    }
    command.sequence = next_sequence_++;
    const std::uint64_t sequence = command.sequence;
    SIM_LOG_VERBOSE("CommandQueue::submit sequence=" + std::to_string(sequence) +
                    " target_time=" + std::to_string(command.target_time));
    commands_.push_back(std::move(command));
    std::stable_sort(commands_.begin(), commands_.end(), [](const Command& lhs, const Command& rhs) {
        if (lhs.target_time != rhs.target_time) {
            return lhs.target_time < rhs.target_time;
        }
        return lhs.sequence < rhs.sequence;
    });
    SIM_LOG_VERBOSE("CommandQueue::submit completed sequence=" + std::to_string(sequence));
    return sequence;
}

std::vector<Command> CommandQueue::take_ready(double simulation_time) {
    std::vector<Command> ready;
    while (!commands_.empty() && commands_.front().target_time <= simulation_time) {
        ready.push_back(std::move(commands_.front()));
        commands_.pop_front();
    }
    if (!ready.empty()) SIM_LOG_VERBOSE("CommandQueue::take_ready time=" +
        std::to_string(simulation_time) + " count=" + std::to_string(ready.size()));
    SIM_LOG_VERBOSE("CommandQueue::take_ready completed count=" + std::to_string(ready.size()));
    return ready;
}

void CommandQueue::clear() noexcept {
    commands_.clear();
}

void ControlMailbox::submit(Task task) {
    if (!task) throw Error(ErrorCode::InvalidArgument, "control mailbox task cannot be empty");
    std::scoped_lock lock(mutex_);
    tasks_.push_back(std::move(task));
    SIM_LOG_VERBOSE("ControlMailbox::submit completed pending=" + std::to_string(tasks_.size()));
}

std::size_t ControlMailbox::drain(std::size_t maximum) {
    std::deque<Task> ready;
    {
        std::scoped_lock lock(mutex_);
        const std::size_t count = std::min(maximum, tasks_.size());
        for (std::size_t index = 0; index < count; ++index) {
            ready.push_back(std::move(tasks_.front()));
            tasks_.pop_front();
        }
    }
    if (!ready.empty()) SIM_LOG_VERBOSE("ControlMailbox::drain executes count=" +
        std::to_string(ready.size()) + " maximum=" + std::to_string(maximum));
    for (auto& task : ready) task();
    SIM_LOG_VERBOSE("ControlMailbox::drain completed executed=" + std::to_string(ready.size()));
    return ready.size();
}

std::size_t ControlMailbox::pending() const {
    std::scoped_lock lock(mutex_);
    const std::size_t result = tasks_.size();
    SIM_LOG_VERBOSE("ControlMailbox::pending returns count=" + std::to_string(result));
    return result;
}

std::string GenerateObjectId() {
    std::array<std::uint8_t, 16> bytes{};
    std::random_device source;
    for (std::size_t offset = 0; offset < bytes.size(); offset += sizeof(std::uint32_t)) {
        const std::uint32_t value = source();
        for (std::size_t index = 0; index < sizeof(value); ++index)
            bytes[offset + index] = static_cast<std::uint8_t>(value >> (index * 8U));
    }
    bytes[6] = static_cast<std::uint8_t>((bytes[6] & 0x0FU) | 0x40U);
    bytes[8] = static_cast<std::uint8_t>((bytes[8] & 0x3FU) | 0x80U);
    std::ostringstream output;
    output << std::hex << std::setfill('0');
    for (std::size_t index = 0; index < bytes.size(); ++index) {
        if (index == 4 || index == 6 || index == 8 || index == 10) output << '-';
        output << std::setw(2) << static_cast<unsigned>(bytes[index]);
    }
    return output.str();
}

void SimulationScheduler::add_system(SimulationPhase phase, std::string name, System system,
                                     int priority) {
    if (name.empty() || !system) {
        throw Error(ErrorCode::InvalidArgument, "scheduler system requires a name and callback");
    }
    for (const auto& [_, entries] : systems_) {
        if (std::any_of(entries.begin(), entries.end(), [&](const Entry& item) { return item.name == name; })) {
            throw Error(ErrorCode::AlreadyExists, "scheduler system name already exists: " + name);
        }
    }
    auto& entries = systems_[phase];
    SIM_LOG_VERBOSE("SimulationScheduler::add_system name=" + name +
                    " priority=" + std::to_string(priority));
    entries.push_back({std::move(name), std::move(system), priority});
    std::stable_sort(entries.begin(), entries.end(), [](const Entry& lhs, const Entry& rhs) {
        return lhs.priority < rhs.priority;
    });
}

bool SimulationScheduler::remove_system(std::string_view name) {
    for (auto& [_, entries] : systems_) {
        const auto before = entries.size();
        std::erase_if(entries, [&](const Entry& entry) { return entry.name == name; });
        if (entries.size() != before) {
            SIM_LOG_VERBOSE("SimulationScheduler::remove_system removed name=" + std::string(name));
            return true;
        }
    }
    return false;
}

void SimulationScheduler::run(double fixed_time_step) {
    SIM_LOG_VERBOSE("SimulationScheduler::run begins delta_seconds=" + std::to_string(fixed_time_step));
    SIM_PROFILE_SCOPE("Scheduler.Run");
    for (auto& [_, entries] : systems_) {
        for (auto& entry : entries) {
            SIM_LOG_VERBOSE("SimulationScheduler::run invokes system=" + entry.name +
                            " delta_seconds=" + std::to_string(fixed_time_step));
            entry.system(fixed_time_step);
        }
    }
    SIM_LOG_VERBOSE("SimulationScheduler::run completed");
}

BackendCapabilities BackendManager::capabilities(BackendKind kind) {
    switch (kind) {
        case BackendKind::None:
            return {};
        case BackendKind::MuJoCo:
            return {.rigid_body_dynamics = true,
                    .articulated_body = true,
                    .collision_query = true,
                    .distance_query = true,
                    .raycast = true,
                    .state_serialization = true};
        case BackendKind::Bullet3:
            return {.rigid_body_dynamics = true, .state_serialization = true};
        case BackendKind::Bullet3Collision:
            return {.collision_query = true, .distance_query = true};
        case BackendKind::FCL:
            return {.collision_query = true, .distance_query = true, .raycast = true};
        case BackendKind::Coal:
            return {.collision_query = true, .distance_query = true};
        case BackendKind::IFPSCPU:
        case BackendKind::IFPSGPU:
            return {.collision_query = true, .distance_query = true};
        case BackendKind::Vulkan:
            return {.rgb = true, .depth = true};
    }
    return {};
}

std::vector<Diagnostic> BackendManager::validate(const BackendConfig& config) {
    SIM_LOG_VERBOSE("BackendManager::validate begins");
    std::vector<Diagnostic> diagnostics;
    if (config.physics != BackendKind::None && config.physics != BackendKind::MuJoCo &&
        config.physics != BackendKind::Bullet3) {
        diagnostics.push_back({Severity::Fatal, "backend.physics.type",
                               "physics must be None, MuJoCo, or Bullet3"});
    }
    if (config.collision != BackendKind::MuJoCo &&
        config.collision != BackendKind::Bullet3Collision &&
        config.collision != BackendKind::FCL &&
        config.collision != BackendKind::Coal &&
        config.collision != BackendKind::IFPSCPU && config.collision != BackendKind::IFPSGPU) {
        diagnostics.push_back({Severity::Fatal, "backend.collision.type",
                               "collision must be MuJoCo, Bullet3, FCL, Coal, IFPS-CPU, or IFPS-GPU"});
    }
    if (config.physics == BackendKind::None &&
        config.collision != BackendKind::Bullet3Collision &&
        config.collision != BackendKind::FCL &&
        config.collision != BackendKind::Coal &&
        config.collision != BackendKind::IFPSCPU && config.collision != BackendKind::IFPSGPU) {
        diagnostics.push_back({Severity::Fatal, "backend.combination.no_physics",
                               "no-physics mode requires Bullet3, FCL, Coal, or an IFPS collision backend"});
    }
    if (config.collision == BackendKind::MuJoCo && config.physics != BackendKind::MuJoCo) {
        diagnostics.push_back({Severity::Fatal, "backend.combination.mujoco_collision",
                               "MuJoCo collision queries require the MuJoCo physics backend"});
    }
    if (config.render != BackendKind::None && config.render != BackendKind::Vulkan) {
        diagnostics.push_back({Severity::Fatal, "backend.render.type",
                               "the first stage supports only Vulkan or headless rendering"});
    }
    if (config.physics != BackendKind::None &&
        (config.collision == BackendKind::Bullet3Collision ||
         config.collision == BackendKind::FCL || config.collision == BackendKind::IFPSCPU ||
         config.collision == BackendKind::Coal ||
         config.collision == BackendKind::IFPSGPU)) {
        diagnostics.push_back({Severity::Warning, "backend.sync.dual_collision",
                               "physics transforms must be synchronized to the collision backend after every step"});
    }
    SIM_LOG_VERBOSE("BackendManager::validate completed diagnostics=" +
                    std::to_string(diagnostics.size()));
    return diagnostics;
}

World::World(WorldId id, double fixed_time_step, std::uint64_t random_seed)
    : id_(id), time_(fixed_time_step), random_(random_seed) {
    scheduler_.add_system(SimulationPhase::PreUpdate, "sim.control", [this](double) {
        (void)control_mailbox_.drain();
    });
    scheduler_.add_system(SimulationPhase::CommandUpdate, "sim.command", [this](double) {
        execute_commands();
    });
    scheduler_.add_system(SimulationPhase::EventDispatch, "sim.events", [this](double) {
        events_.dispatch();
    });
    capture_initial_state();
}

EntityId World::create_entity(std::string name, EntityId parent) {
    SIM_LOG_VERBOSE("World::create_entity validates parent name=" + name + " parent=" +
                    std::to_string(parent));
    if (parent != kInvalidEntity && !registry_.valid(parent)) {
        throw Error(ErrorCode::NotFound, "parent entity does not exist");
    }
    const EntityId entity = registry_.create(std::move(name));
    registry_.emplace<ObjectIdentityComponent>(entity, ObjectIdentityComponent{GenerateObjectId()});
    if (parent != kInvalidEntity) {
        set_parent(entity, parent);
    }
    SIM_LOG_VERBOSE("World::create_entity publishes entity id=" + std::to_string(entity) +
                    " world=" + std::to_string(id_));
    events_.publish({"EntityCreated", id_, entity, time_.state().simulation_time, {}});
    return entity;
}

const std::string& World::object_id(EntityId entity) const {
    return registry_.get<ObjectIdentityComponent>(entity).value;
}

EntityId World::find_entity_by_object_id(std::string_view value) const noexcept {
    if (value.empty()) return kInvalidEntity;
    for (const EntityId entity : registry_.view<ObjectIdentityComponent>()) {
        if (registry_.get<ObjectIdentityComponent>(entity).value == value) return entity;
    }
    return kInvalidEntity;
}

bool World::destroy_entity(EntityId entity) {
    if (!registry_.valid(entity)) {
        SIM_LOG_WARNING("World", "destroy_entity ignored invalid entity=" +
                        std::to_string(entity) + " world=" + std::to_string(id_));
        return false;
    }
    SIM_LOG_VERBOSE("World::destroy_entity recursively removes entity=" + std::to_string(entity));
    destroy_recursive(entity);
    update_world_transforms();
    return true;
}

void World::destroy_recursive(EntityId entity) {
    const auto children = registry_.get<HierarchyComponent>(entity).children;
    for (const EntityId child : children) {
        destroy_recursive(child);
    }
    const EntityId parent = registry_.get<HierarchyComponent>(entity).parent;
    if (parent != kInvalidEntity && registry_.valid(parent)) {
        auto& siblings = registry_.get<HierarchyComponent>(parent).children;
        std::erase(siblings, entity);
    }
    registry_.destroy(entity);
    events_.publish({"EntityDestroyed", id_, entity, time_.state().simulation_time, {}});
}

void World::set_parent(EntityId child, EntityId parent) {
    SIM_LOG_VERBOSE("World::set_parent child=" + std::to_string(child) + " parent=" +
                    std::to_string(parent));
    if (!registry_.valid(child) || (parent != kInvalidEntity && !registry_.valid(parent))) {
        throw Error(ErrorCode::NotFound, "child or parent entity does not exist");
    }
    if (child == parent) {
        throw Error(ErrorCode::ValidationFailed, "entity cannot be its own parent");
    }
    EntityId ancestor = parent;
    while (ancestor != kInvalidEntity) {
        if (ancestor == child) {
            throw Error(ErrorCode::ValidationFailed, "entity hierarchy would create a cycle");
        }
        ancestor = registry_.get<HierarchyComponent>(ancestor).parent;
    }

    auto& child_hierarchy = registry_.get<HierarchyComponent>(child);
    if (child_hierarchy.parent != kInvalidEntity) {
        auto& previous_children = registry_.get<HierarchyComponent>(child_hierarchy.parent).children;
        std::erase(previous_children, child);
    }
    child_hierarchy.parent = parent;
    if (parent != kInvalidEntity) {
        registry_.get<HierarchyComponent>(parent).children.push_back(child);
    }
    update_world_transforms();
}

void World::set_parent_preserving_world(EntityId child, EntityId parent) {
    SIM_LOG_VERBOSE("World::set_parent_preserving_world child=" + std::to_string(child) +
                    " parent=" + std::to_string(parent));
    if (!registry_.valid(child) || (parent != kInvalidEntity && !registry_.valid(parent))) {
        throw Error(ErrorCode::NotFound, "child or parent entity does not exist");
    }
    update_world_transforms();
    const Transform previous_world = registry_.get<TransformComponent>(child).world;
    set_parent(child, parent);
    registry_.get<TransformComponent>(child).local = parent == kInvalidEntity
        ? previous_world
        : registry_.get<TransformComponent>(parent).world.inverse() * previous_world;
    update_world_transforms();
}

void World::update_world_transforms() {
    SIM_LOG_VERBOSE("World::update_world_transforms begins world=" + std::to_string(id_));
    SIM_PROFILE_SCOPE("World.UpdateTransforms");
    for (const EntityId entity : registry_.view<TransformComponent, HierarchyComponent>()) {
        if (registry_.get<HierarchyComponent>(entity).parent == kInvalidEntity) {
            update_subtree(entity, Transform::Identity());
        }
    }
    SIM_LOG_VERBOSE("World::update_world_transforms completed world=" + std::to_string(id_));
}

void World::update_subtree(EntityId entity, const Transform& parent_world) {
    auto& transform = registry_.get<TransformComponent>(entity);
    transform.world = parent_world * transform.local;
    const auto children = registry_.get<HierarchyComponent>(entity).children;
    for (const EntityId child : children) {
        update_subtree(child, transform.world);
    }
}

void World::start() {
    SIM_LOG_VERBOSE("World::start transitions the simulation to Running");
    status_ = SimulationStatus::Running;
    events_.publish({"SimulationStarted", id_, kInvalidEntity, time_.state().simulation_time, {}});
}

void World::pause() {
    SIM_LOG_VERBOSE("World::pause transitions the simulation to Paused");
    status_ = SimulationStatus::Paused;
    events_.publish({"SimulationPaused", id_, kInvalidEntity, time_.state().simulation_time, {}});
}

void World::step(std::uint64_t count) {
    SIM_LOG_VERBOSE("World::step executes fixed-step batch count=" + std::to_string(count) +
                    " world=" + std::to_string(id_));
    SIM_PROFILE_SCOPE("World.Step");
    const double factor = time_.state().real_time_factor;
    for (std::uint64_t index = 0; index < count; ++index) {
        const auto wall_start = std::chrono::steady_clock::now();
        scheduler_.run(time_.state().fixed_time_step);
        update_world_transforms();
        time_.advance();
        if (factor > 0.0) {
            const auto target = std::chrono::duration<double>(time_.state().fixed_time_step / factor);
            const auto elapsed = std::chrono::steady_clock::now() - wall_start;
            if (elapsed < target) std::this_thread::sleep_for(target - elapsed);
        }
    }
    SIM_LOG_VERBOSE("World::step completed world=" + std::to_string(id_) +
                    " simulation_time=" + std::to_string(time_.state().simulation_time));
}

void World::step_unpaced(std::uint64_t count) {
    SIM_LOG_VERBOSE("World::step_unpaced advances offline batch count=" +
                    std::to_string(count) + " world=" + std::to_string(id_));
    SIM_PROFILE_SCOPE("World.StepUnpaced");
    // Keep the browser pacing configuration untouched. Agent execution is an
    // offline batch operation, so run the same tick body without sleeping.
    for (std::uint64_t index = 0; index < count; ++index) {
        scheduler_.run(time_.state().fixed_time_step);
        update_world_transforms();
        time_.advance();
    }
    SIM_LOG_VERBOSE("World::step_unpaced completed world=" + std::to_string(id_) +
                    " simulation_time=" + std::to_string(time_.state().simulation_time));
}

void World::reset() {
    SIM_LOG_VERBOSE("World::reset restores the captured initial snapshot");
    if (!initial_state_) {
        throw Error(ErrorCode::InvalidState, "world has no initial state");
    }
    restore(*initial_state_);
    SIM_LOG_INFO("World", "reset completed world=" + std::to_string(id_));
    events_.publish({"SimulationReset", id_, kInvalidEntity, time_.state().simulation_time, {}});
}

WorldSnapshot World::snapshot() const {
    return {1, registry_.clone(), frames_, time_.state(), status_, random_};
}

void World::restore(const WorldSnapshot& state) {
    if (state.version != 1) {
        throw Error(ErrorCode::ValidationFailed, "unsupported world snapshot version");
    }
    registry_ = state.registry.clone();
    SIM_LOG_VERBOSE("World::restore clones entity state count=" +
                    std::to_string(registry_.entities().size()) + " version=" +
                    std::to_string(state.version));
    frames_ = state.frames;
    time_.restore(state.time);
    status_ = state.status;
    random_ = state.random_state;
    commands_.clear();
    events_.publish({"StateRestored", id_, kInvalidEntity, time_.state().simulation_time, {}});
}

void World::capture_initial_state() { initial_state_ = snapshot(); }

void World::execute_commands() {
    SIM_LOG_VERBOSE("World::execute_commands begins world=" + std::to_string(id_));
    auto ready = commands_.take_ready(time_.state().simulation_time);
    for (const auto& command : ready) {
        SIM_LOG_VERBOSE("World::execute_commands dispatches sequence=" +
                        std::to_string(command.sequence) + " target_time=" +
                        std::to_string(command.target_time));
        std::visit(
            [this](const auto& payload) {
                using Payload = std::decay_t<decltype(payload)>;
                if constexpr (std::is_same_v<Payload, CreateEntityCommand>) {
                    const EntityId created =
                        create_entity(payload.name, payload.parent.value_or(kInvalidEntity));
                    (void)created;
                } else if constexpr (std::is_same_v<Payload, DestroyEntityCommand>) {
                    destroy_entity(payload.entity);
                } else if constexpr (std::is_same_v<Payload, SetTransformCommand>) {
                    registry_.get<TransformComponent>(payload.entity).local = payload.transform;
                } else if constexpr (std::is_same_v<Payload, StartCommand>) {
                    start();
                } else if constexpr (std::is_same_v<Payload, PauseCommand>) {
                    pause();
                } else if constexpr (std::is_same_v<Payload, ResetCommand>) {
                    reset();
                }
            },
            command.payload);
    }
    SIM_LOG_VERBOSE("World::execute_commands completed count=" + std::to_string(ready.size()));
}

Simulator::Simulator(BackendConfig config) : config_(config) {
    SIM_LOG_VERBOSE("Simulator::Simulator validates requested backend combination");
    const auto diagnostics = BackendManager::validate(config_);
    const auto fatal = std::find_if(diagnostics.begin(), diagnostics.end(), [](const Diagnostic& item) {
        return item.severity == Severity::Fatal;
    });
    if (fatal != diagnostics.end()) {
        SIM_LOG_FATAL("Simulator", fatal->message);
        throw Error(ErrorCode::BackendCombinationInvalid, fatal->message);
    }
    for (const auto& diagnostic : diagnostics) {
        if (diagnostic.severity == Severity::Warning)
            SIM_LOG_WARNING("Simulator", diagnostic.message);
    }
}

World& Simulator::create_world(double fixed_time_step, std::uint64_t seed) {
    const WorldId id = next_world_++;
    SIM_LOG_VERBOSE("Simulator::create_world id=" + std::to_string(id) +
                    " fixed_time_step=" + std::to_string(fixed_time_step) +
                    " seed=" + std::to_string(seed));
    auto world = std::make_unique<World>(id, fixed_time_step, seed);
    auto [it, _] = worlds_.emplace(id, std::move(world));
    return *it->second;
}

bool Simulator::destroy_world(WorldId id) {
    const bool removed = worlds_.erase(id) != 0;
    SIM_LOG_VERBOSE("Simulator::destroy_world id=" + std::to_string(id) +
                    " removed=" + (removed ? "true" : "false"));
    return removed;
}

World& Simulator::world(WorldId id) {
    const auto it = worlds_.find(id);
    if (it == worlds_.end()) {
        throw Error(ErrorCode::NotFound, "world handle is invalid");
    }
    return *it->second;
}

const World& Simulator::world(WorldId id) const {
    const auto it = worlds_.find(id);
    if (it == worlds_.end()) {
        throw Error(ErrorCode::NotFound, "world handle is invalid");
    }
    return *it->second;
}

}  // namespace sim
