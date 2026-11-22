#pragma once

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#  if defined(SIM_C_API_EXPORTS)
#    define SIM_C_API __declspec(dllexport)
#  else
#    define SIM_C_API __declspec(dllimport)
#  endif
#else
#  define SIM_C_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct SimHandle SimHandle;
typedef uint64_t SimWorldHandle;
typedef uint64_t SimEntityHandle;
typedef uint64_t SimStateHandle;

typedef enum SimPhysicsBackend {
    SIM_PHYSICS_NONE = 0,
    SIM_PHYSICS_MUJOCO = 1,
    SIM_PHYSICS_BULLET3 = 2
} SimPhysicsBackend;

typedef enum SimCollisionBackend {
    SIM_COLLISION_FCL = 0,
    SIM_COLLISION_IFPS_CPU = 1,
    SIM_COLLISION_IFPS_GPU = 2,
    SIM_COLLISION_BULLET3 = 3,
    SIM_COLLISION_COAL = 4
} SimCollisionBackend;

typedef enum SimPrimitiveShape {
    SIM_PRIMITIVE_BOX = 0,
    SIM_PRIMITIVE_SPHERE = 1,
    SIM_PRIMITIVE_CAPSULE = 2,
    SIM_PRIMITIVE_CYLINDER = 3
} SimPrimitiveShape;

typedef enum SimLightType {
    SIM_LIGHT_POINT = 0,
    SIM_LIGHT_SPOT = 1,
    SIM_LIGHT_DIRECTIONAL = 2,
    SIM_LIGHT_AMBIENT = 3
} SimLightType;

typedef struct SimLightProperties {
    SimLightType type;
    float color_rgb[3];
    float intensity;
    float range;
    float decay;
    float inner_cone_radians;
    float outer_cone_radians;
    float angular_diameter_radians;
    int enabled;
} SimLightProperties;

typedef struct SimConfig {
    const char* cache_directory_utf8;
    SimPhysicsBackend physics_backend;
    SimCollisionBackend collision_backend;
} SimConfig;

typedef struct SimTransform {
    double translation[3];
    double rotation_wxyz[4];
    double scale[3];
} SimTransform;

/* What a joint write does with a target outside the joint's limits. Iterative solvers overshoot
   on intermediate steps, so they want CLAMP or IGNORE rather than the default rejection. */
typedef enum SimJointLimitPolicy {
    SIM_JOINT_LIMIT_ENFORCE = 0,
    SIM_JOINT_LIMIT_CLAMP = 1,
    SIM_JOINT_LIMIT_IGNORE = 2
} SimJointLimitPolicy;

typedef enum SimJointType {
    SIM_JOINT_FIXED = 0,
    SIM_JOINT_REVOLUTE = 1,
    SIM_JOINT_CONTINUOUS = 2,
    SIM_JOINT_PRISMATIC = 3,
    SIM_JOINT_FLOATING = 4,
    SIM_JOINT_PLANAR = 5
} SimJointType;

typedef struct SimJointInfo {
    SimJointType type;
    double axis[3];        /* Joint-frame rotation or translation axis. */
    double lower_limit;    /* -inf / +inf for a continuous joint. */
    double upper_limit;
    SimEntityHandle entity;
    SimEntityHandle parent_link;
    SimEntityHandle child_link;
} SimJointInfo;

/* One robot collider measured against one other collider. `distance` is surface separation, so
   it is 0.0 whenever `colliding` is non-zero; penetration depth is not reported. */
typedef struct SimContactPair {
    SimEntityHandle robot_collider;
    SimEntityHandle other;
    double distance;
    int colliding;
} SimContactPair;

/* Active physics solver contact: signed separation (negative for penetration), normal force N.
   Multiple contact points can refer to the same pair of collider entities. */
typedef struct SimPhysicsContact {
    SimEntityHandle first;
    SimEntityHandle second;
    double distance;
    double normal_force;
} SimPhysicsContact;

