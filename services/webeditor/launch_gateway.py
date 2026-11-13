"""Launch the release gateway with this machine's LAN address in a temporary config."""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def current_ipv4() -> str:
    # UDP connect selects the default route's source address without sending packets.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
    if address.startswith(("127.", "169.254.")) or address == "0.0.0.0":
        raise OSError("No usable LAN IPv4 address found")
    return address


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--config", default=str(ROOT / "config" / "constant_config.json"))
    args, gateway_args = parser.parse_known_args(argv)
    source = Path(args.config)
    config = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
    config["library_server_url"] = f"http://{current_ipv4()}:8000"
    print(f"Object library: {config['library_server_url']}", flush=True)
    # Close before Python opens it again: Windows locks an open NamedTemporaryFile.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="web-gateway-",
                                     encoding="utf-8", delete=False) as temporary:
        runtime_config = Path(temporary.name)
        json.dump(config, temporary, ensure_ascii=False)
    try:
        return subprocess.call([sys.executable, str(ROOT / "local_gateway.py"),
                                *gateway_args, "--config", str(runtime_config)], cwd=ROOT)
    finally:
        runtime_config.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError) as error:
        print(f"Gateway launch failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
