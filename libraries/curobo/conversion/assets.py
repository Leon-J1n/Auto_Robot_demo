"""Read assets through OLS and materialize a verified, private local cache."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener


class ConversionError(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def component(obj, kind):
    return next((c.get('properties', {}) for c in obj.get('components', ())
                 if c.get('component_type') == kind), {})


def asset_reference(obj):
    ref = component(obj, 'MetadataRef')
    if not ref:
        return None
    path = str(ref.get('path', ''))
    match = re.fullmatch(r'\$LIBRARY_SERVER/(\d+)', path)
    if match:
        kind = ref.get('type') or 'model'
        ident = int(match.group(1))
    else:
        parts = urlsplit(path.replace('$LIBRARY_SERVER', 'http://ols'))
        from urllib.parse import parse_qs
        kind = ref.get('type') or parts.path.strip('/')
        try:
            ident = int(parse_qs(parts.query)['id'][0])
        except (KeyError, ValueError, IndexError) as exc:
            raise ConversionError(f'Invalid OLS MetadataRef: {path}') from exc
    if kind not in {'robot', 'model'} or ident < 0:
        raise ConversionError(f'Invalid OLS asset kind/id: {kind}/{ident}')
    if ref.get('object_id', ident) != ident:
        raise ConversionError('MetadataRef path and object_id disagree')
    return kind, ident


def safe_relative(value):
    path = PurePosixPath(str(value).replace('\\', '/'))
    if not path.parts or path.is_absolute() or '..' in path.parts or ':' in str(path):
        raise ConversionError(f'Asset path must stay inside its OLS asset: {value}')
    return path


class AssetStore:
    def __init__(self, base_url, uid, cache_dir, *, timeout=30):
        parts = urlsplit(base_url)
        if parts.scheme not in {'http', 'https'} or not parts.netloc or parts.query or parts.fragment:
            raise ConversionError('OLS base_url must be an HTTP(S) base URL')
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise ConversionError('OLS uid must be a nonnegative integer')
        self.base_url = base_url.rstrip('/')
        self.uid, self.timeout = uid, timeout
        self.root = Path(cache_dir) / 'assets' / fingerprint([self.base_url, uid])[:16]
        self.opener = build_opener(ProxyHandler({}))
        self.memory = {}

    def _get(self, route, **params):
        params['uid'] = self.uid
        url = self.base_url + '/' + route + '?' + urlencode(params)
        try:
            with self.opener.open(url, timeout=self.timeout) as response:
                return response.read()
        except Exception as exc:
            raise ConversionError(f'OLS GET {route} {params} failed: {exc}') from exc

    def resolve(self, kind, ident):
        key = (kind, ident)
        if key in self.memory:
            return self.memory[key]
        metadata = json.loads(self._get(kind, id=ident))
        if metadata.get('semantic_properties', {}).get('object_id') != ident:
            raise ConversionError(f'OLS returned the wrong {kind} identity: {ident}')
        entry = safe_relative(metadata.get('path', ''))
        files = list(dict.fromkeys([str(entry), *metadata.get('reference', [])]))
        folder = self.root / kind / str(ident) / fingerprint(metadata)[:20]
        folder.mkdir(parents=True, exist_ok=True)
        record_path = folder / 'cache_manifest.json'
        previous = json.loads(record_path.read_text('utf-8')) if record_path.exists() else {}
        hashes = {}
        for name in files:
            relative = safe_relative(name)
            target = folder.joinpath(*relative.parts)
            expected = previous.get('files', {}).get(str(relative))
            if target.is_file() and expected:
                data = target.read_bytes()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ConversionError(f'Cached asset changed unexpectedly: {target}')
            else:
                if metadata.get('is_on_cos'):
                    base = metadata.get('cos_base_url')
                    if not base:
                        raise ConversionError('Cloud asset has no cos_base_url')
                    from urllib.parse import quote
                    with self.opener.open(base.rstrip('/') + '/' + quote(str(relative), safe='/'),
                                          timeout=self.timeout) as response:
                        data = response.read()
                else:
                    data = self._get('file', type=kind, id=ident, path=str(relative))
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + '.partial')
                temporary.write_bytes(data)
                temporary.replace(target)
            hashes[str(relative)] = hashlib.sha256(data).hexdigest()
        record = {'ols': self.base_url, 'uid': self.uid, 'type': kind, 'id': ident,
                  'metadata': metadata, 'files': hashes}
        record_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
        result = {'metadata': metadata, 'entry': folder.joinpath(*entry.parts), 'folder': folder,
                  'digest': fingerprint(record)}
        self.memory[key] = result
        return result

    def refresh(self):
        """Explicit asset revision refresh; current joint/pose updates need no refetch."""
        self.memory.clear()
