"""Demo command orchestration for direct release operations."""

from demo.interaction.requests import body_name
from demo.interaction.object_library import scene_object_by_id


def release_request(resources, parameters):
    """Convert a command target into the lower release skill request."""
    name = (body_name(resources, parameters) if parameters.get('link') else
            scene_object_by_id(resources.scene, parameters['target'])['object_name_in_scene'])
    request = {k: v for k, v in parameters.items()
               if k not in {'target', 'link', 'allow_contact'}}
    request['target'] = {'type': 'object', 'object_id': name}
    return request


def execute_release(resources, parameters, releaser):
    return releaser.execute(release_request(resources, parameters))


