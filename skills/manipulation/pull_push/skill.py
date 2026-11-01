"""Executable Push/Pull Skill orchestration."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping, Optional

from control import MujocoPullPushController
from skills.manipulation.pull_push.selector import StrategySelector
from skills.manipulation.pull_push.strategies import CircularPullPush, LinearPullPush
from skills.manipulation.pull_push.utils import PullPushValidationError, validate_pull_push_request


class PullPushSkill:
    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, robot_runtime: Any = None, kinematics=None) -> None:
        self.config = config or {}
        self.robot_runtime = robot_runtime
        self.kinematics = kinematics
        self.selector = StrategySelector()
        self.strategies = {
            "linear_pull_push": LinearPullPush(),
            "circular_pull_push": CircularPullPush(),
        }

    def execute(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        outcome = self._execute(request)
        return self._decorate_outcome(outcome, request)

    def plan(self, request: Mapping[str, Any], *, start_joints=None, start_pose=None,
             allow_unestablished_contact=False) -> Dict[str, Any]:
        """Validate and statically plan a complete push/pull trajectory.

        Planning may use an interaction-pose IK solution even when the live
        robot is still at the pre-approach pose.  It never advances physics.
        """
        selection = None
        try:
            normalized = validate_pull_push_request(deepcopy(request), self.config)
            context = self._runtime_context(
                normalized, planning=True,
                allow_unestablished_contact=allow_unestablished_contact,
            )
            selection = self.selector.select(normalized, context)
            prepared = self.strategies[selection["pull_push_strategy"]].plan(
                normalized,
                context,
                start_joints=start_joints,
                start_pose=start_pose,
                allow_unestablished_contact=allow_unestablished_contact,
            )
            return {'success': True, 'status': 'planned', 'selection': selection,
                    'request': normalized, 'prepared': prepared, 'error': None}
        except PullPushValidationError as exc:
            return self._failure('INVALID_REQUEST', str(exc), 'validation', True, selection)
        except Exception as exc:
            failure = self._failure(
                getattr(exc, 'error_code', 'PUSH_PULL_PLANNING_FAILED'), str(exc),
                getattr(exc, 'failed_stage', 'planning'),
                bool(getattr(exc, 'recoverable', False)), selection)
            failure['error']['details'] = getattr(exc, 'details', {})
            return failure

    def execute_prepared(self, request: Mapping[str, Any], prepared: Mapping[str, Any]) -> Dict[str, Any]:
        """Execute a prepared trajectory without invoking planning again."""
        selection = None
        try:
            normalized = validate_pull_push_request(deepcopy(request), self.config)
            context = self._runtime_context(normalized)
            selection = self.selector.select(normalized, context)
            raw = self.strategies[selection["pull_push_strategy"]].execute_prepared(
                normalized, context, prepared)
            result = self._verify(raw, normalized)
            return self._decorate_outcome(
                {'success': True, 'status': 'success', 'selection': selection,
                 'pull_push_result': result, 'error': None}, normalized)
        except PullPushValidationError as exc:
            return self._failure('INVALID_REQUEST', str(exc), 'validation', True, selection)
        except Exception as exc:
            failure = self._failure(
                getattr(exc, 'error_code', 'PUSH_PULL_EXECUTION_ERROR'), str(exc),
                getattr(exc, 'failed_stage', 'execution'),
                bool(getattr(exc, 'recoverable', False)), selection)
            failure['error']['details'] = getattr(exc, 'details', {})
            failure['pull_push_result'] = failure['error']['details'].get('result')
            return self._decorate_outcome(failure, normalized if 'normalized' in locals() else request)

    def rebase_prepared_object_state(self, request: Mapping[str, Any], prepared: Mapping[str, Any]):
        """Accept an intentional grasp-induced object update without replanning."""
        controller = getattr(self.robot_runtime, 'pull_push_controller', None)
        if controller is None:
            self._runtime_context(dict(request))
            controller = self.robot_runtime.pull_push_controller
        return controller.rebase_prepared_object_state(request, prepared)

    def _decorate_outcome(self, outcome: Dict[str, Any], request: Mapping[str, Any]) -> Dict[str, Any]:
        result = outcome.get('pull_push_result')
        # A stopped, unfinished motion is neither a completed task nor a
        # request/precondition error. Keep the stop diagnostic and progress.
        outcome['status'] = ('success' if outcome['success'] else
                             'suspend' if result and result.get('motion_executed')
                             and not result.get('target_reached') else 'error')
        if result is not None:
            gripper = getattr(self.robot_runtime, 'gripper_controller', None)
            state = gripper.get_state(request['target']['object_id']) if gripper else {}
            mode = request.get('grasp', {}).get('mode') or (state.get('mode', 'binding') if state.get('holding') else 'physical')
            result.update(grasp_mode=mode, bound=bool(state.get('bound', False)))
            result.setdefault('evidence', {}).update(grasp_mode=mode, bound=bool(state.get('bound', False)))
        return outcome

    def _execute(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        selection = None
        try:
            normalized = validate_pull_push_request(request, self.config)
            context = self._runtime_context(normalized)
            selection = self.selector.select(normalized, context)
            raw = dict(self.strategies[selection["pull_push_strategy"]].execute(normalized, context))
            result = self._verify(raw, normalized)
            return {"success": True, "status": "success", "selection": selection, "pull_push_result": result, "error": None}
        except PullPushValidationError as exc:
            return self._failure("INVALID_REQUEST", str(exc), "validation", True, selection)
        except TimeoutError as exc:
            return self._failure("PUSH_PULL_TIMEOUT", str(exc), "execution", True, selection, "timeout")
        except Exception as exc:
            failure = self._failure(getattr(exc, "error_code", "PUSH_PULL_EXECUTION_ERROR"), str(exc), getattr(exc, "failed_stage", "execution"), bool(getattr(exc, "recoverable", False)), selection)
            failure["error"]["details"] = getattr(exc, "details", {})
            failure["pull_push_result"] = failure["error"]["details"].get("result")
            return failure

    def _runtime_context(self, request: Mapping[str, Any], *, planning=False,
                         allow_unestablished_contact=False) -> Dict[str, Any]:
        if self.robot_runtime is None:
            raise RuntimeError("An initialized robot runtime at the pre-contact pose is required.")
        controller = getattr(self.robot_runtime, "pull_push_controller", None)
        if controller is None:
            controller = MujocoPullPushController(self.robot_runtime, kinematics=self.kinematics)
            self.robot_runtime.pull_push_controller = controller
        elif self.kinematics is not None:
            controller.kinematics = self.kinematics
        if request["manipulation"]["contact_mode"] == "auto":
            gripper = getattr(self.robot_runtime, "gripper_controller", None)
            holding = bool(gripper and gripper.get_state(request["target"]["object_id"]).get("holding"))
            request["manipulation"]["contact_mode"] = "grasp" if holding else "push_contact"
        if request["manipulation"]["operation"] == "pull" and request["manipulation"]["contact_mode"] != "grasp":
            raise PullPushValidationError("Pull requires an already grasped target.")
        gripper = getattr(self.robot_runtime, 'gripper_controller', None)
        grasp_state = gripper.get_state(request['target']['object_id']) if gripper else {}
        requested_mode = request.get('grasp', {}).get('mode')
        held_contact = request['manipulation']['contact_mode'] == 'grasp'
        if requested_mode is not None and not (planning and allow_unestablished_contact):
            if not held_contact and requested_mode == 'binding':
                raise PullPushValidationError('binding requires an already bound grasp; push_contact uses physical contact.')
            if held_contact and (grasp_state.get('mode') != requested_mode or
                    bool(grasp_state.get('bound')) != (requested_mode == 'binding')):
                raise PullPushValidationError('Requested grasp mode does not match the actual gripper state; release and grasp in the requested mode first.')
        if held_contact and grasp_state.get('mode') == 'physical':
            request['manipulation']['maintain_contact'] = True
            request['constraints']['stop_on_contact_loss'] = True
        state = dict(controller.get_state(
            request["target"]["object_id"],
            interaction_mode=request["resolved_interaction_mode"],
            contact_mode=request["manipulation"]["contact_mode"],
        ))
        planning_grasp_ready = planning and allow_unestablished_contact and held_contact
        if (not state.get("ready") and not planning_grasp_ready) or state.get("fault"):
            raise RuntimeError("Push/Pull controller is not ready.")
        if not state.get("safe", True):
            raise RuntimeError("Robot safety state prevents Push/Pull execution.")
        return {"pull_push_controller": controller, "controller_state": state}

    @staticmethod
    def _verify(raw: Mapping[str, Any], request: Mapping[str, Any]) -> Dict[str, Any]:
        peak_force = float(raw.get("peak_force", 0.0))
        limit = request["constraints"]["maximum_force"]
        if not math_isfinite(peak_force) or (limit is not None and peak_force > limit + 1e-9):
            raise RuntimeError("Maximum Push/Pull force was exceeded.")
        if request["manipulation"]["maintain_contact"] and not raw.get("contact_maintained", False):
            raise RuntimeError("Target contact was lost during Push/Pull execution.")
        if not raw.get("target_reached", False):
            raise RuntimeError("Target did not reach the requested displacement/angle.")
        return {**raw, "verified": bool(raw.get("target_reached", False))}

    @staticmethod
    def _failure(code: Any, message: str, stage: str, recoverable: bool, selection: Any, status: str = "failed") -> Dict[str, Any]:
        return {"success": False, "status": status, "selection": selection, "pull_push_result": None, "error": {"error_code": str(getattr(code, "value", code)), "error_message": message, "failed_stage": stage, "recoverable": recoverable}}


def math_isfinite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))
