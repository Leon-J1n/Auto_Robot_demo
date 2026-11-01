"""Launch the supplied OLS with project-local configuration and asset database."""
from pathlib import Path
import sys
from .object_library_server.server import main as server_main

ROOT = Path(__file__).resolve().parent / 'object_library_server'


if __name__ == '__main__':
    server_main(['--config', str(ROOT / 'config.json'), '--data-dir', str(ROOT / 'data'), *sys.argv[1:]])
