"""Read joint timing limits in controller order from the robot URDF."""
import math
import xml.etree.ElementTree as ET


def read_urdf_motion_limits(path, joint_names):
    joints = {joint.get('name'): joint for joint in ET.parse(path).getroot().findall('joint')}
    velocities, accelerations, sources = [], [], []
    for name in joint_names:
        joint = joints.get(name)
        limit = joint.find('limit') if joint is not None else None
        if limit is None or limit.get('velocity') is None:
            raise ValueError(f'URDF joint {name} has no velocity limit')
        velocity = float(limit.get('velocity'))
        acceleration_value = limit.get('acceleration')
        acceleration = velocity if acceleration_value is None else float(acceleration_value)
        if not all(math.isfinite(value) and value > 0 for value in (velocity, acceleration)):
            raise ValueError(f'URDF joint {name} requires positive finite velocity/acceleration limits')
        velocities.append(velocity)
        accelerations.append(acceleration)
        sources.append('velocity_numeric_fallback' if acceleration_value is None else 'urdf.limit.acceleration')
    return {'max_velocity': velocities, 'max_acceleration': accelerations,
            'limit_source': {'urdf': str(path), 'joint_names': list(joint_names),
                             'velocity': 'urdf.limit.velocity', 'acceleration': sources}}
