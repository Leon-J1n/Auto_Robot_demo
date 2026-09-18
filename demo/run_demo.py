"""Run structured skill commands with live, sequential target conversion."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import mujoco.viewer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control.gripper_controller import MujocoGripperController
from demo.common.registry import SceneRegistry
from demo.converter import DEFAULT_RUNTIME, DEFAULT_VIEWER, SkillCommandConverter
from demo.runtime import SkillRuntime
from demo.route_param import RouteRecorder


DEFAULT_COMMAND_FILE = Path(__file__).parent / "skill_command" / "config_001.commands.json"
DEFAULT_LOG_FILE = Path(__file__).parent / "run_demo.log"
DEFAULT_ROUTE_FILE = Path(__file__).parent / "route_param.json"
KEY_CODES = {"SPACE": 32, "ENTER": 257}


def run_demo(command_file: str | Path = DEFAULT_COMMAND_FILE, *, generated_config: str | Path | None = None, log_file: str | Path | None = DEFAULT_LOG_FILE, route_file: str | Path | None = DEFAULT_ROUTE_FILE) -> list[dict[str, Any]]:
    """Convert and execute one command at a time on one persistent runtime."""
    command_path = Path(command_file).resolve()
    with command_path.open("r", encoding="utf-8") as stream:
        source = json.load(stream)
    registry_value = source.get("registry")
    if not isinstance(registry_value, str):
        raise ValueError("command document must contain a registry path")
    registry_path = Path(registry_value)
    if not registry_path.is_absolute():
        registry_path = (command_path.parent / registry_path).resolve()
    registry = SceneRegistry(registry_path)
    runtime_config = _deep_merge(DEFAULT_RUNTIME, source.get("runtime", {}))
    viewer_config = _deep_merge(DEFAULT_VIEWER, source.get("viewer", {}))
    request_defaults = _deep_merge(registry.data.get("move_defaults", {}), source.get("request_defaults", {}))
    commands = source.get("commands")
    if not isinstance(commands, list):
        raise ValueError("command document must contain a commands list")

    session = SkillRuntime(registry, runtime_config)
    route_recorder = RouteRecorder(route_file) if route_file is not None else None
    converter = SkillCommandConverter(registry.data, collision_checker=session.approach_checker, pose_provider=session.pose_provider)
    converter.reset()
    allowed_keys = {KEY_CODES[name] for name in viewer_config.get("continue_keys", ["SPACE", "ENTER"])}
    continue_event = threading.Event()

    def on_key(keycode: int) -> None:
        if keycode in allowed_keys:
            continue_event.set()

    results: list[dict[str, Any]] = []
    executed_steps: list[dict[str, Any]] = []
    log_stream = None
    if log_file is not None:
        log_path = Path(log_file).resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_stream = log_path.open("w", encoding="utf-8")

    def write_log(event: Mapping[str, Any]) -> None:
        if log_stream is not None:
            log_stream.write(json.dumps({"timestamp": time.time(), **dict(event)}, ensure_ascii=False) + "\n")
            log_stream.flush()

    write_log({"event": "demo_started", "command_file": str(command_path), "command_count": len(commands)})
    try:
      with mujoco.viewer.launch_passive(session.model, session.data, key_callback=on_key) as viewer:
        _configure_camera(viewer, session.runtime, viewer_config)
        session.runtime.attach_viewer(viewer)
        viewer.sync()
        _hold_viewer(viewer, float(viewer_config.get("initial_pause", 1.0)), session.runtime.playback_fps)
        skip_failed_object = False
        for command_index, command in enumerate(commands, start=1):
            if not viewer.is_running():
                break
            # In random-scene mode a failed object must not abort the whole
            # batch. Skip its remaining grasp/release steps until the fixed
            # home command, then continue with the next object.
            if skip_failed_object:
                parameters = command.get("parameters", {}) if isinstance(command, Mapping) else {}
                if command.get("skill_name") == "move" and parameters.get("target") == "home":
                    skip_failed_object = False
                else:
                    continue
            session.update()
            converted = converter.convert_command(command, command_index)
            if converted is None:
                continue
            steps = converted if isinstance(converted, list) else [converted]
            for step_index, step in enumerate(steps, start=1):
                if not viewer.is_running():
                    break
                label = f"[{command_index}/{len(commands)}]"
                if len(steps) > 1:
                    label += f" ({step_index}/{len(steps)})"
                print(f"\n{label} {step['name']}")
                step_started = time.perf_counter()
                samples: list[dict[str, Any]] = []
                sample_stop = threading.Event()
                def sample_state() -> None:
                    while not sample_stop.is_set():
                        samples.append({"time_from_start_s": time.perf_counter() - step_started, "joint_positions_rad": session.runtime.get_joint_positions().tolist(), "gripper_width_m": session.runtime.get_gripper_width() or 0.0})
                        sample_stop.wait(1.0 / session.runtime.playback_fps)
                sampler = threading.Thread(target=sample_state, daemon=True)
                sampler.start()
                result = session.execute_step(step, request_defaults)
                sample_stop.set()
                sampler.join()
                samples.append({"time_from_start_s": time.perf_counter() - step_started, "joint_positions_rad": session.runtime.get_joint_positions().tolist(), "gripper_width_m": session.runtime.get_gripper_width() or 0.0})
                step_elapsed = time.perf_counter() - step_started
                results.append(result)
                executed_steps.append(deepcopy(step))
                if route_recorder is not None:
                    route_recorder.record_samples(
                        step_index=len(results),
                        step_type=str(step.get("type", "move")),
                        samples=samples,
                    )
                write_log(_build_step_log_event(
                    result=result,
                    step=step,
                    command_index=command_index,
                    step_index=step_index,
                    elapsed=step_elapsed,
                ))
                _print_result(step, result)
                if not result.get("success"):
                    print("当前物体测试失败，回到初始位姿并继续下一个物体。")
                    # Always leave the gripper open before the next object,
                    # including failures that happen before a successful grasp.
                    try:
                        controller = getattr(session.runtime, "gripper_controller", None)
                        if controller is None:
                            controller = MujocoGripperController(session.runtime)
                            session.runtime.gripper_controller = controller
                        controller.open(controller.maximum_width, timeout=5.0)
                        print("夹爪已强制张开。")
                    except Exception as exc:
                        print(f"夹爪复位失败：{exc}")
                    skip_failed_object = True
                    break
                if viewer_config.get("wait_for_key_between_steps", True):
                    continue_event.clear()
                    _wait_for_key(viewer, continue_event, session.runtime.playback_fps)
                else:
                    print(f"{step['name']}完成，自动继续下一步。")
            # A failed object is isolated by skip_failed_object; do not abort
            # the batch. The next home command clears the skip state.
        else:
            print(f"全部 {len(results)} 个步骤完成。")
            if viewer_config.get("keep_open_after_last_test", True):
                print("关闭 MuJoCo 窗口以结束测试。")
                _wait_until_closed(viewer, session.runtime.playback_fps)
        session.runtime.attach_viewer(None)
    finally:
        write_log({"event": "demo_finished", "steps_completed": len(results), "success": bool(results) and all(r.get("success") for r in results)})
        if log_stream is not None:
            log_stream.close()

    if generated_config is not None:
        output = Path(generated_config).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        config = {"scene": registry.data["scene"], "runtime": runtime_config, "viewer": viewer_config, "request_defaults": request_defaults, "steps": executed_steps}
        output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return results


def _configure_camera(viewer: Any, runtime: Any, config: Mapping[str, Any]) -> None:
    viewer.cam.lookat[:] = runtime.model.stat.center
    viewer.cam.distance = max(float(config.get("distance_scale", 1.4)) * runtime.model.stat.extent, 1.0)
    viewer.cam.azimuth = float(config.get("azimuth", 135.0))
    viewer.cam.elevation = float(config.get("elevation", -25.0))


def _wait_for_key(viewer: Any, event: threading.Event, fps: float) -> None:
    while viewer.is_running() and not event.is_set():
        viewer.sync()
        time.sleep(1.0 / fps)


def _wait_until_closed(viewer: Any, fps: float) -> None:
    while viewer.is_running():
        viewer.sync()
        time.sleep(1.0 / fps)


def _hold_viewer(viewer: Any, seconds: float, fps: float) -> None:
    deadline = time.perf_counter() + seconds
    while viewer.is_running() and time.perf_counter() < deadline:
        viewer.sync()
        time.sleep(1.0 / fps)


def _print_result(step: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    if result.get("success"):
        if step.get("type") == "move":
            selection = result.get("selection", {})
            print(f"完成：{selection.get('path_strategy')} / {selection.get('planning_mode')}")
        else:
            print(f"{step.get('type')}完成。")
    else:
        print(json.dumps(result.get("error", result), ensure_ascii=False, indent=2))


def _build_step_log_event(
    *,
    result: Mapping[str, Any],
    step: Mapping[str, Any],
    command_index: int,
    step_index: int,
    elapsed: float,
) -> dict[str, Any]:
    """Build a log event even when optional nested result objects are null."""
    execution_result = _mapping_or_empty(result.get("execution_result"))
    selection = _mapping_or_empty(result.get("selection"))
    trajectory = _mapping_or_empty(result.get("trajectory"))
    return {
        "event": "step",
        "command_index": command_index,
        "step_index": step_index,
        "name": step.get("name"),
        "type": step.get("type", "move"),
        "success": bool(result.get("success")),
        "elapsed": elapsed,
        "selection": {
            key: selection.get(key)
            for key in ("path_strategy", "planning_mode", "fallback_used")
            if key in selection
        },
        "trajectory_points": len(trajectory.get("waypoints", [])),
        "timing": result.get("timing", {}),
        "motion_time": execution_result.get("execution_time", result.get("execution_time")),
        "planning_time": execution_result.get("planning_time", result.get("planning_time")),
        "planned_duration": execution_result.get("planned_duration", result.get("planned_duration")),
        "error": result.get("error"),
    }


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _deep_merge(base: Mapping[str, Any], override: Any) -> dict[str, Any]:
    result = deepcopy(dict(base))
    if not isinstance(override, Mapping):
        return result
    for key, value in override.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commands", type=Path, default=DEFAULT_COMMAND_FILE)
    parser.add_argument("-o", "--generated-config", type=Path)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE, help="逐步执行日志路径；传入空值不可用时可在 API 中设置 log_file=None")
    parser.add_argument("--route-file", type=Path, default=DEFAULT_ROUTE_FILE, help="轨迹参数输出文件")
    parser.add_argument("--random-scene", action="store_true", help="生成并运行随机抓取场景 scene_999")
    parser.add_argument("--random-mode", choices=("contact", "separated"), default="separated")
    parser.add_argument("--random-seed", type=int)
    args = parser.parse_args()
    command_file = args.commands
    if args.random_scene:
        generator_path = Path(__file__).parent / "random-scene" / "generate_scene.py"
        spec = importlib.util.spec_from_file_location("generate_scene_999", generator_path)
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        scene_path, registry_path, command_file = generator.generate(args.random_mode, args.random_seed)
        print(f"随机场景：{scene_path}")
        print(f"交互配置：{registry_path}")
    results = run_demo(command_file, generated_config=args.generated_config, log_file=args.log_file, route_file=args.route_file)
    raise SystemExit(0 if results and all(result.get("success") for result in results) else 1)


if __name__ == "__main__":
    main()
