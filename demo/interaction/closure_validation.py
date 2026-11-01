"""Physical closure trials on isolated model/data and structured grasp errors."""
from copy import copy
from contextlib import contextmanager
import mujoco


@contextmanager
def physical_closure_contacts(controller, checker):
    """Scope the contact exception to close(), never opening or arm motion."""
    original = controller.close
    previous = getattr(checker, '_physical_closure_target', None)
    previous_log = getattr(checker, '_closure_contact_log', None)
    log = {}
    def close(*args, **kwargs):
        checker._physical_closure_target = (kwargs.get('target_object_id')
                                            if kwargs.get('mode', 'binding') == 'physical' else None)
        checker._closure_contact_log = log
        try:
            return original(*args, **kwargs)
        finally:
            checker._physical_closure_target = previous
            checker._closure_contact_log = previous_log
    had_override = 'close' in vars(controller)
    controller.close = close
    try:
        yield log
    finally:
        if had_override:
            controller.close = original
        else:
            del controller.close
        checker._physical_closure_target = previous
        checker._closure_contact_log = previous_log


def collision_error(contacts, phase='closing'):
    return dict(error_code='GRASP_CLOSURE_COLLISION' if phase == 'closing' else 'GRASP_OPENING_COLLISION',
                error_message=f'Gripper {phase} collision: {contacts}',
                failed_stage='grasp_closure' if phase == 'closing' else 'grasp_opening',
                recoverable=True, recommended_action='Choose another grasp pose or inspect the colliding link geometry.',
                details={'phase': phase, 'contacts': list(contacts),
                         'geometry_basis': 'simulation_collision_geometry'})


def execute_checked_grasp(grasper, request, backend, checker):
    previous, failure = backend.frame_callback, []
    def record(state):
        if previous:
            previous(state)
        hit = checker.check(state['joints'])
        if hit.in_collision:
            phase = 'closing' if backend.gripper_controller.target_name == request['target']['object_id'] else 'opening'
            failure.append(collision_error(hit.contacts, phase))
            raise RuntimeError(failure[-1]['error_message'])
    backend.set_frame_callback(record)
    try:
        with physical_closure_contacts(backend.gripper_controller, checker) as contacts:
            result = grasper.execute(request)
        result['allowed_closure_contacts'] = list(contacts.values())
        if failure:
            result.update(success=False, status='failed', error=failure[0])
        return result
    finally:
        backend.set_frame_callback(previous)


def validate_closure(checker, joints, target, request):
    """Simulate opening/closing without moving the live robot or changing forces."""
    from control.binding_gripper import BindingGripperController
    live = checker.backend
    trial = copy(live)
    trial.model = copy(live.model)
    trial.data = mujoco.MjData(trial.model)
    mujoco.mj_copyData(trial.data, trial.model, live.data)
    trial.data.qpos[trial.qpos_indices] = joints
    trial.data.qvel[:] = 0
    trial.data.ctrl[trial.actuator_ids] = joints
    trial.viewer, trial.realtime, trial.frame_callback = None, False, None
    trial.contact_target = None
    trial.gripper_controller = BindingGripperController(trial)
    probe = copy(checker)
    probe.backend, probe.probe = trial, mujoco.MjData(trial.model)
    probe._pair_key = None
    probe._planning_cache = None
    params = request.get('strategy_params', {})
    mode = request.get('grasp', {}).get('mode', 'binding')
    failures = []
    def record(state):
        hit = probe.check(state['joints'])
        if hit.in_collision:
            failures.append(collision_error(hit.contacts, 'closing' if trial.gripper_controller.target_name == target else 'opening'))
            raise RuntimeError(failures[-1]['error_message'])
    trial.set_frame_callback(record)
    def advance(duration):
        # Validate every physics step, including transient non-pad contacts.
        until = trial.data.time + duration
        while trial.data.time < until - 1e-10:
            trial.data.ctrl[trial.actuator_ids] = joints
            trial.data.qfrc_applied[trial.dof_indices] = trial.data.qfrc_bias[trial.dof_indices]
            mujoco.mj_step(trial.model, trial.data)
            mujoco.mj_forward(trial.model, trial.data)
            record({'joints': trial.get_joint_positions()})
    trial.advance_gripper = advance
    mujoco.mj_forward(trial.model, trial.data)
    controller = trial.gripper_controller
    timeout = request.get('constraints', {}).get('timeout', 5.)
    try:
        controller.open(params.get('open_width', .08), speed=params.get('close_speed', .5), timeout=timeout)
        with physical_closure_contacts(controller, probe) as contacts:
            result = controller.close(params.get('close_width', 0.), speed=params.get('close_speed', .5),
                force=params.get('grasp_force', 20.), timeout=timeout, target_object_id=target, mode=mode,
                maximum_force=request.get('constraints', {}).get('maximum_force', 40.),
                **{key: params[key] for key in ('confirmation_time', 'contact_threshold', 'width_stability_tolerance',
                    'position_stability_tolerance', 'orientation_stability_tolerance') if key in params})
        return {'success': result['contact_detected'], 'confirmation': result['evidence'],
                'allowed_closure_contacts': list(contacts.values()),
                'reason': 'confirmed' if result['contact_detected'] else 'grasp_confirmation_failed'}
    except (RuntimeError, TimeoutError) as exc:
        return {'success': False, 'reason': 'grasp_closure_collision' if failures else 'grasp_closure_failed',
                'confirmation': controller.evidence,
                'allowed_closure_contacts': list(contacts.values()) if 'contacts' in locals() else [],
                'error': failures[0] if failures else {'error_message': str(exc)}}