typedef enum SimSimulationStatus {
    SIM_STATUS_STOPPED = 0,
    SIM_STATUS_RUNNING = 1,
    SIM_STATUS_PAUSED = 2
} SimSimulationStatus;

typedef struct SimEvent {
    char type_utf8[64];
    SimWorldHandle world;
    SimEntityHandle entity;
    double simulation_time;
    char detail_utf8[256];
} SimEvent;

typedef enum SimEditorComponentFlag {
    SIM_EDITOR_COMPONENT_VISUAL = 1u << 0,
    SIM_EDITOR_COMPONENT_COLLIDER = 1u << 1,
    SIM_EDITOR_COMPONENT_RIGID_BODY = 1u << 2,
    SIM_EDITOR_COMPONENT_ROBOT = 1u << 3,
    SIM_EDITOR_COMPONENT_JOINT = 1u << 4,
    SIM_EDITOR_COMPONENT_SENSOR = 1u << 5,
    SIM_EDITOR_COMPONENT_CAMERA = 1u << 6
    ,SIM_EDITOR_COMPONENT_LIGHT = 1u << 7
} SimEditorComponentFlag;

typedef struct SimEditorEntityInfo {
    SimEntityHandle entity;
    SimEntityHandle parent;
    uint32_t component_flags;
    char name_utf8[256];
    SimTransform local_transform;
    SimTransform world_transform;
    int visual_visible;
    uint8_t min_lod;
    uint8_t max_lod;
    int visual_shape;
    double visual_size[3];
    float visual_color[4];
    char visual_resource_utf8[1024];
    int collider_shape;
    double collider_size[3];
    char collider_resource_utf8[1024];
    int collider_trigger;
    double friction[3];
    double restitution;
    uint32_t ifps_sphere_count;
    int body_dynamic;
    int gravity_enabled;
    double mass;
    char sensor_frame_utf8[256];
    double sensor_rate_hz;
    uint32_t camera_width;
    uint32_t camera_height;
    double camera_vertical_fov_radians;
    double camera_near_plane;
    double camera_far_plane;
    int camera_rgbd_output;
    SimLightProperties light;
} SimEditorEntityInfo;

SIM_C_API uint32_t sim_api_version(void);
SIM_C_API const char* sim_last_error(void);
SIM_C_API int sim_create(const SimConfig* config, SimHandle** output);
SIM_C_API int sim_create_with_plugins(const SimConfig* config,
                                      const char* plugin_directory_utf8,
                                      SimHandle** output);
SIM_C_API void sim_destroy(SimHandle* simulator);
SIM_C_API int sim_create_world(SimHandle* simulator, double fixed_time_step,
                               uint64_t seed, SimWorldHandle* output);
SIM_C_API int sim_editor_entity_count(SimHandle* simulator, SimWorldHandle world,
                                      size_t* output);
SIM_C_API int sim_editor_entity_info(SimHandle* simulator, SimWorldHandle world,
                                     size_t index, SimEditorEntityInfo* output);
SIM_C_API int sim_scene_snapshot_json_size(SimHandle* simulator, SimWorldHandle world,
                                           size_t* output_size);
SIM_C_API int sim_scene_snapshot_json(SimHandle* simulator, SimWorldHandle world,
                                      char* output_utf8, size_t output_capacity,
                                      size_t* output_size);
/* Same header and buffer contract as scene_snapshot_json; entities contain only
   id, objectId, parent, local, and world. Sizes include the terminating NUL. */
SIM_C_API int sim_scene_transforms_json_size(SimHandle* simulator, SimWorldHandle world,
                                             size_t* output_size);
SIM_C_API int sim_scene_transforms_json(SimHandle* simulator, SimWorldHandle world,
                                        char* output_utf8, size_t output_capacity,
                                        size_t* output_size);
/* Read revision/generation without traversal, transform update, or serialization. */
SIM_C_API int sim_scene_metadata(SimHandle* simulator, SimWorldHandle world,
                                 uint64_t* generation, uint64_t* revision);
