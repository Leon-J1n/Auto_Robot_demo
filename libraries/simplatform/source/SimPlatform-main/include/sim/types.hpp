#pragma once

#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace sim {

using EntityId = std::uint64_t;
using WorldId = std::uint64_t;
using StateId = std::uint64_t;
inline constexpr EntityId kInvalidEntity = 0;

enum class ErrorCode {
    InvalidArgument,
    NotFound,
    AlreadyExists,
    InvalidState,
    Unsupported,
    BackendUnavailable,
    BackendCombinationInvalid,
    IoError,
    ValidationFailed,
};

class Error final : public std::runtime_error {
public:
    Error(ErrorCode code, std::string message)
        : std::runtime_error(std::move(message)), code_(code) {}

    [[nodiscard]] ErrorCode code() const noexcept { return code_; }

private:
    ErrorCode code_;
};

struct Vec3 {
    double x{0.0};
    double y{0.0};
    double z{0.0};

    [[nodiscard]] constexpr Vec3 operator+(const Vec3& rhs) const noexcept {
        return {x + rhs.x, y + rhs.y, z + rhs.z};
    }
    [[nodiscard]] constexpr Vec3 operator-(const Vec3& rhs) const noexcept {
        return {x - rhs.x, y - rhs.y, z - rhs.z};
    }
    [[nodiscard]] constexpr Vec3 operator*(double scalar) const noexcept {
        return {x * scalar, y * scalar, z * scalar};
    }
};

struct Quaternion {
    double w{1.0};
    double x{0.0};
    double y{0.0};
    double z{0.0};

    [[nodiscard]] Quaternion normalized() const;
    [[nodiscard]] Quaternion conjugate() const noexcept { return {w, -x, -y, -z}; }
    [[nodiscard]] Quaternion operator*(const Quaternion& rhs) const noexcept;
    [[nodiscard]] Vec3 rotate(const Vec3& value) const;
};

struct Transform {
    Vec3 translation{};
    Quaternion rotation{};
    Vec3 scale{1.0, 1.0, 1.0};

    [[nodiscard]] static Transform Identity() noexcept { return {}; }
    [[nodiscard]] Transform inverse() const;
    [[nodiscard]] Transform operator*(const Transform& child) const;
};

[[nodiscard]] bool NearlyEqual(double lhs, double rhs, double epsilon = 1e-9) noexcept;
[[nodiscard]] bool NearlyEqual(const Vec3& lhs, const Vec3& rhs, double epsilon = 1e-9) noexcept;

enum class Severity { Info, Warning, Fatal };

struct Diagnostic {
    Severity severity{Severity::Info};
    std::string code;
    std::string message;
};

}  // namespace sim
