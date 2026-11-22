from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UPLOAD_ROOT = PROJECT_ROOT / "web" / "data" / "uploads"
SUPPORTED_SUFFIXES = {".urdf", ".obj", ".dae", ".fbx", ".stl"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
BUNDLE_MANIFEST = ".asset-bundle.json"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetRecord:
    id: str
    name: str
    type: str
    source: str
    path: Path | None = None
    kind: str = "asset"
    size: int | None = None
    # Set when the asset originated from a remote URL. register_resource() returns this URL
    # directly so the browser fetches the asset from the remote server, bypassing the gateway.
    # This field also governs future cloud-stored uploads: any record with remote_url set gets
    # browser-direct routing regardless of source.
    remote_url: str | None = None

    def summary(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "source": self.source,
            "kind": self.kind,
            "size": self.size,
            "importStatus": "ready",
        }
        if self.remote_url is not None:
            result["remoteUrl"] = self.remote_url
        return result


class AssetService:
    def __init__(self, upload_root: Path = UPLOAD_ROOT):
        self.upload_root = upload_root.resolve()
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, AssetRecord] = {}
        self._resource_records: dict[str, AssetRecord] = {}
        self.refresh()

    def refresh(self) -> None:
        records: dict[str, AssetRecord] = {}
        resource_records: dict[str, AssetRecord] = {}
        for kind in ("cube", "sphere", "plane", "capsule", "cylinder"):
            record = AssetRecord(f"builtin:{kind}", kind.title(), "Primitive", "builtin", kind=kind)
            records[record.id] = record
        for output in ("rgb", "rgbd"):
            record = AssetRecord(f"builtin:camera-{output}", f"{output.upper()} Camera",
                                 "Sensor", "builtin", kind="camera")
            records[record.id] = record

        roots = [PROJECT_ROOT / "Assets", PROJECT_ROOT / "examples" / "assets",
                 PROJECT_ROOT / "tests" / "data"]
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                    record = self._file_record(path, "project")
                    records[record.id] = record
        for path in sorted(self.upload_root.iterdir()):
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                record = self._file_record(path, "uploaded")
                records[record.id] = record
            elif path.is_dir():
                manifest = path / BUNDLE_MANIFEST
                if not manifest.is_file():
                    continue
                try:
                    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
                    entry = self._safe_relative_path(str(manifest_data["entry"]))
                    remote_url: str | None = manifest_data.get("remoteUrl") or None
                    entry_path = (path / Path(*entry.parts)).resolve()
                    if entry_path.is_file() and path.resolve() in entry_path.parents:
                        source = "remote" if remote_url else "uploaded"
                        record = self._file_record(entry_path, source)
                        if remote_url:
                            record = replace(record, remote_url=remote_url)
                        records[record.id] = record
                        for resource_path in path.rglob("*"):
                            if (resource_path.is_file() and
                                    resource_path.suffix.lower() in SUPPORTED_SUFFIXES):
                                resource = self._file_record(resource_path, "project")
                                resource_records[resource.id] = resource
                except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                    continue
        self._records = records
        self._resource_records = resource_records

    def _file_record(self, path: Path, source: str) -> AssetRecord:
        resolved = path.resolve()
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:20]
        suffix = resolved.suffix.lower()
        return AssetRecord(
            id=f"{source}:{digest}",
            name=resolved.stem,
            type=suffix[1:].upper(),
            source=source,
            path=resolved,
            kind="robot" if suffix == ".urdf" else "mesh",
            size=resolved.stat().st_size,
        )

    def summaries(self) -> list[dict[str, object]]:
        return [record.summary() for record in self._records.values()]

    def require(self, asset_id: str) -> AssetRecord:
        record = self._records.get(asset_id) or self._resource_records.get(asset_id)
        if record is None:
            raise ValueError("asset was not found")
        return record

    def resolve_file(self, identifier: str) -> AssetRecord:
        normalized = identifier.strip().casefold()
        matches = [record for record in self._records.values()
                   if record.id.casefold() == normalized or
                   record.name.casefold() == normalized]
        # Accept a source-less digest only when it identifies one asset.
        if not matches and ":" not in identifier:
            matches = [record for record in self._records.values()
                       if record.id.rsplit(":", 1)[-1].casefold() == normalized]
        if len(matches) != 1 or matches[0].path is None:
            raise ValueError("asset identifier is not unique or has no file")
        return matches[0]

    def register_resource(self, value: str) -> str | None:
        if not value or value.startswith("builtin://"):
            return value or None
        path_value, marker, mesh_index = value.partition("#mesh=")
        candidate = Path(path_value)
        if not candidate.is_absolute() or not candidate.is_file():
            return None
        resolved = candidate.resolve()
        # When an existing record carries a remote_url the browser fetches directly from the
        # origin server, bypassing the gateway. This applies regardless of source so that
        # future cloud-stored uploads get the same browser-direct routing automatically.
        existing = next((r for r in self._records.values()
                         if r.path == resolved and r.remote_url), None)
        if existing is not None and existing.remote_url:
            resource = existing.remote_url
            if marker and mesh_index.isdigit():
                resource += f"#mesh={mesh_index}"
            return resource
        # Fall back to reading a bundle manifest next to the file (e.g. assets downloaded
        # by sim_editor into .sim-cache/remote-downloads/ that are not in upload_root).
        manifest_path = resolved.parent / BUNDLE_MANIFEST
        if manifest_path.is_file():
            try:
                manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                remote_url = manifest_data.get("remoteUrl") or None
                if remote_url:
                    resource = remote_url
                    if marker and mesh_index.isdigit():
                        resource += f"#mesh={mesh_index}"
                    return resource
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                pass
        record = self._file_record(candidate, "project")
        self._resource_records.setdefault(record.id, record)
        resource = f"/api/assets/{record.id}/file?format={candidate.suffix.lower()[1:]}"
        if marker and mesh_index.isdigit():
            resource += f"#mesh={mesh_index}"
        return resource

    def file(self, asset_id: str, relative_path: str | None = None) -> tuple[Path, str]:
        record = self.require(asset_id)
        if record.path is None or not record.path.is_file():
            raise ValueError("asset has no file payload")
        path = record.path
        if relative_path:
            relative = self._safe_relative_path(relative_path)
            bundle = next((parent for parent in record.path.parents
                           if parent.parent == self.upload_root and
                           (parent / BUNDLE_MANIFEST).is_file()), None)
            if bundle is None:
                path = self._declared_project_dependency(record.path, relative)
            else:
                # Browser loaders resolve OBJ references relative to the entry file. Keep the
                # bundle-root interpretation as a compatibility fallback for API callers.
                candidates = (
                    (record.path.parent / Path(*relative.parts)).resolve(),
                    (bundle / Path(*relative.parts)).resolve(),
                )
                path = next((candidate for candidate in candidates
                             if bundle in candidate.parents and candidate.is_file()), None)
                if path is None:
                    raise ValueError("asset dependency was not found")
        return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    @classmethod
    def _declared_project_dependency(cls, entry: Path, relative: PurePosixPath) -> Path:
        """Resolve only dependencies declared by a registered OBJ or its MTL files."""
        if entry.suffix.lower() != ".obj":
            raise ValueError("asset has no dependency bundle")
        root = entry.parent.resolve()
        requested = (root / Path(*relative.parts)).resolve()
        if root not in requested.parents:
            raise ValueError("asset dependency escapes its directory")
        try:
            declared = cls._obj_dependencies(entry.read_bytes())
        except OSError as error:
            raise ValueError("asset dependency could not be read") from error
        allowed: set[Path] = set()
        for value in declared:
            normalized = value.replace("\\", "/").strip()
            candidate = PurePosixPath(normalized)
            if (not normalized or normalized.startswith("/") or ":" in normalized or
                    any(part in ("", "..") for part in candidate.parts)):
                continue
            resolved = (root / Path(*candidate.parts)).resolve()
            if root in resolved.parents:
                allowed.add(resolved)
        # Browser OBJ loading is two-step: OBJ -> MTL -> texture. Permit the second
        # hop only for texture files explicitly declared by an MTL named by the OBJ.
        for material in tuple(allowed):
            if material.suffix.lower() != ".mtl" or not material.is_file():
                continue
            try:
                material_text = material.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in material_text.splitlines():
                fields = line.strip().split(None, 1)
                if (len(fields) != 2 or fields[0].lower() not in {
                        "map_kd", "map_ka", "map_ks", "map_bump", "bump", "disp", "decal"}):
                    continue
                tokens = fields[1].strip().strip('"').replace("\\", "/").split()
                if not tokens:
                    continue
                texture_name = tokens[-1]
                texture_path = PurePosixPath(texture_name)
                if (texture_name.startswith("/") or ":" in texture_name or
                        not texture_path.parts):
                    continue
                texture = (material.parent / Path(*texture_path.parts)).resolve()
                if root in texture.parents:
                    allowed.add(texture)
        if requested not in allowed or not requested.is_file():
            raise ValueError("asset dependency was not declared or was not found")
        return requested

    def save_upload(self, filename: str, content: bytes) -> AssetRecord:
        return self.save_bundle(filename, {filename: content})

    def save_bundle(self, entry_path: str, files: dict[str, bytes],
                    *, remote_url: str | None = None) -> AssetRecord:
        entry = self._safe_relative_path(entry_path)
        normalized: dict[PurePosixPath, bytes] = {}
        total_size = 0
        for value, content in files.items():
            relative = self._safe_relative_path(value)
            if relative in normalized:
                raise ValueError(f"asset bundle contains duplicate path: {relative.as_posix()}")
            total_size += len(content)
            if total_size > MAX_UPLOAD_BYTES:
                raise ValueError("asset bundle exceeds the 100 MiB upload limit")
            normalized[relative] = content
        if entry not in normalized:
            raise ValueError(f"asset entry does not exist in bundle: {entry.as_posix()}")

        content = normalized[entry]
        safe_name = re.sub(r"[^\w. -]+", "_", entry.name, flags=re.UNICODE).strip(" .")
        if not safe_name:
            raise ValueError("asset filename is empty")
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError("asset type is not supported")
        self._validate_content(suffix, content)
        if suffix == ".obj":
            normalized = self._normalize_obj_bundle(normalized, entry)
            content = normalized[entry]
            LOGGER.debug("normalized OBJ bundle entry=%s files=%d", entry.as_posix(), len(normalized))
        if suffix == ".urdf":
            available = {path.as_posix().casefold() for path in normalized}
            missing = [dependency for dependency in self.urdf_dependencies(content, entry)
                       if dependency.as_posix().casefold() not in available]
            if missing:
                names = ", ".join(path.as_posix() for path in missing)
                raise ValueError(f"asset dependencies do not exist in bundle: {names}")

        digest_input = hashlib.sha256()
        for relative, payload in sorted(normalized.items(), key=lambda item: item[0].as_posix()):
            digest_input.update(relative.as_posix().encode("utf-8"))
            digest_input.update(payload)
        bundle = (self.upload_root / f"{digest_input.hexdigest()[:12]}-{Path(safe_name).stem}").resolve()
        if self.upload_root not in bundle.parents:
            raise ValueError("upload path is invalid")
        if not bundle.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".upload-", dir=self.upload_root)).resolve()
            try:
                for relative, payload in normalized.items():
                    target = temporary.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                (temporary / BUNDLE_MANIFEST).write_text(
                    json.dumps({"entry": entry.as_posix(),
                                **({"remoteUrl": remote_url} if remote_url else {})},
                               ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, bundle)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        self.refresh()
        target = bundle.joinpath(*entry.parts).resolve()
        return next(record for record in self._records.values() if record.path == target)

    @classmethod
    def _normalize_obj_bundle(cls, files: dict[PurePosixPath, bytes],
                              entry: PurePosixPath) -> dict[PurePosixPath, bytes]:
        """Resolve OBJ/MTL references against bundle paths, including absolute exporter paths."""
        text_files = {path: payload.decode("utf-8", errors="ignore") for path, payload in files.items()
                      if path.suffix.lower() in {".obj", ".mtl"}}
        refs: list[tuple[PurePosixPath, str, str]] = []
        for path, text in text_files.items():
            for line in text.splitlines():
                fields = line.strip().split(None, 1)
                supported = {"mtllib", "map_kd", "map_ka", "map_ks", "map_bump",
                             "bump", "disp", "decal"}
                if len(fields) != 2 or fields[0].lower() not in supported:
                    continue
                value = fields[1].strip().strip('"')
                refs.append((path, fields[0], value))
        available = list(files)
        replacements: dict[PurePosixPath, dict[str, str]] = {}
        for source, _kind, value in refs:
            raw = value.replace("\\", "/")
            candidate = PurePosixPath(raw) if not (raw.startswith("/") or ":" in raw) else None
            if candidate is not None and not candidate.is_absolute():
                parts = list(source.parent.parts)
                for part in candidate.parts:
                    if part in ("", "."): continue
                    if part == "..":
                        if parts: parts.pop()
                    else: parts.append(part)
                candidate = PurePosixPath(*parts)
            match = next((path for path in available
                          if candidate is not None and
                          path.as_posix().casefold() == candidate.as_posix().casefold()), None)
            if match is None:
                basename = PurePosixPath(raw).name.casefold()
                matches = [path for path in available if path.name.casefold() == basename]
                match = matches[0] if len(matches) == 1 else None
            if match is None:
                raise ValueError(f"asset dependency does not exist in bundle: {value}")
            relative = Path(os.path.relpath(match, source.parent)).as_posix()
            replacements.setdefault(source, {})[value] = relative
        for path, mapping in replacements.items():
            text = text_files[path]
            for old, new in mapping.items():
                text = re.sub(rf"(?m)^(\s*(?:mtllib|map_\w+|bump|disp|decal)\s+){re.escape(old)}(\s*)$", rf"\g<1>{new}\g<2>", text)
            files[path] = text.encode("utf-8")
        LOGGER.debug("resolved OBJ dependencies entry=%s references=%d", entry.as_posix(), len(refs))
        return files

    @staticmethod
    def _safe_relative_path(value: str) -> PurePosixPath:
        normalized = value.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if (not normalized or normalized.startswith("/") or ":" in normalized or
                any(part in ("", ".", "..") for part in path.parts)):
            raise ValueError(f"asset bundle path is invalid: {value}")
        return path

    @classmethod
    def urdf_dependencies(cls, content: bytes, entry: PurePosixPath) -> list[PurePosixPath]:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as error:
            raise ValueError(f"URDF XML is invalid: {error}") from error
        dependencies: list[PurePosixPath] = []
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] not in {"mesh", "texture"}:
                continue
            value = element.get("filename")
            if not value:
                continue
            raw = value.replace("\\", "/").strip()
            if raw.lower().startswith("package://"):
                package_reference = raw[len("package://"):]
                package_parts = PurePosixPath(package_reference).parts
                if (len(package_parts) < 2 or any(part in ("", ".", "..")
                                                  for part in package_parts)):
                    raise ValueError(f"URDF package dependency path is invalid: {value}")
                package_name, resource_parts = package_parts[0], package_parts[1:]
                entry_parts = list(entry.parts)
                matching = [index for index, part in enumerate(entry_parts)
                            if part.casefold() == package_name.casefold()]
                prefix = entry_parts[:matching[-1] + 1] if matching else []
                dependency = cls._safe_relative_path("/".join([*prefix, *resource_parts]))
                if dependency not in dependencies:
                    dependencies.append(dependency)
                continue
            if "://" in raw:
                raise ValueError(f"URDF dependency URI is not supported: {value}")
            if raw.startswith("/") or ":" in raw:
                raise ValueError(f"URDF dependency path is invalid: {value}")
            parts = list(entry.parent.parts)
            for part in PurePosixPath(raw).parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    if not parts:
                        raise ValueError(f"URDF dependency escapes the asset bundle: {value}")
                    parts.pop()
                else:
                    parts.append(part)
            dependency = cls._safe_relative_path("/".join(parts))
            if dependency not in dependencies:
                dependencies.append(dependency)
        return dependencies

    @staticmethod
    def _validate_content(suffix: str, content: bytes) -> None:
        sample = content[:1024 * 1024]
        if not sample:
            raise ValueError("asset file is empty")
        text = sample.decode("utf-8", errors="ignore")
        if suffix == ".urdf" and "<robot" not in text:
            raise ValueError("URDF content does not contain a robot document")
        if suffix == ".obj" and not any(
                line.lstrip().startswith(("v ", "f ", "o ", "g "))
                for line in text.splitlines()):
            raise ValueError("OBJ content does not contain geometry records")
        if suffix == ".fbx" and not (
                sample.startswith(b"Kaydara FBX Binary") or "FBXHeaderExtension" in text):
            raise ValueError("FBX content signature is invalid")
        if suffix == ".stl" and not (
                text.lstrip().lower().startswith("solid") or len(content) >= 84):
            raise ValueError("STL content signature is invalid")

    def delete(self, asset_id: str) -> None:
        record = self.require(asset_id)
        if record.source not in ("uploaded", "remote") or record.path is None:
            raise ValueError("only uploaded or remote assets can be deleted")
        bundle = next((parent for parent in record.path.parents
                       if parent.parent == self.upload_root and
                       (parent / BUNDLE_MANIFEST).is_file()), None)
        if bundle is not None:
            shutil.rmtree(bundle)
        else:
            record.path.unlink(missing_ok=False)
        self.refresh()

    async def fetch_remote(self, url: str) -> AssetRecord:
        """Download a remote asset (and its dependencies) into a local bundle.

        The resulting AssetRecord carries the original URL in remote_url so that
        register_resource() returns that URL to the browser rather than a gateway
        /api/assets/... URL, keeping the browser fetching directly from the origin.
        """
        import aiohttp

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("remote asset URL must be an http or https address")

        async def fetch_bytes(session: aiohttp.ClientSession, target: str) -> bytes:
            async with session.get(target, allow_redirects=True) as response:
                if response.status != 200:
                    raise ValueError(
                        f"remote asset request failed ({response.status}): {target}")
                return await response.read()

        async with aiohttp.ClientSession() as session:
            main_bytes = await fetch_bytes(session, url)

        filename = PurePosixPath(urllib.parse.unquote(parsed.path)).name or "asset"
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError(
                f"remote asset type is not supported: {suffix or '(none)'}")

        files: dict[str, bytes] = {filename: main_bytes}

        # Resolve and fetch dependencies.
        base_url = url.rsplit("/", 1)[0] + "/"

        if suffix == ".obj":
            dep_names = self._obj_dependencies(main_bytes)
        elif suffix == ".urdf":
            entry_posix = PurePosixPath(filename)
            dep_posix = self.urdf_dependencies(main_bytes, entry_posix)
            dep_names = [p.as_posix() for p in dep_posix]
        else:
            dep_names = []

        if dep_names:
            async with aiohttp.ClientSession() as session:
                for dep in dep_names:
                    dep_url = urllib.parse.urljoin(base_url, dep)
                    try:
                        files[dep] = await fetch_bytes(session, dep_url)
                    except ValueError:
                        LOGGER.warning("remote dependency not found, skipping: %s", dep_url)

        record = self.save_bundle(filename, files, remote_url=url)
        return record

    @staticmethod
    def _obj_dependencies(content: bytes) -> list[str]:
        """Return filenames referenced by mtllib lines in an OBJ file."""
        text = content.decode("utf-8", errors="ignore")
        deps: list[str] = []
        for line in text.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and fields[0].lower() == "mtllib":
                name = fields[1].strip().strip('"')
                if name and name not in deps:
                    deps.append(name)
        return deps
