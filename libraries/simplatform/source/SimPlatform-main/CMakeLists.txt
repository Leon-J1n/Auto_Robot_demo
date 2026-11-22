cmake_minimum_required(VERSION 3.22)

project(sim_platform VERSION 0.1.0 LANGUAGES C CXX)

option(SIM_BUILD_TESTS "Build unit and integration tests" ON)
option(SIM_BUILD_CLI "Build the simulator CLI" ON)
option(SIM_BUILD_C_API "Build the stable C API shared library" ON)
option(SIM_BUILD_ALGORITHMS "Build the C/Python algorithm integration library" ON)
option(SIM_BUILD_ALGORITHM_INTEGRATION_EXAMPLES "Build algorithm integration examples" ON)
option(SIM_BUILD_BENCHMARKS "Build first-stage performance baselines" OFF)
option(SIM_ENABLE_ASSIMP "Enable the Assimp asset importer" OFF)
option(SIM_ENABLE_URDFDOM "Enable the urdfdom robot importer" OFF)
option(SIM_ENABLE_MUJOCO "Enable the MuJoCo physics plugin" OFF)
option(SIM_ENABLE_BULLET3_PLUGIN "Build the Bullet3 physics backend plugin" OFF)
option(SIM_ENABLE_BULLET3_COLLISION_PLUGIN "Build the Bullet3 collision backend plugin" OFF)
option(SIM_ENABLE_COAL_COLLISION_PLUGIN "Build the Coal collision backend plugin" OFF)
option(SIM_ENABLE_IFPS_CPU_PLUGIN "Build the IFPS CPU collision backend plugin" ON)
option(SIM_ENABLE_IFPS_GPU_PLUGIN "Build the IFPS CUDA collision backend plugin" OFF)
option(SIM_ENABLE_FCL "Enable the FCL collision plugin" OFF)
option(SIM_ENABLE_VULKAN "Enable the Vulkan render plugin" OFF)
option(SIM_ENABLE_QT "Enable the Qt editor" OFF)
option(SIM_LOG_VERBOSE "Compile VERBOSE log calls" ON)
option(SIM_LOG_DEBUG "Compile DEBUG log calls" ON)
option(SIM_LOG_INFO "Compile INFO log calls" ON)
option(SIM_LOG_WARNING "Compile WARNING log calls" ON)
option(SIM_LOG_ERROR "Compile ERROR log calls" ON)
option(SIM_LOG_FATAL "Compile FATAL log calls" ON)
option(SIM_LOG_PERF "Compile scoped performance logging" ON)
set(BQLOG_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../deps/BqLog-main" CACHE PATH "BqLog source checkout")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(SIM_MUJOCO_ROOT "" CACHE PATH "MuJoCo root containing include/mujoco and mujoco.dll")

if(NOT EXISTS "${BQLOG_ROOT}/src/CMakeLists.txt")
    message(FATAL_ERROR "BqLog was not found at BQLOG_ROOT=${BQLOG_ROOT}")
endif()

if(WIN32)
    set(_sim_default_platform "win64")
elseif(APPLE)
    set(_sim_default_platform "mac")
else()
    set(_sim_default_platform "linux")
endif()
set(TARGET_PLATFORM "${_sim_default_platform}" CACHE STRING "BqLog target platform")
set(_sim_bq_src "${BQLOG_ROOT}/src")
set(_sim_bq_common "${_sim_bq_src}/bq_common")
file(GLOB_RECURSE SIM_BQLOG_SOURCES CONFIGURE_DEPENDS
    "${_sim_bq_src}/bq_log/*.cpp" "${_sim_bq_common}/*.cpp")

if(SIM_BUILD_ALGORITHMS)
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
    add_library(sim_algorithms_c SHARED
        src/sim_algorithms/test_algorithms.cpp
        src/sim_algorithms/python_bridge.cpp
        src/sim_algorithms/registry.cpp
    )
    add_library(sim::algorithms ALIAS sim_algorithms_c)
    target_include_directories(sim_algorithms_c
        PUBLIC
            $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
            $<INSTALL_INTERFACE:include>
    )
    target_compile_definitions(sim_algorithms_c PRIVATE
        SIM_ALGORITHMS_EXPORTS=1
        SIM_ALGORITHMS_PYTHON_EXECUTABLE="${Python3_EXECUTABLE}"
        SIM_ALGORITHMS_BRIDGE_SCRIPT="${CMAKE_CURRENT_SOURCE_DIR}/python/sim_algorithms/_bridge.py")
    set_target_properties(sim_algorithms_c PROPERTIES OUTPUT_NAME sim_algorithms_c PREFIX "")
    if(MINGW)
        target_link_options(sim_algorithms_c PRIVATE -static-libgcc -static-libstdc++ -static)
    endif()
endif()

if(SIM_BUILD_ALGORITHM_INTEGRATION_EXAMPLES)
    if(NOT SIM_BUILD_ALGORITHMS)
        message(FATAL_ERROR "SIM_BUILD_ALGORITHM_INTEGRATION_EXAMPLES requires SIM_BUILD_ALGORITHMS")
    endif()
    add_subdirectory(example/test_alg_interg)
endif()

add_library(sim_core
    ${SIM_BQLOG_SOURCES}
    src/core.cpp
    src/assets.cpp
    src/logging.cpp
    src/robot.cpp
    src/sensors.cpp
    src/plugin.cpp
    src/foundation.cpp
    src/state_io.cpp
    src/ifps.cpp
)
set_target_properties(sim_core PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(sim_core PRIVATE "${BQLOG_ROOT}/include" "${_sim_bq_src}")
if(WIN32)
    target_link_libraries(sim_core PRIVATE dbghelp)
elseif(UNIX)
    target_link_libraries(sim_core PRIVATE pthread)
endif()
if(MSVC)
    target_compile_options(sim_core PRIVATE /EHsc /utf-8 /Zc:__cplusplus)
    set_source_files_properties(${SIM_BQLOG_SOURCES} PROPERTIES COMPILE_OPTIONS "/W0")
elseif(MINGW)
    set_source_files_properties(${SIM_BQLOG_SOURCES} PROPERTIES COMPILE_OPTIONS "-w")
endif()
if(CMAKE_DL_LIBS)
    target_link_libraries(sim_core PRIVATE ${CMAKE_DL_LIBS})
endif()
add_library(sim::core ALIAS sim_core)
target_include_directories(sim_core
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include>
)
target_compile_definitions(sim_core PUBLIC SIM_PLUGIN_API_VERSION=2)
foreach(level VERBOSE DEBUG INFO WARNING ERROR FATAL PERF)
    if(SIM_LOG_${level})
        target_compile_definitions(sim_core PUBLIC SIM_LOG_ENABLE_${level}=1)
    else()
        target_compile_definitions(sim_core PUBLIC SIM_LOG_ENABLE_${level}=0)
    endif()
endforeach()

if(MSVC)
    target_compile_options(sim_core PRIVATE /W4 /permissive-)
else()
    target_compile_options(sim_core PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
endif()

if(SIM_BUILD_CLI)
    add_executable(simulator src/cli.cpp)
    target_link_libraries(simulator PRIVATE sim::core)
endif()

if(SIM_BUILD_C_API)
    add_library(sim_c_api SHARED src/c_api.cpp)
    target_link_libraries(sim_c_api PRIVATE sim::core)
    target_compile_definitions(sim_c_api PRIVATE SIM_C_API_EXPORTS=1)
    set_target_properties(sim_c_api PROPERTIES OUTPUT_NAME sim_platform_c PREFIX "")
endif()

if(SIM_ENABLE_ASSIMP)
    find_package(assimp CONFIG REQUIRED)
    target_link_libraries(sim_core PRIVATE assimp::assimp)
    target_compile_definitions(sim_core PUBLIC SIM_HAS_ASSIMP=1)
endif()

if(SIM_ENABLE_URDFDOM)
    find_package(urdfdom CONFIG REQUIRED)
    target_link_libraries(sim_core PRIVATE urdfdom::urdf_parser)
    target_compile_definitions(sim_core PUBLIC SIM_HAS_URDFDOM=1)
endif()

if(SIM_ENABLE_MUJOCO)
    find_path(SIM_MUJOCO_INCLUDE_DIR mujoco/mujoco.h HINTS "${SIM_MUJOCO_ROOT}/include" REQUIRED)
    if(WIN32)
        find_file(SIM_MUJOCO_LIBRARY mujoco.dll HINTS "${SIM_MUJOCO_ROOT}" REQUIRED)
        if(MINGW)
            find_program(SIM_DLLTOOL dlltool REQUIRED)
            execute_process(COMMAND "${CMAKE_OBJDUMP}" -p "${SIM_MUJOCO_LIBRARY}"
                            OUTPUT_VARIABLE SIM_MUJOCO_EXPORTS COMMAND_ERROR_IS_FATAL ANY)
            string(REGEX MATCHALL "\n[\t ]+\\[[ ]*[0-9]+\\][\t ]+mj[A-Za-z0-9_]+"
                   SIM_MUJOCO_SYMBOL_LINES "${SIM_MUJOCO_EXPORTS}")
            set(SIM_MUJOCO_DEF "LIBRARY mujoco.dll\nEXPORTS\n")
            foreach(SIM_SYMBOL_LINE IN LISTS SIM_MUJOCO_SYMBOL_LINES)
                string(REGEX REPLACE ".*[\t ](mj[A-Za-z0-9_]+)$" "\\1" SIM_SYMBOL "${SIM_SYMBOL_LINE}")
                string(APPEND SIM_MUJOCO_DEF "${SIM_SYMBOL}\n")
            endforeach()
            set(SIM_MUJOCO_DEF_PATH "${CMAKE_CURRENT_BINARY_DIR}/mujoco.def")
            set(SIM_MUJOCO_IMPORT_LIBRARY "${CMAKE_CURRENT_BINARY_DIR}/libmujoco.a")
            file(WRITE "${SIM_MUJOCO_DEF_PATH}" "${SIM_MUJOCO_DEF}")
            execute_process(COMMAND "${SIM_DLLTOOL}" -d "${SIM_MUJOCO_DEF_PATH}"
                            -D mujoco.dll -l "${SIM_MUJOCO_IMPORT_LIBRARY}"
                            COMMAND_ERROR_IS_FATAL ANY)
        elseif(MSVC)
            get_filename_component(SIM_MSVC_BIN_DIR "${CMAKE_CXX_COMPILER}" DIRECTORY)
            find_program(SIM_DUMPBIN dumpbin HINTS "${SIM_MSVC_BIN_DIR}" REQUIRED)
            execute_process(COMMAND "${SIM_DUMPBIN}" /exports "${SIM_MUJOCO_LIBRARY}"
                            OUTPUT_VARIABLE SIM_MUJOCO_EXPORTS COMMAND_ERROR_IS_FATAL ANY)
            string(REGEX MATCHALL "\n[	 ]+[0-9]+[	 ]+[0-9A-F]+[	 ]+[0-9A-F]+[	 ]+mj[A-Za-z0-9_]+"
                   SIM_MUJOCO_SYMBOL_LINES "${SIM_MUJOCO_EXPORTS}")
            set(SIM_MUJOCO_DEF "LIBRARY mujoco.dll\nEXPORTS\n")
            foreach(SIM_SYMBOL_LINE IN LISTS SIM_MUJOCO_SYMBOL_LINES)
                string(REGEX REPLACE ".*[	 ](mj[A-Za-z0-9_]+)$" "\\1" SIM_SYMBOL "${SIM_SYMBOL_LINE}")
                string(APPEND SIM_MUJOCO_DEF "${SIM_SYMBOL}\n")
            endforeach()
            set(SIM_MUJOCO_DEF_PATH "${CMAKE_CURRENT_BINARY_DIR}/mujoco.def")
            set(SIM_MUJOCO_IMPORT_LIBRARY "${CMAKE_CURRENT_BINARY_DIR}/mujoco.lib")
            file(WRITE "${SIM_MUJOCO_DEF_PATH}" "${SIM_MUJOCO_DEF}")
            execute_process(COMMAND "${CMAKE_AR}" /nologo /def:${SIM_MUJOCO_DEF_PATH}
                                    /out:${SIM_MUJOCO_IMPORT_LIBRARY} /machine:x64
                            COMMAND_ERROR_IS_FATAL ANY)
        else()
            message(FATAL_ERROR "unsupported Windows compiler for MuJoCo import library generation")
        endif()
    else()
        find_library(SIM_MUJOCO_IMPORT_LIBRARY mujoco HINTS "${SIM_MUJOCO_ROOT}/lib" REQUIRED)
    endif()
    target_sources(sim_core PRIVATE src/mujoco_backend.cpp)
    target_include_directories(sim_core PRIVATE "${SIM_MUJOCO_INCLUDE_DIR}")
    target_link_libraries(sim_core PUBLIC "${SIM_MUJOCO_IMPORT_LIBRARY}")
    target_compile_definitions(sim_core PUBLIC SIM_HAS_MUJOCO=1)
    if(WIN32)
        add_custom_command(TARGET sim_core POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different "${SIM_MUJOCO_LIBRARY}"
                    "$<TARGET_FILE_DIR:sim_core>/mujoco.dll")
    endif()
endif()

if(SIM_ENABLE_BULLET3_PLUGIN OR SIM_ENABLE_BULLET3_COLLISION_PLUGIN)
    find_package(Bullet CONFIG REQUIRED)
    set(_sim_bullet_include_dirs "")
    foreach(_directory IN LISTS BULLET_INCLUDE_DIRS)
        if(IS_ABSOLUTE "${_directory}")
            list(APPEND _sim_bullet_include_dirs "${_directory}")
        else()
            list(APPEND _sim_bullet_include_dirs "${BULLET_ROOT_DIR}/${_directory}")
        endif()
    endforeach()
    set(_sim_bullet_library_dirs "")
    foreach(_directory IN LISTS BULLET_LIBRARY_DIRS)
        if(IS_ABSOLUTE "${_directory}")
            list(APPEND _sim_bullet_library_dirs "${_directory}")
        else()
            list(APPEND _sim_bullet_library_dirs "${BULLET_ROOT_DIR}/${_directory}")
        endif()
    endforeach()
endif()

if(SIM_ENABLE_BULLET3_PLUGIN)
    add_library(sim_physics_bullet SHARED plugins/bullet_backend.cpp)
    target_link_libraries(sim_physics_bullet PRIVATE
        sim::core BulletDynamics BulletCollision LinearMath)
    target_compile_definitions(sim_physics_bullet PRIVATE SIM_PLUGIN_EXPORTS=1)
    target_include_directories(sim_physics_bullet PRIVATE ${_sim_bullet_include_dirs})
    target_link_directories(sim_physics_bullet PRIVATE ${_sim_bullet_library_dirs})
    set_target_properties(sim_physics_bullet PROPERTIES
        PREFIX ""
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins"
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        LIBRARY_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins")
    configure_file(plugins/bullet.simplugin.json.in
                   "${CMAKE_CURRENT_BINARY_DIR}/bullet.simplugin.json" @ONLY)
    add_custom_command(TARGET sim_physics_bullet POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${CMAKE_CURRENT_BINARY_DIR}/bullet.simplugin.json"
                "$<TARGET_FILE_DIR:sim_physics_bullet>/bullet.simplugin.json")
    target_compile_definitions(sim_core PUBLIC
        SIM_HAS_BULLET_PLUGIN=1
        SIM_DEFAULT_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_physics_bullet>")
endif()

if(SIM_ENABLE_BULLET3_COLLISION_PLUGIN)
    add_library(sim_collision_bullet SHARED plugins/bullet_collision_backend.cpp)
    target_link_libraries(sim_collision_bullet PRIVATE sim::core BulletCollision LinearMath)
    target_compile_definitions(sim_collision_bullet PRIVATE SIM_PLUGIN_EXPORTS=1)
    target_include_directories(sim_collision_bullet PRIVATE ${_sim_bullet_include_dirs})
    target_link_directories(sim_collision_bullet PRIVATE ${_sim_bullet_library_dirs})
    set_target_properties(sim_collision_bullet PROPERTIES
        PREFIX ""
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins"
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        LIBRARY_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins")
    configure_file(plugins/bullet_collision.simplugin.json.in
                   "${CMAKE_CURRENT_BINARY_DIR}/bullet_collision.simplugin.json" @ONLY)
    add_custom_command(TARGET sim_collision_bullet POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${CMAKE_CURRENT_BINARY_DIR}/bullet_collision.simplugin.json"
                "$<TARGET_FILE_DIR:sim_collision_bullet>/bullet_collision.simplugin.json")
    target_compile_definitions(sim_core PUBLIC
        SIM_HAS_BULLET_COLLISION_PLUGIN=1
        SIM_DEFAULT_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_collision_bullet>")
endif()

if(SIM_ENABLE_COAL_COLLISION_PLUGIN)
    find_package(coal 3.0 CONFIG REQUIRED)
    add_library(sim_collision_coal SHARED plugins/coal_collision_backend.cpp)
    target_link_libraries(sim_collision_coal PRIVATE sim::core coal::coal)
    target_compile_definitions(sim_collision_coal PRIVATE SIM_PLUGIN_EXPORTS=1)
    set_target_properties(sim_collision_coal PROPERTIES
        PREFIX ""
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins"
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        LIBRARY_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins")
    configure_file(plugins/coal_collision.simplugin.json.in
                   "${CMAKE_CURRENT_BINARY_DIR}/coal_collision.simplugin.json" @ONLY)
    add_custom_command(TARGET sim_collision_coal POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${CMAKE_CURRENT_BINARY_DIR}/coal_collision.simplugin.json"
                "$<TARGET_FILE_DIR:sim_collision_coal>/coal_collision.simplugin.json")
    if(WIN32)
        get_filename_component(SIM_COAL_PREFIX "${coal_DIR}/../../.." ABSOLUTE)
        file(GLOB SIM_COAL_PLUGIN_RUNTIME_DLLS "${SIM_COAL_PREFIX}/bin/*.dll")
        if(SIM_COAL_PLUGIN_RUNTIME_DLLS)
            add_custom_command(TARGET sim_collision_coal POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                        ${SIM_COAL_PLUGIN_RUNTIME_DLLS}
                        "$<TARGET_FILE_DIR:sim_collision_coal>"
                COMMAND_EXPAND_LISTS)
        endif()
    endif()
    target_compile_definitions(sim_core PUBLIC
        SIM_HAS_COAL_COLLISION_PLUGIN=1
        SIM_DEFAULT_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_collision_coal>")
endif()

if(SIM_ENABLE_IFPS_CPU_PLUGIN)
    add_library(sim_collision_ifps_cpu SHARED plugins/ifps_cpu_backend.cpp)
    target_link_libraries(sim_collision_ifps_cpu PRIVATE sim::core)
    target_compile_definitions(sim_collision_ifps_cpu PRIVATE SIM_PLUGIN_EXPORTS=1)
    set_target_properties(sim_collision_ifps_cpu PROPERTIES
        PREFIX ""
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins"
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        LIBRARY_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins")
    configure_file(plugins/ifps_cpu.simplugin.json.in
                   "${CMAKE_CURRENT_BINARY_DIR}/ifps_cpu.simplugin.json" @ONLY)
    add_custom_command(TARGET sim_collision_ifps_cpu POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${CMAKE_CURRENT_BINARY_DIR}/ifps_cpu.simplugin.json"
                "$<TARGET_FILE_DIR:sim_collision_ifps_cpu>/ifps_cpu.simplugin.json")
    target_compile_definitions(sim_core PUBLIC
        SIM_HAS_IFPS_CPU_PLUGIN=1
        SIM_DEFAULT_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_collision_ifps_cpu>")
endif()

if(SIM_ENABLE_IFPS_GPU_PLUGIN)
    find_package(CUDAToolkit REQUIRED)
    add_library(sim_collision_ifps_gpu SHARED plugins/ifps_gpu_backend.cpp)
    target_link_libraries(sim_collision_ifps_gpu PRIVATE sim::core CUDA::cuda_driver CUDA::nvrtc)
    target_include_directories(sim_collision_ifps_gpu PRIVATE plugins ${CUDAToolkit_INCLUDE_DIRS})
    target_compile_definitions(sim_collision_ifps_gpu PRIVATE SIM_PLUGIN_EXPORTS=1)
    set_target_properties(sim_collision_ifps_gpu PROPERTIES
        PREFIX ""
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins"
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
        LIBRARY_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/plugins")
    configure_file(plugins/ifps_gpu.simplugin.json.in
                   "${CMAKE_CURRENT_BINARY_DIR}/ifps_gpu.simplugin.json" @ONLY)
    add_custom_command(TARGET sim_collision_ifps_gpu POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${CMAKE_CURRENT_BINARY_DIR}/ifps_gpu.simplugin.json"
                "$<TARGET_FILE_DIR:sim_collision_ifps_gpu>/ifps_gpu.simplugin.json")
    target_compile_definitions(sim_core PUBLIC SIM_HAS_IFPS_GPU_PLUGIN=1)
endif()

if(SIM_ENABLE_FCL)
    find_package(fcl CONFIG REQUIRED)
    target_sources(sim_core PRIVATE src/fcl_backend.cpp)
    target_link_libraries(sim_core PRIVATE fcl)
    target_compile_definitions(sim_core PUBLIC SIM_HAS_FCL=1)
endif()

if(SIM_ENABLE_VULKAN)
    find_package(Vulkan REQUIRED COMPONENTS glslc)
    set(SIM_VERTEX_SHADER "${CMAKE_CURRENT_BINARY_DIR}/shaders/basic.vert.spv")
    set(SIM_FRAGMENT_SHADER "${CMAKE_CURRENT_BINARY_DIR}/shaders/basic.frag.spv")
    add_custom_command(
        OUTPUT "${SIM_VERTEX_SHADER}"
        COMMAND "${CMAKE_COMMAND}" -E make_directory "${CMAKE_CURRENT_BINARY_DIR}/shaders"
        COMMAND "${Vulkan_GLSLC_EXECUTABLE}" -fshader-stage=vert
                "${CMAKE_CURRENT_SOURCE_DIR}/shaders/basic.vert" -o "${SIM_VERTEX_SHADER}"
        DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/shaders/basic.vert"
        VERBATIM)
    add_custom_command(
        OUTPUT "${SIM_FRAGMENT_SHADER}"
        COMMAND "${CMAKE_COMMAND}" -E make_directory "${CMAKE_CURRENT_BINARY_DIR}/shaders"
        COMMAND "${Vulkan_GLSLC_EXECUTABLE}" -fshader-stage=frag
                "${CMAKE_CURRENT_SOURCE_DIR}/shaders/basic.frag" -o "${SIM_FRAGMENT_SHADER}"
        DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/shaders/basic.frag"
        VERBATIM)
    add_custom_target(sim_vulkan_shaders DEPENDS "${SIM_VERTEX_SHADER}" "${SIM_FRAGMENT_SHADER}")
    add_dependencies(sim_core sim_vulkan_shaders)
    target_sources(sim_core PRIVATE src/vulkan_backend.cpp)
    target_link_libraries(sim_core PRIVATE Vulkan::Vulkan)
    target_compile_definitions(sim_core
        PUBLIC SIM_HAS_VULKAN=1
        PRIVATE SIM_VULKAN_VERTEX_SHADER="${SIM_VERTEX_SHADER}"
                SIM_VULKAN_FRAGMENT_SHADER="${SIM_FRAGMENT_SHADER}")
endif()

if(SIM_ENABLE_QT)
    find_package(Qt6 COMPONENTS Widgets Network QUIET)
    if(Qt6_FOUND)
        set(SIM_QT_WIDGETS Qt6::Widgets)
        set(SIM_QT_NETWORK Qt6::Network)
    else()
        find_package(Qt5 5.15 COMPONENTS Widgets Network REQUIRED)
        set(SIM_QT_WIDGETS Qt5::Widgets)
        set(SIM_QT_NETWORK Qt5::Network)
    endif()
    add_executable(sim_editor src/editor.cpp)
    target_link_libraries(sim_editor PRIVATE sim::core ${SIM_QT_WIDGETS} ${SIM_QT_NETWORK})
    set_target_properties(sim_editor PROPERTIES WIN32_EXECUTABLE ON)
    if(WIN32)
        find_program(SIM_WINDEPLOYQT windeployqt HINTS "${Qt5_DIR}/../../../bin")
        if(SIM_WINDEPLOYQT)
            add_custom_command(TARGET sim_editor POST_BUILD
                COMMAND "${SIM_WINDEPLOYQT}" --no-translations --release
                        "$<TARGET_FILE:sim_editor>"
                VERBATIM)
        endif()
    endif()
endif()

include(CTest)
if(BUILD_TESTING AND SIM_BUILD_TESTS)
    add_executable(sim_logging_compile_tests tests/test_logging_compile.cpp)
    target_link_libraries(sim_logging_compile_tests PRIVATE sim::core)
    add_test(NAME sim_logging_compile_tests COMMAND sim_logging_compile_tests)
    add_library(sim_test_plugin SHARED tests/sample_plugin.cpp)
    target_include_directories(sim_test_plugin PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/include")
    target_compile_definitions(sim_test_plugin PRIVATE SIM_PLUGIN_EXPORTS=1)
    set_target_properties(sim_test_plugin PROPERTIES PREFIX "")
    add_executable(sim_core_tests tests/test_core.cpp)
    target_link_libraries(sim_core_tests PRIVATE sim::core)
    target_compile_definitions(sim_core_tests PRIVATE
        SIM_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data"
        SIM_TEST_PLUGIN_PATH="$<TARGET_FILE:sim_test_plugin>")
    add_dependencies(sim_core_tests sim_test_plugin)
    if(SIM_ENABLE_BULLET3_PLUGIN)
        add_dependencies(sim_core_tests sim_physics_bullet)
        target_compile_definitions(sim_core_tests PRIVATE
            SIM_TEST_BULLET_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_physics_bullet>")
    endif()
    if(SIM_ENABLE_BULLET3_COLLISION_PLUGIN)
        add_dependencies(sim_core_tests sim_collision_bullet)
        target_compile_definitions(sim_core_tests PRIVATE
            SIM_TEST_BULLET_COLLISION_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_collision_bullet>")
    endif()
    if(SIM_ENABLE_COAL_COLLISION_PLUGIN)
        add_dependencies(sim_core_tests sim_collision_coal)
        target_compile_definitions(sim_core_tests PRIVATE
            SIM_TEST_COAL_COLLISION_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_collision_coal>")
    endif()
    if(SIM_ENABLE_IFPS_CPU_PLUGIN)
        add_dependencies(sim_core_tests sim_collision_ifps_cpu)
        target_compile_definitions(sim_core_tests PRIVATE
            SIM_TEST_IFPS_CPU_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_collision_ifps_cpu>")
    endif()
    if(SIM_ENABLE_IFPS_GPU_PLUGIN)
        add_dependencies(sim_core_tests sim_collision_ifps_gpu)
        target_compile_definitions(sim_core_tests PRIVATE
            SIM_TEST_IFPS_GPU_PLUGIN_DIR="$<TARGET_FILE_DIR:sim_collision_ifps_gpu>")
    endif()
    add_test(NAME sim_core_tests COMMAND sim_core_tests)
    if(SIM_BUILD_ALGORITHM_INTEGRATION_EXAMPLES)
        add_test(NAME sim_algorithm_integration_c COMMAND test_algorithm_integration_c)
    endif()
    if(SIM_BUILD_CLI)
        add_test(NAME sim_cli_validate
                 COMMAND simulator validate "${CMAKE_CURRENT_SOURCE_DIR}/tests/data/triangle.obj")
        add_test(NAME sim_cli_bake
                 COMMAND simulator bake "${CMAKE_CURRENT_SOURCE_DIR}/tests/data/triangle.obj")
        add_test(NAME sim_cli_backend_check COMMAND simulator backends check)
    endif()
endif()

if(SIM_BUILD_BENCHMARKS)
    add_executable(sim_benchmarks tests/benchmark.cpp)
    target_link_libraries(sim_benchmarks PRIVATE sim::core)
endif()

# Make build-tree binaries runnable without relying on a globally modified PATH.
# This is especially important for the project-local dependency installation used
# by the Windows preset, where vcpkg's toolchain is intentionally not required.
if(WIN32)
    set(SIM_PROJECT_RUNTIME_DLLS)
    foreach(SIM_DEPENDENCY_PREFIX IN LISTS CMAKE_PREFIX_PATH)
        file(TO_CMAKE_PATH "${SIM_DEPENDENCY_PREFIX}" SIM_NORMALIZED_PREFIX)
        string(FIND "${SIM_NORMALIZED_PREFIX}" "${CMAKE_CURRENT_SOURCE_DIR}/third_party/" SIM_LOCAL_PREFIX)
        if(SIM_LOCAL_PREFIX EQUAL 0)
            file(GLOB SIM_PREFIX_RUNTIME_DLLS "${SIM_DEPENDENCY_PREFIX}/bin/*.dll")
            list(APPEND SIM_PROJECT_RUNTIME_DLLS ${SIM_PREFIX_RUNTIME_DLLS})
        endif()
    endforeach()
    if(SIM_PROJECT_RUNTIME_DLLS)
        add_custom_command(TARGET sim_core POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different ${SIM_PROJECT_RUNTIME_DLLS}
                    "$<TARGET_FILE_DIR:sim_core>"
            COMMAND_EXPAND_LISTS)
    endif()
endif()

include(GNUInstallDirs)
install(TARGETS sim_core
        EXPORT sim-platform-targets
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
        LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
if(SIM_BUILD_ALGORITHMS)
    install(TARGETS sim_algorithms_c
            EXPORT sim-platform-targets
            RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
            LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
            ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
endif()
if(SIM_BUILD_CLI)
    install(TARGETS simulator RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()
if(SIM_BUILD_C_API)
    install(TARGETS sim_c_api
            RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
            LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
            ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
endif()
install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(EXPORT sim-platform-targets NAMESPACE sim:: DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/sim-platform)
if(SIM_ENABLE_BULLET3_PLUGIN)
    install(TARGETS sim_physics_bullet
            RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins
            LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}/sim-platform/plugins)
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/bullet.simplugin.json"
            DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins)
endif()
if(SIM_ENABLE_BULLET3_COLLISION_PLUGIN)
    install(TARGETS sim_collision_bullet
            RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins
            LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}/sim-platform/plugins)
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/bullet_collision.simplugin.json"
            DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins)
endif()
if(SIM_ENABLE_COAL_COLLISION_PLUGIN)
    install(TARGETS sim_collision_coal
            RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins
            LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}/sim-platform/plugins)
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/coal_collision.simplugin.json"
            DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins)
endif()
if(SIM_ENABLE_IFPS_CPU_PLUGIN)
    install(TARGETS sim_collision_ifps_cpu
            RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins
            LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}/sim-platform/plugins)
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/ifps_cpu.simplugin.json"
            DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins)
endif()
if(SIM_ENABLE_IFPS_GPU_PLUGIN)
    install(TARGETS sim_collision_ifps_gpu
            RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins
            LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}/sim-platform/plugins)
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/ifps_gpu.simplugin.json"
            DESTINATION ${CMAKE_INSTALL_BINDIR}/plugins)
endif()
