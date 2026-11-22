"""Tests for the runtime command executor.

These run without a simulator: a fake Live session and fake runtime services stand in
for the platform. What they pin is the behaviour the rest of the service depends on --
the trace chain stays complete, every planned segment produces a timeline entry, the
frame budget is respected, absent hardware fails with a structured code rather than a
fabricated reading, and a failure still returns what was captured before it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
# The platform SDK is not installed in the dev tree; it lives beside the service.
sys.path.insert(0, str(_ROOT / "python"))

from agent_service.runtime.collision import CollisionResult  # noqa: E402
from agent_service.runtime.executor import (DEFAULT_MAX_FRAMES, ExecutionOutcome,  # noqa: E402
                                            execute_commands)
from agent_service.skills import COMMAND_SKILLS  # noqa: E402

ARM_JOINTS = 6
BOX = 20
BOX_COLLIDERS = {20, 21, 22}
PAD = 130


class FakeSnapshot:
    def __init__(self, revision: int) -> None:
        self.revision = revision

    def as_dict(self) -> dict[str, Any]:
        return {"schemaVersion": "1.0", "sceneId": "main", "worldId": 1,
                "sceneGeneration": 0, "revision": self.revision,
                "simulationTime": 0.002 * self.revision, "status": "paused",
                "entities": [{"id": 1, "name": "ur5e", "parent": None}]}


class FakeSession:
    endpoint = "tcp://127.0.0.1:1"

    def __init__(self) -> None:
        self.snapshots = 0

    def scene_snapshot(self) -> FakeSnapshot:
        self.snapshots += 1
        return FakeSnapshot(self.snapshots)


class FakeScene:
    def __init__(self, *, present: bool = True, parented: bool = False,
                 dynamic: bool = True) -> None:
        self.present = present
        self.parented = parented
        self.dynamic = dynamic

    def object_entity(self, name: str) -> int | None:
        return BOX if self.present and name not in {"", "missing"} else None

    def colliders_of(self, entity: int) -> set[int]:
        return set(BOX_COLLIDERS)

    def is_dynamic_object(self, entity: int) -> bool:
        return self.dynamic

    def parent_of(self, entity: int) -> int | None:
        return 1 if self.parented else None

    def name_of(self, entity: int) -> str:
        return f"entity_{entity}"


class FakeProxy:
    def __init__(self) -> None:
        self.position = np.array([0.5, 0.0, 0.1])
        self.writes = 0

    @property
    def transform(self) -> dict[str, tuple[float, ...]]:
        return {"translation": tuple(self.position),
                "rotation_wxyz": (1.0, 0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0)}

    def set_position(self, value: Any) -> None:
        self.position = np.asarray(value, dtype=float)
        self.writes += 1


class FakeRobot:
    def __init__(self) -> None:
        self.contacts: list[dict[str, Any]] = []
        self.contact_calls = 0

    def configuration_contacts(self, positions: Any, distance_threshold: float = 0.0,
                               limits: str = "ignore") -> list[dict[str, Any]]:
        self.contact_calls += 1
        return list(self.contacts)


class FakeRuntime:
    """The subset of LiveSceneRuntime the executor drives."""

    def __init__(self, *, scene: FakeScene | None = None) -> None:
        self.session = FakeSession()
        self.robot = FakeRobot()
        self.scene = scene or FakeScene()
        self.joints = np.zeros(ARM_JOINTS)
        self.joint_limits = np.tile(np.array([-3.2, 3.2]), (ARM_JOINTS, 1))
        self.platform_joint_names = tuple(f"joint_{i}" for i in range(14))
        self.trajectories: list[np.ndarray] = []
        self.commands: list[np.ndarray] = []
        self.writes: list[np.ndarray] = []
        self.steps = 0
        self.syncs = 0
        self.proxy = FakeProxy()
        self.gripper_controller: Any = None
        self.execution_mode = "kinematic"
        self.execution_modes: list[str] = []

    def get_joint_positions(self) -> np.ndarray:
        return self.joints.copy()

    def full_joint_positions(self) -> np.ndarray:
        full = np.zeros(14)
        full[:ARM_JOINTS] = self.joints
        return full

    def invalidate(self) -> None:
        pass

    def command_arm(self, joints: Any) -> None:
        self.joints = np.asarray(joints, dtype=float)
        self.commands.append(self.joints.copy())

    def set_arm_positions(self, joints: Any, *, limits: str = "clamp") -> None:
        self.joints = np.asarray(joints, dtype=float)
        self.writes.append(self.joints.copy())

    def step(self, count: int = 1) -> float:
        self.steps += count
        return 0.002 * self.steps

    def sync_backends(self) -> None:
        self.syncs += 1

    def execute_trajectory(self, trajectory: dict[str, Any], **_: Any) -> dict[str, Any]:
        waypoints = np.asarray(trajectory["waypoints"], dtype=float)
        self.execution_modes.append(self.execution_mode)
        self.trajectories.append(waypoints)
        self.joints = waypoints[-1].copy()
        return {"final_joint_state": self.joints.tolist(), "joint_error": 0.0,
                "servo_limit_applied": False}

    def get_end_effector_pose(self) -> dict[str, Any]:
        return {"position": np.array([0.4, 0.0, 0.5]),
                "quaternion_wxyz": np.array([1.0, 0.0, 0.0, 0.0]),
                "rotation_matrix": np.eye(3)}

    def get_context(self) -> dict[str, Any]:
        return {"current_pose": self.get_end_effector_pose(),
                "current_joint_state": self.get_joint_positions()}

    def object_pose(self, name: str) -> dict[str, Any] | None:
        if self.scene.object_entity(name) is None:
            return None
        return {"position": self.proxy.position.copy(),
                "quaternion_wxyz": np.array([1.0, 0.0, 0.0, 0.0]),
                "rotation_matrix": np.eye(3)}

    def object_proxy(self, name: str) -> FakeProxy | None:
        return self.proxy if self.scene.object_entity(name) is not None else None


class FakeKinematics:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def solve(self, pose: Any, *, seed: Any = None, method: str = "auto",
              timeout: float = 1.0) -> dict[str, Any]:
        if self.fail:
            from agent_service.runtime.kinematics import IKError
            raise IKError("no solution")
        return {"solution": np.array([0.4, 0.1, 0.0, 0.0, 0.0, 0.0]), "solver": "trac_ik"}

    def forward_batch(self, configurations: Any) -> list[dict[str, Any]]:
        values = np.atleast_2d(np.asarray(configurations, dtype=float))
        return [{"position": np.array([0.4 + 0.3 * row[0], 0.1 * row[1], 0.5])}
                for row in values]


class FakeChecker:
    """Blocks a slab the straight path must cross, leaving a detour around it.

    The obstacle only exists while joint 1 is near zero, so lifting joint 1 gets past
    it. A slab blocking every value of joint 1 would be genuinely unsolvable, and the
    RRT would be right to fail rather than exercising the fallback.
    """

    allowed_target_geom_ids = None
    baseline_allowed_pairs: frozenset = frozenset()

    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.baselines = 0

    def refresh_baseline(self) -> None:
        self.baselines += 1

    def check(self, joints: Any, safety_distance: float = 0.0,
              allowed_geom_ids: Any = None) -> CollisionResult:
        values = np.asarray(joints, dtype=float)
        bad = (self.blocked and 0.15 < float(values[0]) < 0.30
               and abs(float(values[1])) < 0.40)
        return CollisionResult(bad, 0.0 if bad else 0.1,
                               ({"geom1": "arm", "geom2": "table"},) if bad else ())

    def check_path(self, waypoints: Any, resolution: float = 0.05,
                   safety_distance: float = 0.0,
                   allowed_geom_ids: Any = None) -> CollisionResult:
        points = np.asarray(waypoints, dtype=float)
        for start, goal in zip(points[:-1], points[1:]):
            count = max(2, int(np.ceil(np.max(np.abs(goal - start)) / resolution)) + 1)
            for alpha in np.linspace(0.0, 1.0, count):
                result = self.check(start + alpha * (goal - start))
                if result.in_collision:
                    return result
        return CollisionResult(False, 0.1, ())


class TargetContactChecker:
    """Reports contact with the manipulated object, as the real checker does.

    Reaching a grasp pose puts the pads around the target, so the platform reports a
    pad-vs-object pair. Only an exemption distinguishes that from a real collision, and
    this fake honours `allowed_target_geom_ids` exactly as `LiveCollisionChecker` does
    so the executor's wiring is what the test exercises.
    """

    allowed_target_geom_ids: set[int] | None = None
    baseline_allowed_pairs: frozenset = frozenset()

    def __init__(self) -> None:
        self.baselines = 0
        self.seen_exemptions: list[Any] = []

    def refresh_baseline(self) -> None:
        self.baselines += 1

    def check(self, joints: Any, safety_distance: float = 0.0,
              allowed_geom_ids: Any = None) -> CollisionResult:
        if allowed_geom_ids is None:
            allowed_geom_ids = self.allowed_target_geom_ids
        self.seen_exemptions.append(
            None if allowed_geom_ids is None else set(allowed_geom_ids))
        if allowed_geom_ids and BOX in allowed_geom_ids:
            return CollisionResult(False, 0.1, ())
        return CollisionResult(True, 0.0, ({"geom1": "robotiq_2f85_left_pad",
                                           "geom2": "green_box"},))

    def check_path(self, waypoints: Any, resolution: float = 0.05,
                   safety_distance: float = 0.0,
                   allowed_geom_ids: Any = None) -> CollisionResult:
        return self.check(np.asarray(waypoints, dtype=float)[0],
                          safety_distance=safety_distance,
                          allowed_geom_ids=allowed_geom_ids)


class FakeGripper:
    maximum_width = 0.085

    def __init__(self, *, contact: bool = True,
                 held: tuple[int, ...] = ()) -> None:
        self.contact = contact
        self.held_colliders = held
        self.closed: list[float] = []
        self.opened: list[float] = []

    def close(self, width: float, **_: Any) -> dict[str, Any]:
        self.closed.append(width)
        return {"target_width": width, "final_width": 0.03,
                "contact_detected": self.contact, "object_present": self.contact,
                "fingers_met": not self.contact}

    def open(self, width: float, **_: Any) -> dict[str, Any]:
        self.opened.append(width)
        return {"target_width": width, "final_width": width,
                "contact_detected": False, "object_present": False}


class ObservingReleaseGripper(FakeGripper):
    def open(self, width: float, **_: Any) -> dict[str, Any]:
        self.opened.append(width)
        observer = getattr(self, "step_observer", None)
        if callable(observer):
            observer()
            observer()
        return {"target_width": width, "final_width": width,
                "contact_detected": False, "object_present": False}


def _command(command_id: str, skill: str, target: str = "green_box",
             step: str | None = None, **parameters: Any) -> dict[str, Any]:
    return {"command_id": command_id, "source_skill_step_id": step or f"step-{command_id}",
            "skill_name": skill, "parameters": {"target": target, **parameters}}


def _run(commands: list[dict[str, Any]], *, runtime: FakeRuntime | None = None,
         checker: FakeChecker | None = None, gripper: FakeGripper | None = None,
         kinematics: FakeKinematics | None = None, **kwargs: Any) -> ExecutionOutcome:
    runtime = runtime or FakeRuntime()
    return execute_commands(
        runtime, {"commands": commands},
        kinematics=kinematics or FakeKinematics(),
        collision_checker=checker or FakeChecker(),
        gripper=gripper or FakeGripper(), **kwargs)


def test_locate_is_skipped_but_still_reported() -> None:
    # The trace chain skill_step_id -> command_id -> runtime_step_id must not have a
    # hole where a compile-time skill sat.
    outcome = _run([_command("command-001", "locate")])
    assert outcome.success
    step = outcome.steps[0]
    assert step["result"]["skipped"] is True
    assert step["command_id"] == "command-001"
    assert step["source_skill_step_id"] == "step-command-001"
    assert step["runtime_step_id"]
    # A locate resolves nothing at runtime, so it moved no object.
    assert outcome.affected_objects == []
    assert COMMAND_SKILLS.module_for("locate") == (
        "agent_service.skills.perception.location.command"
    )


def test_move_uses_the_direct_planner_when_the_path_is_clear() -> None:
    runtime = FakeRuntime()
    outcome = _run([_command("command-001", "move")], runtime=runtime)
    assert outcome.success
    result = outcome.steps[0]["result"]
    assert result["planner"] == "direct"
    assert result["direct_probe"] == "accepted"
    assert runtime.trajectories


def test_move_with_held_object_uses_physics_and_restores_runtime_mode() -> None:
    runtime = FakeRuntime()
    outcome = _run([_command("command-001", "move")], runtime=runtime,
                   gripper=FakeGripper(held=(BOX,)))
    assert outcome.success
    assert runtime.execution_modes
    assert set(runtime.execution_modes) == {"actuator"}
    assert runtime.execution_mode == "kinematic"


@pytest.mark.parametrize(("skill_name", "class_name", "result_field"), [
    ("move", "MoveSkill", "execution_result"),
    ("grasp", "GraspSkill", "grasp_result"),
    ("release", "ReleaseSkill", "release_result"),
    ("press", "PressSkill", "press_result"),
    ("push", "PushPullSkill", "push_pull_result"),
    ("pull", "PushPullSkill", "push_pull_result"),
])
def test_live_runtime_dispatches_into_restored_skill_pipeline(
        monkeypatch, skill_name: str, class_name: str, result_field: str) -> None:
    module = __import__(COMMAND_SKILLS.module_for(skill_name), fromlist=[class_name])
    requests: list[dict[str, Any]] = []

    class RecordingSkill:
        def __init__(self, *_, **kwargs):
            self.kinematics = FakeKinematics()

        def execute(self, request):
            requests.append(request)
            result = {"success": True, "selection": {}}
            if skill_name == "move":
                result.update({"trajectory": {"waypoints": []}, result_field: {}})
            else:
                result[result_field] = {"pipeline": class_name}
            return result

    monkeypatch.setattr(module, class_name, RecordingSkill)
    runtime = FakeRuntime()
    runtime.qpos_indices = {}
    gripper = FakeGripper()
    gripper.get_state = lambda *_: {}
    outcome = _run([_command("command-001", skill_name)], runtime=runtime,
                   gripper=gripper)

    assert outcome.success
    assert requests
    if skill_name != "move":
        assert requests[0]["target"]["object_id"] == "green_box"


def test_move_adapter_uses_actuator_mode_when_object_is_held(monkeypatch) -> None:
    module = __import__(COMMAND_SKILLS.module_for("move"), fromlist=["MoveSkill"])
    modes: list[str] = []

    class RecordingSkill:
        def __init__(self, *_, **kwargs):
            self.kinematics = FakeKinematics()

        def execute(self, request):
            modes.append(runtime.execution_mode)
            return {"success": True, "selection": {},
                    "trajectory": {"waypoints": []}, "execution_result": {}}

    monkeypatch.setattr(module, "MoveSkill", RecordingSkill)
    runtime = FakeRuntime()
    runtime.qpos_indices = {}
    outcome = _run([_command("command-001", "move")], runtime=runtime,
                   gripper=FakeGripper(held=(BOX,)))

    assert outcome.success
    assert modes == ["actuator"]
    assert runtime.execution_mode == "kinematic"


def test_legacy_linear_move_parameter_maps_to_linear_path(monkeypatch) -> None:
    module = __import__(COMMAND_SKILLS.module_for("move"), fromlist=["MoveSkill"])
    requests: list[dict[str, Any]] = []

    class RecordingSkill:
        def __init__(self, *_, **kwargs):
            self.kinematics = FakeKinematics()

        def execute(self, request):
            requests.append(request)
            return {"success": True, "selection": {},
                    "trajectory": {"waypoints": []}, "execution_result": {}}

    monkeypatch.setattr(module, "MoveSkill", RecordingSkill)
    runtime = FakeRuntime()
    runtime.qpos_indices = {}
    outcome = _run([_command("command-001", "move", planning_method="linear")],
                   runtime=runtime)
    assert outcome.success
    assert requests[0]["planning"]["mode"] == "auto"
    assert requests[0]["motion"]["path_type"] == "linear"


def test_static_move_defaults_to_elevated_pre_approach_only(monkeypatch) -> None:
    module = __import__(COMMAND_SKILLS.module_for("move"), fromlist=["MoveSkill"])
    requests: list[dict[str, Any]] = []

    class RecordingSkill:
        def __init__(self, *_, **kwargs):
            self.kinematics = FakeKinematics()

        def execute(self, request):
            requests.append(request)
            return {"success": True, "selection": {"planning_mode": "direct"},
                    "trajectory": {"waypoints": []}, "execution_result": {}}

    monkeypatch.setattr(module, "MoveSkill", RecordingSkill)
    runtime = FakeRuntime(scene=FakeScene(dynamic=False))
    runtime.qpos_indices = {}
    outcome = _run([_command("command-001", "move")], runtime=runtime)

    assert outcome.success
    assert len(requests) == 1
    assert requests[0]["target"]["position"]["z"] == pytest.approx(0.25)
    # Auto is resolved by MoveSkill's selector, not by the command adapter.
    assert requests[0]["motion"]["path_type"] == "auto"
    assert outcome.steps[0]["result"]["target_adjustment"] == {
        "applied": True,
        "offset_world_m": [0.0, 0.0, 0.15],
        "reason": "static_target_pre_approach_only",
    }


def test_static_move_can_restore_vertical_descent(monkeypatch) -> None:
    module = __import__(COMMAND_SKILLS.module_for("move"), fromlist=["MoveSkill"])
    requests: list[dict[str, Any]] = []

    class RecordingSkill:
        def __init__(self, *_, **kwargs):
            self.kinematics = FakeKinematics()

        def execute(self, request):
            requests.append(request)
            return {"success": True, "selection": {"planning_mode": "direct"},
                    "trajectory": {"waypoints": []}, "execution_result": {}}

    monkeypatch.setattr(module, "MoveSkill", RecordingSkill)
    runtime = FakeRuntime(scene=FakeScene(dynamic=False))
    runtime.qpos_indices = {}
    outcome = _run([_command("command-001", "move",
                            disable_vertical_move_when_static_item=False)], runtime=runtime)

    assert outcome.success
    assert len(requests) == 2
    assert requests[1]["target"]["position"]["z"] == pytest.approx(0.10)
    assert requests[1]["motion"]["path_type"] == "linear"
    assert requests[1]["motion"]["phase"] == "approach"
    assert requests[1]["motion"]["path_constraint"] == "hard"


def test_every_executable_skill_has_an_adapter_in_its_skill_package() -> None:
    expected = {
        "locate": ".skills.perception.location.",
        "move": ".skills.motion.move.",
        "grasp": ".skills.manipulation.grasp.",
        "release": ".skills.manipulation.release.",
        "press": ".skills.manipulation.press.",
        "push": ".skills.manipulation.push_pull.",
        "pull": ".skills.manipulation.push_pull.",
    }
    assert COMMAND_SKILLS.names == frozenset(expected)
    for skill_name, package in expected.items():
        assert package in COMMAND_SKILLS.module_for(skill_name)


def test_move_falls_back_to_rrt_only_after_probing_direct() -> None:
    outcome = _run([_command("command-001", "move")], checker=FakeChecker(blocked=True))
    assert outcome.success
    result = outcome.steps[0]["result"]
    assert result["planner"] == "collision_free"
    assert result["direct_probe"] == "rejected"
    assert result["direct_rejection"]


def test_every_planned_segment_becomes_one_timeline_entry() -> None:
    outcome = _run([_command("command-001", "move"), _command("command-002", "move")])
    assert outcome.success
    assert len(outcome.segments) == 2
    ids = [segment.id for segment in outcome.segments]
    assert len(set(ids)) == 2
    colors = {segment.color for segment in outcome.segments}
    assert len(colors) == 2
    for segment in outcome.segments:
        assert segment.marker_count >= 2


def test_frames_bracket_the_run_and_respect_the_budget() -> None:
    runtime = FakeRuntime()
    outcome = _run([_command("command-001", "move")], runtime=runtime, max_frames=4)
    assert 2 <= len(outcome.frames) <= 4
    assert outcome.frames_captured == len(outcome.frames)
    # Passed through unmodified: the gateway validates this envelope.
    assert outcome.frames[0]["schemaVersion"] == "1.0"
    assert outcome.frames[0]["sceneId"] == "main"
    assert "entities" in outcome.frames[0]
    # No marker injection happens here; that is the publisher's job.
    assert all("trajectory" not in frame for frame in outcome.frames)


def test_move_is_resampled_for_at_least_50_fps_capture() -> None:
    outcome = _run([_command("command-001", "move")])
    result = outcome.steps[0]["result"]
    assert result["execution_waypoint_count"] >= int(result["duration"] * 50.0) + 1
    assert result["capture_stride"] == 1


def test_frame_budget_is_spread_across_several_motions() -> None:
    commands = [_command(f"command-{i:03d}", "move") for i in range(1, 5)]
    outcome = _run(commands, max_frames=12)
    assert outcome.success
    assert len(outcome.frames) <= 12
    # The run still executed every command even though capture is bounded.
    assert outcome.commands_completed == 4


def test_capture_can_be_disabled_without_affecting_execution() -> None:
    runtime = FakeRuntime()
    outcome = _run([_command("command-001", "move")], runtime=runtime, capture=False)
    assert outcome.frames == []
    assert outcome.success
    assert runtime.trajectories
    assert runtime.session.snapshots == 0


def test_grasp_reports_the_clamp_and_refreshes_the_baseline() -> None:
    checker = FakeChecker()
    gripper = FakeGripper(contact=True)
    outcome = _run([_command("command-001", "grasp", width=0.0)],
                   checker=checker, gripper=gripper)
    assert outcome.success
    assert outcome.steps[0]["result"]["contact_detected"] is True
    # A carried object changes which contacts are normal, so the baseline must move.
    assert checker.baselines == 1
    assert outcome.affected_objects == ["green_box"]


def test_grasp_on_nothing_fails() -> None:
    outcome = _run([_command("command-001", "grasp")], gripper=FakeGripper(contact=False))
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("EXECUTION_FAILED")
    assert outcome.failure["command_id"] == "command-001"


def test_release_opens_and_refreshes_the_baseline() -> None:
    checker = FakeChecker()
    gripper = FakeGripper()
    outcome = _run([_command("command-001", "release")], checker=checker, gripper=gripper)
    assert outcome.success
    assert gripper.opened == [FakeGripper.maximum_width]
    assert checker.baselines == 1


def test_release_installs_timeline_sampling_observer() -> None:
    outcome = _run([_command("command-001", "release")],
                   gripper=ObservingReleaseGripper())
    assert outcome.success
    # Initial frame + at least one frame captured while the release motion runs.
    assert len(outcome.frames) >= 2


def test_force_mode_press_fails_instead_of_inventing_a_reading() -> None:
    outcome = _run([_command("command-001", "press", control_mode="force",
                             target_force=5.0)])
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("SENSOR_DATA_UNAVAILABLE")
    assert outcome.failure["recoverable"] is True
    assert "force" in outcome.failure["error_message"].lower()


def test_displacement_press_reports_zero_force_and_no_feedback() -> None:
    runtime = FakeRuntime()
    runtime.robot.contacts = [{"robot_collider": PAD, "other": 22,
                               "distance": 0.0, "colliding": True}]
    outcome = _run([_command("command-001", "press", direction=[0, 0, -1])],
                   runtime=runtime)
    assert outcome.success
    result = outcome.steps[0]["result"]
    assert result["contact_detected"] is True
    assert result["force_feedback_available"] is False
    assert result["peak_force"] == 0.0


def test_press_that_never_touches_reports_depth_not_reached() -> None:
    outcome = _run([_command("command-001", "press", maximum_travel=0.01, step=0.005)])
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("TARGET_DEPTH_NOT_REACHED")


def test_mechanism_push_pull_is_refused() -> None:
    for skill in ("push", "pull"):
        outcome = _run([_command("command-001", skill, interaction_mode="mechanism")])
        assert not outcome.success
        assert outcome.failure["error_code"].endswith("PUSH_PULL_UNSUPPORTED")
        assert outcome.failure["recoverable"] is False


def test_free_object_push_moves_the_target() -> None:
    runtime = FakeRuntime()
    runtime.robot.contacts = [{"robot_collider": PAD, "other": 22,
                               "distance": 0.0, "colliding": True}]
    start = runtime.proxy.position.copy()
    outcome = _run([_command("command-001", "push", direction=[1, 0, 0], distance=0.02)],
                   runtime=runtime)
    assert outcome.success
    result = outcome.steps[0]["result"]
    assert result["target_moved"] is True
    assert result["travel_distance"] == pytest.approx(0.02, abs=1e-6)
    assert runtime.proxy.position[0] > start[0]
    # Object writes must be followed by a sync or contact queries read a stale pose.
    assert runtime.syncs >= runtime.proxy.writes


def test_pull_drives_the_opposite_way_from_push() -> None:
    runtime = FakeRuntime()
    runtime.robot.contacts = [{"robot_collider": PAD, "other": 22,
                               "distance": 0.0, "colliding": True}]
    start = runtime.proxy.position.copy()
    outcome = _run([_command("command-001", "pull", direction=[1, 0, 0], distance=0.02)],
                   runtime=runtime)
    assert outcome.success
    assert runtime.proxy.position[0] < start[0]


def test_push_without_an_existing_contact_fails() -> None:
    outcome = _run([_command("command-001", "push", distance=0.02)])
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("EXECUTION_FAILED")


def test_push_on_a_parented_entity_is_refused() -> None:
    runtime = FakeRuntime(scene=FakeScene(parented=True))
    outcome = _run([_command("command-001", "push")], runtime=runtime)
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("PUSH_PULL_UNSUPPORTED")


def test_missing_target_is_reported_as_such() -> None:
    runtime = FakeRuntime(scene=FakeScene(present=False))
    outcome = _run([_command("command-001", "move", target="missing")], runtime=runtime)
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("TARGET_NOT_FOUND")


def test_ik_failure_is_reported_as_ik_failed() -> None:
    outcome = _run([_command("command-001", "move")], kinematics=FakeKinematics(fail=True))
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("IK_FAILED")
    assert outcome.failure["recoverable"] is True


def test_unsupported_skill_is_rejected() -> None:
    outcome = _run([{"command_id": "command-001", "source_skill_step_id": "step-1",
                     "skill_name": "teleport", "parameters": {"target": "green_box"}}])
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("UNSUPPORTED_SKILL")


def test_failure_stops_the_run_but_keeps_what_was_captured() -> None:
    commands = [_command("command-001", "move"),
                _command("command-002", "grasp"),
                _command("command-003", "move")]
    outcome = _run(commands, gripper=FakeGripper(contact=False))
    assert not outcome.success
    # Fail fast: the third command must never have been attempted.
    assert outcome.commands_started == 2
    assert outcome.commands_completed == 1
    assert [step["command_id"] for step in outcome.steps] == ["command-001", "command-002"]
    assert outcome.steps[-1]["success"] is False
    # The user still sees how far the robot got.
    assert outcome.frames
    assert len(outcome.segments) == 1
    assert outcome.failure["runtime_step_id"] == outcome.steps[-1]["runtime_step_id"]


def test_move_to_home_needs_no_scene_target() -> None:
    runtime = FakeRuntime(scene=FakeScene(present=False))
    outcome = _run([_command("command-001", "move", target="home")], runtime=runtime)
    assert outcome.success
    assert np.allclose(runtime.get_joint_positions(), np.zeros(ARM_JOINTS))
    assert outcome.affected_objects == []


def test_move_config_named_states_are_read_from_robot_section() -> None:
    from agent_service.skills.motion.move.strategies.joint_move.joint_move import JointMove
    strategy = JointMove({"robot": {"named_states": {"home": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]}}},
                         kinematics=type("Kinematics", (), {"limits": np.tile([[-6.28, 6.28]], (6, 1))})())
    request = {"target": {"type": "named_state", "state_name": "home"},
               "constraints": {"avoid_collision": False}, "strategy_params": {},
               "planning": {"planning_time": 1.0}}
    path = strategy.build_path_request(request, {"current_joint_state": np.zeros(6)})
    assert np.allclose(path["goal"], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])


def test_explicit_joint_configuration_skips_ik() -> None:
    goal = [0.2, 0.1, 0.0, 0.0, 0.0, 0.0]
    runtime = FakeRuntime()
    outcome = _run([_command("command-001", "move", joint_configuration=goal)],
                   runtime=runtime, kinematics=FakeKinematics(fail=True))
    assert outcome.success
    assert np.allclose(runtime.get_joint_positions(), goal)


def test_step_reports_carry_timing_and_the_full_trace_chain() -> None:
    outcome = _run([_command("command-001", "move", step="skill-step-7")])
    step = outcome.steps[0]
    assert set(step) >= {"runtime_step_id", "command_id", "source_skill_step_id",
                         "skill", "target", "success", "duration_seconds", "result",
                         "started_at", "finished_at"}
    assert step["source_skill_step_id"] == "skill-step-7"
    assert step["skill"] == "move"
    assert step["target"] == "green_box"
    assert step["duration_seconds"] >= 0.0


def test_outcome_serializes_to_plain_data() -> None:
    outcome = _run([_command("command-001", "move")])
    value = outcome.as_dict()
    assert value["success"] is True
    assert isinstance(value["segments"][0]["points"], list)
    assert isinstance(value["segments"][0]["color"], list)
    assert value["commands_total"] == 1


def test_empty_command_list_is_a_no_op_with_bracket_frames() -> None:
    outcome = _run([])
    assert outcome.success
    assert outcome.commands_total == 0
    assert outcome.steps == []
    assert len(outcome.frames) == 2


def test_default_capture_has_no_legacy_frame_limit() -> None:
    assert DEFAULT_MAX_FRAMES is None


# -- manipulated-object exemption ------------------------------------------------
#
# Verified against a live simulator: reaching a grasp pose reports
# `robotiq_2f85_left_pad::collision vs red_block_01`, which without an exemption
# fails the RRT's final validation gate and no path can ever satisfy it.

def test_a_move_may_touch_the_object_it_is_manipulating() -> None:
    checker = TargetContactChecker()
    outcome = _run([_command("command-001", "move")], checker=checker)
    assert outcome.success
    assert outcome.steps[0]["result"]["exempt_colliders"] == sorted(BOX_COLLIDERS)


def test_the_exemption_covers_a_carried_object_too() -> None:
    # A held object must be excused against the destination it is lowered into, which
    # the gripper's own pad exemption does not cover.
    checker = TargetContactChecker()
    runtime = FakeRuntime(scene=FakeScene(present=False))
    outcome = execute_commands(
        runtime, {"commands": [_command("command-001", "move", target="home")]},
        kinematics=FakeKinematics(), collision_checker=checker,
        gripper=FakeGripper(held=(BOX,)))
    assert outcome.success
    # The target resolves to nothing here, so only the held colliders are excused.
    assert outcome.steps[0]["result"]["exempt_colliders"] == [BOX]


def test_the_exemption_does_not_outlive_the_motion() -> None:
    checker = TargetContactChecker()
    outcome = _run([_command("command-001", "move")], checker=checker)
    assert outcome.success
    # Left set, it would silently excuse the object in every later check.
    assert checker.allowed_target_geom_ids is None
    assert checker.check(np.zeros(6)).in_collision is True


def test_a_collision_unrelated_to_the_target_still_fails() -> None:
    # The exemption must not become a blanket pass: the arm hitting the table is
    # still a collision while the target is excused.
    class TableChecker(TargetContactChecker):
        def check(self, joints: Any, safety_distance: float = 0.0,
                  allowed_geom_ids: Any = None) -> CollisionResult:
            return CollisionResult(True, 0.0, ({"geom1": "upper_arm_link",
                                               "geom2": "work_table"},))

    outcome = _run([_command("command-001", "move")], checker=TableChecker())
    assert not outcome.success
    assert outcome.failure["error_code"].endswith("LOCAL_COLLISION_DETECTED")


def test_a_planning_failure_names_the_offending_pair() -> None:
    # Diagnosing this from a live run was impossible while the message said only
    # "Final RRT path failed collision validation".
    class TableChecker(TargetContactChecker):
        def check(self, joints: Any, safety_distance: float = 0.0,
                  allowed_geom_ids: Any = None) -> CollisionResult:
            return CollisionResult(True, 0.0, ({"geom1": "upper_arm_link",
                                               "geom2": "work_table"},))

    outcome = _run([_command("command-001", "move")], checker=TableChecker())
    message = outcome.failure["error_message"]
    assert "upper_arm_link" in message and "work_table" in message
