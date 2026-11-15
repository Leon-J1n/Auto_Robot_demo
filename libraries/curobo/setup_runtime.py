"""Create an isolated dependency directory without changing the host environment."""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys

PACKAGE=Path(__file__).resolve().parent
PROJECT=PACKAGE.parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-packages',type=Path,help='Copy verified installed dependencies instead of downloading')
    parser.add_argument('--python',default=sys.executable,help='Python 3.10 executable for the worker')
    args=parser.parse_args()
    cache=PROJECT/'.cache/curobo'
    destination=cache/'site-packages'
    destination.mkdir(parents=True,exist_ok=True)
    if args.reuse_packages:
        source=args.reuse_packages.resolve()
        sys.path.insert(0,str(source))
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
        distributions={canonicalize_name(d.metadata['Name']):d for d in metadata.distributions(path=[str(source)])}
        requested=['torch','warp-lang','numpy','scipy','trimesh','yourdfpy','pyyaml',
            'networkx','matplotlib','numpy-quaternion','setuptools-scm','tqdm','wheel',
            'importlib-resources','viser','cuda-core','cuda-bindings','cuda-pathfinder',
            'nvidia-cuda-runtime-cu12','nvidia-cuda-nvrtc-cu12','nvidia-cuda-nvcc-cu12',
            'nvidia-nvfatbin-cu12','nvidia-nvjitlink-cu12','embreex','rtree','manifold3d']
        selected={}
        pending=list(requested)
        while pending:
            name=canonicalize_name(pending.pop())
            if name in selected:
                continue
            if name not in distributions:
                raise RuntimeError(f'Required dependency is missing from the source environment: {name}')
            dist=distributions[name]
            selected[name]=dist
            for line in dist.requires or []:
                requirement=Requirement(line)
                if requirement.marker and not requirement.marker.evaluate({'extra':''}):
                    continue
                if canonicalize_name(requirement.name) not in selected:
                    pending.append(requirement.name)
        total=0
        for name,dist in sorted(selected.items()):
            copied=0
            for entry in dist.files or []:
                relative=Path(str(entry))
                if relative.is_absolute() or '..' in relative.parts or '__pycache__' in relative.parts:
                    continue
                src=source/relative
                if not src.is_file():
                    continue
                target=destination/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.is_file() or target.stat().st_size!=src.stat().st_size:
                    shutil.copy2(src,target)
                copied+=1
            total+=copied
            print(f'{name}=={dist.version}: {copied} files',flush=True)
        requirements='\n'.join(f'{d.metadata["Name"]}=={d.version}' for _,d in sorted(selected.items()))+'\n'
        (cache/'installed-requirements.txt').write_text(requirements,encoding='utf-8')
        (cache/'environment.json').write_text(json.dumps({'python':sys.version,'packages':{
            name:d.version for name,d in sorted(selected.items())},'copied_files':total},indent=2),encoding='utf-8')
    else:
        subprocess.run([args.python,'-m','pip','install','--target',str(destination),
            '-r',str(PACKAGE/'config/requirements-windows-cu12.txt')],check=True)
    (cache/'runtime.json').write_text(json.dumps({'python':args.python,
        'dependency_path':'.cache/curobo/site-packages'},indent=2),encoding='utf-8')
    print(f'Worker configuration: {cache / "runtime.json"}')
    print('The application environment was not modified.')


if __name__=='__main__':
    main()
