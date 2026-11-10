"""Verify a tracked payload once, after a complete task command."""
from copy import deepcopy


def verify_command_grasp(backend, row):
    controller = getattr(backend, 'gripper_controller', None)
    if controller is None:
        return row
    payload = controller.get_state()
    # A dropped payload reports holding=False. Its confirmed hold intent is
    # retained until release and must still be checked here.
    holding = bool(payload.get('holding') or (
        getattr(controller, 'held_name', None) is not None and
        getattr(controller, '_holding', False)))
    if not holding:
        return row
    cause = None
    # Relative displacement/rotation remain diagnostics, not failure criteria.
    if payload.get('contact_lost'):
        cause = 'command 结束后，持物未保持双侧指垫接触'
    row['grasp_verification'] = {
        'mode': 'command_end', 'success': cause is None, 'evidence': deepcopy(payload)}
    if cause is not None and row.get('success'):
        row.update(success=False, status='error', motion_completed=True, error={
            'error_code': 'GRASP_FAILED', 'failed_stage': 'post_command_verification',
            'error_message': cause, 'evidence': deepcopy(payload)})
    return row
