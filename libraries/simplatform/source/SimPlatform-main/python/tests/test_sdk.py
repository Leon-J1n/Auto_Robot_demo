from __future__ import annotations

import ctypes
import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from sim_platform import Robot, Simulator, Transform, api_version
from sim_platform import sdk as sdk_module


DATA = Path(__file__).resolve().parents[2] / "tests" / "data"


def test_c_api_version_is_exposed() -> None:
    assert api_version() == 9


def test_c_scene_snapshot_buffer_contract() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none") as simulator:
            world = simulator.create_world()
            world.create_entity("unicode-\u6d4b\u8bd5")
            required = ctypes.c_size_t()
            assert sdk_module._LIB.sim_scene_snapshot_json_size(
                simulator._handle, world.handle, ctypes.byref(required)) == 0
            assert required.value > 1

            too_small = ctypes.create_string_buffer(required.value - 1)
            actual = ctypes.c_size_t()
            assert sdk_module._LIB.sim_scene_snapshot_json(
                simulator._handle, world.handle, too_small, len(too_small),
                ctypes.byref(actual)) != 0
            assert actual.value == required.value

            output = ctypes.create_string_buffer(required.value)
            assert sdk_module._LIB.sim_scene_snapshot_json(
                simulator._handle, world.handle, output, len(output),
                ctypes.byref(actual)) == 0
            assert output.raw[actual.value - 1] == 0
            payload = json.loads(output.value.decode("utf-8"))
            assert payload["schemaVersion"] == "1.0"
            assert any(item["name"] == "unicode-\u6d4b\u8bd5" for item in payload["entities"])


def test_ifps_sphere_count_property() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none") as simulator:
            world = simulator.create_world(fixed_time_step=0.01)
            entity = world.create_primitive(
                "ifps-box", "box", (1.0, 1.0, 1.0), ifps_sphere_count=73)
            assert entity.ifps_sphere_count == 73
            entity.ifps_sphere_count = 128
            assert entity.ifps_sphere_count == 128
            entity.ifps_sphere_count = 0
            assert entity.ifps_sphere_count == 0
            with pytest.raises(ValueError):
                entity.ifps_sphere_count = -1
            with pytest.raises(ValueError):
                world.create_primitive(
                    "invalid-ifps-box", "box", (1.0, 1.0, 1.0), ifps_sphere_count=True)


def test_editor_entity_commands_and_snapshot() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none") as simulator:
            world = simulator.create_world()
            parent = world.create_primitive("parent", "box", (1.0, 1.0, 1.0))
            child = world.create_primitive("child", "sphere", (0.5, 0.5, 0.5))
            child.transform = Transform(translation=(1.0, 2.0, 3.0))
            child.set_parent(parent, preserve_world=True)
            child.set_name("renamed")
            child.set_visual(visible=False, min_lod=1, max_lod=4)
            child.set_rigid_body(dynamic=True, gravity=False, mass=2.5,
                                 friction=(0.7, 0.01, 0.002), restitution=0.25)
            child.ifps_sphere_count = 48

            camera = world.create_camera(parent, width=32, height=24)
            camera.configure(frame_id="web_camera", update_rate_hz=20.0,
                             width=80, height=60, near_plane=0.05,
                             far_plane=50.0, output="rgbd")
            ambient = world.create_light("环境光", "ambient", intensity=0.25)
            ambient.set_light(type="ambient", intensity=0.3)

            entities = {item.entity: item for item in world.editor_entities()}
            snapshot = simulator.scene_snapshot(world)
            snapshots = {item["id"]: item for item in snapshot.entities}
            assert snapshot.schema_version == "1.0"
            assert snapshot.world_id == world.handle
            assert snapshot.status == "stopped"
            assert snapshots[child.handle]["objectId"] == child.object_id
            assert snapshots[child.handle]["name"] == "renamed"
            assert snapshots[child.handle]["world"]["translation"] == [1.0, 2.0, 3.0]
            assert snapshots[camera.handle]["camera"]["output"] == "rgbd"
            item = entities[child.handle]
            assert item.parent == parent.handle
            assert item.name == "renamed"
            assert item.world.translation == (1.0, 2.0, 3.0)
            assert item.visual is not None
            assert item.visual["visible"] is False
            assert item.visual["min_lod"] == 1 and item.visual["max_lod"] == 4
            assert item.visual["shape"] == 1
            assert item.visual["size"] == pytest.approx((1.0, 1.0, 1.0))
            assert item.visual["color"] == pytest.approx((0.62, 0.68, 0.76, 1.0))
            assert item.visual["resource"] == "builtin://sphere"
            assert item.rigid_body == {"dynamic": True, "gravity": False, "mass": 2.5}
            assert item.collider is not None
            assert item.collider["size"] == pytest.approx((0.5, 0.5, 0.5))
            assert item.collider["ifps_sphere_count"] == 48
            assert entities[camera.handle].camera == {
                "width": 80, "height": 60,
                "vertical_fov_radians": pytest.approx(np.pi / 3.0),
                "near_plane": 0.05, "far_plane": 50.0, "output": "rgbd",
            }
            assert entities[ambient.handle].light is not None
            assert entities[ambient.handle].light["type"] == "ambient"
            assert entities[ambient.handle].light["intensity"] == pytest.approx(0.3)
            child.destroy()
            assert child.handle not in {item.entity for item in world.editor_entities()}


