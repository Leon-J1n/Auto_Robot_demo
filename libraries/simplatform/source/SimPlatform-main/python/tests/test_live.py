from __future__ import annotations

import os
import threading
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sim_platform.live import (LaunchSpec, LiveControlError, connect, connect_or_launch,
                               launch, parse_endpoint)
from sim_platform.server import HeadlessService, _Handler, _LiveTCPServer, _tcp_uri


DATA = Path(__file__).resolve().parents[2] / "tests" / "data"
EDITOR = Path(__file__).resolve().parents[2] / "build" / "windows-gui" / "Release" / "sim_editor.exe"


def test_endpoint_parser_supports_pid_tcp_and_ipv6() -> None:
    assert parse_endpoint("pid:123").pid == 123
    assert str(parse_endpoint("example.com:8765")) == "tcp://example.com:8765"
    assert str(parse_endpoint("tcp://[::1]:9000")) == "tcp://[::1]:9000"
    with pytest.raises(ValueError):
        parse_endpoint("pid:0")


def test_two_live_connections_share_aliases_and_scene(tmp_path: Path) -> None:
    service = HeadlessService(cache_directory=str(tmp_path / "cache"))
    server = _LiveTCPServer(("127.0.0.1", 0), _Handler)
    server.service = service  # type: ignore[attr-defined]
    port = int(server.server_address[1])
    service.endpoints = (_tcp_uri("127.0.0.1", port),)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first = connect(f"tcp://127.0.0.1:{port}")
        robot = first.ensure_robot(DATA / "two_link.urdf", alias="robot")
        cube = first.create_primitive("cube")
        cube.set_scale((0.4, 0.4, 0.2))
        cube.set_name("Live Cube")
        cube.set_visual(visible=True, min_lod=0, max_lod=4)
        cube.ifps_sphere_count = 16
        assert cube.ifps_sphere_count == 16
        assert cube.transform["scale"] == pytest.approx((0.4, 0.4, 0.2))
        assert robot.joint_count == len(robot.joint_names)
        assert robot.joint_state[0].shape == (robot.joint_count,)
        snapshot = first.scene_snapshot()
        assert snapshot.as_dict() == service.simulator.scene_snapshot(service.world).as_dict()
        snapshot_entities = {item["id"]: item for item in snapshot.entities}
        assert snapshot.schema_version == "1.0"
        assert snapshot_entities[cube.handle]["objectId"] == cube.object_id
        assert snapshot_entities[cube.handle]["name"] == "Live Cube"
        assert snapshot_entities[robot.handle]["robot"]["jointNames"] == list(robot.joint_names)
        robot.set_position((1.0, 2.0, 3.0))
        first.set_transforms([(cube, {
            "translation": (0.5, 0.0, 0.0), "rotation_wxyz": (1.0, 0.0, 0.0, 0.0),
            "scale": (0.4, 0.4, 0.2),
        })])
        saved = first.save_state()
        cube.set_position((2.0, 0.0, 0.0))
        first.restore_state(saved)
        assert cube.transform["translation"] == pytest.approx((0.5, 0.0, 0.0))
        camera = first.create_camera(cube, width=32, height=24)
        assert camera.dimensions == (32, 24)
        assert camera.intrinsics.shape == (3, 3)
        lidar = first.create_lidar(cube, horizontal_samples=4, vertical_channels=2)
        first.step()
        points, distances = lidar.get_points()
        assert points.shape == (8, 3) and distances.shape == (8,)
        robot.command_joint_velocities([0.0] * robot.joint_count)
        assert isinstance(robot.configuration_collides([0.0] * robot.joint_count), bool)
        assert robot.configurations_collide([[0.0] * robot.joint_count]).shape == (1,)
        link = robot.get_link("tool_link")
        robot_id = robot.object_id
        first.close()

        second = connect(f"tcp://127.0.0.1:{port}")
        restored = second.require_robot("robot")
        assert restored.object_id == robot_id
        assert restored.transform["translation"] == pytest.approx((1.0, 2.0, 3.0))
        gripper = second.ensure_robot(DATA / "two_link.urdf", alias="gripper")
        gripper.set_parent(link, preserve_world=False)
        gripper.set_rotation((1.0, 0.0, 0.0, 0.0))
        assert second.ensure_robot(DATA / "two_link.urdf", alias="gripper").object_id == gripper.object_id
        with pytest.raises(LiveControlError, match="different object") as captured:
            second.ensure_robot(DATA / "mesh_two_link.urdf", alias="robot")
        assert captured.value.code == "alias_conflict"

        entities = {item.entity: item for item in service.world.editor_entities()}
        assert entities[gripper.handle].parent == link.handle
        gripper.destroy()
        with pytest.raises(LiveControlError) as stale:
            gripper.set_position((0.0, 0.0, 0.0))
        assert stale.value.code == "stale_object"
        second.close()
    finally:
        server.shutdown()
        server.server_close()
        service.close()
        thread.join(timeout=5.0)


