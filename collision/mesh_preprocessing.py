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


CACHE_ROOT = Path(__file__).resolve().parents[1] / '.cache' / 'collision'
SETTINGS = dict(threshold=0.05, real_metric=False, preprocess_mode='auto',
                preprocess_resolution=50, resolution=500, mcts_iterations=10,
                mcts_nodes=5, mcts_max_depth=2, seed=0)


@dataclass(frozen=True)
class CollisionMesh:
    parts: tuple[Path, ...]
    volumes: tuple[float, ...]
    key: str


def prepare_mesh(source, *, scale=(1, 1, 1), cache_root=None):
    """Build once, validate cached bytes, and fail explicitly on invalid geometry."""
    import coacd
    source = Path(source).resolve()
    scale = np.asarray(scale, dtype=float)
    if scale.shape != (3,) or not np.all(np.isfinite(scale)) or np.any(scale <= 0):
        raise ValueError('Collision mesh scale must contain three positive finite values')
    identity = dict(schema=2, source=sha256(source.read_bytes()).hexdigest(),
                    scale=scale.tolist(), settings=SETTINGS, coacd=version('coacd'),
                    trimesh=version('trimesh'))
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
    print(f'Collision preprocessing: {source.name}', flush=True)
    if mesh.is_convex:
        hulls = [mesh.convex_hull]
    else:
        coacd.set_log_level(os.environ.get('COACD_LOG_LEVEL', 'warn'))
        hulls = [trimesh.Trimesh(v, f, process=True).convex_hull for v, f in
                 coacd.run_coacd(coacd.Mesh(np.asarray(mesh.vertices, dtype=np.float64),
                                           np.asarray(mesh.faces, dtype=np.int32)), **SETTINGS)]
    if not hulls or any(not h.is_volume or h.volume <= 1e-15 for h in hulls):
        raise ValueError(f'Convex decomposition produced invalid solids: {source}')
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
            return prepare_mesh(source, scale=scale, cache_root=cache_root)
    print(f'Collision ready: {source.name}, {len(hulls)} convex parts', flush=True)
    return CollisionMesh(tuple(directory / p['file'] for p in records),
                         tuple(p['volume'] for p in records), key)
