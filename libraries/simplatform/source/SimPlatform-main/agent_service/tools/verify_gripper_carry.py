"""Real MuJoCo/Live API grasp, lift, translate and release acceptance probe.

Run with --scene pointing to a UR5e/Robotiq .simstate. Uses an isolated platform
instance, never the user's active scene or a remote language model. Exit code 1
means an acceptance condition failed, even if the grasp skill reported success.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import threading
import time

import numpy as np

from sim_platform import Simulator
from sim_platform.live import connect
from sim_platform.server import HeadlessService, _Handler, _LiveTCPServer

from agent_service.planing.trajectory import _retime_polyline
from agent_service.runtime.gripper import LiveGripperController
from agent_service.runtime.live_runtime import LiveSceneRuntime
from agent_service.runtime.executor import execute_commands
from agent_service.skills.manipulation.grasp.strategies.default_grasp.default_grasp import DefaultGrasp
from agent_service.skills.platform_runtime.kinematics import PlatformKinematics
from agent_service.skills.platform_runtime.transforms import rotation_mapping, rpy_to_matrix


def run(args):
    samples = []
    with tempfile.TemporaryDirectory(prefix="sim-gripper-carry-") as cache:
        sim = Simulator(physics="mujoco", collision="fcl", cache_directory=cache)
        world = sim.create_world(fixed_time_step=.002)
        sim.restore_state_file(world, str(args.scene.resolve()))
        service = HeadlessService(simulator=sim, world=world, physics="mujoco", run_simulation=False)
        server = _LiveTCPServer(("127.0.0.1", 0), _Handler)
        server.service = service
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = connect(f"tcp://127.0.0.1:{server.server_address[1]}")
        try:
            runtime = LiveSceneRuntime(session, execution_mode="actuator", fixed_time_step=.002)
            gripper = runtime.gripper_controller = LiveGripperController(runtime)
            initial = runtime.get_end_effector_pose()["position"].copy()
            if args.catalog_pose:
                initial = np.asarray(args.position)
                pose = json.loads(args.object.with_name("grasp_pose.json").read_text(encoding="utf-8"))["poses"][0]
                target = {"position": (initial + pose[:3]).tolist(),
                          "orientation": rotation_mapping(rpy_to_matrix(*pose[3:]))}
                runtime.set_arm_positions(PlatformKinematics(runtime).solve(target, timeout=10.)["solution"])
                runtime.sync_backends()
            support = session.create_primitive("box", name="carry_probe_support", size=(.75, 1.5, .03) if args.web_layout else (.1, .1, .02))
            vertices = np.asarray([list(map(float, line.split()[1:4]))
                                   for line in args.object.read_text(encoding="utf-8").splitlines()
                                   if line.startswith("v ")])
            support.set_position(initial + [0, 0, float(vertices[:, 2].min()) - .01])
            if args.web_layout:
                support.set_position([.525, 0., .585])
                container = session.load_asset("F:/jinliang-agent/demo2/objects/green_box.obj")
                container.set_name("green_box_01")
                container.set_position([.365, .369665, .6])
                container.set_rigid_body(dynamic=False, gravity=False)
            support.set_rigid_body(dynamic=False, gravity=False,
                                   friction=(1, .005, .0001) if args.web_layout else (1, 1, 1))
            ball = session.load_asset(str(args.object.resolve()))
            ball.set_name("carry_probe_ball")
            ball.set_position(initial)
            ball.set_rigid_body(dynamic=True, gravity=True, mass=.2, friction=(1, 1, 1))
            runtime.refresh_scene()
            runtime.sync_backends()

            def measure(stage):
                tcp = runtime.get_end_effector_pose()
                position = np.asarray(ball.transform["translation"])
                sample = {
                    "stage": stage, "object_position": position.tolist(),
                    "tcp_position": tcp["position"].tolist(),
                    "relative_position": (tcp["rotation_matrix"].T @ (position - tcp["position"])).tolist(),
                    "bilateral_contact": bool(gripper._target_clamped("carry_probe_ball")),
                }
                samples.append(sample)
                print(json.dumps(sample), flush=True)
                return sample

            measure("before_grasp")
            grasp_started = time.perf_counter()
            if args.web_layout:
                outcome = execute_commands(runtime, {"commands": [{
                    "command_id": "command-003", "source_skill_step_id": "step-3",
                    "skill_name": "grasp", "parameters": {"target": "carry_probe_ball"}}]}, capture=True)
                print(json.dumps({"web_grasp_success": outcome.success, "failure": outcome.failure,
                                  "frames_captured": outcome.frames_captured}), flush=True)
                if not outcome.success:
                    raise AssertionError(f"Web executor grasp failed: {outcome.failure}")
                result = gripper.get_state("carry_probe_ball")
            else:
                result = DefaultGrasp().execute({
                    "target": {"object_id": "carry_probe_ball"}, "grasp": {"operation": "grasp"},
                    "constraints": {"timeout": 5., "hold_on_success": True},
                    "strategy_params": {"post_contact_steps": args.post_contact_steps,
                                        "grasp_force": args.grasp_force, "hold_force": args.hold_force},
                }, {"runtime": runtime, "gripper_controller": gripper})
            if not result["holding"]:
                raise AssertionError("Grasp skill did not establish bilateral holding contact")
            grasp_wall_seconds = time.perf_counter() - grasp_started
            grasped = measure("grasped")
            motion_evidence = {"samples": 0, "max_relative_drift_m": 0., "contact_loss_samples": 0,
                               "max_contact_gap_samples": 0}
            contact_gap_samples = 0

            def observe_motion():
                nonlocal contact_gap_samples
                tcp = runtime.get_end_effector_pose()
                relative = tcp["rotation_matrix"].T @ (np.asarray(ball.transform["translation"]) - tcp["position"])
                drift = float(np.linalg.norm(relative - grasped["relative_position"]))
                motion_evidence["samples"] += 1
                motion_evidence["max_relative_drift_m"] = max(motion_evidence["max_relative_drift_m"], drift)
                if not gripper._target_clamped("carry_probe_ball"):
                    motion_evidence["contact_loss_samples"] += 1
                    contact_gap_samples += 1
                    motion_evidence["max_contact_gap_samples"] = max(
                        motion_evidence["max_contact_gap_samples"], contact_gap_samples)
                    if motion_evidence["contact_loss_samples"] == 1:
                        print(json.dumps({"contact_loss_at_sample": motion_evidence["samples"], "drift_m": drift,
                                          "contacts": session.physics_contacts()}), flush=True)
                else:
                    contact_gap_samples = 0

            runtime.waypoint_observer = observe_motion
            kin = PlatformKinematics(runtime)
            runtime.max_joint_speed_deg = float(np.degrees(.75))
            execution_wall_seconds = {}
            for stage, delta in (("lift", [0, 0, .1]), ("translate", [args.translation_x, 0, 0])):
                tcp = runtime.get_end_effector_pose()
                target = {"position": (tcp["position"] + delta).tolist(),
                          "orientation": dict(zip(("w", "x", "y", "z"), tcp["quaternion_wxyz"]))}
                goal = kin.solve(target, method="auto", timeout=10.)["solution"]
                times, positions, *_ = _retime_polyline(
                    np.asarray([runtime.get_joint_positions(), goal]), np.full(6, .75),
                    np.full(6, .25), np.full(6, .25), args.trajectory_dt, "ruckig")
                execution_started = time.perf_counter()
                runtime.execute_trajectory({"waypoints": positions, "time_from_start": times,
                                            "duration": float(times[-1])})
                execution_wall_seconds[stage] = time.perf_counter() - execution_started
                sample = measure(stage)
                drift = np.linalg.norm(np.asarray(sample["relative_position"]) - grasped["relative_position"])
                if not sample["bilateral_contact"] or drift > .01:
                    raise AssertionError(f"{stage}: contact lost or relative drift {drift:.6f} m exceeds 10 mm")
            runtime.step(500)
            held = measure("hold_1s")
            if (motion_evidence["max_relative_drift_m"] > .01 or
                    motion_evidence["max_contact_gap_samples"] * args.trajectory_dt > .1):
                raise AssertionError(f"Contact/drift acceptance failed during motion: {motion_evidence}")
            displacement = np.asarray(held["object_position"]) - initial
            if not held["bilateral_contact"] or not (abs(displacement[0] - args.translation_x) <= .01 and .09 <= displacement[2] <= .11):
                raise AssertionError(f"Object did not follow 100 mm lift/translation: {displacement}")
            gripper.release()
            runtime.step(150)
            released = measure("released")
            if released["bilateral_contact"] or released["object_position"][2] >= held["object_position"][2] - .02:
                raise AssertionError("Released object did not separate and fall under gravity")
            return {"passed": True, "samples": samples, "displacement_m": displacement.tolist(),
                    "motion_evidence": motion_evidence, "grasp_wall_seconds": grasp_wall_seconds,
                    "execution_wall_seconds": execution_wall_seconds}
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            service.close()
            sim.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--object", type=Path, default=Path(__file__).resolve().parents[1] / "objects/grasp_objects/baseball/textured.obj")
    parser.add_argument("--post-contact-steps", type=int, default=3)
    parser.add_argument("--grasp-force", type=float, default=40.)
    parser.add_argument("--hold-force", type=float, default=40.)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--catalog-pose", action="store_true", help="Use the object's grasp_pose.json pose 0")
    parser.add_argument("--position", type=float, nargs=3, default=[.6, .067, .65])
    parser.add_argument("--translation-x", type=float, default=.1)
    parser.add_argument("--trajectory-dt", type=float, default=.02)
    parser.add_argument("--web-layout", action="store_true", help="Reproduce reported table/container placement and capture grasp through the web command executor")
    args = parser.parse_args()
    result = run(args)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("PASS: real contact carried the object and release let gravity act.")


if __name__ == "__main__":
    main()
