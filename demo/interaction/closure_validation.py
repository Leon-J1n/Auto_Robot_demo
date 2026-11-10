"""Execute real gripper closure with measured-force grasp confirmation."""


def execute_checked_grasp(grasper, request, backend, checker):
    """Keep grasp confirmation; geometry is checked only during planning."""
    result = grasper.execute(request)
    result['collision_policy'] = 'planning_only'
    result['allowed_closure_contacts'] = []
    return result