def test_editor_snapshot_exposes_mesh_collider_resource() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none") as simulator:
            world = simulator.create_world()
            world.load_robot(DATA / "mesh_two_link.urdf")
            resources = [item.collider["resource"] for item in world.editor_entities()
                         if item.collider and item.collider["shape"] == 4]
            assert resources
            assert all(Path(resource).is_absolute() for resource in resources)
            assert all(Path(resource).name == "triangle.obj" for resource in resources)


def test_imported_mesh_root_accepts_dynamic_rigid_body() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="mujoco") as simulator:
            world = simulator.create_world(fixed_time_step=0.002)
            imported = world.load_asset(DATA / "triangle.obj")
            imported.transform = Transform(translation=(0.0, 0.0, 1.0))
            imported.set_rigid_body(
                dynamic=True, gravity=True, mass=0.2,
                friction=(0.8, 0.005, 0.0001), restitution=0.05)

            entities = world.editor_entities()
            root = next(item for item in entities if item.entity == imported.handle)
            mesh_colliders = [
                item for item in entities
                if item.collider and item.collider["shape"] == 4
                and item.collider["resource"].endswith("triangle.obj#mesh=0")
            ]
            assert root.parent is None
            assert root.collider is None
            assert root.rigid_body == {
                "dynamic": True, "gravity": True, "mass": 0.2,
            }
            assert len(mesh_colliders) == 1
            assert mesh_colliders[0].rigid_body is None
            assert mesh_colliders[0].collider["friction"] == pytest.approx(
                (0.8, 0.005, 0.0001))
            assert mesh_colliders[0].collider["restitution"] == pytest.approx(0.05)

            start_z = imported.transform.translation[2]
            simulator.step(world, 50)
            assert imported.transform.translation[2] < start_z


def test_python_robot_control_state_restore() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="mujoco") as simulator:
            world = simulator.create_world(fixed_time_step=0.002, seed=42)
            robot = world.load_robot(DATA / "two_link.urdf")
            assert len(robot.object_id) == 36
            assert world.find_entity(robot.object_id).handle == robot.handle
            assert {name for name, _ in robot.links} == {"base_link", "tool_link"}
            assert robot.get_link("tool_link").handle in {
                entity.handle for _, entity in robot.links
            }
            obstacle = world.load_asset(DATA / "triangle.obj")
            obstacle.transform = Transform(translation=(2.0, 0.0, 0.0))
            lidar = world.create_lidar(robot, update_rate_hz=100.0,
                                       horizontal_samples=8, vertical_channels=2,
                                       min_distance=0.01, max_distance=10.0)
            camera = world.create_camera(robot, update_rate_hz=100.0, width=32, height=24,
                                         near_plane=0.05, far_plane=20.0)
            assert robot.joint_count == 1
            assert robot.joint_names == ("tool_joint",)
            assert robot.collision_backend == "fcl"
            saved = simulator.save_state(world)
            robot.apply_wrench((1.0, 0.0, 0.0))
            robot.command_joint_positions([0.5])
            simulator.step(world, 20)
            moved = robot.joint_state[0].copy()
            assert np.any(np.abs(moved) > 1e-8)
            points, distances = lidar.get_points()
            assert points.shape == (16, 3) and distances.shape == (16,)
            assert np.all(np.isfinite(points)) and np.all(distances >= 0.01)
            assert camera.rgb.shape == (24, 32, 3)
            assert camera.depth.shape == (24, 32)
            assert np.isclose(simulator.time(world), 0.04)
            simulator.restore_state(world, saved)
            assert np.allclose(robot.joint_state[0], 0.0)
            assert np.isclose(simulator.time(world), 0.0)


