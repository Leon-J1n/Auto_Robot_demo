#pragma once

#include "sim/components.hpp"

#include <algorithm>
#include <memory>
#include <typeindex>
#include <unordered_map>
#include <unordered_set>

namespace sim {

class Registry {
private:
    struct IPool {
        virtual ~IPool() = default;
        virtual void erase(EntityId entity) = 0;
        [[nodiscard]] virtual std::unique_ptr<IPool> clone() const = 0;
    };

    template <typename Component>
    struct Pool final : IPool {
        std::unordered_map<EntityId, Component> values;
        void erase(EntityId entity) override { values.erase(entity); }
        [[nodiscard]] std::unique_ptr<IPool> clone() const override {
            return std::make_unique<Pool<Component>>(*this);
        }
    };

public:
    Registry() = default;
    Registry(const Registry& other) : alive_(other.alive_), next_entity_(other.next_entity_) {
        for (const auto& [type, pool] : other.pools_) {
            pools_.emplace(type, pool->clone());
        }
    }
    Registry& operator=(const Registry& other) {
        if (this == &other) {
            return *this;
        }
        Registry copy(other);
        *this = std::move(copy);
        return *this;
    }
    Registry(Registry&&) noexcept = default;
    Registry& operator=(Registry&&) noexcept = default;

    [[nodiscard]] EntityId create(std::string name = {}) {
        const EntityId entity = next_entity_++;
        return create_with_id(entity, std::move(name));
    }

    [[nodiscard]] EntityId create_with_id(EntityId entity, std::string name = {}) {
        if (entity == kInvalidEntity) {
            throw Error(ErrorCode::InvalidArgument, "entity handle zero is reserved");
        }
        if (alive_.contains(entity)) {
            throw Error(ErrorCode::AlreadyExists, "entity handle already exists");
        }
        alive_.insert(entity);
        next_entity_ = std::max(next_entity_, entity + 1);
        emplace<IdentityComponent>(entity, IdentityComponent{entity});
        emplace<NameComponent>(entity, NameComponent{std::move(name)});
        emplace<TransformComponent>(entity);
        emplace<HierarchyComponent>(entity);
        return entity;
    }

    bool destroy(EntityId entity) {
        if (alive_.erase(entity) == 0) {
            return false;
        }
        for (auto& [_, pool] : pools_) {
            pool->erase(entity);
        }
        return true;
    }

    [[nodiscard]] bool valid(EntityId entity) const noexcept { return alive_.contains(entity); }
    [[nodiscard]] std::size_t size() const noexcept { return alive_.size(); }

    template <typename Component, typename... Args>
    Component& emplace(EntityId entity, Args&&... args) {
        require_valid(entity);
        auto& values = pool<Component>().values;
        auto [it, inserted] = values.emplace(entity, Component{std::forward<Args>(args)...});
        if (!inserted) {
            throw Error(ErrorCode::AlreadyExists, "component already exists on entity");
        }
        return it->second;
    }

    template <typename Component>
    [[nodiscard]] bool has(EntityId entity) const {
        const auto* values = try_pool<Component>();
        return values != nullptr && values->values.contains(entity);
    }

    template <typename Component>
    Component& get(EntityId entity) {
        require_valid(entity);
        auto* values = try_pool<Component>();
        if (values == nullptr) {
            throw Error(ErrorCode::NotFound, "component not found");
        }
        const auto it = values->values.find(entity);
        if (it == values->values.end()) {
            throw Error(ErrorCode::NotFound, "component not found");
        }
        return it->second;
    }

    template <typename Component>
    const Component& get(EntityId entity) const {
        require_valid(entity);
        const auto* values = try_pool<Component>();
        if (values == nullptr) {
            throw Error(ErrorCode::NotFound, "component not found");
        }
        const auto it = values->values.find(entity);
        if (it == values->values.end()) {
            throw Error(ErrorCode::NotFound, "component not found");
        }
        return it->second;
    }

    template <typename Component>
    bool remove(EntityId entity) {
        require_valid(entity);
        auto* values = try_pool<Component>();
        return values != nullptr && values->values.erase(entity) != 0;
    }

    template <typename... Components>
    [[nodiscard]] std::vector<EntityId> view() const {
        std::vector<EntityId> result;
        result.reserve(alive_.size());
        for (const EntityId entity : alive_) {
            if ((has<Components>(entity) && ...)) {
                result.push_back(entity);
            }
        }
        std::sort(result.begin(), result.end());
        return result;
    }

    [[nodiscard]] std::vector<EntityId> entities() const {
        std::vector<EntityId> result(alive_.begin(), alive_.end());
        std::sort(result.begin(), result.end());
        return result;
    }

    [[nodiscard]] Registry clone() const { return *this; }

private:
    void require_valid(EntityId entity) const {
        if (!valid(entity)) {
            throw Error(ErrorCode::NotFound, "invalid entity handle");
        }
    }

    template <typename Component>
    Pool<Component>& pool() {
        const std::type_index type(typeid(Component));
        auto [it, inserted] = pools_.try_emplace(type);
        if (inserted) {
            it->second = std::make_unique<Pool<Component>>();
        }
        return *static_cast<Pool<Component>*>(it->second.get());
    }

    template <typename Component>
    Pool<Component>* try_pool() {
        const auto it = pools_.find(std::type_index(typeid(Component)));
        return it == pools_.end() ? nullptr : static_cast<Pool<Component>*>(it->second.get());
    }

    template <typename Component>
    const Pool<Component>* try_pool() const {
        const auto it = pools_.find(std::type_index(typeid(Component)));
        return it == pools_.end() ? nullptr : static_cast<const Pool<Component>*>(it->second.get());
    }

    std::unordered_set<EntityId> alive_;
    std::unordered_map<std::type_index, std::unique_ptr<IPool>> pools_;
    EntityId next_entity_{1};
};

class FrameTree {
public:
    struct Record { std::string frame; std::string parent; Transform parent_from_frame; };
    explicit FrameTree(std::string root = "world");

    void set_frame(std::string frame, std::string parent, Transform parent_from_frame);
    bool remove_frame(const std::string& frame);
    [[nodiscard]] bool contains(const std::string& frame) const;
    [[nodiscard]] Transform lookup(const std::string& source, const std::string& target) const;
    [[nodiscard]] const std::string& root() const noexcept { return root_; }
    [[nodiscard]] std::vector<Record> records() const;

private:
    struct FrameNode { std::string parent; Transform parent_from_frame; };
    [[nodiscard]] Transform root_from(const std::string& frame) const;
    [[nodiscard]] bool would_create_cycle(const std::string& frame, const std::string& parent) const;

    std::string root_;
    std::unordered_map<std::string, FrameNode> frames_;
};

}  // namespace sim
