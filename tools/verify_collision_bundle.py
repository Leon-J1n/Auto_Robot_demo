#!/usr/bin/env python3
"""Verify a collision-generation delivery without importing the application."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(root: Path) -> tuple[list[str], dict]:
    root = root.resolve()
    errors: list[str] = []
    bundle_root = root / "reference_bundle"
    manifest_path = bundle_root / "bundle.json"
    if not manifest_path.is_file():
        return ["collision_bundle_manifest_missing"], {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"collision_bundle_manifest_invalid:{exc}"], {}

    status = manifest.get("review_status", "unknown")
    blobs = bundle_root / "blobs"
    if not blobs.is_dir():
        errors.append("collision_bundle_blobs_missing")

    checked_blobs = set()
    for link in manifest.get("links", []):
        for part in link.get("convex_parts", []):
            name = part.get("convex_part_sha256")
            # Primitive geometry and authored non-mesh colliders are
            # intentionally represented without a content-addressed OBJ.
            if name is None:
                continue
            if not name:
                errors.append(f"missing_blob_hash:{link.get('link_name', '<unknown>')}")
                continue
            if re.fullmatch(r"[0-9a-f]{64}", name) is None:
                errors.append(f"invalid_blob_hash:{name}")
                continue
            path = blobs / f"{name}.obj"
            if not path.is_file():
                errors.append(f"missing_blob:{name}")
                continue
            checked_blobs.add(name)
            if digest(path) != name:
                errors.append(f"blob_hash_mismatch:{name}")

    evidence_count = 0
    evidence_root = root / "reference" / "cache_evidence"
    for path in evidence_root.glob("*/manifest.json"):
        evidence_count += 1
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            for part in record.get("parts", []):
                part_path = path.parent / part["file"]
                if not part_path.is_file():
                    errors.append(f"missing_evidence_part:{part_path}")
                elif digest(part_path) != part.get("sha256"):
                    errors.append(f"evidence_hash_mismatch:{part_path}")
        except Exception as exc:
            errors.append(f"invalid_evidence_manifest:{path}:{exc}")

    fingerprint = root / "reference" / "local_fingerprint.json"
    if not fingerprint.is_file():
        errors.append("collision_bundle_fingerprint_missing")
    portable = bundle_root / "robot_portable.urdf"
    if not portable.is_file():
        errors.append("collision_bundle_robot_missing")

    summary = {
        "root": str(root),
        "bundle_id": manifest.get("bundle_id"),
        "review_status": status,
        "links": len(manifest.get("links", [])),
        "checked_blobs": len(checked_blobs),
        "cache_evidence_manifests": evidence_count,
        "production_ready": status == "verified" and not errors,
    }
    return errors, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="extracted delivery bundle root")
    parser.add_argument("--allow-unreviewed", action="store_true",
                        help="allow candidate bundles for local validation")
    args = parser.parse_args()
    errors, summary = verify(args.root)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary and summary["review_status"] != "verified" and not args.allow_unreviewed:
        errors.append("collision_bundle_unreviewed")
    if errors:
        print("Verification failed:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in errors), file=sys.stderr)
        return 2
    print("Collision bundle verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
