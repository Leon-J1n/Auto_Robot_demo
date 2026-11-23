"""Read-only, content-addressed collision bundle loader.

Bundles are generated outside the application.  This module only verifies and
loads published bytes; it never runs convex decomposition and never falls
back to local generation when a bundle is explicitly selected.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re


class CollisionBundleError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _blob_path(blobs: Path, digest: str) -> Path:
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CollisionBundleError("collision_bundle_invalid_blob_name")
    return blobs / f"{digest}.obj"


@dataclass(frozen=True)
class BundleMesh:
    parts: tuple[Path, ...]
    volumes: tuple[float, ...]
    key: str


class CollisionBundle:
    """Validated bundle rooted at a directory containing ``reference_bundle``."""

    def __init__(self, root: Path, *, allow_unreviewed: bool = False):
        self.root = Path(root).expanduser().resolve()
        self.bundle_root = self.root / "reference_bundle"
        manifest = self.bundle_root / "bundle.json"
        if not manifest.is_file():
            raise CollisionBundleError("collision_bundle_manifest_missing")
        self.manifest = json.loads(manifest.read_text(encoding="utf-8"))
        status = self.manifest.get("review_status")
        if status != "verified" and not allow_unreviewed:
            raise CollisionBundleError("collision_bundle_unverified")
        self.bundle_id = self.manifest.get("bundle_id") or _digest(manifest)
        self.blobs = self.bundle_root / "blobs"
        if not self.blobs.is_dir():
            raise CollisionBundleError("collision_bundle_blobs_missing")
        self._links = {x.get("link_name"): x for x in self.manifest.get("links", [])}
        self._mesh_manifests = self._index_mesh_manifests()
        self._validate_blobs()

    def _index_mesh_manifests(self):
        result = {}
        for path in (self.root / "reference" / "cache_evidence").glob("*/manifest.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            identity = record.get("identity", {})
            source = identity.get("source")
            if source and "parts" in record:
                result.setdefault((source, tuple(identity.get("scale", [1, 1, 1]))), record)
        return result

    def _validate_blobs(self):
        for link in self._links.values():
            for part in link.get("convex_parts", []):
                digest = part.get("convex_part_sha256")
                if not digest:
                    continue
                path = _blob_path(self.blobs, digest)
                if not path.is_file() or _digest(path) != digest:
                    raise CollisionBundleError("collision_bundle_hash_mismatch")

    def mesh(self, source: Path, scale=(1, 1, 1)) -> BundleMesh | None:
        source_hash = _digest(Path(source).resolve())
        scale_key = tuple(float(x) for x in scale)
        record = self._mesh_manifests.get((source_hash, scale_key))
        if record is None:
            return None
        parts = []
        volumes = []
        for item in record["parts"]:
            digest = item["sha256"]
            path = _blob_path(self.blobs, digest)
            if not path.is_file() or _digest(path) != digest:
                raise CollisionBundleError("collision_bundle_hash_mismatch")
            parts.append(path)
            volumes.append(float(item["volume"]))
        key_material = json.dumps({
            "bundle_id": self.bundle_id,
            "source": source_hash,
            "scale": scale_key,
        }, sort_keys=True, separators=(",", ":")).encode()
        return BundleMesh(tuple(parts), tuple(volumes), sha256(key_material).hexdigest())

    def robot_urdf(self, source: Path) -> Path | None:
        """Materialize the portable robot URDF with bundle-local blob paths."""
        fingerprint = self.root / "reference" / "local_fingerprint.json"
        expected = None
        if fingerprint.is_file():
            data = json.loads(fingerprint.read_text(encoding="utf-8"))
            for asset in data.get("source_assets", []):
                if asset.get("asset_type") == "robot":
                    candidate = asset.get("entrance", {}).get("sha256")
                    if candidate == _digest(Path(source).resolve()):
                        expected = candidate
                        break
        if expected is None:
            return None
        portable = self.bundle_root / "robot_portable.urdf"
        if not portable.is_file():
            raise CollisionBundleError("collision_bundle_robot_missing")
        target_dir = self.bundle_root / ".materialized"
        target_dir.mkdir(exist_ok=True)
        target = target_dir / f"robot-{expected}.urdf"
        if not target.exists():
            text = portable.read_text(encoding="utf-8")
            # The portable file is already relative to reference_bundle; keep
            # those paths and copy it under the same directory hierarchy.
            target.write_text(text, encoding="utf-8")
        return target


def configured_bundle() -> CollisionBundle | None:
    import os
    root = os.environ.get("COLLISION_BUNDLE_ROOT")
    if not root:
        return None
    allow = os.environ.get("COLLISION_BUNDLE_ALLOW_UNREVIEWED") == "1"
    return CollisionBundle(Path(root), allow_unreviewed=allow)
