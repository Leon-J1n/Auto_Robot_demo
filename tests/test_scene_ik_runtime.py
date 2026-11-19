"""Opt-in real scene IK verification, without executing motion."""
import os
import json
from pathlib import Path
import numpy as np
import pytest


def test_scene_chain_uses_validated_solver_fallback():
    source = os.environ.get('CONTROL_STATE_TEST_SCENE')
    assets = os.environ.get('CONTROL_STATE_TEST_ASSETS')
    if not source or not assets:
        pytest.skip('explicit scene and OLS assets required')
    from execution.resources.resolver import SceneResourceCoordinator, component, pose_matrix
    from libraries.eaik import load_urdf
    scene = json.loads(Path(source).read_text())
    task = {'schema_version': '2.0', 'request_id': 'ik-state-check',
            'scene_id': scene['scene_id'], 'scene_version': scene['scene_version'], 'commands': []}
    resources = SceneResourceCoordinator(scene_file=source, asset_root=assets).resolve(task)
    ref = component(resources.robot_object, 'RobotModelRef')
    kin = load_urdf(resources.robot_urdf, base_link=ref['base_link'], tip_link=ref['tip_link'],
                    tcp_transform=ref.get('tcp_transform'), world_from_base=pose_matrix(resources.robot_object),
                    initial_joints=resources.initial_joints, validation_backend='auto')
    q = resources.initial_joints.copy()
    q[0] += .01
    target = kin.forward(q)
    solution = kin.solve_one(target, seed=resources.initial_joints, backend='auto', timeout=2.)
    actual = kin.forward(solution.solutions[0])
    assert np.linalg.norm(actual['position'] - target['position']) < 2e-4
    assert np.allclose(actual['rotation_matrix'], target['rotation_matrix'], atol=2e-3)
