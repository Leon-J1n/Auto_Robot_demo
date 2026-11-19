"""Export auditable per-shape sphere templates from an existing cuRobo model.

This is an offline authoring tool, never imported by the planning loop.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True,help='Unsimplified _runtime_collision JSON snapshot')
    parser.add_argument('--robot-yaml',type=Path,required=True,help='Previously validated matching runtime robot YAML')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        import yaml
    except ImportError:
        sys.path.append(str(Path(__file__).resolve().parents[2]/'.cache/curobo/site-packages'))
        import yaml
    raw=json.loads(args.snapshot.read_text(encoding='utf-8'))['_runtime_collision']
    model=yaml.safe_load(args.robot_yaml.read_text(encoding='utf-8'))
    spheres=model['kinematics']['collision_spheres']
    if not isinstance(spheres,dict):
        raise ValueError('Expected an embedded collision_spheres mapping')
    templates={}
    for g in raw['geometries']:
        if not g['robot'] or not g['body_name'].startswith('robotiq_2f85'):
            continue
        shape={key:g[key] for key in ('kind','size')}
        if 'file_path' in g:
            shape['mesh_sha256']=hashlib.sha256(Path(g['file_path']).read_bytes()).hexdigest()
        key=hashlib.sha256(json.dumps(shape,sort_keys=True,allow_nan=False).encode()).hexdigest()
        values=spheres['runtime_geom_'+str(g['id'])]
        for s in values:
            if len(s['center'])!=3 or not all(math.isfinite(x) for x in [*s['center'],s['radius']]) or s['radius']<=0:
                raise ValueError('Invalid sphere template')
        if not values or key in templates and templates[key]['spheres']!=values:
            raise ValueError('Missing or inconsistent source sphere fit')
        templates[key]=dict(shape=shape,spheres=values)
    if not templates:
        raise ValueError('No matching gripper geometry in snapshot')
    provenance=dict(method='Retained cuRobo VOXEL sphere templates from the previously validated articulated gripper; not a conservative mesh enclosure',
        upstream_commit=json.loads((Path(__file__).resolve().parents[2]/'libraries/curobo/config/defaults.json').read_text(encoding='utf-8'))['official_commit'],
        source_robot_yaml_sha256=hashlib.sha256(args.robot_yaml.read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(dict(schema_version=1,provenance=provenance,templates=templates),indent=2)+'\n',encoding='utf-8')
    print(f'Exported {len(templates)} templates to {args.output}')


if __name__=='__main__':
    main()
