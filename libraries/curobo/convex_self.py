"""CUDA separation certificates for selected convex self-collision pairs."""

from itertools import combinations

import numpy as np
import torch
import warp as wp

from curobo._src.util.warp import get_warp_device_stream


@wp.func
def project(points: wp.array(dtype=wp.vec3), start: int, count: int, axis: wp.vec3):
    low = wp.dot(points[start], axis)
    high = low
    first = start
    last = start
    for offset in range(1, count):
        index = start + offset
        value = wp.dot(points[index], axis)
        if value < low:
            low = value
            first = index
        if value > high:
            high = value
            last = index
    return low, high, first, last


@wp.kernel(enable_backward=False)
def separating_axis(
    bases: wp.array(dtype=wp.mat33), origins: wp.array(dtype=wp.vec3),
    centers: wp.array(dtype=wp.vec3), vertices: wp.array(dtype=wp.vec3),
    normals: wp.array(dtype=wp.vec3), spans: wp.array2d(dtype=wp.int32),
    pairs: wp.array2d(dtype=wp.int32), frame_count: int, margin: float,
    gaps: wp.array2d(dtype=wp.float32), directions: wp.array2d(dtype=wp.vec3),
    witnesses: wp.array3d(dtype=wp.int32),
):
    sample, pair = wp.tid()
    first = pairs[pair, 0]
    second = pairs[pair, 1]
    frame_a = sample * frame_count + spans[first, 4]
    frame_b = sample * frame_count + spans[second, 4]
    basis_a = bases[frame_a]
    basis_b = bases[frame_b]
    relative = origins[frame_b] - origins[frame_a]
    normal_a = spans[first, 3]
    normal_b = spans[second, 3]
    best = float(-1.0e10)
    direction = wp.vec3(0.0, 0.0, 0.0)
    witness_a = spans[first, 0]
    witness_b = spans[second, 0]
    for index in range(-1, normal_a + normal_b):
        axis = wp.vec3(0.0, 0.0, 0.0)
        if index == -1:
            axis = relative + basis_b * centers[second] - basis_a * centers[first]
        elif index < normal_a:
            axis = basis_a * normals[spans[first, 2] + index]
        else:
            axis = basis_b * normals[spans[second, 2] + index - normal_a]
        length = wp.length(axis)
        if length > 1.0e-8:
            axis = axis / length
            lo_a, hi_a, min_a, max_a = project(
                vertices, spans[first, 0], spans[first, 1], wp.transpose(basis_a) * axis)
            lo_b, hi_b, min_b, max_b = project(
                vertices, spans[second, 0], spans[second, 1], wp.transpose(basis_b) * axis)
            offset = wp.dot(relative, axis)
            forward = offset + lo_b - hi_a
            reverse = lo_a - hi_b - offset
            if forward > best and forward >= reverse:
                best = forward
                direction = axis
                witness_a = max_a
                witness_b = min_b
            elif reverse > best:
                best = reverse
                direction = -axis
                witness_a = min_a
                witness_b = max_b
            if best >= margin:
                break
    gaps[sample, pair] = best
    directions[sample, pair] = direction
    witnesses[sample, pair, 0] = witness_a
    witnesses[sample, pair, 1] = witness_b


def anchor_frame(positions):
    """Select three non-collinear anchors from existing FK sphere centers."""
    center = np.mean(positions, axis=0)
    origin = np.argmax(np.linalg.norm(positions - center, axis=1))
    relative = positions - positions[origin]
    farthest = np.argmax(np.linalg.norm(relative, axis=1))
    cross = np.cross(relative, relative[farthest])
    planar = np.argmax(np.linalg.norm(cross, axis=1))
    normal = np.cross(relative[farthest], relative[planar])
    length = np.linalg.norm(normal)
    if length < 1.e-6:
        return None
    indices = [origin, farthest, planar]
    basis = np.column_stack((relative[farthest], relative[planar], normal * (.05 / length)))
    if np.linalg.svd(basis, compute_uv=False)[-1] < .001:
        return None
    return indices, np.linalg.inv(basis)


