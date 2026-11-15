"""Check the vendored source against the downloaded official archive file hashes."""
import hashlib
import json
from pathlib import Path

PACKAGE=Path(__file__).resolve().parent


def verify():
    expected=json.loads((PACKAGE/'UPSTREAM.files.json').read_text('utf-8'))
    source=PACKAGE/'source'
    actual={path.relative_to(source).as_posix():path for path in source.rglob('*') if path.is_file()}
    failures=[name for name,digest in expected.items() if name not in actual or
              hashlib.sha256(actual[name].read_bytes()).hexdigest()!=digest]
    extras=sorted(set(actual)-set(expected))
    result={'expected_files':len(expected),'changed_or_missing':failures,'unexpected_files':extras}
    print(json.dumps(result,indent=2))
    return not failures and not extras


if __name__=='__main__':
    raise SystemExit(0 if verify() else 1)
