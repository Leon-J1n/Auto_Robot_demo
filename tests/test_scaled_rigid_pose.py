import numpy as np
import pytest

from execution.resources.resolver import pose_matrix


def object_with_scale(scale, extra=None):
    return {'components': [
        {'component_type': 'Transform', 'properties': {
            'position': [0, 0, .2], 'scale': scale}},
        {'component_type': 'Collider', 'properties': {
            'shape': 'box', 'size': [1, 1, 1]}},
    ] + (extra or [])}


def test_scaled_pedestal_keeps_rigid_pose():
    pose = pose_matrix(object_with_scale([1, 1, .9]))
    np.testing.assert_allclose(pose[:3, :3], np.eye(3))
    np.testing.assert_allclose(pose[:3, 3], [0, 0, .2])


@pytest.mark.parametrize('scale', [[0, 1, 1], [-1, 1, 1], [float('nan'), 1, 1], [1, 1]])
def test_invalid_scale_rejected(scale):
    with pytest.raises(ValueError, match='finite positive'):
        pose_matrix(object_with_scale(scale))


@pytest.mark.parametrize('kind', ['RobotModelRef', 'ArticulatedModel'])
def test_articulated_scale_still_rejected(kind):
    with pytest.raises(ValueError, match='Scaled articulated'):
        pose_matrix(object_with_scale([1, 1, .9], [
            {'component_type': kind, 'properties': {'urdf': 'robot.urdf'}}]))