class ConvexSelf:
    def __init__(self, data, robot, converted, compiled):
        wp.init()
        kin = robot.kinematics.kinematics_config
        config = robot.kinematics.self_collision_config
        device = config.collision_pairs.device
        geometry_links = {gid: name for name, gid in converted['geometry_links'].items()}
        groups = {}
        shapes = data['shapes']
        source_pairs = {frozenset(pair) for pair in data['pairs']}
        local_spheres = kin.link_spheres[0, :, :3].cpu().numpy()
        sphere_ids = kin.link_sphere_idx_map.cpu().numpy()
        geom_by_sphere = np.full(len(sphere_ids), -1, dtype=np.int32)
        frame_indices, inverse_bases, local_origins = [], [], []
        for shape in shapes:
            link_idx = kin.link_name_to_idx_map[geometry_links[shape['id']]]
            indices = np.flatnonzero(sphere_ids == link_idx)
            geom_by_sphere[indices] = shape['id']
            groups.setdefault(shape['body'], []).extend(indices)
        frames = {}
        for body, indices in groups.items():
            frame = anchor_frame(local_spheres[indices])
            if frame is None:
                continue
            selected, inverse = frame
            anchors = np.asarray(indices)[selected]
            frames[body] = len(frame_indices)
            frame_indices.append(anchors)
            inverse_bases.append(inverse)
            local_origins.append(local_spheres[anchors[0]])

        native_pairs = config.collision_pairs
        geom_pairs = geom_by_sphere[native_pairs.cpu().numpy()]
        active_pairs = {tuple(sorted(pair)) for pair in geom_pairs if min(pair) >= 0}
        pairs, replaced = [], set()
        for (first, shape_a), (second, shape_b) in combinations(enumerate(shapes), 2):
            pair = tuple(sorted((shape_a['id'], shape_b['id'])))
            if (frozenset((shape_a['name'], shape_b['name'])) in source_pairs
                    and pair in active_pairs and pair not in compiled['allowed']
                    and shape_a['body'] in frames and shape_b['body'] in frames):
                pairs.append((first, second))
                replaced.add(pair)
        self.pair_count = len(pairs)
        if not self.pair_count:
            return

        centers, vertices, normals, spans = [], [], [], []
        for shape in shapes:
            frame = frames.get(shape['body'])
            if frame is None:
                spans.append((0, 0, 0, 0, 0))
                centers.append([0., 0., 0.])
                continue
            points = np.asarray(shape['vertices']) - local_origins[frame]
            directions = shape['normals']
            spans.append((len(vertices), len(points), len(normals), len(directions), frame))
            centers.append(points.mean(axis=0))
            vertices.extend(points)
            normals.extend(directions)

        def tensor(values, dtype):
            return torch.as_tensor(np.asarray(values), device=device, dtype=dtype).contiguous()

        self.anchors = tensor(frame_indices, torch.long)
        self.inverse = tensor(inverse_bases, torch.float32)
        self.centers = tensor(centers, torch.float32)
        self.vertices = tensor(vertices, torch.float32)
        self.normals = tensor(normals, torch.float32)
        self.spans = tensor(spans, torch.int32)
        self.pairs = tensor(pairs, torch.int32)
        self.pair_frames = self.spans[self.pairs.long(), 4].long()
        self.margin = compiled['margin'] + 2.e-6
        keep = [tuple(sorted(pair)) not in replaced for pair in geom_pairs]
        config.collision_pairs = native_pairs[tensor(keep, torch.bool)].contiguous()
        config.convex_self = self

    def clearance_cost(self, spheres):
        batch, horizon = spheres.shape[:2]
        selected = spheres.reshape(batch * horizon, -1, 4)[:, self.anchors, :3]
        origins = selected[:, :, 0].contiguous()
        first, second = (selected[:, :, 1:] - origins.unsqueeze(2)).unbind(dim=2)
        normal = torch.nn.functional.normalize(torch.cross(first, second, dim=-1), dim=-1) * .05
        edges = torch.stack((first, second, normal), dim=-1)
        bases = (edges.unsqueeze(-1) * self.inverse[:, None]).sum(dim=-2).contiguous()
        count = batch * horizon
        gaps = spheres.new_empty((count, self.pair_count))
        directions = spheres.new_empty((count, self.pair_count, 3))
        witnesses = torch.empty((count, self.pair_count, 2), dtype=torch.int32, device=spheres.device)
        device, stream = get_warp_device_stream(spheres)
        wp.launch(separating_axis, dim=gaps.shape, device=device, stream=stream, inputs=[
            wp.from_torch(bases.detach().view(-1, 3, 3), dtype=wp.mat33),
            wp.from_torch(origins.detach().view(-1, 3), dtype=wp.vec3),
            wp.from_torch(self.centers, dtype=wp.vec3), wp.from_torch(self.vertices, dtype=wp.vec3),
            wp.from_torch(self.normals, dtype=wp.vec3), wp.from_torch(self.spans), wp.from_torch(self.pairs),
            len(self.anchors), self.margin, wp.from_torch(gaps),
            wp.from_torch(directions, dtype=wp.vec3), wp.from_torch(witnesses)])
        gap, worst = gaps.min(dim=1)
        if spheres.requires_grad:
            samples = torch.arange(count, device=spheres.device)
            frame = self.pair_frames[worst]
            points = self.vertices[witnesses[samples, worst].long()]
            local = (bases[samples[:, None], frame] @ points.unsqueeze(-1)).squeeze(-1)
            offset = origins[samples, frame[:, 1]] - origins[samples, frame[:, 0]]
            projection = ((offset + local[:, 1] - local[:, 0]) * directions[samples, worst]).sum(dim=-1)
            gap = gap + (projection - projection.detach())
        return (self.margin - gap).clamp_min(0.).view(batch, horizon, 1)
