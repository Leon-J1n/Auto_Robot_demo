#!/usr/bin/env bash
set -euo pipefail

sim_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
sim_build="$sim_root/build/linux"
sim_jobs="${SIM_BUILD_JOBS:-4}"
sim_bqlog="${BQLOG_ROOT:-$sim_root/deps/BqLog-main}"
if [[ ! -f "$sim_bqlog/src/CMakeLists.txt" ]]; then
    echo "BqLog source missing: $sim_bqlog (set BQLOG_ROOT)" >&2
    exit 1
fi
cmake -S "$sim_root/source/SimPlatform-main" -B "$sim_build" \
    -DCMAKE_BUILD_TYPE=Release -DBQLOG_ROOT="$sim_bqlog" -DTARGET_PLATFORM=linux \
    -DSIM_ENABLE_FCL=ON -DSIM_ENABLE_ASSIMP=ON -DSIM_ENABLE_URDFDOM=ON \
    -DSIM_BUILD_C_API=ON -DSIM_ENABLE_MUJOCO=OFF -DSIM_ENABLE_IFPS_CPU_PLUGIN=OFF \
    -DSIM_BUILD_ALGORITHMS=OFF -DSIM_BUILD_ALGORITHM_INTEGRATION_EXAMPLES=OFF \
    -DSIM_BUILD_CLI=OFF -DSIM_BUILD_TESTS=ON "$@"
cmake --build "$sim_build" --parallel "$sim_jobs"
ctest --test-dir "$sim_build" --output-on-failure
mkdir -p "$sim_root/bin"
# The upstream CMake target suppresses the platform's usual "lib" prefix.
install -m 755 "$sim_build/sim_platform_c.so" "$sim_root/bin/libsim_platform_c.so"
echo "Library: $sim_root/bin/libsim_platform_c.so"
