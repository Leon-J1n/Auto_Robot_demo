"""Demo command orchestration for direct grasp operations."""

from demo.interaction.carrying_safety import split_carrying_safety
from demo.interaction.closure_validation import execute_checked_grasp
from demo.interaction.requests import body_name
from demo.interaction.object_library import scene_object_by_id


def grasp_request(resources, parameters):
    """Convert a command target into the lower grasp skill request."""
    name = (body_name(resources, parameters) if parameters.get('link') else
            scene_object_by_id(resources.scene, parameters['target'])['object_name_in_scene'])
    request = {k: v for k, v in parameters.items()
               if k not in {'target', 'link', 'allow_contact'}}
    request['target'] = {'type': 'object', 'object_id': name}
    return split_carrying_safety(request)


def execute_grasp(resources, parameters, grasper, backend, checker):
    request, carrying_distance = grasp_request(resources, parameters)
    result = execute_checked_grasp(grasper, request, backend, checker)
    return result, carrying_distance


