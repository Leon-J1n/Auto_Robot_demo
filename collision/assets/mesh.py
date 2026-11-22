"""Content-addressed convex collision assets shared by FCL and MuJoCo.

Authored URDF colliders are preserved by their importers. Standalone meshes
without authored colliders use this pipeline; source/visual assets are untouched.
"""
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import tempfile

import numpy as np
import trimesh


CACHE_ROOT = Path(__file__).resolve().parents[2] / '.cache' / 'collision'
SETTINGS = dict(threshold=0.05, real_metric=False, preprocess_mode='auto',
                preprocess_resolution=50, resolution=500, mcts_iterations=10,
                mcts_nodes=5, mcts_max_depth=2, seed=0)


@dataclass(frozen=True)
class CollisionMesh:
    parts: tuple[Path, ...]
    volumes: tuple[float, ...]
    key: str


def bound_convex_parts(hulls, max_parts, max_vertices=None):
    """Conservatively combine nearest parts when native merging hits a limit."""
    hulls = list(hulls)
    while max_parts > 0 and len(hulls) > max_parts:
        centers = np.array([h.bounds.mean(axis=0) for h in hulls])
        distances = np.linalg.norm(centers[:, None] - centers[None, :], axis=-1)
        np.fill_diagonal(distances, np.inf)
        a, b = sorted(np.unravel_index(np.argmin(distances), distances.shape))
        merged = trimesh.convex.convex_hull(np.vstack((hulls[a].vertices, hulls[b].vertices)))
        hulls[a] = merged
        hulls.pop(b)
    if max_vertices:
        if max_vertices < 8:
            raise ValueError('Conservative convex budget enforcement requires at least 8 vertices')
        hulls = [h.bounding_box.to_mesh() if len(h.vertices) > max_vertices else h for h in hulls]
    return hulls


def prepare_mesh(source, *, scale=(1, 1, 1), cache_root=None, settings=None):
    """Build once, validate cached bytes, and fail explicitly on invalid geometry."""
    import coacd
    settings = {**SETTINGS, **(settings or {})}
    max_input_faces = settings.pop('max_input_faces', None)
    source = Path(source).resolve()
    scale = np.asarray(scale, dtype=float)
    if scale.shape != (3,) or not np.all(np.isfinite(scale)) or np.any(scale <= 0):
        raise ValueError('Collision mesh scale must contain three positive finite values')
    identity = dict(schema=2, source=sha256(source.read_bytes()).hexdigest(),
                    scale=scale.tolist(), settings=settings, coacd=version('coacd'),
                    trimesh=version('trimesh'))
    if max_input_faces:
        identity.update(max_input_faces=max_input_faces, simplifier=version('fast-simplification'))
    key = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    directory = Path(cache_root or CACHE_ROOT) / key
    manifest = directory / 'manifest.json'
    if directory.exists() and not manifest.exists():
        raise ValueError(f'Incomplete collision cache: {directory}')
    if manifest.exists():
        record = json.loads(manifest.read_text(encoding='utf-8'))
        parts = tuple(directory / p['file'] for p in record['parts'])
        if record['identity'] != identity or not parts or any(
                not np.isfinite(p['volume']) or p['volume'] <= 0 for p in record['parts']) or any(
                sha256(p.read_bytes()).hexdigest() != r['sha256']
                for p, r in zip(parts, record['parts'])):
            raise ValueError(f'Invalid collision cache: {directory}')
        return CollisionMesh(parts, tuple(p['volume'] for p in record['parts']), key)
    mesh = trimesh.load(source, force='mesh', process=True)
    # OBJ texture/normal seams duplicate positions, not physical boundaries.
    mesh.merge_vertices(merge_tex=True, merge_norm=True)
    mesh.remove_unreferenced_vertices()
    mesh.apply_scale(scale)
    if not len(mesh.faces) or not np.isfinite(mesh.vertices).all():
        raise ValueError(f'Invalid collision mesh: {source}')
    if max_input_faces and len(mesh.faces) > max_input_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=max_input_faces)
    print(f'Collision preprocessing: {source.name}', flush=True)
    if mesh.is_convex and not (settings.get('decimate') and
                               len(mesh.convex_hull.vertices) > settings.get('max_ch_vertex', 256)):
        hulls = [mesh.convex_hull]
    else:
        coacd.set_log_level(os.environ.get('COACD_LOG_LEVEL', 'warn'))
        native_settings = dict(settings)
        # Forcing CoACD to meet a small hull cap can spend minutes attempting
        # impossible native merges. Enforce that cap deterministically below.
        if native_settings.get('max_convex_hull', -1) > 0:
            native_settings['max_convex_hull'] = -1
        hulls = [trimesh.Trimesh(v, f, process=True).convex_hull for v, f in
                 coacd.run_coacd(coacd.Mesh(np.asarray(mesh.vertices, dtype=np.float64),
                                           np.asarray(mesh.faces, dtype=np.int32)), **native_settings)]
    hulls = bound_convex_parts(hulls, settings.get('max_convex_hull', -1),
                              settings.get('max_ch_vertex', 256) if settings.get('decimate') else None)
    if not hulls or any(not h.is_volume or h.volume <= 1e-15 for h in hulls):
        raise ValueError(f'Convex decomposition produced invalid solids: {source}')
    if settings.get('max_convex_hull', -1) > 0 and len(hulls) > settings['max_convex_hull']:
        raise ValueError(f'Convex decomposition exceeded its part budget: {source}')
    if settings.get('decimate') and any(len(h.vertices) > settings.get('max_ch_vertex', 256) for h in hulls):
        raise ValueError(f'Convex decomposition exceeded its vertex budget: {source}')
    directory.parent.mkdir(parents=True, exist_ok=True)
    records = []
    # Publish the whole directory atomically, so parallel readers never see
    # partially written parts. A concurrent builder may win the same key.
    with tempfile.TemporaryDirectory(dir=directory.parent, prefix=key + '.') as tmp:
        staging = Path(tmp)
        for i, hull in enumerate(hulls):
            path = staging / f'part_{i:04d}.obj'
            data = trimesh.exchange.obj.export_obj(hull, include_texture=False).encode()
            path.write_bytes(data)
            records.append(dict(file=path.name, sha256=sha256(data).hexdigest(), volume=float(hull.volume)))
        (staging / 'manifest.json').write_text(json.dumps(
            dict(identity=identity, source=str(source), parts=records), indent=2), encoding='utf-8')
        try:
            os.rename(staging, directory)
        except FileExistsError:
            if not manifest.exists():
                raise ValueError(f'Incomplete collision cache: {directory}')
            return prepare_mesh(source, scale=scale, cache_root=cache_root,
                                settings={**settings, **({'max_input_faces': max_input_faces} if max_input_faces else {})})
    print(f'Collision ready: {source.name}, {len(hulls)} convex parts', flush=True)
    return CollisionMesh(tuple(directory / p['file'] for p in records),
                         tuple(p['volume'] for p in records), key)
