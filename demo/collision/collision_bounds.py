"""Conservative broad phase for capped MuJoCo geometry-distance queries."""
import mujoco
import numpy as np


def exact_distance_mask(model, data, pairs, distance_caps):
    """False means mj_geomDistance would return its supplied distance cap.

    Transform MuJoCo's local bounding boxes to world AABBs. Only reject a
    pair when these enclosing boxes are farther apart than its query cap.
    Unknown/unbounded shapes always keep the original exact query.
    """
    pairs = np.asarray(pairs, dtype=np.intp).reshape(-1, 2)
    if not len(pairs) or not hasattr(model, 'geom_aabb'):
        return np.ones(len(pairs), dtype=bool)
    local = model.geom_aabb
    rotation = data.geom_xmat.reshape(-1, 3, 3)
    centers = data.geom_xpos + np.einsum('nij,nj->ni', rotation, local[:, :3])
    extents = np.einsum('nij,nj->ni', np.abs(rotation), local[:, 3:])
    # Inflate rather than round inward at a threshold or rotated-box boundary.
    extents = extents + 1e-12 * (1. + np.abs(centers) + extents)
    a, z = pairs[:, 0], pairs[:, 1]
    separation = np.maximum(0., np.abs(centers[a]-centers[z])-extents[a]-extents[z])
    lower_bound = np.linalg.norm(separation, axis=1)
    supported = ((model.geom_type >= int(mujoco.mjtGeom.mjGEOM_SPHERE))
                 & (model.geom_type <= int(mujoco.mjtGeom.mjGEOM_MESH))
                 & np.all(np.isfinite(local), axis=1)
                 & np.all(local[:, 3:] >= 0., axis=1))
    caps = np.asarray(distance_caps, dtype=float)
    exact = ~(supported[a] & supported[z] & (lower_bound > caps))
    remaining = np.flatnonzero(exact & supported[a] & supported[z])
    if len(remaining):
        # Thin finger meshes rotate together: world AABBs can overlap even
        # when the boxes are far apart. Bound separation in each box's frame
        # as well; each projected box still encloses its original geometry.
        aa, zz = a[remaining], z[remaining]
        ra, rz = rotation[aa], rotation[zz]
        delta = centers[aa] - centers[zz]
        relative = np.einsum('nki,nkj->nij', ra, rz)
        ha = local[aa, 3:] + 1e-12 * (1. + np.abs(centers[aa]) + local[aa, 3:])
        hz = local[zz, 3:] + 1e-12 * (1. + np.abs(centers[zz]) + local[zz, 3:])
        in_a = np.einsum('nki,nk->ni', ra, delta)
        in_z = np.einsum('nki,nk->ni', rz, delta)
        gap_a = np.maximum(0., np.abs(in_a)-ha-np.einsum('nij,nj->ni', np.abs(relative), hz))
        gap_z = np.maximum(0., np.abs(in_z)-hz-np.einsum('nji,nj->ni', np.abs(relative), ha))
        lower = np.maximum(np.linalg.norm(gap_a, axis=1), np.linalg.norm(gap_z, axis=1))
        exact[remaining] = lower <= caps[remaining]
    return exact
