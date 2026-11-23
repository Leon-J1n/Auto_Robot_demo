import hashlib
import json
from pathlib import Path

import pytest

from collision.assets.bundle import CollisionBundle, CollisionBundleError


def _make_bundle(tmp_path, *, status="verified"):
    root = tmp_path / "bundle"
    (root / "reference_bundle" / "blobs").mkdir(parents=True)
    (root / "reference" / "cache_evidence" / "key").mkdir(parents=True)
    source = tmp_path / "source.obj"
    source.write_text("v 0 0 0\nf 1 1 1\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    part = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
    part_hash = hashlib.sha256(part).hexdigest()
    (root / "reference_bundle" / "blobs" / f"{part_hash}.obj").write_bytes(part)
    (root / "reference_bundle" / "bundle.json").write_text(json.dumps({
        "schema_version": 1, "review_status": status, "links": []
    }), encoding="utf-8")
    (root / "reference" / "cache_evidence" / "key" / "manifest.json").write_text(json.dumps({
        "identity": {"schema": 2, "source": digest, "scale": [1.0, 1.0, 1.0]},
        "parts": [{"file": "part_0000.obj", "sha256": part_hash, "volume": 1.0}]
    }), encoding="utf-8")
    return root, source


def test_unreviewed_bundle_requires_explicit_opt_in(tmp_path):
    root, _ = _make_bundle(tmp_path, status="local_success_reference_unreviewed")
    with pytest.raises(CollisionBundleError, match="unverified"):
        CollisionBundle(root)
    assert CollisionBundle(root, allow_unreviewed=True).bundle_id


def test_bundle_mesh_validates_and_returns_content_addressed_part(tmp_path):
    root, source = _make_bundle(tmp_path)
    mesh = CollisionBundle(root).mesh(source)
    assert mesh is not None
    assert len(mesh.parts) == 1
    assert mesh.parts[0].is_file()
    assert mesh.volumes == (1.0,)


def test_bundle_hash_mismatch_fails_closed(tmp_path):
    root, source = _make_bundle(tmp_path)
    blob = next((root / "reference_bundle" / "blobs").glob("*.obj"))
    blob.write_text("corrupt", encoding="utf-8")
    with pytest.raises(CollisionBundleError, match="hash_mismatch"):
        CollisionBundle(root).mesh(source)