def test_live_entity_exposes_sdk_rigid_body_configuration(tmp_path: Path) -> None:
    service = HeadlessService(physics="none", cache_directory=str(tmp_path / "cache"))
    server = _LiveTCPServer(("127.0.0.1", 0), _Handler)
    server.service = service  # type: ignore[attr-defined]
    port = int(server.server_address[1])
    service.endpoints = (_tcp_uri("127.0.0.1", port),)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = connect(f"tcp://127.0.0.1:{port}")
    try:
        floor = session.create_primitive("plane")
        floor.set_position((0.0, 0.0, -0.01))
        floor.set_rigid_body(dynamic=False, gravity=False)
        sphere = session.create_primitive("sphere")
        sphere.set_position((0.0, 0.0, 1.5))
        assert session.backend_status() == {"physics": "none", "collision": "fcl"}
        assert session.apply_backends(physics="bullet3") == {
            "physics": "bullet3", "collision": "fcl", "changed": True,
        }
        assert sphere.set_rigid_body(
            dynamic=True, gravity=True, mass=1.0,
            restitution=0.7, friction=(0.4, 0.005, 0.001),
        ) == "bullet3"
        entity = next(item for item in session.scene_snapshot().entities if item["id"] == sphere.handle)
        assert entity["rigidBody"] == {"enabled": True, "dynamic": True, "gravity": True, "mass": 1}
        assert entity["collider"]["restitution"] == pytest.approx(0.7)
        assert entity["collider"]["friction"] == pytest.approx([0.4, 0.005, 0.001])
        session.step(1500)
        assert sphere.transform["translation"][2] < 1.0
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        service.close()
        thread.join(timeout=5.0)


def test_launch_dynamic_port_and_reconnect_by_pid(tmp_path: Path) -> None:
    session = launch(LaunchSpec(
        instance_name="dynamic-live-test",
        listen=("tcp://127.0.0.1:0",),
        registry_directory=tmp_path,
        detached=False,
        startup_timeout=15.0,
    ))
    try:
        assert session.pid > 0
        assert session.endpoint.startswith("tcp://127.0.0.1:")
        assert not session.endpoint.endswith(":0")
        assert session._connection.gettimeout() >= 60.0
        reconnected = connect(session.pid, registry_directory=tmp_path)
        try:
            assert reconnected.instance_id == session.instance_id
        finally:
            reconnected.close()
        session.shutdown()
    finally:
        session.close()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and list(tmp_path.glob("*.json")):
        time.sleep(0.05)
    assert not list(tmp_path.glob("*.json"))


