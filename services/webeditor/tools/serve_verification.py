"""Run the real gateway plus an object library for browser verification.

The gateway half is disposable and lives in one temporary directory: the feature
file it reads, the world cache, and `X-Standalone-Verification`, the header
tools/browser_smoke.cjs uses to refuse to run against a gateway that owns real
data. The library half is ObjectLibraryServer itself - it is a separate checkout,
so this script finds it (`--library-dir`, `$OLS_DIR`, or the sibling directory of
this repository), starts it if nothing answers on the port, and stops only the
process it started. Pass `--library-url` to point at a library that is already
running somewhere else.

Usage:
    python tools/serve_verification.py
    $env:WEB_GATEWAY_URL='http://127.0.0.1:8098'
    npm run dev -- --host 127.0.0.1 --port 5188
    node tools/browser_smoke.cjs
"""
from __future__ import annotations
import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import local_gateway
from aiohttp import web

DEFAULT_LIBRARY_DIR = ROOT.parent / "ObjectLibraryServer"


@web.middleware
async def identify_verification(request, handler):
    response = await handler(request)
    response.headers["X-Standalone-Verification"] = "1"
    return response


def is_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex((host, port)) == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Disposable gateway + OLS library for browser verification")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--library-port", type=int, default=8000)
    parser.add_argument("--library-dir", default=os.environ.get("OLS_DIR") or str(DEFAULT_LIBRARY_DIR),
                        help="ObjectLibraryServer checkout to start when the port is free")
    parser.add_argument("--library-url", default=None, help="use this library instead of starting one")
    parser.add_argument("--frontend-port", type=int, default=5188)
    args = parser.parse_args()

    library_url = args.library_url or f"http://127.0.0.1:{args.library_port}"
    server: subprocess.Popen[bytes] | None = None
    original = local_gateway.ROOT
    with tempfile.TemporaryDirectory(prefix="web-frontend-verification-") as folder:
        root = Path(folder)
        shutil.copyfile(original / "features.json", root / "features.json")
        # §11.7: the catch-all now serves dist/, so hand the disposable gateway the same
        # build a release bundle would carry. Without a build its `/` answers the honest
        # 404; the smoke still loads the page from Vite either way.
        built = original / "dist"
        if built.is_dir():
            shutil.copytree(built, root / "dist")
        local_gateway.ROOT = root
        if args.library_url is None and not is_listening(args.library_port):
            entry = Path(args.library_dir) / "server.py"
            if not entry.is_file():
                sys.exit(f"[verification] no ObjectLibraryServer at {args.library_dir} - pass --library-dir "
                         f"or --library-url, or start OLS yourself on port {args.library_port}")
            # OLS reads its own config.json and ./data relative to its working directory.
            server = subprocess.Popen([sys.executable, "server.py", "--port", str(args.library_port)],
                                      cwd=str(entry.parent))
        # Only the library address is redirected: everything else is the real
        # config, so the caps the smoke relies on are the caps production uses.
        config = local_gateway.load_constant_config()
        config["library_server_url"] = library_url
        config["gateway"] = dict(config["gateway"], host="127.0.0.1", port=args.port)
        app = local_gateway.create_app(config, state_path=root / "var" / "gateway_state.json")
        app.middlewares.append(identify_verification)
        print(f"[verification] library {library_url}"
              f"{' (started by this run)' if server else ' (already running)'}")
        print(f"[verification] gateway http://127.0.0.1:{args.port} - start Vite with "
              f"WEB_GATEWAY_URL=http://127.0.0.1:{args.port} --port {args.frontend_port}")
        try:
            web.run_app(app, host="127.0.0.1", port=args.port)
        finally:
            if server:
                server.terminate()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