def test_state_file_restores_across_simulator_instances() -> None:
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "checkpoint.simstate"
        with Simulator(cache_directory=f"{directory}/cache", physics="mujoco") as first:
            world = first.create_world(fixed_time_step=0.002, seed=7)
            robot = world.load_robot(DATA / "two_link.urdf")
            robot.command_joint_positions([0.4])
            first.step(world, 10)
            expected = robot.joint_state[0].copy()
            expected_time = first.time(world)
            first.save_state_file(world, state_path)

        with Simulator(cache_directory=f"{directory}/cache", physics="mujoco") as second:
            restored_world = second.create_world(fixed_time_step=0.01, seed=999)
            second.restore_state_file(restored_world, state_path)
            restored_robot = Robot(restored_world, robot.handle)
            assert np.allclose(restored_robot.joint_state[0], expected)
            assert np.isclose(second.time(restored_world), expected_time)


def test_batch_transforms_and_joint_targets() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none") as simulator:
            world = simulator.create_world(fixed_time_step=0.01)
            assert simulator.status(world) == "stopped"
            simulator.start(world)
            assert simulator.status(world) == "running"
            simulator.pause(world)
            assert simulator.status(world) == "paused"
            first_entity = world.create_entity("first")
            second_entity = world.create_entity("second")
            world.set_transforms(
                [first_entity, second_entity],
                [Transform(translation=(1.0, 2.0, 3.0)),
                 Transform(translation=(-1.0, -2.0, -3.0))],
            )
            assert first_entity.transform.translation == (1.0, 2.0, 3.0)
            assert second_entity.transform.translation == (-1.0, -2.0, -3.0)

            first_robot = world.load_robot(DATA / "two_link.urdf")
            second_robot = world.load_robot(DATA / "two_link.urdf")
            simulator.command_joint_positions_batch(
                world, [first_robot, second_robot], [[0.5], [-0.5]])
            simulator.step(world, 10)
            assert first_robot.joint_state[0][0] > 0.0
            assert second_robot.joint_state[0][0] < 0.0
            events = []
            while (event := simulator.poll_event(world)) is not None:
                events.append(event)
            assert all(event.world == world.handle for event in events)
            assert {event.type for event in events} >= {"SimulationStarted", "SimulationPaused"}


def test_joint_velocity_command() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none") as simulator:
            world = simulator.create_world(fixed_time_step=0.01)
            robot = world.load_robot(DATA / "two_link.urdf")
            robot.command_joint_velocities([0.5])
            simulator.start(world)
            simulator.step(world, 20)
            positions, velocities, _ = robot.joint_state
            assert np.allclose(positions, [0.1])
            assert np.allclose(velocities, [0.5])


def test_vulkan_camera_rgb_and_depth() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none") as simulator:
            world = simulator.create_world(fixed_time_step=0.01)
            mount = world.create_entity("camera_mount")
            rendered_robot = world.load_robot(DATA / "two_link.urdf")
            rendered_robot.transform = Transform(translation=(3.0, 0.0, 0.0))
            camera = world.create_camera(mount, update_rate_hz=100.0, width=64, height=64,
                                         near_plane=0.1, far_plane=25.0)
            simulator.step(world)
            assert camera.dimensions == (64, 64)
            intrinsics = camera.intrinsics
            assert intrinsics.shape == (3, 3)
            assert intrinsics[0, 0] > 0.0 and intrinsics[1, 1] > 0.0
            assert np.isclose(intrinsics[0, 2], 31.5)
            assert np.isclose(intrinsics[1, 2], 31.5)
            assert camera.rgb.shape == (64, 64, 3)
            assert camera.rgb.dtype == np.uint8
            assert camera.depth.shape == (64, 64)
            assert camera.depth.dtype == np.float32
            assert np.all(np.isfinite(camera.depth))
            assert np.all(camera.depth >= 0.1)
            assert np.all(camera.depth <= 25.0)
            assert np.min(camera.depth) < 25.0