SIM_C_API int sim_get_object_id(SimHandle* simulator, SimWorldHandle world,
                                SimEntityHandle entity, char* object_id_utf8,
                                size_t capacity, size_t* required);
SIM_C_API int sim_find_entity_by_object_id(SimHandle* simulator, SimWorldHandle world,
                                           const char* object_id_utf8,
                                           SimEntityHandle* output);
SIM_C_API int sim_load_asset(SimHandle* simulator, SimWorldHandle world,
                             const char* path_utf8, SimEntityHandle* output);
SIM_C_API int sim_create_entity(SimHandle* simulator, SimWorldHandle world,
                                const char* name_utf8, SimEntityHandle parent,
                                SimEntityHandle* output);
SIM_C_API int sim_destroy_entity(SimHandle* simulator, SimWorldHandle world,
                                 SimEntityHandle entity);
SIM_C_API int sim_set_parent(SimHandle* simulator, SimWorldHandle world,
                             SimEntityHandle entity, SimEntityHandle parent,
                             int preserve_world_transform);
SIM_C_API int sim_set_entity_name(SimHandle* simulator, SimWorldHandle world,
                                  SimEntityHandle entity, const char* name_utf8);
SIM_C_API int sim_create_primitive(SimHandle* simulator, SimWorldHandle world,
                                   const char* name_utf8, SimPrimitiveShape shape,
                                   const double size_xyz[3], SimEntityHandle* output);
SIM_C_API int sim_create_light(SimHandle* simulator, SimWorldHandle world,
                               const char* name_utf8, SimLightType type,
                               SimEntityHandle parent, const SimLightProperties* properties,
                               SimEntityHandle* output);
SIM_C_API int sim_set_light_properties(SimHandle* simulator, SimWorldHandle world,
                                       SimEntityHandle entity, const SimLightProperties* properties);
SIM_C_API int sim_get_light_properties(SimHandle* simulator, SimWorldHandle world,
                                       SimEntityHandle entity, SimLightProperties* output);
SIM_C_API int sim_set_visual_properties(SimHandle* simulator, SimWorldHandle world,
                                        SimEntityHandle entity, int visible,
                                        uint8_t min_lod, uint8_t max_lod);
SIM_C_API int sim_set_rigid_body(SimHandle* simulator, SimWorldHandle world,
                                 SimEntityHandle entity, int enabled, int dynamic,
                                 int gravity_enabled, double mass,
                                 const double friction[3], double restitution);
SIM_C_API int sim_set_ifps_sphere_count(SimHandle* simulator, SimWorldHandle world,
                                        SimEntityHandle entity, uint32_t sphere_count);
SIM_C_API int sim_get_ifps_sphere_count(SimHandle* simulator, SimWorldHandle world,
                                        SimEntityHandle entity, uint32_t* output);
SIM_C_API int sim_set_transform(SimHandle* simulator, SimWorldHandle world,
                                SimEntityHandle entity, const SimTransform* transform);
SIM_C_API int sim_get_transform(SimHandle* simulator, SimWorldHandle world,
                                SimEntityHandle entity, SimTransform* output);
SIM_C_API int sim_set_transforms(SimHandle* simulator, SimWorldHandle world,
                                 const SimEntityHandle* entities,
                                 const SimTransform* transforms, size_t count);
SIM_C_API int sim_robot_joint_count(SimHandle* simulator, SimWorldHandle world,
                                    SimEntityHandle robot, size_t* output);
SIM_C_API int sim_robot_joint_name(SimHandle* simulator, SimWorldHandle world,
                                   SimEntityHandle robot, size_t joint_index,
                                   char* name_utf8, size_t capacity, size_t* required);
SIM_C_API int sim_robot_link_count(SimHandle* simulator, SimWorldHandle world,
                                   SimEntityHandle robot, size_t* output);
