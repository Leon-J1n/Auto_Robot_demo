"""Restore dependent joints from authored equality constraints, without stepping."""
import mujoco
import numpy as np


def restore_dependent_joints(model, data, restored_joint_ids):
    known = set(restored_joint_ids)
    for _ in range(model.neq + 1):
        changed = False
        for eq in range(model.neq):
            if not data.eq_active[eq] or int(model.eq_type[eq]) != int(mujoco.mjtEq.mjEQ_JOINT):
                continue
            child, parent = int(model.eq_obj1id[eq]), int(model.eq_obj2id[eq])
            if parent not in known or child in known or child < 0:
                continue
            if any(int(model.jnt_type[j]) not in (2, 3) for j in (child, parent)):
                raise ValueError('Cannot restore dependent non-scalar joint')
            parent_address, child_address = model.jnt_qposadr[parent], model.jnt_qposadr[child]
            value = float(model.qpos0[child_address] + np.polynomial.polynomial.polyval(
                data.qpos[parent_address] - model.qpos0[parent_address], model.eq_data[eq, :5]))
            if not np.isfinite(value):
                raise ValueError('Non-finite dependent joint state')
            data.qpos[model.jnt_qposadr[child]] = value
            known.add(child)
            changed = True
        if not changed:
            break