def test_fcl_lidar_without_physics_backend() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none") as simulator:
            world = simulator.create_world(fixed_time_step=0.01)
            robot = world.load_robot(DATA / "two_link.urdf")
            batch_collision = robot.configurations_collide([[0.0], [0.5]])
            assert batch_collision.shape == (2,)
            assert batch_collision.dtype == np.bool_
            assert robot.configuration_collides([0.0]) == bool(batch_collision[0])
            lidar = world.create_lidar(robot, update_rate_hz=100.0,
                                       horizontal_samples=6, vertical_channels=2,
                                       min_distance=0.01, max_distance=5.0)
            simulator.step(world)
            points, distances = lidar.get_points()
            assert points.shape == (12, 3)
            assert distances.shape == (12,)
            assert np.all(np.isfinite(points))
            assert np.all(distances >= 0.01)
            assert np.all(distances <= 5.0)


def test_bullet3_plugin_rigid_body_state_and_wrench() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="bullet") as simulator:
            world = simulator.create_world(fixed_time_step=0.002, seed=11)
            floor = world.create_primitive("floor", "box", (10.0, 10.0, 1.0))
            floor.transform = Transform(translation=(0.0, 0.0, -0.5))
            falling = world.create_primitive("falling", "box", (0.5, 0.5, 0.5))
            falling.transform = Transform(translation=(0.0, 0.0, 3.0))
            falling.set_rigid_body(dynamic=True, gravity=True, restitution=0.2)
            floating = world.create_primitive("floating", "box", (0.5, 0.5, 0.5))
            floating.transform = Transform(translation=(2.0, 0.0, 3.0))
            floating.set_rigid_body(dynamic=True, gravity=False)

            saved = simulator.save_state(world)
            floating.apply_wrench((20.0, 0.0, 0.0))
            simulator.step(world, 1000)
            assert 0.2 < falling.transform.translation[2] < 1.0
            assert np.isclose(floating.transform.translation[2], 3.0, atol=1e-5)
            assert floating.transform.translation[0] > 2.0

            simulator.restore_state(world, saved)
            assert np.allclose(falling.transform.translation, (0.0, 0.0, 3.0))
            assert np.allclose(floating.transform.translation, (2.0, 0.0, 3.0))


def test_bullet3_collision_plugin_pair_batch_and_distance() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none", collision="bullet3") as simulator:
            world = simulator.create_world()
            first = world.create_primitive("first", "box", (1.0, 1.0, 1.0))
            second = world.create_primitive("second", "box", (1.0, 1.0, 1.0))
            distant = world.create_primitive("distant", "box", (1.0, 1.0, 1.0))
            second.transform = Transform(translation=(0.4, 0.0, 0.0))
            distant.transform = Transform(translation=(3.0, 0.0, 0.0))

            assert world.check_collision(first, second)
            colliding, _elapsed_ms = world.scene_collisions([first, second, distant])
            assert colliding.tolist() == [True, True, False]
            assert np.isclose(world.distance(first, distant), 2.0)

            second.transform = Transform(translation=(4.0, 0.0, 0.0))
            colliding, _elapsed_ms = world.scene_collisions([first, second, distant])
            assert colliding.tolist() == [False, True, True]


def test_coal_collision_plugin_pair_batch_and_distance() -> None:
    with tempfile.TemporaryDirectory() as cache:
        with Simulator(cache_directory=cache, physics="none", collision="coal") as simulator:
            world = simulator.create_world()
            first = world.create_primitive("first", "box", (1.0, 1.0, 1.0))
            second = world.create_primitive("second", "box", (1.0, 1.0, 1.0))
            distant = world.create_primitive("distant", "box", (1.0, 1.0, 1.0))
            second.transform = Transform(translation=(0.4, 0.0, 0.0))
            distant.transform = Transform(translation=(3.0, 0.0, 0.0))

            assert world.check_collision(first, second)
            colliding, _elapsed_ms = world.scene_collisions([first, second, distant])
            assert colliding.tolist() == [True, True, False]
            assert np.isclose(world.distance(first, distant), 2.0)

            second.transform = Transform(translation=(3.9, 0.0, 0.0))
            colliding, _elapsed_ms = world.scene_collisions([first, second, distant])
            assert colliding.tolist() == [False, True, True]
