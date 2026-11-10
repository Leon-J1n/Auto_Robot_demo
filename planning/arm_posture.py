"""Soft preference for an elevated arm, evaluated on a kinematic scratch state."""
from collections import OrderedDict
import json
from pathlib import Path

import mujoco
import numpy as np


CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config' / 'arm_posture.json'


class ArmPosturePreference:
    def __init__(self, runtime, config=None):
        self.runtime = runtime
        self.model = runtime.model
        self.config = dict(config if config is not None else json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
        if not isinstance(self.config.get('enabled'), bool):
            raise ValueError('arm_posture.enabled must be a boolean')
        for key in ('weight_rad_per_m', 'elbow_height_above_reference_m',
                    'arm_height_above_reference_m', 'arm_radius_m'):
            value = self.config[key]
            if isinstance(value, bool) or not np.isfinite(value) or value < 0:
                raise ValueError(f'arm_posture.{key} must be finite and nonnegative')
        probability = self.config['rrt_preferred_sample_probability']
        if isinstance(probability, bool) or not np.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('arm_posture.rrt_preferred_sample_probability must be in [0, 1]')
        # Resolve the arm chain rather than assuming particular joint angle signs.
        self.joints = tuple(int(j) for j in runtime.joint_ids)
        self.enabled = bool(self.config['enabled']) and len(self.joints) >= 4
        self.data = mujoco.MjData(self.model) if self.enabled else None
        self.cache = OrderedDict()
        self.table_height = self._table_height() if self.enabled else None

    def _table_height(self):
        """Read static table tops once; no scene/asset writes or physics steps."""
        m, d = self.model, self.runtime.data
        tops = []
        for geom in range(m.ngeom):
            body = int(m.geom_bodyid[geom])
            if int(m.body_weldid[body]) != 0:
                continue
            names = [mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, geom) or '']
            ancestor = body
            while ancestor:
                names.append(mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, ancestor) or '')
                ancestor = int(m.body_parentid[ancestor])
            if not any('table' in name.lower() for name in names):
                continue
            if int(m.geom_type[geom]) == int(mujoco.mjtGeom.mjGEOM_PLANE):
                continue
            rotation = d.geom_xmat[geom].reshape(3, 3)
            center = d.geom_xpos[geom] + rotation @ m.geom_aabb[geom, :3]
            extent = np.abs(rotation) @ m.geom_aabb[geom, 3:]
            tops.append(float(center[2] + extent[2]))
        return max(tops) if tops else None

    def metrics(self, joints):
        if not self.enabled:
            return {'enabled': False, 'penalty_rad': 0.}
        key = np.asarray(joints, dtype=float).tobytes()
        if key in self.cache:
            self.cache.move_to_end(key)
            return dict(self.cache[key])
        self.data.qpos[:] = self.runtime.data.qpos
        self.data.qpos[self.runtime.qpos_indices] = joints
        mujoco.mj_kinematics(self.model, self.data)
        shoulder, elbow, wrist = self.data.xanchor[list(self.joints[1:4])]
        reference = max(float(shoulder[2]), self.table_height if self.table_height is not None else -np.inf)
        # Exclude the wrist/hand: they must descend to the task target. Sample
        # both long links so a high elbow cannot hide a low forearm.
        upper = [shoulder + t * (elbow - shoulder) for t in (.25, .5, .75, 1.)]
        forearm = [elbow + t * (wrist - elbow) for t in (0., .25, .5, .75)]
        heights = np.asarray(upper + forearm)[:, 2] - self.config['arm_radius_m']
        elbow_deficit = max(0., reference + self.config['elbow_height_above_reference_m'] - elbow[2])
        arm_deficit = float(np.mean(np.maximum(0., reference + self.config['arm_height_above_reference_m'] - heights)))
        result = {'enabled': True, 'penalty_rad': float(self.config['weight_rad_per_m'] *
                    (.6 * elbow_deficit + .4 * arm_deficit)),
                  'elbow_height_m': float(elbow[2]), 'sampled_arm_lowest_z_m': float(heights.min()),
                  'reference_height_m': reference, 'table_height_m': self.table_height}
        self.cache[key] = result
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return dict(result)

    def penalty(self, joints):
        return self.metrics(joints)['penalty_rad']

    def path_penalty(self, path):
        """Average by joint arc length, independent of input waypoint density."""
        points = np.asarray(path, dtype=float)
        lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
        total = float(lengths.sum())
        if total < 1e-12:
            return self.penalty(points[0])
        distances = np.r_[0., np.cumsum(lengths)]
        samples = np.linspace(0., total, 13)
        joints = np.column_stack([np.interp(samples, distances, points[:, j]) for j in range(points.shape[1])])
        penalties = np.asarray([self.penalty(q) for q in joints])
        return float(np.mean((penalties[:-1] + penalties[1:]) * .5))


def preference_for(kinematics):
    preference = getattr(kinematics, 'arm_posture', None)
    return preference if isinstance(preference, ArmPosturePreference) and preference.enabled else None


def configuration_cost(kinematics, joints, reference, *, norm=1):
    delta = np.asarray(joints, dtype=float) - np.asarray(reference, dtype=float)
    travel = float(np.linalg.norm(np.arctan2(np.sin(delta), np.cos(delta)), ord=norm))
    preference = preference_for(kinematics)
    return travel + (preference.penalty(joints) if preference is not None else 0.)


def path_cost(kinematics, path):
    travel = float(np.sum(np.abs(np.diff(np.asarray(path, dtype=float), axis=0))))
    preference = preference_for(kinematics)
    return travel + (preference.path_penalty(path) if preference is not None else 0.)
