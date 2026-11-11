"""Infer visual point requests and bind approach/retreat to interactions."""


def infer_visual_targets(commands):
    """Return one RGB-D request per distinct target and semantic point type."""
    point_types = infer_command_point_types(commands)
    targets = {}
    for index, command in enumerate(commands):
        command_id = str(command.get('command_id', index))
        point_type = point_types[command_id]
        if point_type is None:
            continue
        target = command.get('parameters', {}).get('target')
        identity = (str(target), point_type)
        targets.setdefault(identity, {
            'target_id': target, 'point_type': point_type, 'camera_id': 'global'})
    # The pick owns perception settings; its bound approach must use them too.
    configured = {}
    for command in commands:
        if command.get('skill_name') != 'pick':
            continue
        parameters = command.get('parameters', {})
        identity = (str(parameters.get('target')), 'grasp')
        settings = {key: parameters[key] for key in
                    ('grasp_type', 'target_description', 'camera_id', 'link') if key in parameters}
        if settings.get('grasp_type', 'handle') not in {'handle', 'top_down'}:
            raise ValueError('grasp_type must be handle or top_down')
        if identity in configured and configured[identity] != settings:
            raise ValueError('One target cannot use different visual grasp settings in the same run')
        configured[identity] = settings
        targets[identity].update(settings)
    return list(targets.values())


def infer_command_point_types(commands):
    """Bind each interaction to its nearest matching approach and retreat."""
    return _infer_command_bindings(commands)[0]


def infer_command_place_cycles(commands):
    """Give a put and its bound approach/retreat the same placement cycle ID."""
    return _infer_command_bindings(commands)[1]


def infer_command_place_parameters(commands):
    """A put owns relative-placement parameters for its entire interaction cycle."""
    from demo.interaction.relative_placement import placement_parameters
    cycles = infer_command_place_cycles(commands)
    requests = {str(command.get('command_id', index)): placement_parameters(command.get('parameters', {}))
                for index, command in enumerate(commands) if command.get('skill_name') == 'put'}
    return {command_id: dict(requests[cycle]) for command_id, cycle in cycles.items()}


def _infer_command_bindings(commands):
    interactions = []
    for index, command in enumerate(commands):
        point_type = {'pick': 'grasp', 'put': 'place'}.get(command.get('skill_name'))
        if point_type is None:
            continue
        target = command.get('parameters', {}).get('target')
        if isinstance(target, bool) or not isinstance(target, (int, str)):
            raise ValueError(f"{command['skill_name']} requires an object target")
        interactions.append((index, str(target), point_type))

    interaction_targets = {target for _, target, _ in interactions}
    bindings = {str(command.get('command_id', index)): None
                for index, command in enumerate(commands)}
    place_cycles = {}
    for index, command in enumerate(commands):
        target = command.get('parameters', {}).get('target')
        if (command.get('skill_name') == 'move_to' and str(target) in interaction_targets):
            bindings[str(command.get('command_id', index))] = 'grasp'
    for interaction_index, target, point_type in interactions:
        command_id = str(commands[interaction_index].get('command_id', interaction_index))
        bindings[command_id] = point_type
        previous = max((index for index, other, _ in interactions
                        if other == target and index < interaction_index), default=-1)
        following = min((index for index, other, _ in interactions
                         if other == target and index > interaction_index), default=len(commands))
        approach = max((index for index in range(previous + 1, interaction_index)
                        if commands[index].get('skill_name') == 'approach'
                        and str(commands[index].get('parameters', {}).get('target')) == target),
                       default=None)
        retreat = min((index for index in range(interaction_index + 1, following)
                       if commands[index].get('skill_name') == 'retreat'
                       and str(commands[index].get('parameters', {}).get('target')) == target),
                      default=None)
        if approach is not None:
            bindings[str(commands[approach].get('command_id', approach))] = point_type
        if retreat is not None:
            bindings[str(commands[retreat].get('command_id', retreat))] = point_type
        if point_type == 'place':
            for bound_index in (approach, interaction_index, retreat):
                if bound_index is not None:
                    place_cycles[str(commands[bound_index].get('command_id', bound_index))] = command_id
    return bindings, place_cycles