def test_separate_python_processes_share_scene_aliases(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    session = launch(LaunchSpec(
        instance_name="separate-process-test",
        listen=("tcp://127.0.0.1:0",),
        registry_directory=registry,
        detached=True,
        startup_timeout=15.0,
    ))
    first_script = tmp_path / "first.py"
    second_script = tmp_path / "second.py"
    first_script.write_text(
        f"robot = ensure_robot(alias='robot', path={str(DATA / 'two_link.urdf')!r})\n"
        "robot.set_position((1.0, 2.0, 3.0))\n",
        encoding="utf-8",
    )
    second_script.write_text(
        f"robot = require_robot('robot')\n"
        f"gripper = ensure_robot(alias='gripper', path={str(DATA / 'two_link.urdf')!r})\n"
        "gripper.set_parent(robot.get_link('tool_link'), preserve_world=False)\n"
        "gripper.set_rotation((1.0, 0.0, 0.0, 0.0))\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    command = [sys.executable, "-m", "sim_platform.cli", "run"]
    try:
        subprocess.run(
            [*command, str(first_script), "--connect", f"pid:{session.pid}",
             "--registry-dir", str(registry)],
            check=True, env=environment, capture_output=True, text=True,
        )
        subprocess.run(
            [*command, str(second_script), "--connect", f"pid:{session.pid}",
             "--registry-dir", str(registry)],
            check=True, env=environment, capture_output=True, text=True,
        )
        assert session.require_robot("robot").transform["translation"] == pytest.approx(
            (1.0, 2.0, 3.0)
        )
        assert session.require_robot("gripper").object_id
        session.shutdown()
    finally:
        session.close()


def test_fixed_port_conflict_returns_bind_failed(tmp_path: Path) -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    port = int(occupied.getsockname()[1])
    try:
        with pytest.raises(LiveControlError) as captured:
            launch(LaunchSpec(
                listen=(f"tcp://127.0.0.1:{port}",),
                registry_directory=tmp_path,
                detached=False,
            ))
        assert captured.value.code == "bind_failed"
    finally:
        occupied.close()


def test_connect_or_launch_is_singleton_and_runner_advances(tmp_path: Path) -> None:
    sessions = []
    errors = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait()
            sessions.append(connect_or_launch(
                "singleton-live-test",
                LaunchSpec(listen=("tcp://127.0.0.1:0",), registry_directory=tmp_path,
                           detached=True, startup_timeout=20.0),
            ))
        except Exception as error:  # pragma: no cover - surfaced by the assertion below.
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    assert not errors
    assert len(sessions) == 2
    assert len({session.pid for session in sessions}) == 1
    try:
        sessions[0].start()
        _, before = sessions[0].status()
        time.sleep(0.05)
        status, after = sessions[1].status()
        assert status == "running" and after > before
        sessions[1].pause()
        sessions[0].shutdown()
    finally:
        for session in sessions:
            session.close()


@pytest.mark.skipif(not EDITOR.is_file(), reason="Qt editor build is unavailable")
def test_launch_editor_and_reconnect_by_pid(tmp_path: Path) -> None:
    first = launch(LaunchSpec(
        kind="editor",
        instance_name="editor-live-test",
        listen=("tcp://127.0.0.1:0",),
        executable=EDITOR,
        registry_directory=tmp_path,
        detached=True,
        startup_timeout=20.0,
    ))
    robot = first.ensure_robot(DATA / "two_link.urdf", alias="robot")
    cube = first.create_primitive("cube")
    cube.set_scale((0.4, 0.4, 0.2))
    assert cube.transform["scale"] == pytest.approx((0.4, 0.4, 0.2))
    assert robot.joint_state[0].shape == (robot.joint_count,)
    assert first.status()[0] == "stopped"
    robot.set_position((1.0, 2.0, 3.0))
    pid = first.pid
    first.close()

    second = connect(pid, registry_directory=tmp_path)
    try:
        restored = second.require_robot("robot")
        assert restored.object_id == robot.object_id
        assert restored.transform["translation"] == pytest.approx((1.0, 2.0, 3.0))
        assert restored.get_link("tool_link").reference.kind == "link"
        second.shutdown()
    finally:
        second.close()


def _serve(tmp_path: Path, **kwargs: object) -> tuple[HeadlessService, int, object]:
    """Run a real HeadlessService over a loopback socket, as an embedded host would."""
    service = HeadlessService(cache_directory=str(tmp_path / "cache"), **kwargs)  # type: ignore[arg-type]
    server = _LiveTCPServer(("127.0.0.1", 0), _Handler)
    server.service = service  # type: ignore[attr-defined]
    port = int(server.server_address[1])
    service.endpoints = (_tcp_uri("127.0.0.1", port),)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return service, port, server


def test_live_kinematics_contacts_and_limits_round_trip(tmp_path: Path) -> None:
    service, port, server = _serve(tmp_path)
    try:
        session = connect(f"tcp://127.0.0.1:{port}")
        try:
            robot = session.ensure_robot(DATA / "two_link.urdf", alias="robot")
            count = robot.joint_count
            rest = [0.0] * count

            # Link listing replaces probing candidate names one request at a time.
            links = robot.list_links()
            assert links and all(proxy.reference.kind == "link" for _, proxy in links)
            names = [name for name, _ in links]
            assert "tool_link" in names

            # Batch FK: indexed [configuration][link], and evaluated on a private copy of the
            # world, so the live pose is untouched.
            before = robot.joint_state[0].copy()
            batch = robot.kinematics([rest, [0.5] + rest[1:]], ["tool_link"])
            assert len(batch.transforms) == 2
            assert len(batch.transforms[0]) == 1
            pose = batch.transforms[0][0]
            assert len(pose["translation"]) == 3 and len(pose["rotation_wxyz"]) == 4
            assert robot.joint_state[0] == pytest.approx(before)

            # A Jacobian comes back as (configuration_count, 6, joint_count).
            jacobian_batch = robot.kinematics([rest], ["tool_link"], jacobian_link="tool_link")
            assert jacobian_batch.jacobians is not None
            assert jacobian_batch.jacobians.shape == (1, 6, count)

            # Contact pairs for a hypothetical configuration, individually reported.
            contacts = robot.configuration_contacts(rest, distance_threshold=0.01)
            for pair in contacts:
                assert isinstance(pair.colliding, bool)
                assert pair.distance >= 0.0

            # Limits arrive as (joint_count, 2); the URDF declares -1.5..1.5.
            limits = robot.joint_limits
            assert limits.shape == (count, 2)
            assert (limits[:, 0] <= limits[:, 1]).all()
            index = list(robot.joint_names).index("tool_joint")
            assert limits[index] == pytest.approx((-1.5, 1.5))

            # Posing directly bypasses the controller and costs no simulation steps.
            target = list(rest)
            target[index] = 0.4
            robot.set_joint_positions(target, limits="clamp")
            assert robot.joint_state[0][index] == pytest.approx(0.4, abs=1e-6)

            # "clamp" pulls an out-of-range target into range rather than raising.
            over = list(rest)
            over[index] = 99.0
            robot.set_joint_positions(over, limits="clamp")
            assert robot.joint_state[0][index] <= 1.5 + 1e-6

            # Controller overrides must survive the round trip.
            robot.command_joint_positions(rest, gain=5.0, velocity_limit=0.5)

            # Server-side interpolation must match the same tick commands sent
            # individually, including simulation time and finite effort limits.
            saved_segment = session.save_state()
            robot.execute_joint_segment(rest, target, 5, gain=5., velocity_limit=.5,
                                        effort_limits=[.2] * count)
            batched_state = robot.joint_state[0].copy()
            session.restore_state(saved_segment)
            for tick in range(1, 6):
                robot.command_joint_positions([a+(b-a)*tick/5 for a,b in zip(rest,target)],
                                              gain=5., velocity_limit=.5, effort_limits=[.2]*count)
                session.step()
            assert robot.joint_state[0] == pytest.approx(batched_state, abs=1e-10)
            with pytest.raises(LiveControlError):
                robot.execute_joint_segment(rest, target, 101)

            # A direct write leaves the backends holding stale geometry until synced.
            session.sync_physics()
            session.sync_collision()

            # The hello handshake advertises capability groups; the method list comes from
            # capabilities.query. A client that probes before calling must find both.
            assert {"robot.links", "robot.control"} <= session.capabilities
            advertised = set(service._invoke(
                "capabilities.query", {}, session=None)["methods"])  # type: ignore[arg-type]
            assert {"robot.kinematics", "robot.configuration_contacts", "robot.joint_limits",
                    "robot.set_joint_positions", "robot.list_links",
                    "simulation.sync_physics", "simulation.sync_collision",
                    "state.save_file", "state.restore_file"} <= advertised
        finally:
            session.close()
    finally:
        server.shutdown()
        server.server_close()
        service.close()


def test_live_state_file_round_trip_rebinds_objects(tmp_path: Path) -> None:
    service, port, server = _serve(tmp_path)
    try:
        session = connect(f"tcp://127.0.0.1:{port}")
        try:
            cube = session.create_primitive("cube", name="Saved Cube")
            cube.set_position((0.25, 0.0, 0.5))
            path = tmp_path / "scene.simstate"
            assert Path(session.save_state_file(str(path))) == path
            assert path.is_file()

            generation = session.scene_snapshot().scene_generation
            cube.set_position((2.0, 0.0, 0.0))
            session.restore_state_file(str(path))

            # Restoring replaces the ECS, so the generation advances and proxies re-resolve.
            snapshot = session.scene_snapshot()
            assert snapshot.scene_generation > generation
            names = {entity.get("name") for entity in snapshot.entities}
            assert "Saved Cube" in names
        finally:
            session.close()
    finally:
        server.shutdown()
        server.server_close()
        service.close()