SIM_C_API int sim_robot_link_info(SimHandle* simulator, SimWorldHandle world,
                                  SimEntityHandle robot, size_t link_index,
                                  SimEntityHandle* link, char* name_utf8,
                                  size_t capacity, size_t* required);
SIM_C_API int sim_robot_get_joint_state(SimHandle* simulator, SimWorldHandle world,
                                        SimEntityHandle robot, double* positions,
                                        double* velocities, double* efforts, size_t count);
SIM_C_API int sim_robot_command_joint_positions(SimHandle* simulator, SimWorldHandle world,
                                                SimEntityHandle robot, const double* targets,
                                                size_t count);
SIM_C_API int sim_robot_set_joint_continuous(SimHandle* simulator, SimWorldHandle world,
                                             SimEntityHandle robot, const char* joint_name_utf8,
                                             int enabled);
SIM_C_API int sim_robot_command_joint_velocities(SimHandle* simulator, SimWorldHandle world,
                                                 SimEntityHandle robot, const double* targets,
                                                 size_t count);
SIM_C_API int sim_robot_command_joint_positions_batch(
    SimHandle* simulator, SimWorldHandle world, const SimEntityHandle* robots,
    size_t robot_count, const double* targets, size_t targets_per_robot);
/* Pose the robot directly, bypassing the controller: the joint values are written and the scene
   transforms refreshed in one call, with no stepping and no dynamics. */
SIM_C_API int sim_robot_set_joint_positions(SimHandle* simulator, SimWorldHandle world,
                                            SimEntityHandle robot, const double* positions,
                                            size_t count, SimJointLimitPolicy policy);
/* Joint type, axis, limits and the links either side. `joint_index` matches the ordering used by
   sim_robot_joint_name and sim_robot_get_joint_state. */
SIM_C_API int sim_robot_joint_info(SimHandle* simulator, SimWorldHandle world,
                                   SimEntityHandle robot, size_t joint_index,
                                   SimJointInfo* output);
/* As sim_robot_command_joint_positions, plus the controller gain and velocity limit that call
   leaves at its defaults. */
SIM_C_API int sim_robot_command_joint_positions_tuned(
    SimHandle* simulator, SimWorldHandle world, SimEntityHandle robot, const double* targets,
    size_t count, double position_gain, double velocity_limit);
/* Atomic position command with per-joint effort ceilings (Nm for revolute, N for prismatic).
   Positive infinity restores the default unrestricted servo. */
SIM_C_API int sim_robot_command_joint_positions_with_effort_limits(
    SimHandle* simulator, SimWorldHandle world, SimEntityHandle robot, const double* targets,
    size_t count, double position_gain, double velocity_limit, const double* effort_limits);
SIM_C_API int sim_robot_command_joint_efforts(SimHandle* simulator, SimWorldHandle world,
                                              SimEntityHandle robot, const double* targets,
                                              size_t count);
/* Cap the torque each joint's position or velocity servo may apply. One limit per joint, in the
   sim_robot_joint_name ordering. Infinity -- the default -- leaves a joint a kinematic servo that
   tracks its target whatever the load, which suits a stiff arm. A finite limit makes the servo a
   torque source that contact can stall short of its target, which is what a gripper needs: the
   commanded angle sets which way to squeeze, this sets how hard. Persists across later position
   and velocity commands, since it describes the actuator rather than any one command. Honoured by
   dynamics backends; a kinematic controller has no contact to stall against and ignores it. */
SIM_C_API int sim_robot_set_joint_effort_limits(SimHandle* simulator, SimWorldHandle world,
                                               SimEntityHandle robot, const double* limits,
                                               size_t count);
