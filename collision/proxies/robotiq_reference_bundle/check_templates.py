"""Compare a raw runtime collision snapshot with the reviewed Robotiq templates."""
import argparse
import hashlib
import json
from pathlib import Path


TEMPLATE_FILE = Path(__file__).with_name('robotiq_2f85.json')


def inspect_snapshot(snapshot, templates=None):
    if templates is None:
        templates = json.loads(TEMPLATE_FILE.read_text(encoding='utf-8'))['templates']
    geometries = snapshot.get('_runtime_collision', snapshot).get('geometries', [])
    report = []
    for geom in geometries:
        if not geom.get('robot') or not geom.get('body_name', '').startswith('robotiq_2f85'):
            continue
        shape = {key: geom[key] for key in ('kind', 'size')}
        if 'file_path' in geom:
            shape['mesh_sha256'] = hashlib.sha256(Path(geom['file_path']).read_bytes()).hexdigest()
        key = hashlib.sha256(json.dumps(shape, sort_keys=True, allow_nan=False).encode()).hexdigest()
        reference_hashes = sorted({entry['shape'].get('mesh_sha256', 'n/a')
                                   for entry in templates.values()
                                   if entry['shape'].get('kind') == shape['kind']
                                   and entry['shape'].get('size') == shape['size']})
        report.append({
            'geom_id': geom['id'],
            'body_name': geom['body_name'],
            'kind': shape['kind'],
            'size': shape['size'],
            'mesh_sha256': shape.get('mesh_sha256'),
            'status': 'matched' if key in templates and templates[key]['shape'] == shape else 'missing',
            'reference_mesh_sha256': reference_hashes,
        })
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True,
                        help='JSON containing an unsimplified _runtime_collision snapshot')
    args = parser.parse_args()
    report = inspect_snapshot(json.loads(args.snapshot.read_text(encoding='utf-8')))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report:
        parser.error('No Robotiq 2F-85 robot geometries found in snapshot')
    return 0 if all(item['status'] == 'matched' for item in report) else 1


if __name__ == '__main__':
    raise SystemExit(main())
