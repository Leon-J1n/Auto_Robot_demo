"""Read-only HTTP client plus optional task-scoped local OLS lifecycle."""
import json
from pathlib import Path
import threading
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, ProxyHandler
from urllib.error import HTTPError, URLError


class ObjectLibraryError(RuntimeError):
    pass


class ObjectLibraryClient:
    def __init__(self, base_url='http://127.0.0.1:8000', *, uid=0, timeout=5.0):
        parts = urlsplit(base_url)
        if parts.scheme not in {'http', 'https'} or not parts.netloc or parts.query or parts.fragment:
            raise ValueError('Object library URL must be an HTTP(S) base URL')
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise ValueError('Object library uid must be a nonnegative integer')
        self.base_url, self.uid, self.timeout = base_url.rstrip('/'), uid, timeout
        self._opener = build_opener(ProxyHandler({}))

    def _get_asset(self, asset_type, asset_id):
        if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id < 0:
            raise ValueError(f'{asset_type.title()} ID must be a nonnegative integer')
        url = self.base_url + f'/{asset_type}?' + urlencode({'id': asset_id, 'uid': self.uid})
        try:
            with self._opener.open(url, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ObjectLibraryError('Model metadata exceeds 2 MiB')
            data = json.loads(raw)
        except HTTPError as exc:
            raise ObjectLibraryError(f'OLS {asset_type} {asset_id}: HTTP {exc.code}') from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise ObjectLibraryError(f'OLS {asset_type} {asset_id} query failed: {exc}') from exc
        if not isinstance(data, dict) or data.get('semantic_properties', {}).get('object_id') != asset_id:
            raise ObjectLibraryError(f'OLS returned inconsistent {asset_type} identity for {asset_id}')
        return data

    def get_model(self, model_id):
        return self._get_asset('model', model_id)

    def get_robot(self, robot_id):
        return self._get_asset('robot', robot_id)


class LocalObjectLibraryService:
    """Use the original service and its copied database; only bind loopback."""
    def __init__(self):
        from .object_library_server.server import make_server
        root = Path(__file__).resolve().parent / 'object_library_server'
        config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
        config.update(host='127.0.0.1', port=0, data_dir=str(root / 'data'))
        self.server = make_server(config)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