/* Forward kinematics, and optionally the geometric Jacobian, at arbitrary configurations.
   Evaluated in a private clone of the world, so live state is untouched.

   Kinematics are read from the scene graph -- joint origins, axes and hierarchy as held by the
   ECS -- so no source model file is consulted and this works for any robot present in the scene.

   `transforms` receives configuration_count * link_count entries, configuration-major:
       transforms[configuration * link_count + link]
   `jacobians` may be NULL, and is otherwise configuration_count * joint_count entries of six
   doubles, {linear xyz, angular xyz}, one per joint in joint order:
       jacobians[(configuration * joint_count + joint) * 6 + component]
   Pass jacobian_link = 0 for no Jacobian. Columns of joints that do not drive the requested link
   are zero. */
SIM_C_API int sim_robot_kinematics_batch(
    SimHandle* simulator, SimWorldHandle world, SimEntityHandle robot,
    const double* joint_positions, size_t configuration_count, size_t joints_per_configuration,
    const SimEntityHandle* links, size_t link_count, SimEntityHandle jacobian_link,
    SimJointLimitPolicy policy, SimTransform* transforms, double* jacobians);
/* Individual contact pairs at one configuration, rather than the single bool that
   sim_robot_configuration_collision reports.

   An articulated arm always "collides" with itself, because neighbouring links share a joint and
   therefore overlap. Deciding what counts as a genuine collision needs the pairs themselves, so
   the caller can discount adjacent-link contacts, the baseline contacts present at rest, and
   whatever the gripper is deliberately holding.

   Pairs closer than `distance_threshold` are reported even when not touching, which is what a
   safety margin needs; pass 0.0 for touching pairs only. `*required` always receives the full
   pair count, and at most `capacity` are written, so a short buffer is detected by comparing the
   two and retrying. Requires the FCL collision backend. */
SIM_C_API int sim_robot_configuration_contacts(
    SimHandle* simulator, SimWorldHandle world, SimEntityHandle robot,
    const double* joint_positions, size_t joint_count, double distance_threshold,
    SimJointLimitPolicy policy, SimContactPair* pairs, size_t capacity, size_t* required);
/* The filtered form has the same result as the function above while excluding
   known-allowed entity pairs before FCL narrow-phase processing. */
SIM_C_API int sim_robot_configuration_contacts_filtered(
    SimHandle* simulator, SimWorldHandle world, SimEntityHandle robot,
    const double* joint_positions, size_t joint_count, double distance_threshold,
    SimJointLimitPolicy policy, const SimEntityHandle* excluded_pairs,
    size_t excluded_pair_count, SimContactPair* pairs, size_t capacity, size_t* required);
/* Push current scene transforms into the collision backend without stepping the simulation. */
SIM_C_API int sim_collision_sync(SimHandle* simulator, SimWorldHandle world);
SIM_C_API int sim_physics_contacts(SimHandle* simulator, SimWorldHandle world,
    SimPhysicsContact* contacts, size_t capacity, size_t* required);
/* Adopt the scene's current joint and body poses as the physics state, clearing
   velocities. Needed after posing the scene outside physics -- writing a joint
   directly, applying an IK solution, moving an object -- so that stepping continues
   from what the scene shows rather than from the pose the backend was built with. */
SIM_C_API int sim_physics_sync(SimHandle* simulator, SimWorldHandle world);
SIM_C_API int sim_create_camera(SimHandle* simulator, SimWorldHandle world,
                                SimEntityHandle parent, const char* frame_id_utf8,
                                double update_rate_hz, uint32_t width, uint32_t height,
                                double vertical_fov_radians, double near_plane,
                                double far_plane, SimEntityHandle* output);
SIM_C_API int sim_set_camera_properties(SimHandle* simulator, SimWorldHandle world,
                                        SimEntityHandle camera, const char* frame_id_utf8,
                                        double update_rate_hz, uint32_t width, uint32_t height,
                                        double vertical_fov_radians, double near_plane,
                                        double far_plane, int rgbd_output);
SIM_C_API int sim_camera_get_dimensions(SimHandle* simulator, SimWorldHandle world,
                                        SimEntityHandle camera, uint32_t* width,
                                        uint32_t* height);
