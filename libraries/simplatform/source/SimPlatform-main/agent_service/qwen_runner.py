"""The OpenAI-compatible (Qwen) model runner and the turn pipeline it drives.

Layering, per design doc §4.2: this module may use the model for natural-language understanding
and task decomposition only. Everything downstream of a `SkillPlan` -- command compilation, IK,
collision checking, trajectories -- is deterministic and never consults the model, so the same
plan and scene always produce the same commands.

Route selection follows scene occupancy: any non-light entity selects Route B and
preserves the bound world. Only an empty or light-only scene is generated (Route A).
Missing task objects in an existing scene are reported rather than replaced.

Configuration comes from the environment and is never written into repo files:
`QWEN_BASE_URL`, `QWEN_MODEL`, `QWEN_API_KEY` (with `ALIYUN_API_KEY` accepted as a fallback for
the key). A missing base URL or model name marks the runner `MODEL_UNAVAILABLE`; `/health` still
reports ok so the Gateway can tell "online but unconfigured" from "down", and turn submission
then fails with an explicit blocker instead of silently degrading.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gateway_client import (GatewayClient, GatewayError, TrajectorySegment,
                             normalize_frame_times)
from .runner import (EmitEvent, PRESENTATION_REQUESTED, TURN_DEBUG, TURN_PROGRESS, TurnBlocked,
                     TurnOutcome, TurnRequest)

LOGGER = logging.getLogger("agent_service.runner")

# Every Chinese reply that published a scene ends with this sentence (design doc §9).
SYNC_SUFFIX = "场景同步完成。"

PHASES = ("scene", "understand", "plan", "compile", "execute", "publish")
PHASE_TITLES = {"scene": "检查场景", "understand": "理解指令", "plan": "分解步骤",
                "compile": "编译命令", "execute": "执行动作", "publish": "同步场景"}
DEBUG_REUSE_PREFIX = "DEBUG"
DEBUG_SCENE_SEED_RE = re.compile(r"^DEBUG\s*(-?\d+)\s*$", re.IGNORECASE)


class _PlanTracker:
    """Drives the plan rows the Agent panel renders from debug events."""

    def __init__(self, emit: EmitEvent, enabled: bool):
        self._emit = emit
        self._enabled = enabled
        self._rows = [{"title": PHASE_TITLES[name], "status": "pending"} for name in PHASES]
        self._current = "scene"

    async def enter(self, phase: str) -> None:
        index = PHASES.index(phase)
        self._current = phase
        for row in self._rows[:index]:
            if row["status"] != "failed":
                row["status"] = "completed"
        self._rows[index]["status"] = "in_progress"
        await self._emit(TURN_PROGRESS, {"phase": phase, "status": "in_progress",
                                         "title": PHASE_TITLES[phase]})
        await self._publish(phase=phase, status="in_progress")

    async def complete(self, phase: str, *, details: dict[str, Any] | None = None) -> None:
        self._rows[PHASES.index(phase)]["status"] = "completed"
        await self._emit(TURN_PROGRESS, {"phase": phase, "status": "completed",
                                         "title": PHASE_TITLES[phase]})
        await self._publish(phase=phase, status="completed", details=details)

    async def fail(self, phase: str, failure: dict[str, Any],
                   *, details: dict[str, Any] | None = None) -> None:
        self._rows[PHASES.index(phase)]["status"] = "failed"
        await self._emit(TURN_PROGRESS, {"phase": phase, "status": "failed",
                                         "title": PHASE_TITLES[phase],
                                         "failure": failure})
        payload = dict(details or {})
        payload["failure"] = failure
        await self._publish(phase=phase, status="failed", details=payload)

    async def _publish(self, *, phase: str, status: str,
                       details: dict[str, Any] | None = None) -> None:
        if self._enabled:
            # The panel mines a plan/steps array out of each debug payload; without one the
            # todo rows stay empty.
            await self._emit(TURN_DEBUG, {
                "type": "phase", "phase": phase, "status": status,
                "title": PHASE_TITLES[phase], "details": details or {},
                "plan": [dict(row) for row in self._rows],
            })


@dataclass
class PlatformAgentRunner:
    """Runs one turn: sync the scene, decompose it, plan paths, execute, publish."""

    config: Any = None
    # Injected in tests to replace the provider and the startup self-check.
    provider: Any = None

    def __post_init__(self) -> None:
        if self.config is None:
            from .planning.qwen_client import load_model_config

            self.config = load_model_config()
        if not self._availability().available:
            LOGGER.warning("model is unconfigured; turns will report MODEL_UNAVAILABLE")

    # -- health ----------------------------------------------------------------------

    def _availability(self) -> Any:
        from .planning.qwen_client import check_model_availability

        return check_model_availability(self.config)

    def model_status(self) -> dict[str, Any]:
        availability = self._availability()
        if hasattr(availability, "as_public_dict"):
            return availability.as_public_dict()
        return {"status": "ok" if availability.available else "MODEL_UNAVAILABLE"}

    def _require_provider(self) -> Any:
        if self.provider is not None:
            return self.provider
        availability = self._availability()
        if not availability.available:
            raise TurnBlocked(
                "MODEL_UNAVAILABLE",
                "模型未配置：请设置 QWEN_BASE_URL 与 QWEN_MODEL 环境变量后重试。")
        from .planning.qwen_client import QwenHTTPProvider

        self.provider = QwenHTTPProvider.from_config(self.config)
        return self.provider

    # -- turn ------------------------------------------------------------------------

    async def run(self, request: TurnRequest, emit: EmitEvent) -> TurnOutcome:
        context = request.control_context
        endpoint = (getattr(context, "liveEndpoint", None) if context else None) or ""
        if not endpoint:
            # Connect-only: no implicit substitute instance, ever.
            raise TurnBlocked(
                "SESSION_NOT_BOUND",
                "没有绑定的仿真会话：Gateway 未提供 Live endpoint，服务不会自行启动替代实例。")

        tracker = _PlanTracker(emit, request.debug)
        session = None
        runtime = None
        snapshot_revision: int | None = None
        reuse_model_result = request.message.startswith(DEBUG_REUSE_PREFIX)
        workdir = Path(tempfile.mkdtemp(prefix=f"turn-{request.turn_id[:8]}-"))
        try:
            await tracker.enter("scene")
            session = await asyncio.to_thread(self._connect, endpoint)
            await tracker.complete("scene", details={"route": "pending"})

            await tracker.enter("understand")
            if reuse_model_result:
                intent = self._cached_intent(request)
                LOGGER.debug("reusing cached Qwen understanding for conversation %.8s",
                             request.conversation_id)
            else:
                intent = await asyncio.to_thread(self._understand_for_turn, request)
            await tracker.complete("understand", details={
                "taskTypes": [str(getattr(item, "value", item)) for item in
                              (getattr(intent, "task_types", ()) or ())],
                "entities": [str(getattr(item, "semantic_name", "")) for item in
                             (getattr(intent, "entities", ()) or ())],
            })

            await tracker.enter("plan")
            # Scene resolution runs on the bare session, before the runtime exists: the
            # execution runtime requires a robot, and on Route A the robot only arrives with
            # the base scene this step restores. Building it first can never bootstrap an
            # empty world.
            placement_seed = self._debug_scene_seed(request.message)
            if placement_seed is None:
                # Keep the narrow legacy/test override contract for ordinary turns.
                plan, applied = await asyncio.to_thread(self._resolve_scene, session, intent)
            else:
                plan, applied = await asyncio.to_thread(
                    self._resolve_scene, session, intent, placement_seed=placement_seed)
            if applied is not None:
                # Route A first makes the composed world visible. Execution then publishes a
                # second presentation containing the motion timeline and trajectory markers.
                snapshot_revision = await self._publish_snapshot(context, emit)
            runtime = await asyncio.to_thread(self._build_runtime, session)
            task = await asyncio.to_thread(self._ground, intent, plan)
            if reuse_model_result:
                skill_plan = await asyncio.to_thread(
                    self._skill_plan_from_cache, task, request)
            else:
                skill_plan = await asyncio.to_thread(
                    self._skill_plan_for_turn, task, request)
            await tracker.complete("plan", details={
                "route": "A" if applied is not None else "B",
                "composed": applied is not None,
                "presentationRevision": snapshot_revision,
                "steps": _skill_step_summaries(skill_plan),
            })

            await tracker.enter("compile")
            commands = await asyncio.to_thread(self._compile, skill_plan, task, plan, workdir)
            await tracker.complete("compile", details={
                "commandCount": len(getattr(commands, "commands", ()) or ()),
                "commands": _command_summaries(commands),
            })

            await tracker.enter("execute")
            outcome = await self._execute_observed(runtime, commands, tracker)
            execute_details = {
                "commandsTotal": getattr(outcome, "commands_total", 0),
                "commandsCompleted": getattr(outcome, "commands_completed", 0),
                "framesCaptured": len(getattr(outcome, "frames", ()) or ()),
                "steps": list(getattr(outcome, "steps", ()) or ()),
            }
            if not getattr(outcome, "success", True):
                execute_details["failure"] = dict(getattr(outcome, "failure", {}) or {})
            if getattr(outcome, "success", True):
                await tracker.complete("execute", details=execute_details)
            else:
                await tracker.fail("execute", dict(getattr(outcome, "failure", {}) or {}),
                                   details=execute_details)

            await tracker.enter("publish")
            frames = normalize_frame_times(list(outcome.frames))
            segments = _segments(outcome.segments)
            has_motion = _frames_show_motion(frames)
            if applied is not None and not has_motion and not getattr(outcome, "success", True):
                revision = snapshot_revision
            else:
                revision = await self._publish(context, session, frames, segments, emit)
            published_frame_count = len(frames) if has_motion else 0
            await tracker.complete("publish", details={"kind": "timeline" if has_motion else "snapshot",
                                                        "frameCount": published_frame_count,
                                                        "presentationRevision": revision})

            affected = list(getattr(outcome, "affected_objects", ()) or ())
            if not getattr(outcome, "success", True):
                failure = dict(getattr(outcome, "failure", {}) or {})
                message = self._failure_reply(failure, segments, published_frame_count,
                                              published=revision is not None)
                return TurnOutcome(status="failed", message=message,
                                   error_code=str(failure.get("error_code")
                                                  or "EXECUTION_FAILED"),
                                   presentation_revision=revision,
                                   affected_objects=affected)
            return TurnOutcome(status="succeeded",
                               message=self._reply(intent, task, segments, published_frame_count,
                                                   applied),
                               presentation_revision=revision, affected_objects=affected)
        except TurnBlocked as error:
            await tracker.fail(getattr(tracker, "_current", "scene"),
                               {"code": error.code, "message": error.message})
            raise
        except Exception as error:
            await tracker.fail(getattr(tracker, "_current", "scene"),
                               {"code": type(error).__name__, "message": str(error)})
            raise
        finally:
            # The runtime borrows the session, so closing either one releases the socket;
            # close through the runtime when it exists to let it drop its caches too.
            if runtime is not None:
                runtime.close()
            elif session is not None:
                session.close()
            shutil.rmtree(workdir, ignore_errors=True)

    # -- stages ----------------------------------------------------------------------

    def _understand_for_turn(self, request: TurnRequest) -> Any:
        if type(self)._understand is not PlatformAgentRunner._understand:
            return self._understand(request.message)

        from .planning.task_understanding import enrich_task

        parsed = self._parse_understanding(request.message)
        intent = enrich_task(parsed, request.message)
        if str(getattr(intent.status, "value", intent.status)) != "accepted":
            explanation = str(getattr(intent, "explanation", "") or "").strip()
            raise TurnBlocked(
                "INVALID_REQUEST",
                explanation or "无法理解该指令，请换一种说法或补充要操作的对象。")
        self._cache_model_result(request, "understanding", parsed,
                                 instruction=request.message, reset=True)
        return intent

    @staticmethod
    def _cache_payload(request: TurnRequest) -> dict[str, Any]:
        payload = request.model_context
        if not isinstance(payload, dict) or not payload:
            raise TurnBlocked(
                "MODEL_CONTEXT_MISSING",
                "服务中没有可复用的千问结果，请先发送一条普通指令。")
        return payload

    def _cached_intent(self, request: TurnRequest) -> Any:
        from .planning.task_understanding import TaskParseLLMOutput, enrich_task

        cache = self._cache_payload(request)
        payload = cache.get("understanding")
        instruction = cache.get("instruction")
        try:
            parsed = TaskParseLLMOutput.model_validate(payload)
            return enrich_task(parsed, str(instruction or "DEBUG"))
        except Exception as error:  # noqa: BLE001 - persisted state is an external boundary
            LOGGER.debug("cached Qwen understanding is unavailable for conversation %.8s",
                         request.conversation_id)
            raise TurnBlocked(
                "MODEL_CONTEXT_MISSING",
                "服务中没有可复用的千问结果，请先发送一条普通指令。") from error

    def _skill_plan_from_cache(self, task: Any, request: TurnRequest) -> Any:
        from .planning.recipe_planner import RecipePlanner
        from .planning.semantic_validator import validate_semantic_plan
        from .planning.skill_planning import SkillPlanLLMOutput, enrich_skill_plan

        planner = RecipePlanner()
        if planner.supports(task):
            return planner.plan(task)
        payload = self._cache_payload(request).get("planning")
        try:
            output = SkillPlanLLMOutput.model_validate(payload)
            plan = enrich_skill_plan(output, task)
            validate_semantic_plan(plan, task)
            LOGGER.debug("reusing cached Qwen planning for conversation %.8s",
                         request.conversation_id)
            return plan
        except Exception as error:  # noqa: BLE001 - persisted state is an external boundary
            LOGGER.debug("cached Qwen skill plan is unavailable for conversation %.8s",
                         request.conversation_id)
            raise TurnBlocked(
                "MODEL_CONTEXT_MISSING",
                "最近一次千问结果不包含此任务所需的规划，请先发送一条普通指令。") from error

    @staticmethod
    def _cache_model_result(request: TurnRequest, name: str, value: Any, *,
                            instruction: str | None = None, reset: bool = False) -> None:
        dump = getattr(value, "model_dump", None)
        if not callable(dump):
            return
        if reset:
            payload: dict[str, Any] = {"version": 1}
            request.model_context = payload
        else:
            payload = request.model_context
            if not isinstance(payload, dict):
                payload = {"version": 1}
                request.model_context = payload
        if instruction is not None:
            payload["instruction"] = instruction
        payload[name] = dump(mode="json")
        request.model_context_updated = True
        LOGGER.debug("updated cached Qwen %s for conversation %.8s",
                     name, request.conversation_id)

    def _connect(self, endpoint: str) -> Any:
        """Attach to the bound Live session. Connect-only: never launches an instance."""
        from sim_platform.live import LiveControlError, connect

        try:
            session = connect(endpoint)
            try:
                self._ensure_runtime_backends(session)
            except Exception:
                session.close()
                raise
            return session
        except TurnBlocked:
            raise
        except (LiveControlError, ValueError, OSError) as error:
            raise TurnBlocked("SESSION_UNREACHABLE",
                              f"无法连接绑定的仿真会话 {endpoint}：{error}") from error
        except Exception as error:  # noqa: BLE001 - surfaced as an explicit blocker
            raise TurnBlocked("SESSION_UNREACHABLE",
                              f"无法连接绑定的仿真会话 {endpoint}：{error}") from error

    @staticmethod
    def _ensure_runtime_backends(session: Any) -> None:
        """Use the backend pair required by manipulation and collision planning."""
        try:
            current = session.backend_status()
            if current == {"physics": "mujoco", "collision": "fcl"}:
                LOGGER.debug("live runtime backends already configured: physics=mujoco collision=fcl")
                return
            applied = session.apply_backends(physics="mujoco", collision="fcl")
            LOGGER.debug(
                "configured live runtime backends: previous_physics=%s previous_collision=%s "
                "physics=%s collision=%s changed=%s",
                current.get("physics"), current.get("collision"),
                applied.get("physics"), applied.get("collision"), applied.get("changed"),
            )
        except Exception as error:  # noqa: BLE001 - exposed as a stable turn error
            raise TurnBlocked(
                "BACKEND_CONFIGURATION_FAILED",
                f"无法启用 MuJoCo/FCL 仿真后端：{error}",
            ) from error

    def _build_runtime(self, session: Any) -> Any:
        """Wrap the session in the execution runtime, which requires a robot in the scene."""
        from .runtime.live_runtime import LiveSceneRuntime, RuntimeConnectionError

        try:
            return LiveSceneRuntime(session)
        except RuntimeConnectionError as error:
            # By this point the scene has been resolved, so a missing robot means the bound
            # world has none and no base scene supplied one.
            raise TurnBlocked("SCENE_INVALID",
                              f"场景中没有可用的机械臂：{error}") from error
        except ValueError as error:
            raise TurnBlocked("ROBOT_MODEL_INCOMPATIBLE",
                              f"场景中的机械臂与 UR5e 配置不匹配：{error}") from error

    def _understand(self, message: str) -> Any:
        from .planning.task_understanding import enrich_task

        parsed = self._parse_understanding(message)
        if str(getattr(parsed, "status", "")) != "accepted":
            explanation = str(getattr(parsed, "explanation", "") or "").strip()
            raise TurnBlocked(
                "INVALID_REQUEST",
                explanation or "无法理解该指令，请换一种说法或补充要操作的对象。")
        return enrich_task(parsed, message)

    def _parse_understanding(self, message: str) -> Any:
        from .planning.task_understanding import TaskUnderstandingRequest

        provider = self._require_provider()
        parsed = provider.understand(TaskUnderstandingRequest(instruction=message))
        if str(getattr(parsed, "status", "")) != "accepted":
            explanation = str(getattr(parsed, "explanation", "") or "").strip()
            raise TurnBlocked(
                "INVALID_REQUEST",
                explanation or "无法理解该指令，请换一种说法或补充要操作的对象。")
        return parsed

    def _resolve_scene(self, session: Any, intent: Any,
                       *, placement_seed: int | None = None) -> tuple[Any, Any]:
        """Generate a scene only for an empty or light-only bound world.

        Any other entity selects Route B. Missing targets or robots are reported by
        grounding/runtime validation, never repaired by replacing the user's scene.
        """
        from .scene import build_route_a_plan, build_route_b_plan
        from .scene_apply import apply_plan, read_back_generated_plan

        entities = self._scene_entities(session)
        if any(entity.get("light") is None and "light" not in (entity.get("flags") or ())
               for entity in entities):
            return build_route_b_plan(entities, scene_id="main"), None

        if placement_seed is None:
            registry, assets, base_scene = self._compose(intent)
        else:
            registry, assets, base_scene = self._compose(intent, placement_seed=placement_seed)
        plan = build_route_a_plan(registry, assets=assets, base_scene=base_scene)
        applied = apply_plan(session, plan, restore_base=True)
        plan = read_back_generated_plan(plan, applied, self._scene_entities(session))
        return plan, applied

    def _compose(self, intent: Any, *, placement_seed: int | None = None) -> tuple[Any, Any, Path]:
        from .scene.composer import SceneComposer
        from .scene.model_catalog import load_route_a_model_catalog

        try:
            catalog = load_route_a_model_catalog()
            resolved = catalog.resolve_intent(intent)
            # Normal Route A generation must not repeat a fixed layout. A numeric
            # DEBUG command is the explicit reproducibility escape hatch.
            seed = (int(placement_seed) if placement_seed is not None else
                    random.SystemRandom().randrange(0, 2**63))
            registry = SceneComposer().compose(
                intent, resolved.by_entity, robot="ur5e", seed=seed)
            return registry, resolved.by_model_id, catalog.base_scene
        except Exception as error:  # noqa: BLE001
            raise TurnBlocked("SCENE_INVALID", f"无法为该指令生成场景：{error}") from error

    @staticmethod
    def _debug_scene_seed(message: str) -> int | None:
        match = DEBUG_SCENE_SEED_RE.fullmatch(str(message or "").strip())
        return int(match.group(1)) if match else None

    @staticmethod
    def _scene_entities(session: Any) -> list[dict[str, Any]]:
        try:
            snapshot = session.scene_snapshot()
        except Exception as error:  # noqa: BLE001
            raise TurnBlocked("SESSION_UNREACHABLE",
                              f"无法读取当前场景：{error}") from error
        return [dict(entity) for entity in (getattr(snapshot, "entities", ()) or ())
                if isinstance(entity, dict)]

    def _ground(self, intent: Any, plan: Any) -> Any:
        from .grounding import GroundingError, ground_task

        try:
            return ground_task(intent, plan)
        except GroundingError as error:
            raise TurnBlocked(error.code, error.message) from error

    def _skill_plan(self, task: Any) -> Any:
        from .planning.recipe_planner import RecipePlanner
        from .planning.semantic_validator import validate_semantic_plan

        planner = RecipePlanner()
        if planner.supports(task):
            # Recipes are deterministic, so a covered task never spends a model call.
            return planner.plan(task)
        plan = self._model_plan(task)
        try:
            validate_semantic_plan(plan, task)
        except Exception as error:  # noqa: BLE001 - a rejected plan fails the turn
            raise TurnBlocked("INVALID_REQUEST",
                              f"任务分解不合法：{error}") from error
        return plan

    def _skill_plan_for_turn(self, task: Any, request: TurnRequest) -> Any:
        if type(self)._skill_plan is not PlatformAgentRunner._skill_plan:
            return self._skill_plan(task)

        from .planning.recipe_planner import RecipePlanner
        from .planning.semantic_validator import validate_semantic_plan
        from .planning.skill_planning import enrich_skill_plan

        planner = RecipePlanner()
        if planner.supports(task):
            return planner.plan(task)
        output = self._model_plan_output(task)
        try:
            plan = enrich_skill_plan(output, task)
            validate_semantic_plan(plan, task)
        except Exception as error:  # noqa: BLE001 - a rejected plan fails the turn
            raise TurnBlocked("INVALID_REQUEST",
                              f"任务分解不合法：{error}") from error
        self._cache_model_result(request, "planning", output)
        return plan

    def _model_plan(self, task: Any) -> Any:
        from .planning.skill_planning import enrich_skill_plan

        output = self._model_plan_output(task)
        try:
            return enrich_skill_plan(output, task)
        except ValueError as error:
            # Parse or contract failure fails the turn; nothing is guessed.
            raise TurnBlocked("INVALID_REQUEST", f"任务分解不合法：{error}") from error

    def _model_plan_output(self, task: Any) -> Any:
        from .planning.context_builder import build_planner_context
        from .planning.skill_planning import SkillPlanningRequest
        from .planning.skills import REGISTRY

        provider = self._require_provider()
        context = build_planner_context(task)
        return provider.plan(SkillPlanningRequest(
            context=context, skill_catalog=REGISTRY.prompt_catalog()))

    def _compile(self, skill_plan: Any, task: Any, plan: Any, workdir: Path) -> Any:
        from .compile.compiler import compile_execution_bundle
        from .protocol import load_command_document

        try:
            bundle = compile_execution_bundle(
                skill_plan, task, scene_plan=plan, output_dir=workdir, robot="ur5e")
            return load_command_document(Path(bundle.commands))
        except TurnBlocked:
            raise
        except Exception as error:  # noqa: BLE001
            raise TurnBlocked("EXECUTION_METADATA_MISSING",
                              f"无法编译执行命令：{error}") from error

    def _execute(self, runtime: Any, commands: Any,
                 observer: Any = None) -> Any:
        from .anchors import AnchorResolver
        from .runtime.executor import execute_commands

        # The compiler names an anchor per move and the registry stores where that anchor sits
        # on the object. Without this resolver the executor falls back to the object's origin,
        # which for a container is on its floor -- reaching it drives the gripper into the
        # tabletop and the goal reads as a collision.
        registry = getattr(commands, "registry", None)
        resolver = AnchorResolver(registry, runtime) if registry else None
        return execute_commands(runtime, commands, resolve_target=resolver, observer=observer)

    async def _execute_observed(self, runtime: Any, commands: Any,
                                tracker: _PlanTracker) -> Any:
        """Run the blocking executor in a worker while streaming command observations."""
        if type(self)._execute is not PlatformAgentRunner._execute:
            return await asyncio.to_thread(self._execute, runtime, commands)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def observe(value: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, value)

        worker = asyncio.create_task(asyncio.to_thread(
            self._execute, runtime, commands, observe))
        while not worker.done():
            try:
                value = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            await tracker._emit(TURN_DEBUG, {
                "type": "command", "phase": "execute",
                "status": value.get("status", "in_progress"),
                "details": value,
                "plan": [dict(row) for row in tracker._rows],
            })
        while not queue.empty():
            value = queue.get_nowait()
            await tracker._emit(TURN_DEBUG, {
                "type": "command", "phase": "execute",
                "status": value.get("status", "in_progress"),
                "details": value,
                "plan": [dict(row) for row in tracker._rows],
            })
        return await worker

    async def _publish(self, context: Any, session: Any, frames: list[dict[str, Any]],
                       segments: list[TrajectorySegment], emit: EmitEvent) -> int | None:
        base_url = getattr(context, "gatewayBaseUrl", None)
        token = getattr(context, "controlToken", None)
        job_id = getattr(context, "jobId", None)
        if not (base_url and token and job_id):
            # Local CLI mode has no Web presentation and must not call the publish APIs.
            LOGGER.info("no gateway control context; skipping presentation publish")
            return None
        await emit(PRESENTATION_REQUESTED, {"frameCount": len(frames)})
        async with GatewayClient(base_url=str(base_url), control_token=str(token),
                                 job_id=str(job_id)) as client:
            try:
                if _frames_show_motion(frames):
                    # Capture already samples at playback FPS. Publish one static base
                    # plus the sampled transforms, without repeating scene assets.
                    result = await client.publish_capture(frames, segments=segments)
                else:
                    # Nothing moved, so there is no trajectory to show either.
                    result = await client.publish_snapshot()
            except GatewayError as error:
                raise TurnBlocked(error.code, f"发布场景失败：{error.message}") from error
        revision = result.get("presentationRevision") if isinstance(result, dict) else None
        return int(revision) if isinstance(revision, int) else None

    async def _publish_snapshot(self, context: Any, emit: EmitEvent) -> int | None:
        """Publish the authoritative composed scene without manufacturing timeline frames."""
        base_url = getattr(context, "gatewayBaseUrl", None)
        token = getattr(context, "controlToken", None)
        job_id = getattr(context, "jobId", None)
        if not (base_url and token and job_id):
            return None
        await emit(PRESENTATION_REQUESTED, {"kind": "snapshot", "frameCount": 0})
        async with GatewayClient(base_url=str(base_url), control_token=str(token),
                                 job_id=str(job_id)) as client:
            try:
                result = await client.publish_snapshot()
            except GatewayError as error:
                raise TurnBlocked(error.code, f"发布场景失败：{error.message}") from error
        revision = result.get("presentationRevision") if isinstance(result, dict) else None
        return int(revision) if isinstance(revision, int) else None

    # -- replies ---------------------------------------------------------------------

    @staticmethod
    def _describe(segments: list[TrajectorySegment], frame_count: int) -> list[str]:
        parts: list[str] = []
        if segments:
            parts.append(f"已显示 {len(segments)} 段路径轨迹。")
        if frame_count >= 2:
            parts.append(f"时间线共 {frame_count} 帧。")
        return parts

    def _reply(self, intent: Any, task: Any, segments: list[TrajectorySegment],
               frame_count: int, applied: Any) -> str:
        names = [str(getattr(entity, "semantic_name", "")) for entity in
                 (getattr(task, "entities", ()) or ())]
        subject = "、".join(name for name in names if name)
        actions = "、".join(sorted({str(getattr(value, "value", value)) for value in
                                    (getattr(task, "task_types", ()) or ())}))
        head = f"已完成 {actions}" if actions else "已完成任务"
        if subject:
            head += f"（{subject}）"
        parts = [head + "。"]
        if applied is not None:
            parts.append("已按指令重建场景并布置物体。")
        parts.extend(self._describe(segments, frame_count))
        parts.append(SYNC_SUFFIX)
        return "".join(parts)

    def _failure_reply(self, failure: dict[str, Any], segments: list[TrajectorySegment],
                       frame_count: int, *, published: bool) -> str:
        reason = str(failure.get("error_message") or failure.get("error_code")
                     or "执行未完成")
        parts = [f"任务未完成：{reason}。"]
        parts.extend(self._describe(segments, frame_count))
        if published:
            # The scene is not rolled back on failure, so say what is now on screen.
            parts.append(f"已同步失败前的场景状态。{SYNC_SUFFIX}")
        return "".join(parts)


def _skill_step_summaries(plan: Any) -> list[dict[str, str]]:
    summaries = []
    for step in list(getattr(plan, "steps", ()) or ()):
        summaries.append({
            "stepId": str(getattr(step, "step_id", "")),
            "skill": str(getattr(step, "skill_name", getattr(step, "skill", ""))),
            "target": str(getattr(step, "target", "")),
        })
    return summaries


def _command_summaries(document: Any) -> list[dict[str, str]]:
    summaries = []
    for command in list(getattr(document, "commands", ()) or ()):
        parameters = getattr(command, "parameters", {}) or {}
        target = parameters.get("target", "") if isinstance(parameters, dict) else ""
        summaries.append({
            "commandId": str(getattr(command, "command_id", "")),
            "stepId": str(getattr(command, "source_skill_step_id", "")),
            "skill": str(getattr(command, "skill_name", "")),
            "target": str(target),
        })
    return summaries


def _segments(values: Any) -> list[TrajectorySegment]:
    """Adapt runtime `TrajectoryMarkers` to the publish layer's segment shape."""
    result: list[TrajectorySegment] = []
    for value in list(values or ()):
        points = [tuple(float(item) for item in point)
                  for point in getattr(value, "points", ()) or ()]
        if len(points) < 2:
            continue
        color = getattr(value, "color", None)
        result.append(TrajectorySegment(
            id=str(getattr(value, "id", f"segment_{len(result) + 1}")),
            points=points,  # type: ignore[arg-type]
            color=tuple(float(item) for item in color) if color else None,  # type: ignore[arg-type]
            radius=float(getattr(value, "radius", 0.008)),
            label=str(getattr(value, "label", "") or "") or None))
    return result


def _frames_show_motion(frames: list[dict[str, Any]]) -> bool:
    """True when supplied frames contain an actual entity pose or robot-state change."""
    if len(frames) < 2:
        return False

    def state(frame: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
        values = []
        for entity in frame.get("entities", ()) or ():
            if not isinstance(entity, dict):
                continue
            local = entity.get("local") or entity.get("transform")
            robot = entity.get("robot")
            values.append((entity.get("id"), local,
                           robot.get("positions") if isinstance(robot, dict) else None))
        return values

    first = state(frames[0])
    return any(state(frame) != first for frame in frames[1:])
