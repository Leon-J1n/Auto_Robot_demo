"""Measure independent chain FK/Jacobian errors against the scene reference."""
import argparse
import json
from pathlib import Path

import numpy as np

from kinematics.models.mujoco_model import MujocoRobotModel
from utils import SceneRobotRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenes', nargs='+', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=200)
    parser.add_argument('--output', type=Path, default=Path(__file__).parent/'checks/scene_comparison.json')
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    summary = []
    for scene in args.scenes:
        runtime = SceneRobotRuntime(scene)
        try:
            independent = runtime.kinematics
            reference = MujocoRobotModel(runtime)
            limits = independent.limits
            q_samples = np.vstack((np.zeros(len(limits)), runtime.get_joint_positions(),
                                   np.random.default_rng(20261007).uniform(limits[:, 0], limits[:, 1], (args.samples, len(limits)))))
            max_position = max_rotation = max_jacobian = 0.
            live = runtime.data.qpos.copy()
            for q in q_samples:
                actual, expected = independent.forward(q), reference.forward(q)
                max_position = max(max_position, float(np.linalg.norm(actual['position']-expected['position'])))
                max_rotation = max(max_rotation, float(np.max(np.abs(actual['rotation_matrix']-expected['rotation_matrix']))))
                max_jacobian = max(max_jacobian, float(np.max(np.abs(independent.jacobian(q)-reference.jacobian(q)))))
            np.testing.assert_array_equal(live, runtime.data.qpos)
            if max(max_position, max_rotation, max_jacobian) > 2e-12:
                raise ValueError(f'Chain geometry disagrees with MuJoCo: {scene}')
            summary.append({'scene': str(scene), 'sample_count': len(q_samples),
                            'max_position_error_m': max_position,
                            'max_rotation_element_error': max_rotation,
                            'max_jacobian_element_error': max_jacobian,
                            'live_qpos_unchanged': True})
        finally:
            runtime.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