SIM_C_API int sim_camera_get_intrinsics(SimHandle* simulator, SimWorldHandle world,
                                        SimEntityHandle camera, double* fx, double* fy,
                                        double* cx, double* cy);
SIM_C_API int sim_camera_get_rgb(SimHandle* simulator, SimWorldHandle world,
                                 SimEntityHandle camera, uint8_t* rgb, size_t byte_count);
SIM_C_API int sim_camera_get_depth(SimHandle* simulator, SimWorldHandle world,
                                   SimEntityHandle camera, float* depth, size_t pixel_count);
SIM_C_API int sim_create_lidar(SimHandle* simulator, SimWorldHandle world,
                               SimEntityHandle parent, const char* frame_id_utf8,
                               double update_rate_hz, uint32_t horizontal_samples,
                               uint32_t vertical_channels, double min_distance,
                               double max_distance, SimEntityHandle* output);
SIM_C_API int sim_lidar_point_count(SimHandle* simulator, SimWorldHandle world,
                                    SimEntityHandle lidar, size_t* output);
SIM_C_API int sim_lidar_get_points(SimHandle* simulator, SimWorldHandle world,
                                   SimEntityHandle lidar, float* xyz, float* distances,
                                   size_t point_count);
SIM_C_API int sim_collision_check(SimHandle* simulator, SimWorldHandle world,
                                  SimEntityHandle first, SimEntityHandle second,
                                  int* colliding);
SIM_C_API int sim_collision_distance(SimHandle* simulator, SimWorldHandle world,
                                     SimEntityHandle first, SimEntityHandle second,
                                     double* distance);
SIM_C_API int sim_scene_collision_batch(SimHandle* simulator, SimWorldHandle world,
                                        const SimEntityHandle* entities, size_t count,
                                        uint8_t* colliding, double* elapsed_ms);
SIM_C_API int sim_robot_configuration_collision(
    SimHandle* simulator, SimWorldHandle world, SimEntityHandle robot,
    const double* joint_positions, size_t joint_count, int* colliding);
SIM_C_API int sim_robot_configurations_collision(
    SimHandle* simulator, SimWorldHandle world, SimEntityHandle robot,
    const double* joint_positions, size_t configuration_count, size_t joints_per_configuration,
    uint8_t* colliding);
SIM_C_API int sim_apply_body_wrench(SimHandle* simulator, SimWorldHandle world,
                                    SimEntityHandle entity, const double force_xyz[3],
                                    const double torque_xyz[3]);
SIM_C_API int sim_start(SimHandle* simulator, SimWorldHandle world);
SIM_C_API int sim_pause(SimHandle* simulator, SimWorldHandle world);
SIM_C_API int sim_get_status(SimHandle* simulator, SimWorldHandle world,
                             SimSimulationStatus* status);
SIM_C_API int sim_poll_event(SimHandle* simulator, SimWorldHandle world,
                             SimEvent* event, int* has_event);
SIM_C_API int sim_step(SimHandle* simulator, SimWorldHandle world, uint64_t count);
SIM_C_API int sim_step_unpaced(SimHandle* simulator, SimWorldHandle world, uint64_t count);
SIM_C_API int sim_step_profiled(SimHandle* simulator, SimWorldHandle world,
                                uint64_t count, double* elapsed_ms);
SIM_C_API int sim_get_time(SimHandle* simulator, SimWorldHandle world, double* output);
SIM_C_API int sim_save_state(SimHandle* simulator, SimWorldHandle world, SimStateHandle* output);
SIM_C_API int sim_restore_state(SimHandle* simulator, SimWorldHandle world, SimStateHandle state);
SIM_C_API int sim_save_state_file(SimHandle* simulator, SimWorldHandle world,
                                  const char* path_utf8);
SIM_C_API int sim_restore_state_file(SimHandle* simulator, SimWorldHandle world,
                                     const char* path_utf8);
SIM_C_API int sim_reset(SimHandle* simulator, SimWorldHandle world);

#ifdef __cplusplus
}
#endif
