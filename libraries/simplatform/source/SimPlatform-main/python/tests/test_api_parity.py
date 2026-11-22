from __future__ import annotations

from sim_platform import sdk as sdk_module
from sim_platform.live import CameraProxy, EntityProxy, LidarProxy, LiveSession, RobotProxy
from sim_platform.sdk import Camera, Entity, LiDAR, Robot, Simulator, World


def test_c_python_and_live_scene_capability_contract() -> None:
    capabilities = [
        ("sim_scene_snapshot_json", Simulator, "scene_snapshot", LiveSession, "scene_snapshot"),
        ("sim_find_entity_by_object_id", World, "find_entity", LiveSession, "find_entity"),
        ("sim_get_object_id", Entity, "object_id", EntityProxy, "object_id"),
        ("sim_load_asset", World, "load_asset", LiveSession, "load_asset"),
        ("sim_create_entity", World, "create_entity", LiveSession, "create_entity"),
        ("sim_destroy_entity", Entity, "destroy", EntityProxy, "destroy"),
        ("sim_set_parent", Entity, "set_parent", EntityProxy, "set_parent"),
        ("sim_set_entity_name", Entity, "set_name", EntityProxy, "set_name"),
        ("sim_create_primitive", World, "create_primitive", LiveSession, "create_primitive"),
        ("sim_set_visual_properties", Entity, "set_visual", EntityProxy, "set_visual"),
        ("sim_set_rigid_body", Entity, "set_rigid_body", EntityProxy, "set_rigid_body"),
        ("sim_set_ifps_sphere_count", Entity, "ifps_sphere_count", EntityProxy,
         "ifps_sphere_count"),
        ("sim_set_transform", Entity, "transform", EntityProxy, "set_position"),
        ("sim_set_transforms", World, "set_transforms", LiveSession, "set_transforms"),
        ("sim_robot_get_joint_state", Robot, "joint_state", RobotProxy, "joint_state"),
        ("sim_robot_command_joint_positions", Robot, "command_joint_positions", RobotProxy,
         "command_joint_positions"),
        ("sim_robot_set_joint_continuous", Robot, "set_joint_continuous", RobotProxy,
         "set_joint_continuous"),
        ("sim_robot_command_joint_velocities", Robot, "command_joint_velocities", RobotProxy,
         "command_joint_velocities"),
        ("sim_robot_command_joint_positions_batch", Simulator,
         "command_joint_positions_batch", LiveSession, "command_joint_positions_batch"),
        ("sim_create_camera", World, "create_camera", LiveSession, "create_camera"),
        ("sim_set_camera_properties", Camera, "configure", CameraProxy, "configure"),
        ("sim_camera_get_dimensions", Camera, "dimensions", CameraProxy, "dimensions"),
        ("sim_camera_get_intrinsics", Camera, "intrinsics", CameraProxy, "intrinsics"),
        ("sim_camera_get_rgb", Camera, "rgb", CameraProxy, "rgb"),
        ("sim_camera_get_depth", Camera, "depth", CameraProxy, "depth"),
        ("sim_create_lidar", World, "create_lidar", LiveSession, "create_lidar"),
        ("sim_lidar_get_points", LiDAR, "get_points", LidarProxy, "get_points"),
        ("sim_collision_check", World, "check_collision", LiveSession, "check_collision"),
        ("sim_collision_distance", World, "distance", LiveSession, "collision_distance"),
        ("sim_scene_collision_batch", World, "scene_collisions", LiveSession,
         "scene_collisions"),
        ("sim_robot_configuration_collision", Robot, "configuration_collides", RobotProxy,
         "configuration_collides"),
        ("sim_robot_configurations_collision", Robot, "configurations_collide", RobotProxy,
         "configurations_collide"),
        ("sim_apply_body_wrench", Entity, "apply_wrench", EntityProxy, "apply_wrench"),
        ("sim_start", Simulator, "start", LiveSession, "start"),
        ("sim_pause", Simulator, "pause", LiveSession, "pause"),
        ("sim_get_status", Simulator, "status", LiveSession, "status"),
        ("sim_poll_event", Simulator, "poll_event", LiveSession, "poll_event"),
        ("sim_step", Simulator, "step", LiveSession, "step"),
        ("sim_step_profiled", Simulator, "step_profiled", LiveSession, "step_profiled"),
        ("sim_save_state", Simulator, "save_state", LiveSession, "save_state"),
        ("sim_restore_state", Simulator, "restore_state", LiveSession, "restore_state"),
        ("sim_reset", Simulator, "reset", LiveSession, "reset"),
    ]
    for c_symbol, python_type, python_name, live_type, live_name in capabilities:
        assert hasattr(sdk_module._LIB, c_symbol), c_symbol
        assert hasattr(python_type, python_name), f"{python_type.__name__}.{python_name}"
        assert hasattr(live_type, live_name), f"{live_type.__name__}.{live_name}"


def test_backend_selection_is_exposed_on_all_surfaces() -> None:
    assert hasattr(sdk_module._LIB, "sim_create_with_plugins")
    assert hasattr(Simulator, "__init__")
    assert hasattr(LiveSession, "backend_status")
    assert hasattr(LiveSession, "apply_backends")
