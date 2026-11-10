"""Dry, non-contact pouring orchestration for the demo layer."""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation
from copy import deepcopy
from demo.task_commands.converter import _deep_merge


def _transform(record):
    q = np.asarray(record["quaternion_xyzw"], dtype=float)
    t = np.eye(4)
    t[:3, :3] = Rotation.from_quat(q).as_matrix()
    t[:3, 3] = np.asarray(record["position"], dtype=float)
    return t


def _pose_request(transform, *, phase):
    quat = Rotation.from_matrix(transform[:3, :3]).as_quat()
    return {
        "target": {
            "type": "pose", "frame": "world",
            "position": dict(zip("xyz", transform[:3, 3].tolist())),
            "orientation": {"representation": "quaternion", **dict(zip("xyzw", quat.tolist()))},
        },
        "motion": {"path_type": ("joint" if phase == "approach" else "linear"), "phase": phase},
        "planning": {"mode": "auto", "allow_replan": True},
    }


class DemoPourExecutor:
    """Execute APPROACH, TILT and RETURN around a virtual bottle outlet."""

    def __init__(self, *, resources, backend, kinematics, execute_motion, synchronizer, checker, report):
        self.resources = resources
        self.backend = backend
        self.kinematics = kinematics
        self.execute_motion = execute_motion
        self.sync = synchronizer
        self.checker = checker
        self.report = report
        self.motion_overrides = {}

    def _tool_world(self):
        fk = self.kinematics.forward(self.backend.get_joint_positions())
        t = np.eye(4)
        t[:3, :3] = fk["rotation_matrix"]
        t[:3, 3] = fk["position"]
        return t

    def _execute_pose(self, pose, phase, angle, row, *, request_phase=None):
        request = _deep_merge(self.motion_overrides, _pose_request(pose, phase=request_phase or phase))
        result = self.execute_motion(request)
        row["substeps"].append({"phase": phase, "tilt_deg": float(angle), "result": result})
        if not result.get("success"):
            row.update(success=False, status="error", error=result.get("error", {
                "error_code": "POUR_MOTION_FAILED", "failed_stage": phase,
                "error_message": "pour waypoint failed"}))
            return False
        self.sync.sync(self.backend, self.kinematics, self.checker)
        return True

    def execute(self, parameters, row):
        self.motion_overrides = deepcopy(parameters.get('request', {}))
        source = str(parameters.get("source", ""))
        target = str(parameters.get("target", ""))
        held = getattr(self.backend.gripper_controller, "held_name", None)
        if not source or not target:
            raise ValueError("pour requires source and target")
        if held != source:
            raise ValueError(f"pour requires {source!r} to be held; currently held={held!r}")
        self.sync.sync(self.backend, self.kinematics, self.checker)
        board = self.sync.blackboard
        source_pose = board.require_pose(source)
        target_pose = board.require_pose(target)

        outlet = np.asarray(parameters.get("outlet_offset_object", [0.0, 0.0, 0.15]), dtype=float)
        opening_axis = np.asarray(parameters.get("opening_axis_object", [0.0, 0.0, 1.0]), dtype=float)
        tilt_axis = np.asarray(parameters.get("tilt_axis_object", [1.0, 0.0, 0.0]), dtype=float)
        for value, name in ((outlet, "outlet_offset_object"), (opening_axis, "opening_axis_object"), (tilt_axis, "tilt_axis_object")):
            if value.shape != (3,) or not np.all(np.isfinite(value)):
                raise ValueError(f"{name} must contain three finite values")
        if np.linalg.norm(opening_axis) < 1e-9 or np.linalg.norm(tilt_axis) < 1e-9:
            raise ValueError("opening_axis_object and tilt_axis_object must be non-zero")
        opening_axis /= np.linalg.norm(opening_axis)
        tilt_axis /= np.linalg.norm(tilt_axis)

        target_offset = np.asarray(parameters.get("target_offset_object", [0.0, 0.0, 0.08]), dtype=float)
        target_world = _transform(target_pose)
        target_point = target_world @ np.r_[target_offset, 1.0]
        target_point[2] += float(parameters.get("clearance_m", 0.08))
        xy_tolerance = float(parameters.get("outlet_xy_tolerance_m", 0.025))
        opening_down_threshold = float(parameters.get("opening_down_threshold", 0.5))
        start_deg = float(parameters.get("start_tilt_deg", 0.0))
        end_deg = float(parameters.get("pour_tilt_deg", 140.0))
        step_deg = float(parameters.get("tilt_step_deg", 10.0))
        if not (0.0 <= start_deg < end_deg <= 180.0 and step_deg > 0.0):
            raise ValueError("invalid pour tilt parameters")

        source_world = _transform(source_pose)
        tool_world = self._tool_world()
        tool_from_source = np.linalg.inv(tool_world) @ source_world
        base_rotation = source_world[:3, :3]
        down = np.array([0.0, 0.0, -1.0])
        row.update(mode="dry_pour", fluid_simulated=False, source=source, target=target,
                   substeps=[], phases=["APPROACH", "TILT", "RETURN"])

        # APPROACH: keep the bottle upright and place its outlet above the target.
        approach = np.eye(4)
        approach[:3, :3] = base_rotation
        approach[:3, 3] = target_point[:3] - base_rotation @ outlet
        approach_outlet = approach[:3, 3] + approach[:3, :3] @ outlet
        approach_xy_error = float(np.linalg.norm(approach_outlet[:2] - target_point[:2]))
        row["approach_outlet_xy_error_m"] = approach_xy_error
        if approach_xy_error > xy_tolerance:
            raise ValueError("APPROACH cannot place the outlet over the target region")
        self.report["active_substep"] = "pour_approach"
        # Lift vertically first so the horizontal approach does not sweep the table.
        lift = approach.copy()
        lift[:3, 3] = source_world[:3, 3]
        lift[:3, 3][2] = max(float(source_world[2, 3] + 0.22), float(approach[2, 3]))
        if not self._execute_pose(lift @ np.linalg.inv(tool_from_source), "approach_lift", start_deg, row, request_phase="approach"):
            return row
        if not self._execute_pose(approach @ np.linalg.inv(tool_from_source), "approach", start_deg, row):
            return row

        # Re-measure the fixed grasp transform after approach.
        self.sync.sync(self.backend, self.kinematics, self.checker)
        source_world = _transform(board.require_pose(source))
        tool_from_source = np.linalg.inv(self._tool_world()) @ source_world
        row["tilt_angles_deg"] = []

        def run_angle(angle, phase):
            relative = Rotation.from_rotvec(tilt_axis * np.deg2rad(angle)).as_matrix()
            source_rotation = base_rotation @ relative
            source_pose = np.eye(4)
            source_pose[:3, :3] = source_rotation
            source_pose[:3, 3] = target_point[:3] - source_rotation @ outlet
            outlet_world = source_pose[:3, 3] + source_rotation @ outlet
            opening_world = source_rotation @ opening_axis
            xy_error = float(np.linalg.norm(outlet_world[:2] - target_point[:2]))
            down_component = float(np.dot(opening_world, down))
            row["tilt_angles_deg"].append(float(angle))
            if xy_error > xy_tolerance:
                row.update(success=False, status="error", error={
                    "error_code": "POUR_OUTLET_DRIFT", "failed_stage": phase,
                    "error_message": f"outlet XY error {xy_error:.4f} m exceeds tolerance"})
                return False
            if phase == "tilt" and angle >= end_deg and down_component < opening_down_threshold:
                row.update(success=False, status="error", error={
                    "error_code": "POUR_OPENING_NOT_DOWN", "failed_stage": phase,
                    "error_message": f"opening down component {down_component:.3f} is below threshold"})
                return False
            self.report["active_substep"] = f"pour_{phase}"
            return self._execute_pose(source_pose @ np.linalg.inv(tool_from_source), phase, angle, row)

        for angle in list(np.arange(start_deg + step_deg, end_deg, step_deg)) + [end_deg]:
            if not run_angle(float(angle), "tilt"):
                return row
        for angle in list(np.arange(end_deg - step_deg, start_deg, -step_deg)) + [start_deg]:
            if not run_angle(float(angle), "return"):
                return row

        row.update(success=True, status="success", max_tilt_deg=end_deg,
                   outlet_xy_tolerance_m=xy_tolerance, opening_down_threshold=opening_down_threshold,
                   waypoint_count=len(row["substeps"]))
        return row

